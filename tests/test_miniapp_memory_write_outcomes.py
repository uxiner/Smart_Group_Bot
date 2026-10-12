"""Mini App 群永久备忘写操作的可证明结果（B7 回归）。

两组用例，边界刻意分开：

* `MemoryWriteOutcomeHttpTests` —— 真 `init_db` + 真 SQLite + 真
  `VerifyWebServer` + 真实 HTTP + 真实 DML，**只用基线既有入口**（路由、请求
  体、响应体），不 import 本批新模块。所以它在冻结基线上的失败必须是**业务
  语义**失败（intended `AssertionError`）。
* `PermanentMemoryWriteHelperTests` —— 直接调新 helper，验证严格 rowcount、
  三态、取消身份与有界归还。新模块缺失时在 `setUp` 里直接 fail（属于「缺符号
  兼容红」，不计入业务红检）。

注入点全部放在真实会话实例上，只影响服务自己创建的 session；夹具与验收读取
一律走干净的 `self.session_factory`，不用 fixture teardown 冒充「服务已经安全
归还资源」。
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
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select, text as sa_text

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Admin, AuthorizedGroup, Group, GroupPermanentMemory
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN = 42
ADMIN_A = 99
ADMIN_B = 100
MEMBER = 777
GROUP_A = -1001234567890
GROUP_B = -1009999999999

UNCONFIRMED_MESSAGE = "未能确认这次永久记忆操作是否已生效。请重新查看列表后再决定是否重试。"
UNAVAILABLE_MESSAGE = "这条永久记忆已不可用，未确认本次修改。请重新查看列表。"
NOT_FOUND_BODY = {
    "ok": False,
    "error": {"code": "memory_not_found", "message": "永久记忆不存在。"},
}

try:  # 守卫式导入：冻结基线上没有这个模块。
    from bot.services import permanent_memory_writes as pmw
except Exception as _import_error:  # pragma: no cover - 只在冻结基线上发生
    pmw = None  # type: ignore[assignment]
    _PMW_IMPORT_ERROR: Exception | None = _import_error
else:
    _PMW_IMPORT_ERROR = None


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-memory-write-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _row_document(row: GroupPermanentMemory) -> dict:
    return {
        "id": int(row.id),
        "content": str(row.content or ""),
        "created_by": int(row.created_by or 0),
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


class _FaultFactory:
    """包住真实 sessionmaker，只给「碰过 group_permanent_memories 的会话」注入故障。

    标记点选在**实例方法** `get` / `execute` 上：ORM 的内部惰性加载走的是 sync
    session，绕不过实例补丁，所以 `refresh` 单独包。写锁 barrier 放在
    `_acquire_write_lock` **之前**——绝不在拿到写锁之后等另一个 writer。
    """

    def __init__(self, factory, *, db_path: str) -> None:
        self._factory = factory
        self.db_path = db_path
        self.armed = False
        self.execute_faults: dict[str, str] = {}
        self.refresh_fault: str | None = None
        self.commit_fault: str | None = None
        self.rollback_plan: str | tuple[str, ...] | None = None
        self.close_plan: str | tuple[str, ...] | None = None
        self.rowcount_override: tuple[str, object] | None = None
        self.independent_delete_id: int | None = None
        self.barrier_writers = 0
        self.barrier_timeout = 5.0
        self.calls = {
            "sessions": 0,
            "get": 0,
            "delete": 0,
            "update": 0,
            "commit": 0,
            "rollback": 0,
            "close": 0,
            "refresh": 0,
            "write_lock": 0,
        }
        self.statements: list[str] = []
        self.raised: list[tuple[str, BaseException]] = []
        self.close_entered = asyncio.Event()
        self.close_gate = asyncio.Event()
        self._plans: dict[str, list[str]] = {}
        self._arrived = 0
        self._barrier = asyncio.Event()

    # --- 测试接口 ---------------------------------------------------------
    def arm(self, **options: object) -> None:
        for key, value in options.items():
            if not hasattr(self, key):
                raise AttributeError(f"未知注入项：{key}")
            setattr(self, key, value)
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

    def _raised(self, label: str) -> BaseException:
        for name, exc in self.raised:
            if name == label:
                return exc
        raise AssertionError(f"没有记录到 {label} 的故障注入")

    # --- 工厂 -------------------------------------------------------------
    def __call__(self):
        self.calls["sessions"] += 1
        session = self._factory()
        if self.armed:
            self._install(session)
        return session

    # --- 注入 -------------------------------------------------------------
    @staticmethod
    def _kind(statement: object) -> str | None:
        text = str(statement)
        if "group_permanent_memories" not in text:
            return None
        head = text.lstrip()[:6].upper()
        if head.startswith("DELETE"):
            return "delete"
        if head.startswith("UPDATE"):
            return "update"
        return "select"

    def _record(self, label: str, exc: BaseException) -> None:
        self.raised.append((label, exc))

    def _next_mode(self, name: str) -> str | None:
        plan = self._plans.get(name)
        if plan is None:
            raw = self.rollback_plan if name == "rollback" else self.close_plan
            if raw is None:
                return None
            plan = list(raw) if isinstance(raw, tuple) else [raw]
            self._plans[name] = plan
        if len(plan) > 1:
            return plan.pop(0)
        return plan[0]

    async def _run_fault(self, label: str, mode: str, call, *args, **kwargs):
        if mode in ("fail_before", "fail"):
            # `fail` = 这个阶段**永远**失败（负控制），不是只失败一次。
            exc = RuntimeError(f"synthetic {label} failure")
            self._record(f"{label}_fail", exc)
            raise exc
        if mode == "cancel_before":
            exc = asyncio.CancelledError(f"synthetic {label} cancelled")
            self._record(f"{label}_cancel", exc)
            raise exc
        result = await call(*args, **kwargs)
        if mode == "fail_after":
            exc = RuntimeError(f"synthetic {label} failure (after)")
            self._record(f"{label}_fail", exc)
            raise exc
        if mode == "cancel_after":
            exc = asyncio.CancelledError(f"synthetic {label} cancelled (after)")
            self._record(f"{label}_cancel", exc)
            raise exc
        return result

    def _delete_with_stdlib(self, memory_id: int) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "DELETE FROM group_permanent_memories WHERE id = ?", (int(memory_id),)
            )
            connection.commit()
        finally:
            connection.close()

    async def _before_write_lock(self) -> None:
        self.calls["write_lock"] += 1
        if self.independent_delete_id is not None:
            memory_id, self.independent_delete_id = self.independent_delete_id, None
            self._delete_with_stdlib(memory_id)
        if self.barrier_writers > 1:
            self._arrived += 1
            if self._arrived >= self.barrier_writers:
                self._barrier.set()
            await asyncio.wait_for(self._barrier.wait(), self.barrier_timeout)

    def _install(self, session) -> None:
        real_get = session.get
        real_execute = session.execute
        real_refresh = session.refresh
        real_commit = session.commit
        real_rollback = session.rollback
        real_close = session.close
        real_lock = getattr(session, "_acquire_write_lock", None)
        state = {"touched": False, "dml_seen": False}

        async def _get(entity, ident, *args, **kwargs):
            if entity is GroupPermanentMemory:
                state["touched"] = True
                self.calls["get"] += 1
            return await real_get(entity, ident, *args, **kwargs)

        async def _execute(statement, *args, **kwargs):
            kind = self._kind(statement)
            if kind is not None:
                state["touched"] = True
                self.statements.append(str(statement))
                if kind in ("delete", "update"):
                    state["dml_seen"] = True
                    self.calls[kind] += 1
                    mode = self.execute_faults.get(kind)
                    if mode and mode != "ok":
                        self.execute_faults[kind] = "ok"
                        return await self._run_fault(
                            f"{kind}_execute", mode, real_execute, statement, *args, **kwargs
                        )
                    override = self.rowcount_override
                    if override is not None and override[0] == kind:
                        self.rowcount_override = None
                        await real_execute(statement, *args, **kwargs)
                        return SimpleNamespace(rowcount=override[1])
                elif state["dml_seen"] and self.refresh_fault:
                    mode = self.refresh_fault
                    self.refresh_fault = None
                    return await self._run_fault(
                        "select_execute", mode, real_execute, statement, *args, **kwargs
                    )
            return await real_execute(statement, *args, **kwargs)

        async def _refresh(instance, *args, **kwargs):
            if state["touched"]:
                self.calls["refresh"] += 1
                mode = self.refresh_fault
                if mode and mode != "ok":
                    self.refresh_fault = None
                    return await self._run_fault(
                        "refresh", mode, real_refresh, instance, *args, **kwargs
                    )
            return await real_refresh(instance, *args, **kwargs)

        async def _commit(*args, **kwargs):
            if state["touched"]:
                self.calls["commit"] += 1
                mode = self.commit_fault
                if mode and mode != "ok":
                    self.commit_fault = None
                    return await self._run_fault("commit", mode, real_commit, *args, **kwargs)
            return await real_commit(*args, **kwargs)

        async def _rollback(*args, **kwargs):
            if state["touched"]:
                self.calls["rollback"] += 1
                mode = self._next_mode("rollback")
                if mode and mode != "ok":
                    return await self._run_fault("rollback", mode, real_rollback, *args, **kwargs)
            return await real_rollback(*args, **kwargs)

        async def _close(*args, **kwargs):
            if state["touched"]:
                self.calls["close"] += 1
                mode = self._next_mode("close")
                if mode == "block":
                    self.close_entered.set()
                    await self.close_gate.wait()
                    return await real_close(*args, **kwargs)
                if mode == "fail":
                    exc = RuntimeError("synthetic close failure")
                    self._record("close_fail", exc)
                    raise exc
                if mode and mode != "ok":
                    return await self._run_fault("close", mode, real_close, *args, **kwargs)
            return await real_close(*args, **kwargs)

        session.get = _get
        session.execute = _execute
        session.refresh = _refresh
        session.commit = _commit
        session.rollback = _rollback
        session.close = _close
        if real_lock is not None:

            async def _lock(*args, **kwargs):
                if state["touched"]:
                    await self._before_write_lock()
                return await real_lock(*args, **kwargs)

            session._acquire_write_lock = _lock


class _MemoryWriteTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        handle, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN,
            config_master_key="miniapp-memory-write-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-miniapp-memory-writes.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        # 不提供 get_chat_administrators：权限交叉校验按既有离线降级处理（None），
        # 本地 Admin 表照常生效；也不接任何真实 Telegram/LLM。
        self.bot = SimpleNamespace(token=BOT_TOKEN)
        self.factory = _FaultFactory(self.session_factory, db_path=self._db_path)
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
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    # --- 夹具 -------------------------------------------------------------
    async def _seed(self) -> None:
        async with self.session_factory() as session:
            # `admins.group_id` 外键指向 `authorized_groups.group_id`，先 flush 授权行
            # 再插管理员（与既有 settings_web 夹具同一顺序）。
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

    async def _seed_memory(
        self,
        *,
        group_id: int = GROUP_A,
        content: str = "第一条永久备忘",
        created_by: int = ADMIN_A,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ) -> int:
        created = created_at or datetime(2020, 1, 1, 0, 0, 0)
        updated = updated_at if updated_at is not None else created
        async with self.session_factory() as session:
            row = GroupPermanentMemory(
                group_id=group_id,
                content=content,
                created_by=created_by,
                created_at=created,
                updated_at=updated,
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _fresh_row(self, memory_id: int) -> tuple[int, str] | None:
        async with self.session_factory() as session:
            row = await session.get(GroupPermanentMemory, memory_id)
            if row is None:
                return None
            return (int(row.id), str(row.content or ""))

    async def _fresh_ids(self, group_id: int = GROUP_A) -> list[int]:
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(GroupPermanentMemory.id)
                .where(GroupPermanentMemory.group_id == group_id)
                .order_by(GroupPermanentMemory.id)
            )
            return [int(value) for value in rows.all()]

    async def _fresh_updated_at(self, memory_id: int) -> str:
        async with self.session_factory() as session:
            row = await session.get(GroupPermanentMemory, memory_id)
            self.assertIsNotNone(row)
            return row.updated_at.isoformat()  # type: ignore[union-attr]

    async def _assert_write_lock_available(self, timeout: float = 1.0) -> None:
        """独立 writer 限时完成才算「服务确实把写锁还回来了」。"""

        async def _probe() -> None:
            async with self.session_factory() as session:
                await session.execute(
                    sa_text("DELETE FROM group_permanent_memories WHERE id = -1")
                )
                await session.commit()

        await asyncio.wait_for(_probe(), timeout)

    # --- 请求 -------------------------------------------------------------
    @staticmethod
    def _headers(user_id: int = SUPER_ADMIN) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    @staticmethod
    def _item_url(group_id: int, memory_id: int) -> str:
        return f"/api/v1/groups/{group_id}/memories/{memory_id}"

    def _field(self, body: dict, name: str):
        """读响应字段：字段缺失本身就是失败，且以 AssertionError 形式报出来。"""

        self.assertIn(name, body, f"响应体缺少字段 {name}：{body!r}")
        return body[name]

    async def _delete(
        self, memory_id: int, *, group_id: int = GROUP_A, user_id: int = SUPER_ADMIN
    ):
        return await self.client.delete(
            self._item_url(group_id, memory_id), headers=self._headers(user_id)
        )

    async def _patch(
        self,
        memory_id: int,
        *,
        content: str,
        group_id: int = GROUP_A,
        user_id: int = SUPER_ADMIN,
    ):
        return await self.client.patch(
            self._item_url(group_id, memory_id),
            headers={**self._headers(user_id), "Content-Type": "application/json"},
            data=json.dumps({"content": content}),
        )


class MemoryWriteOutcomeHttpTests(_MemoryWriteTestCase):
    """真 HTTP + 真 DML：只断言对调用者可证明的三态结果。"""

    # --- 正常路径 ---------------------------------------------------------
    async def test_patch_normal_path_returns_the_real_written_document(self) -> None:
        memory_id = await self._seed_memory(
            content="旧正文",
            created_by=ADMIN_A,
            created_at=datetime(2020, 1, 1, 0, 0, 0),
            updated_at=datetime(2020, 1, 1, 0, 0, 0),
        )

        response = await self._patch(memory_id, content="新正文")
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(self._field(body, "status"), "updated")
        document = body["memory"]
        self.assertEqual(
            sorted(document), ["content", "created_at", "created_by", "id", "updated_at"]
        )
        self.assertEqual(document["id"], memory_id)
        self.assertEqual(document["content"], "新正文")
        self.assertEqual(document["created_by"], ADMIN_A)
        self.assertEqual(document["created_at"], "2020-01-01T00:00:00")
        # `updated_at` 必须来自**真实写入**，不是 commit 之前的旧值。
        self.assertNotEqual(document["updated_at"], "2020-01-01T00:00:00")
        self.assertEqual(document["updated_at"], await self._fresh_updated_at(memory_id))
        self.assertEqual(await self._fresh_row(memory_id), (memory_id, "新正文"))

    async def test_patch_same_content_still_returns_a_consistent_document(self) -> None:
        memory_id = await self._seed_memory(
            content="没变",
            created_by=ADMIN_A,
            updated_at=datetime(2020, 1, 1, 0, 0, 0),
        )

        response = await self._patch(memory_id, content="没变")
        self.assertEqual(response.status, 200)
        document = (await response.json())["memory"]
        self.assertEqual(document["content"], "没变")
        self.assertEqual(document["created_by"], ADMIN_A)
        # 同内容不产生 UPDATE：时间戳保持既有值，也不得改写创建者/创建时间。
        self.assertEqual(document["updated_at"], "2020-01-01T00:00:00")
        self.assertEqual(document["updated_at"], await self._fresh_updated_at(memory_id))

    async def test_delete_normal_path_reports_a_confirmed_deletion(self) -> None:
        keep = await self._seed_memory(content="留着")
        target = await self._seed_memory(content="删掉")

        response = await self._delete(target)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["deleted"], True)
        self.assertEqual(self._field(body, "changed"), True)
        self.assertEqual(self._field(body, "status"), "deleted")
        self.assertIsNone(await self._fresh_row(target))
        self.assertEqual(await self._fresh_ids(), [keep])

    async def test_delete_statement_is_group_scoped_and_never_repeated(self) -> None:
        target = await self._seed_memory()
        self.factory.arm()  # 只记录，不注入故障

        response = await self._delete(target)
        self.assertEqual(response.status, 200)
        deletes = [
            statement
            for statement in self.factory.statements
            if statement.lstrip().upper().startswith("DELETE")
        ]
        self.assertEqual(len(deletes), 1)
        self.assertIn("group_permanent_memories.group_id", deletes[0])
        self.assertIn("group_permanent_memories.id", deletes[0])
        # 正常结局：恰好一次 commit，没有补偿 commit、没有重删。
        self.assertEqual(self.factory.calls["commit"], 1)
        self.assertEqual(self.factory.calls["close"] >= 1, True)

    # --- 入口状态 ---------------------------------------------------------
    async def test_missing_and_cross_group_are_the_same_verbatim_404(self) -> None:
        other_group_memory = await self._seed_memory(group_id=GROUP_B, content="别的群")

        missing = await self._delete(999_999_999)
        cross_group = await self._delete(other_group_memory)
        cross_patch = await self._patch(other_group_memory, content="偷改")

        self.assertEqual(missing.status, 404)
        self.assertEqual(cross_group.status, 404)
        self.assertEqual(cross_patch.status, 404)
        self.assertEqual(await missing.json(), NOT_FOUND_BODY)
        self.assertEqual(await cross_group.json(), NOT_FOUND_BODY)
        self.assertEqual(await cross_patch.json(), NOT_FOUND_BODY)
        self.assertEqual(
            await self._fresh_row(other_group_memory), (other_group_memory, "别的群")
        )

    async def test_delete_no_change_when_an_independent_writer_already_deleted(self) -> None:
        target = await self._seed_memory()
        # 本群预读之后、写锁之前，另一个**独立** session 把这一行删掉并提交。
        self.factory.arm(independent_delete_id=target)

        response = await self._delete(target)
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["ok"], True)
        self.assertEqual(body["deleted"], False)
        self.assertEqual(self._field(body, "changed"), False)
        self.assertEqual(self._field(body, "status"), "no_change")
        self.assertIsNone(await self._fresh_row(target))
        self.assertGreaterEqual(self.factory.calls["write_lock"], 1)

    async def test_patch_conflicts_with_a_concurrent_deletion_as_409(self) -> None:
        target = await self._seed_memory(content="竞争前")
        self.factory.arm(independent_delete_id=target)

        response = await self._patch(target, content="竞争后")
        self.assertEqual(response.status, 409)
        body = await response.json()
        self.assertEqual(
            body, {"ok": False, "error": {"code": "memory_unavailable", "message": UNAVAILABLE_MESSAGE}}
        )

    async def test_two_overlapping_deletes_produce_exactly_one_real_change(self) -> None:
        target = await self._seed_memory()
        self.factory.arm(barrier_writers=2)

        first, second = await asyncio.gather(self._delete(target), self._delete(target))
        self.assertEqual([first.status, second.status], [200, 200])
        first_body = await first.json()
        second_body = await second.json()
        statuses = sorted([self._field(first_body, "status"), self._field(second_body, "status")])
        self.assertEqual(statuses, ["deleted", "no_change"])
        bodies = [await first.json(), await second.json()]
        confirmed = [body for body in bodies if self._field(body, "status") == "deleted"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(self._field(confirmed[0], "changed"), True)
        self.assertIsNone(await self._fresh_row(target))
        self.assertEqual(len(self.factory.statements), 2)

    # --- 未确认 -----------------------------------------------------------
    async def test_commit_failure_before_returning_is_unconfirmed(self) -> None:
        target = await self._seed_memory()
        self.factory.arm(commit_fault="fail_before")

        response = await self._delete(target)
        self.assertEqual(response.status, 503)
        self.assertEqual(
            await response.json(),
            {"ok": False, "error": {"code": "memory_write_unconfirmed", "message": UNCONFIRMED_MESSAGE}},
        )
        self.assertEqual(await self._fresh_row(target), (target, "第一条永久备忘"))
        await self._assert_write_lock_available()

    async def test_commit_failure_after_persisting_is_still_unconfirmed(self) -> None:
        target = await self._seed_memory()
        self.factory.arm(commit_fault="fail_after")

        response = await self._delete(target)
        # 没有「commit 正常返回」就只能是未确认：不能因为事后看得见落库就报成功。
        self.assertEqual(response.status, 503)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "memory_write_unconfirmed")
        # 数据真值与客户端确认分开：库里确实已经不在了。
        self.assertIsNone(await self._fresh_row(target))
        await self._assert_write_lock_available()

    async def test_execute_failure_before_and_after_are_unconfirmed(self) -> None:
        for mode in ("fail_before", "fail_after"):
            with self.subTest(mode=mode):
                target = await self._seed_memory(content=f"未确认-{mode}")
                self.factory.arm(execute_faults={"delete": mode})

                response = await self._delete(target)
                self.assertEqual(response.status, 503)
                body = await response.json()
                self.assertEqual(body["error"]["code"], "memory_write_unconfirmed")
                # DELETE 没被 commit，行必须完好。
                self.assertEqual(await self._fresh_row(target), (target, f"未确认-{mode}"))
                self.factory.disarm()
                self.factory.execute_faults = {}
                await self._assert_write_lock_available()

    async def test_refresh_failure_on_the_update_path_is_unconfirmed(self) -> None:
        target = await self._seed_memory(content="刷新前")
        self.factory.arm(refresh_fault="fail_before")

        response = await self._patch(target, content="刷新后")
        self.assertEqual(response.status, 503)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "memory_write_unconfirmed")
        self.assertEqual(await self._fresh_row(target), (target, "刷新前"))

    async def test_malformed_rowcount_is_never_reported_as_a_change(self) -> None:
        for bad in (True, 1.0, "1", -1, 2, None):
            with self.subTest(rowcount=bad):
                target = await self._seed_memory(content="rowcount 未确认")
                self.factory.arm(rowcount_override=("delete", bad))

                response = await self._delete(target)
                self.assertEqual(response.status, 503)
                body = await response.json()
                self.assertEqual(body["error"]["code"], "memory_write_unconfirmed")
                self.factory.disarm()
                await self._assert_write_lock_available()

    async def test_unconfirmed_cleanup_still_releases_the_resources(self) -> None:
        target = await self._seed_memory()
        # commit 从未正常返回；rollback 首次失败、第二次成功；close 首次失败、
        # 第二次成功。可恢复的清理不能被第一次失败提前放弃。
        self.factory.arm(
            commit_fault="fail_before",
            rollback_plan=("fail", "ok"),
            close_plan=("fail_before", "ok"),
        )

        response = await self._delete(target)
        self.assertEqual(response.status, 503)
        body = await response.json()
        self.assertEqual(body["error"]["code"], "memory_write_unconfirmed")
        self.assertEqual(await self._fresh_row(target), (target, "第一条永久备忘"))
        self.assertGreaterEqual(self.factory.calls["rollback"], 2)
        self.assertGreaterEqual(self.factory.calls["close"], 2)
        await self._assert_write_lock_available()

    async def test_close_failure_after_a_confirmed_delete_keeps_the_confirmed_result(self) -> None:
        """commit 正常返回之后 close 永远失败：数据真值仍是「已删除」，不当 5xx。"""

        target = await self._seed_memory(content="确认后清理失败")
        self.factory.arm(close_plan="fail")

        response = await self._delete(target)
        # 资源归还失败不能颠倒已经确认的持久化结果。
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertIs(self._field(body, "deleted"), True)
        self.assertIs(self._field(body, "changed"), True)
        self.assertEqual(self._field(body, "status"), "deleted")
        self.assertIsNone(await self._fresh_row(target))
        # 两次有界尝试都做过（本用例不声称归还成功：全部恢复手段都失败时锁确实
        # 留在这个用例自己的事件循环里，这正是负控制要如实披露的边界）。
        self.assertGreaterEqual(self.factory.calls["close"], 2)

    async def test_original_cancellation_produces_no_json_response_and_frees_the_lock(self) -> None:
        target = await self._seed_memory()
        # 原始取消 A 在 commit 前发生；rollback 永远失败；close 首次又被取消 B、
        # 第二次本可成功。A 必须原样传播，且临时故障不能阻止真正的资源归还。
        self.factory.arm(
            commit_fault="cancel_before",
            rollback_plan="fail",
            close_plan=("cancel_before", "ok"),
        )
        original = None

        # 取到回执就说明取消被吞成了普通业务结果——`assertRaises` 本身就是
        # 「没有 JSON 回执」的断言；传输层会表现为连接中断。
        with self.assertRaises(BaseException):
            await self._delete(target)
        original = self.factory._raised("commit_cancel")
        self.assertIsInstance(original, asyncio.CancelledError)
        self.assertGreaterEqual(self.factory.calls["close"], 2)
        self.assertEqual(await self._fresh_row(target), (target, "第一条永久备忘"))
        await self._assert_write_lock_available()

    # --- 不泄漏 -----------------------------------------------------------
    async def test_error_bodies_never_leak_internals_or_other_groups(self) -> None:
        target = await self._seed_memory()
        other_group_memory = await self._seed_memory(group_id=GROUP_B, content="别的群")
        self.factory.arm(commit_fault="fail_before")

        response = await self._delete(target)
        self.assertEqual(response.status, 503)
        text = await response.text()
        for forbidden in (
            "RuntimeError",
            "synthetic",
            "SELECT",
            "UPDATE",
            "DELETE",
            "commit",
            "rowcount",
            str(GROUP_B),
            "别的群",
        ):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("第一条永久备忘", text)
        self.assertEqual(await self._fresh_row(other_group_memory), (other_group_memory, "别的群"))

    # --- 既有契约 ---------------------------------------------------------
    async def test_authentication_and_group_permissions_are_unchanged(self) -> None:
        target = await self._seed_memory()
        other_group_memory = await self._seed_memory(group_id=GROUP_B, content="别的群")

        anonymous = await self.client.delete(self._item_url(GROUP_A, target))
        self.assertEqual(anonymous.status, 401)

        foreign_admin = await self._delete(target, user_id=ADMIN_B)
        self.assertEqual(foreign_admin.status, 403)
        self.assertEqual(
            (await foreign_admin.json())["error"]["code"], "group_access_denied"
        )

        member = await self._delete(target, user_id=MEMBER)
        self.assertEqual(member.status, 403)
        self.assertEqual((await member.json())["error"]["code"], "group_access_denied")

        own_admin = await self._delete(target, user_id=ADMIN_A)
        self.assertEqual(own_admin.status, 200)
        self.assertEqual(self._field(await own_admin.json(), "status"), "deleted")

        super_admin = await self._delete(
            other_group_memory, group_id=GROUP_B, user_id=SUPER_ADMIN
        )
        self.assertEqual(super_admin.status, 200)

    async def test_content_validation_and_stored_content_are_unchanged(self) -> None:
        target = await self._seed_memory(content="校验")

        blank = await self._patch(target, content="   ")
        self.assertEqual(blank.status, 400)
        self.assertEqual((await blank.json())["error"]["code"], "empty_memory")

        too_long = await self._patch(target, content="长" * 4001)
        self.assertEqual(too_long.status, 400)
        self.assertEqual((await too_long.json())["error"]["code"], "validation_error")

        unknown_field = await self.client.patch(
            self._item_url(GROUP_A, target),
            headers={**self._headers(), "Content-Type": "application/json"},
            data=json.dumps({"content": "x", "created_by": 1}),
        )
        self.assertEqual(unknown_field.status, 400)

        html = "<b>危险</b>&<script>alert(1)</script>"
        stored = await self._patch(target, content=html)
        self.assertEqual(stored.status, 200)
        document = (await stored.json())["memory"]
        self.assertEqual(document["content"], html)
        self.assertEqual(await self._fresh_row(target), (target, html))
        self.assertEqual(document["created_by"], ADMIN_A)


class PermanentMemoryWriteHelperTests(_MemoryWriteTestCase):
    """直接调新 helper：严格 rowcount、三态、取消身份、有界归还。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        if pmw is None:  # pragma: no cover - 冻结基线上必然走到这里
            self.fail(f"bot.services.permanent_memory_writes 不可导入：{_PMW_IMPORT_ERROR!r}")

    async def _delete_row(self, memory_id: int):
        return await pmw.delete_permanent_memory_row(
            self.factory, group_id=GROUP_A, memory_id=memory_id
        )

    async def _update_row(self, memory_id: int, content: str):
        return await pmw.update_permanent_memory_row(
            self.factory,
            group_id=GROUP_A,
            memory_id=memory_id,
            content=content,
            document_factory=_row_document,
        )

    async def test_delete_helper_reports_the_terminal_states(self) -> None:
        deleted = await self._seed_memory(content="删掉")
        no_change = await self._seed_memory(content="竞争")
        other_group = await self._seed_memory(group_id=GROUP_B, content="别的群")

        outcome = await self._delete_row(deleted)
        self.assertEqual(outcome.status, pmw.PM_WRITE_DELETED)
        self.assertTrue(outcome.confirmed_deleted)
        self.assertFalse(outcome.confirmed_no_change)
        self.assertEqual(outcome.changed_rows, 1)
        self.assertTrue(outcome.resource_released)

        missing = await self._delete_row(999_999_999)
        self.assertEqual(missing.status, pmw.PM_WRITE_NOT_FOUND)
        self.assertEqual(missing.changed_rows, None)

        cross = await self._delete_row(other_group)
        self.assertEqual(cross.status, pmw.PM_WRITE_NOT_FOUND)
        self.assertEqual(
            await self._fresh_row(other_group), (other_group, "别的群")
        )

        self.factory.arm(independent_delete_id=no_change)
        raced = await self._delete_row(no_change)
        self.assertEqual(raced.status, pmw.PM_WRITE_NO_CHANGE)
        self.assertTrue(raced.confirmed_no_change)
        self.assertEqual(raced.changed_rows, 0)

    async def test_update_helper_reports_the_terminal_states(self) -> None:
        target = await self._seed_memory(content="旧", updated_at=datetime(2020, 1, 1))
        other_group = await self._seed_memory(group_id=GROUP_B, content="别的群")

        outcome = await self._update_row(target, "新")
        self.assertEqual(outcome.status, pmw.PM_WRITE_UPDATED)
        self.assertTrue(outcome.confirmed_updated)
        self.assertTrue(outcome.resource_released)
        self.assertEqual(outcome.document["content"], "新")
        self.assertEqual(outcome.document["created_by"], ADMIN_A)
        self.assertNotEqual(outcome.document["updated_at"], "2020-01-01T00:00:00")

        missing = await self._update_row(999_999_999, "x")
        self.assertEqual(missing.status, pmw.PM_WRITE_NOT_FOUND)
        self.assertIsNone(missing.document)

        cross = await self._update_row(other_group, "偷改")
        self.assertEqual(cross.status, pmw.PM_WRITE_NOT_FOUND)

        raced = await self._seed_memory(content="竞争")
        self.factory.arm(independent_delete_id=raced)
        conflicted = await self._update_row(raced, "竞争后")
        self.assertEqual(conflicted.status, pmw.PM_WRITE_UNAVAILABLE)
        self.assertTrue(conflicted.unavailable)

    async def test_commit_failure_is_unconfirmed_and_released(self) -> None:
        target = await self._seed_memory()
        self.factory.arm(commit_fault="fail_before")

        outcome = await self._delete_row(target)
        self.assertEqual(outcome.status, pmw.PM_WRITE_UNCONFIRMED)
        self.assertEqual(outcome.detail, pmw.PM_WRITE_REASON_COMMIT_FAILED)
        self.assertTrue(outcome.resource_released)
        self.assertEqual(await self._fresh_row(target), (target, "第一条永久备忘"))
        await self._assert_write_lock_available()

    async def test_confirmed_delete_survives_a_recoverable_close_fault(self) -> None:
        target = await self._seed_memory(content="可恢复的清理故障")
        # commit 已正常返回；close 首次失败、第二次成功 ⇒ 结果仍确认，资源已归还。
        self.factory.arm(close_plan=("fail_before", "ok"))

        outcome = await self._delete_row(target)
        self.assertEqual(outcome.status, pmw.PM_WRITE_DELETED)
        self.assertEqual(outcome.changed_rows, 1)
        self.assertTrue(outcome.resource_released)
        self.assertIsNone(await self._fresh_row(target))
        self.assertGreaterEqual(self.factory.calls["close"], 2)
        await self._assert_write_lock_available()

    async def test_original_cancellation_identity_survives_cleanup(self) -> None:
        target = await self._seed_memory()
        self.factory.arm(
            commit_fault="cancel_before",
            rollback_plan="fail",
            close_plan=("cancel_before", "ok"),
        )

        with self.assertRaises(asyncio.CancelledError) as caught:
            await self._delete_row(target)
        original = self.factory._raised("commit_cancel")
        self.assertIs(caught.exception, original)
        self.assertGreaterEqual(self.factory.calls["close"], 2)
        await self._assert_write_lock_available()

    async def test_cleanup_cancellation_without_an_original_cancel_propagates(self) -> None:
        target = await self._seed_memory()
        # commit 是普通失败；cleanup 期间出现的是**新**取消：尽力归还后必须原样
        # 抛出，绝不能被吞成普通业务结局。
        self.factory.arm(
            commit_fault="fail_before",
            rollback_plan=("cancel_before", "ok"),
            close_plan=("cancel_before", "ok"),
        )

        with self.assertRaises(asyncio.CancelledError) as caught:
            await self._delete_row(target)
        self.assertIs(caught.exception, self.factory._raised("rollback_cancel"))
        self.assertGreaterEqual(self.factory.calls["close"], 2)
        await self._assert_write_lock_available()

    async def test_real_task_cancellation_at_the_cleanup_boundary(self) -> None:
        target = await self._seed_memory(content="取消前")
        self.factory.arm(close_plan=("block", "ok"))
        self.factory.close_gate.clear()

        task = asyncio.ensure_future(self._update_row(target, "取消边界"))
        await asyncio.wait_for(self.factory.close_entered.wait(), 1.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # 取消发生在 cleanup 的 await 边界上：资源仍必须归还，task 结果确定。
        await self._assert_write_lock_available()
        # commit 早已正常返回，数据真值不因 cleanup 取消被改写。
        self.assertEqual(await self._fresh_row(target), (target, "取消边界"))

    async def test_permanent_recovery_failure_is_disclosed_not_pretended_safe(self) -> None:
        target = await self._seed_memory(content="清理永久失败")

        with self.assertLogs("bot.services.permanent_memory_writes", level="WARNING") as logs:
            self.factory.arm(close_plan="fail")
            outcome = await self._update_row(target, "清理永久失败后")

        self.assertEqual(outcome.status, pmw.PM_WRITE_UPDATED)
        self.assertEqual(outcome.document["content"], "清理永久失败后")
        # 不虚构「已经成功归还」：这里如实上报 False，并留固定故障信号。
        self.assertFalse(outcome.resource_released)
        self.assertIn(pmw.PM_WRITE_REASON_CLEANUP_UNRELEASED, "\n".join(logs.output))

    async def test_delete_permanent_memory_legacy_path_is_untouched(self) -> None:
        """B6 的 `_lm` 内联按钮路径不能因为 B7 而改变形状。"""

        from bot.config import BotConfig
        from bot.services.memory import (
            PM_DELETE_DELETED,
            PM_DELETE_NO_CHANGE,
            PM_DELETE_UNCONFIRMED,
            MemoryService,
        )

        target = await self._seed_memory(content="legacy")
        holder = MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            SimpleNamespace(),
            session_factory=self.session_factory,
        )
        # legacy 入口仍返回被删除的行对象列表，并且仍然真的删掉了。
        leftover = await holder.delete_permanent_memory(GROUP_A, f"#{target}")
        self.assertEqual([int(row.id) for row in leftover], [target])
        self.assertIsNone(await self._fresh_row(target))
        # B6 的结果方法仍然挂在类上，状态集合未被 B7 改写。
        outcome_method = getattr(MemoryService, "delete_permanent_memory_outcome", None)
        self.assertTrue(callable(outcome_method))
        self.assertEqual(
            {PM_DELETE_DELETED, PM_DELETE_NO_CHANGE, PM_DELETE_UNCONFIRMED},
            {"deleted", "no_change", "unconfirmed"},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
