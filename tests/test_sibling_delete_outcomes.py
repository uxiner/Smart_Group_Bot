"""B8-2：群规 / 关键词回复 / 定时消息三个同型 DELETE 的可靠回执。

父代理独立探针（真 HTTP + 真 DML）证明：三个端点在「预读之后被另一个独立 writer
删掉该行」的竞态下**全部仍回 ``200 {"ok":true,"deleted":true}``**，同时 SQLAlchemy
打出 ``expected to delete 1 row(s); 0 were matched``。即与 B7 修掉的永久备忘删除
是同一形状：把「本次没删掉」报成「本次删除成功」。

本模块分两段：

* ``SiblingDeleteOutcomeHttpTests``（业务段）：只用基线 ``605234b`` 上**已存在**的
  HTTP 入口（真 ``init_db`` + 真 SQLite + 真 ``VerifyWebServer`` + 签名合成身份），
  在冻结基线上必须红，红必须是业务断言失败（例如 ``200 != 503``、响应缺
  ``status``/``changed``），不是缺符号。
* ``SiblingDeleteHelperProtocolTests``（协议段）：本批新增的
  ``bot.services.group_scoped_writes`` 符号与严格 rowcount 契约。基线上缺符号时在
  ``asyncSetUp`` 里明确失败——属「缺符号兼容红」，不计入业务红检。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    Group,
    KeywordReply,
    ModerationRule,
    ScheduledMessage,
)
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

try:  # pragma: no cover - 基线上不存在
    from bot.services.group_scoped_writes import (
        GROUP_ROW_DELETED,
        GROUP_ROW_NO_CHANGE,
        GROUP_ROW_NOT_FOUND,
        GROUP_ROW_UNCONFIRMED,
        GroupScopedDeleteOutcome,
        delete_group_scoped_row,
    )

    _HELPER_IMPORT_ERROR: BaseException | None = None
except Exception as exc:  # noqa: BLE001
    GROUP_ROW_DELETED = "deleted"
    GROUP_ROW_NO_CHANGE = "no_change"
    GROUP_ROW_NOT_FOUND = "not_found"
    GROUP_ROW_UNCONFIRMED = "unconfirmed"
    GroupScopedDeleteOutcome = None  # type: ignore[assignment]
    delete_group_scoped_row = None  # type: ignore[assignment]
    _HELPER_IMPORT_ERROR = exc

#: 与「不覆盖」区分开的哨兵：`rowcount_override=None` 本身是一个要测的坏值。
_NO_OVERRIDE = object()

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN = 42
ADMIN_A = 99
ADMIN_B = 100
MEMBER = 777
GROUP_A = -1001234567890
GROUP_B = -1009999999999

UNCONFIRMED_MESSAGE = "未能确认这次永久记忆操作是否已生效。请重新查看列表后再决定是否重试。"

#: 三个同型资源：路径段、模型、404 文案、每群种子构造函数名。
RESOURCES = {
    "rules": {
        "model": ModerationRule,
        "not_found": ("rule_not_found", "群规不存在。"),
        "path": "rules",
        "label": "群规",
    },
    "keyword-replies": {
        "model": KeywordReply,
        "not_found": ("keyword_reply_not_found", "关键词回复不存在。"),
        "path": "keyword-replies",
        "label": "关键词回复",
    },
    "scheduled-messages": {
        "model": ScheduledMessage,
        "not_found": ("scheduled_message_not_found", "定时消息不存在。"),
        "path": "scheduled-messages",
        "label": "定时消息",
    },
}


def _signed_init_data(user_id: int) -> str:
    auth_date = int(time.time())
    payload = {
        "auth_date": str(auth_date),
        "query_id": f"AA{user_id}",
        "user": json.dumps(
            {"id": user_id, "first_name": "合成", "username": f"synthetic{user_id}"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**payload, "hash": signature})


class _FaultFactory:
    """只给服务自己创建的 session 注入故障；夹具自己的会话保持干净。"""

    def __init__(self, factory, *, db_path: str):
        self._factory = factory
        self.db_path = db_path
        self.armed = False
        self.execute_faults: dict[str, str] = {}
        self.commit_fault: str | None = None
        self.rollback_fault: str = "ok"
        self.close_fault: str = "ok"
        self.rowcount_override: object = _NO_OVERRIDE
        self.independent_delete_key: tuple[str, int] | None = None
        self.barrier_writers = 0
        self.calls = {"execute": 0, "dml": 0, "commit": 0, "rollback": 0, "close": 0, "write_lock": 0}
        self.statements: list[str] = []
        self.raised: list[tuple[str, BaseException]] = []
        self._barrier_count = 0
        self._barrier_gate = asyncio.Event()

    def arm(self, **options) -> None:
        for key, value in options.items():
            setattr(self, key, value)
        self.armed = True

    def __call__(self):
        session = self._factory()
        if self.armed:
            self._install(session)
        return session

    def _kind(self, statement: str) -> str:
        lowered = statement.strip().lower()
        for kind, spec in RESOURCES.items():
            table = spec["model"].__tablename__
            if table in lowered:
                if lowered.startswith("delete"):
                    return "delete"
                if lowered.startswith("update"):
                    return "update"
                return "select"
        return "other"

    def _record(self, label: str, exc: BaseException) -> BaseException:
        self.raised.append((label, exc))
        return exc

    def raised_for(self, label: str) -> BaseException:
        for name, exc in self.raised:
            if name == label:
                return exc
        raise AssertionError(f"没有记录到 {label}：{[name for name, _ in self.raised]}")

    def _independent_delete(self) -> None:
        """用一条完全独立的 SQLite 连接删掉目标行（模拟另一个 writer 先提交）。"""

        if self.independent_delete_key is None:
            return
        table, row_id = self.independent_delete_key
        self.independent_delete_key = None
        connection = sqlite3.connect(self.db_path, timeout=1.0)
        try:
            connection.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            connection.commit()
        finally:
            connection.close()

    def _install(self, session) -> None:
        model_instance = session

        real_get = model_instance.get
        real_execute = model_instance.execute
        real_commit = model_instance.commit
        real_rollback = model_instance.rollback
        real_close = model_instance.close
        real_lock = model_instance._acquire_write_lock

        async def get(entity, ident, *args, **kwargs):
            found = await real_get(entity, ident, *args, **kwargs)
            # 预读命中之后、DML 之前，另一个独立 writer 把这行删掉并提交。
            if found is not None:
                self._independent_delete()
            return found

        async def execute(statement, *args, **kwargs):
            self.calls["execute"] += 1
            sql = str(statement)
            self.statements.append(sql)
            kind = self._kind(sql)
            if kind == "delete":
                self.calls["dml"] += 1
                mode = self.execute_faults.get("delete")
                if mode == "fail_before":
                    raise self._record("delete_fail", RuntimeError("synthetic execute failure"))
                if mode == "cancel_before":
                    raise self._record("delete_cancel", asyncio.CancelledError("synthetic execute cancelled"))
                result = await real_execute(statement, *args, **kwargs)
                if mode == "fail_after":
                    raise self._record("delete_fail", RuntimeError("synthetic execute failure"))
                if mode == "cancel_after":
                    raise self._record("delete_cancel", asyncio.CancelledError("synthetic execute cancelled"))
                if self.rowcount_override is not _NO_OVERRIDE:
                    return SimpleNamespace(rowcount=self.rowcount_override)
                return result
            return await real_execute(statement, *args, **kwargs)

        async def commit():
            self.calls["commit"] += 1
            mode = self.commit_fault
            if mode == "fail_before":
                raise self._record("commit_fail", RuntimeError("synthetic commit failure"))
            if mode == "cancel_before":
                raise self._record("commit_cancel", asyncio.CancelledError("synthetic commit cancelled"))
            result = await real_commit()
            if mode == "fail_after":
                raise self._record("commit_fail", RuntimeError("synthetic commit failure"))
            if mode == "cancel_after":
                raise self._record("commit_cancel", asyncio.CancelledError("synthetic commit cancelled"))
            return result

        async def rollback():
            self.calls["rollback"] += 1
            if self.rollback_fault == "fail":
                raise self._record("rollback_fail", RuntimeError("synthetic rollback failure"))
            return await real_rollback()

        async def close():
            self.calls["close"] += 1
            if self.close_fault == "fail":
                raise self._record("close_fail", RuntimeError("synthetic close failure"))
            return await real_close()

        async def acquire(*args, **kwargs):
            self.calls["write_lock"] += 1
            # 真 DML barrier：在拿到写锁之前让竞争 writer 先落库。
            if self.barrier_writers > 1:
                self._barrier_count += 1
                if self._barrier_count >= self.barrier_writers:
                    self._barrier_gate.set()
                await asyncio.wait_for(self._barrier_gate.wait(), 5.0)
            return await real_lock(*args, **kwargs)

        model_instance.get = get
        model_instance.execute = execute
        model_instance.commit = commit
        model_instance.rollback = rollback
        model_instance.close = close
        model_instance._acquire_write_lock = acquire


class _SiblingDeleteTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.clean_factory = await init_db(f"sqlite+aiosqlite:///{self.db_path}")
        self.factory = _FaultFactory(self.clean_factory, db_path=self.db_path)
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN,
            config_master_key="sibling-delete-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "http://127.0.0.1:8081"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8081
        self.settings.join_verification_public_base_url = "http://127.0.0.1:8081"
        self.manager = RuntimeConfigManager(
            session_factory=self.factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-sibling-delete.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self.bot = SimpleNamespace(token=BOT_TOKEN)
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()
        await self._seed()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            path = f"{self.db_path}{suffix}"
            if os.path.exists(path):
                os.unlink(path)

    async def _seed(self) -> None:
        async with self.clean_factory() as session:
            session.add_all(
                [
                    AuthorizedGroup(group_id=GROUP_A, authorized_by=SUPER_ADMIN, bot_present=True),
                    AuthorizedGroup(group_id=GROUP_B, authorized_by=SUPER_ADMIN, bot_present=True),
                    Group(id=GROUP_A, title="合成群 A", settings={}),
                    Group(id=GROUP_B, title="合成群 B", settings={}),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Admin(group_id=GROUP_A, user_id=ADMIN_A, role="admin"),
                    Admin(group_id=GROUP_B, user_id=ADMIN_B, role="admin"),
                ]
            )
            await session.commit()

    async def _seed_row(self, kind: str, *, group_id: int = GROUP_A, marker: str = "合成") -> int:
        if kind == "rules":
            row = ModerationRule(group_id=group_id, rule_type="keyword", pattern=f"{marker}规则")
        elif kind == "keyword-replies":
            row = KeywordReply(group_id=group_id, keyword=f"{marker}关键词", reply_text=f"{marker}回复")
        else:
            row = ScheduledMessage(
                group_id=group_id,
                text=f"{marker}定时",
                schedule_type="daily",
                schedule_time="09:00",
            )
        async with self.clean_factory() as session:
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _fresh(self, kind: str, memory_id: int):
        model = RESOURCES[kind]["model"]
        async with self.clean_factory() as session:
            return await session.get(model, memory_id)

    async def _fresh_ids(self, kind: str, *, group_id: int = GROUP_A) -> list[int]:
        model = RESOURCES[kind]["model"]
        async with self.clean_factory() as session:
            rows = (
                await session.scalars(
                    select(model).where(model.group_id == group_id).order_by(model.id)
                )
            ).all()
            return [int(row.id) for row in rows]

    async def _assert_write_lock_available(self, *, timeout: float = 1.0) -> None:
        async def _probe() -> None:
            async with self.clean_factory() as session:
                await session.execute(
                    ModerationRule.__table__.delete().where(ModerationRule.id == -1)
                )
                await session.commit()

        await asyncio.wait_for(_probe(), timeout)

    def _headers(self, user_id: int) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    def _url(self, kind: str, memory_id: int, *, group_id: int = GROUP_A) -> str:
        return f"/api/v1/groups/{group_id}/{RESOURCES[kind]['path']}/{memory_id}"

    async def _delete(self, kind: str, memory_id: int, *, user_id: int = SUPER_ADMIN, group_id: int = GROUP_A):
        return await self.client.delete(self._url(kind, memory_id, group_id=group_id), headers=self._headers(user_id))

    @staticmethod
    def _field(body: dict, name: str):
        if name not in body:
            raise AssertionError(f"响应缺少字段 {name!r}：{sorted(body)}")
        return body[name]

    @classmethod
    def _error(cls, body: dict) -> dict:
        error = cls._field(body, "error")
        if not isinstance(error, dict):
            raise AssertionError(f"error 不是对象：{error!r}")
        return error


class SiblingDeleteOutcomeHttpTests(_SiblingDeleteTestCase):
    """业务段：只用基线既有 HTTP 入口。"""

    async def test_normal_path_reports_a_confirmed_deletion(self) -> None:
        for kind in RESOURCES:
            with self.subTest(kind=kind):
                memory_id = await self._seed_row(kind)
                response = await self._delete(kind, memory_id)
                self.assertEqual(response.status, 200)
                body = await response.json()
                self.assertIs(body["ok"], True)
                self.assertIs(self._field(body, "deleted"), True)
                self.assertIs(self._field(body, "changed"), True)
                self.assertEqual(self._field(body, "status"), "deleted")
                self.assertIsNone(await self._fresh(kind, memory_id))

    async def test_missing_and_cross_group_are_the_same_verbatim_404(self) -> None:
        for kind, spec in RESOURCES.items():
            with self.subTest(kind=kind):
                code, message = spec["not_found"]
                missing = await self._delete(kind, 987654)
                self.assertEqual(missing.status, 404)
                self.assertEqual(
                    await missing.json(),
                    {"ok": False, "error": {"code": code, "message": message}},
                )

                foreign_id = await self._seed_row(kind, group_id=GROUP_B)
                cross = await self._delete(kind, foreign_id)
                self.assertEqual(cross.status, 404)
                self.assertEqual(
                    await cross.json(),
                    {"ok": False, "error": {"code": code, "message": message}},
                )
                # 他群的行必须原封不动。
                self.assertIsNotNone(await self._fresh(kind, foreign_id))

    async def test_no_change_when_an_independent_writer_already_deleted_the_row(self) -> None:
        for kind, spec in RESOURCES.items():
            with self.subTest(kind=kind):
                memory_id = await self._seed_row(kind)
                self.factory.arm(
                    independent_delete_key=(spec["model"].__tablename__, memory_id)
                )
                response = await self._delete(kind, memory_id)
                self.factory.arm(independent_delete_key=None)
                self.assertEqual(response.status, 200)
                body = await response.json()
                self.assertIs(self._field(body, "deleted"), False)
                self.assertIs(self._field(body, "changed"), False)
                self.assertEqual(self._field(body, "status"), "no_change")
                self.assertIsNone(await self._fresh(kind, memory_id))

    async def test_malformed_rowcount_is_never_reported_as_a_change(self) -> None:
        for kind in RESOURCES:
            for bad in (True, 1.0, "1", -1, 2, None):
                with self.subTest(kind=kind, rowcount=bad):
                    memory_id = await self._seed_row(kind)
                    self.factory.arm(rowcount_override=bad)
                    response = await self._delete(kind, memory_id)
                    self.factory.arm(rowcount_override=None)
                    self.assertEqual(response.status, 503)
                    body = await response.json()
                    self.assertIs(self._field(body, "ok"), False)
                    error = self._error(body)
                    self.assertEqual(self._field(error, "code"), "memory_write_unconfirmed")
                    self.assertEqual(self._field(error, "message"), UNCONFIRMED_MESSAGE)
                    await self._assert_write_lock_available()

    async def test_commit_failure_before_and_after_are_both_unconfirmed(self) -> None:
        for mode in ("fail_before", "fail_after"):
            with self.subTest(mode=mode):
                memory_id = await self._seed_row("rules")
                self.factory.arm(commit_fault=mode)
                response = await self._delete("rules", memory_id)
                self.factory.arm(commit_fault=None)
                self.assertEqual(response.status, 503)
                body = await response.json()
                error = self._error(body)
                self.assertEqual(self._field(error, "code"), "memory_write_unconfirmed")
                self.assertEqual(self._field(error, "message"), UNCONFIRMED_MESSAGE)
                await self._assert_write_lock_available()

    async def test_statement_is_single_group_scoped_and_never_repeated(self) -> None:
        for kind, spec in RESOURCES.items():
            with self.subTest(kind=kind):
                memory_id = await self._seed_row(kind)
                self.factory.statements.clear()
                self.factory.calls["dml"] = 0
                self.factory.arm()
                response = await self._delete(kind, memory_id)
                self.assertEqual(response.status, 200)
                deletes = [
                    sql
                    for sql in self.factory.statements
                    if sql.strip().lower().startswith("delete")
                    and spec["model"].__tablename__ in sql
                ]
                self.assertEqual(len(deletes), 1, self.factory.statements)
                lowered = deletes[0].lower()
                self.assertIn("group_id", lowered)
                self.assertIn("id =", lowered)
                self.assertEqual(self.factory.calls["dml"], 1)

    async def test_two_overlapping_deletes_produce_exactly_one_change(self) -> None:
        memory_id = await self._seed_row("rules")
        self.factory.arm(barrier_writers=2)

        async def _once():
            return await self._delete("rules", memory_id)

        first, second = await asyncio.gather(_once(), _once())
        self.factory.arm(barrier_writers=0)
        first_body = await first.json()
        second_body = await second.json()
        statuses = sorted(
            [self._field(first_body, "status"), self._field(second_body, "status")]
        )
        self.assertEqual(statuses, ["deleted", "no_change"])
        self.assertIsNone(await self._fresh("rules", memory_id))

    async def test_authentication_and_group_permissions_are_unchanged(self) -> None:
        memory_id = await self._seed_row("rules")

        anonymous = await self.client.delete(self._url("rules", memory_id))
        self.assertEqual(anonymous.status, 401)

        member = await self._delete("rules", memory_id, user_id=MEMBER)
        self.assertEqual(member.status, 403)

        foreign_admin = await self._delete("rules", memory_id, user_id=ADMIN_B)
        self.assertEqual(foreign_admin.status, 403)

        group_admin = await self._delete("rules", memory_id, user_id=ADMIN_A)
        self.assertEqual(group_admin.status, 200)

    async def test_error_bodies_never_leak_internals_or_other_groups(self) -> None:
        memory_id = await self._seed_row("rules")
        self.factory.arm(commit_fault="fail_before")
        response = await self._delete("rules", memory_id)
        self.factory.arm(commit_fault=None)
        raw = await response.text()
        for forbidden in ("Traceback", "SELECT", "DELETE FROM", "sqlite", str(GROUP_B), str(GROUP_A)):
            self.assertNotIn(forbidden, raw)
        self.assertEqual(response.status, 503)


class SiblingDeleteHelperProtocolTests(_SiblingDeleteTestCase):
    """协议段：本批新增 helper 的严格 rowcount 与资源归还契约。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        if delete_group_scoped_row is None or GroupScopedDeleteOutcome is None:
            self.fail(
                "本批新增的 group_scoped_writes 契约不可用："
                f"delete_group_scoped_row={delete_group_scoped_row!r} "
                f"outcome={GroupScopedDeleteOutcome!r} error={_HELPER_IMPORT_ERROR!r}"
            )

    async def _call(self, kind: str, memory_id: int):
        return await delete_group_scoped_row(
            self.factory,
            model=RESOURCES[kind]["model"],
            group_id=GROUP_A,
            row_id=memory_id,
            operation=f"probe_{kind}",
        )

    async def test_helper_reports_the_three_terminal_states(self) -> None:
        missing = await self._call("rules", 987654)
        self.assertEqual(missing.status, GROUP_ROW_NOT_FOUND)
        self.assertTrue(missing.not_found)

        memory_id = await self._seed_row("rules")
        deleted = await self._call("rules", memory_id)
        self.assertEqual(deleted.status, GROUP_ROW_DELETED)
        self.assertTrue(deleted.confirmed_deleted)
        self.assertEqual(deleted.changed_rows, 1)
        self.assertTrue(deleted.resource_released)
        self.assertIsNone(await self._fresh("rules", memory_id))

        again = await self._seed_row("rules")
        self.factory.arm(independent_delete_key=("moderation_rules", again))
        no_change = await self._call("rules", again)
        self.factory.arm(independent_delete_key=None)
        self.assertEqual(no_change.status, GROUP_ROW_NO_CHANGE)
        self.assertTrue(no_change.confirmed_no_change)
        self.assertEqual(no_change.changed_rows, 0)

    async def test_helper_is_strict_about_malformed_rowcounts(self) -> None:
        for bad in (True, 1.0, "1", -1, 2, None):
            with self.subTest(rowcount=bad):
                memory_id = await self._seed_row("keyword-replies")
                self.factory.arm(rowcount_override=bad)
                outcome = await self._call("keyword-replies", memory_id)
                self.factory.arm(rowcount_override=None)
                self.assertEqual(outcome.status, GROUP_ROW_UNCONFIRMED)
                self.assertTrue(outcome.unconfirmed)
                self.assertIsNone(outcome.changed_rows)
                await self._assert_write_lock_available()

    async def test_helper_unconfirmed_on_commit_and_execute_failures(self) -> None:
        for fault in ("execute_faults", "commit_fault"):
            with self.subTest(fault=fault):
                memory_id = await self._seed_row("rules")
                if fault == "execute_faults":
                    self.factory.arm(execute_faults={"delete": "fail_before"})
                else:
                    self.factory.arm(commit_fault="fail_before")
                outcome = await self._call("rules", memory_id)
                self.factory.arm(execute_faults={}, commit_fault=None)
                self.assertEqual(outcome.status, GROUP_ROW_UNCONFIRMED)
                self.assertIsNotNone(await self._fresh("rules", memory_id))
                await self._assert_write_lock_available()

    async def test_helper_propagates_cancellation_without_swallowing_it(self) -> None:
        memory_id = await self._seed_row("rules")
        self.factory.arm(commit_fault="cancel_before")

        with self.assertRaises(BaseException) as caught:
            await self._call("rules", memory_id)

        self.assertIsInstance(caught.exception, asyncio.CancelledError)
        self.assertIs(caught.exception, self.factory.raised_for("commit_cancel"))
        self.assertIsNotNone(await self._fresh("rules", memory_id))
        await self._assert_write_lock_available()

    async def test_helper_discloses_an_unreleased_session(self) -> None:
        """全部恢复手段永久失败：如实披露，不假称已经释放。"""

        memory_id = await self._seed_row("rules")
        self.factory.arm(
            execute_faults={"delete": "fail_before"},
            rollback_fault="fail",
            close_fault="fail",
        )

        with self.assertLogs("bot.services.group_scoped_writes", level="WARNING") as captured:
            outcome = await self._call("rules", memory_id)

        self.assertEqual(outcome.status, GROUP_ROW_UNCONFIRMED)
        self.assertFalse(outcome.resource_released)
        self.assertTrue(
            any("cleanup unreleased" in line for line in captured.output), captured.output
        )
        self.assertIsNotNone(await self._fresh("rules", memory_id))


if __name__ == "__main__":
    unittest.main()
