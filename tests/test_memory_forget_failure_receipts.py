"""B5：``/memory forget <编号>`` 的失败回执与事务边界。

背景（父代理独立复现，基线产品 ``7caa54cc6f66a48b4fddeab338d08a8258f8687e``）：
``soft_delete_fact`` 捕获所有普通写异常、执行 ``_safe_rollback`` 之后返回 ``False``，
与「成功但零命中」同形；handler 于是把三种语义完全不同的情形都渲染成
「这条记录已经删过了。」：

1. commit 在**实际提交之前**抛错 → 事实仍 ``active``，回执却说是「已经删过了」；
2. commit **已真实持久化之后**抛错 → 事实已 ``deleted``，回执仍是同一句；
3. commit 未真实执行 + ``rollback`` 也抛错 → 服务正常 return，
   ``DbSessionMiddleware`` 的补偿 commit 把残留删除**真的写进库**（``commit=2``）。

本文件按**红检资格**分两段：

* ``MemoryForgetReceiptTests`` —— 只用**基线既有**产品符号（真实 ``cmd_memory`` + 真实
  service + 真实临时 SQLite + 真实 ``DbSessionMiddleware`` + 真实渲染/发送助手，只把最终
  ``Message.answer`` 换成记录器）。在冻结基线 ``7e89191`` 上这些用例必须是**业务语义
  AssertionError**，不能是导入失败或夹具错误。红检只跑这一段。
* ``MemoryForgetOutcomeProtocolTests`` —— 直接验证服务层新接口
  （``soft_delete_fact_outcome``）与 legacy 包装。基线不存在该符号，故**不参与旧红检**；
  这里用守卫式导入标注，运行时若缺符号会明确失败。

替换边界：只替换**最终外部 transport**（``Message.answer``）与**精确的 DB 边界**
（``AsyncSession`` 上的 ``execute`` / ``commit`` / ``rollback`` / ``close``）；授权查询与
持久删除队列用替身。绝不发真实 Telegram 消息、绝不调用真实 LLM。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Group, UserFact
from bot.handlers import commands
from bot.middlewares.db import DbSessionMiddleware
from bot.utils import telegram as telegram_utils

# B4 既有符号：真实基线产品已经存在，业务红检段直接用它们做断言。
from bot.services.long_term_memory import (  # noqa: E402 - 保持导入分组清晰
    MemorySessionCleanupUnsafeError,
    MemoryWriteOutcome,
)

try:  # B5 新增符号：基线不存在 → 只有协议段受影响，业务段仍能 collect。
    from bot.services.long_term_memory import soft_delete_fact_outcome

    _HAS_FORGET_OUTCOME = True
except ImportError:  # pragma: no cover - 冻结基线
    soft_delete_fact_outcome = None  # type: ignore[assignment]
    _HAS_FORGET_OUTCOME = False


SUPERGROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009999999999
MEMBER_ID = 777
OTHER_MEMBER_ID = 778

CONFIRMED_AT = datetime(2026, 1, 4, 12, 0, 0)
FIRST_SEEN_AT = datetime(2026, 1, 1, 9, 0, 0)

# 父代理任务书逐字给定的三条确定文案（B5 §3）。
FORGOTTEN_TEXT = "已标记删除这条长期记忆。"
UNCHANGED_TEXT = "未改动这条长期记忆；记录可能已删除或已不可用。请使用 /memory 重新查看。"
UNCONFIRMED_HEAD = "<b>未能确认这条长期记忆是否已标记删除</b>"
UNCONFIRMED_BODY = (
    "这次操作未能确认成功，请使用 /memory 重新查看；如果持续失败，请联系机器人维护者。"
)

# 本节要**保留**的既有文案（逐字不变）。
MISSING_OR_DENIED_TEXT = (
    "没有找到这个编号，或者你没有权限删这条"
    "（只能删关于你本人的；群管理员可以删本群任意一条）。"
    "编号是 /memory 列表里的全局序号，跨页连续。"
)
USAGE_HEAD = "用法：<code>/memory forget &lt;编号&gt;</code>"
USAGE_TAIL = "编号就是 /memory 列表里的 #编号。"

# 被 B5 取代的旧文案：任何路径都不得再出现。
RETIRED_TEXTS = ("这条记录已经删过了。", "已删除这条长期记忆。")

# 不得出现在失败回执里的东西：异常文本、SQL、个人/群标识、更强的承诺。
FORBIDDEN_IN_FAILURE = (
    "synthetic",
    "RuntimeError",
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "-1001234567890",
    "-1009999999999",
    "彻底删除",
    "无残留",
    "永不再记",
    "已持久生效",
    "没有生效",
)


def _settings() -> Settings:
    return Settings(_env_file=None)


def _message(text: str, *, group_id: int = SUPERGROUP_ID, user_id: int = MEMBER_ID):
    return SimpleNamespace(
        text=text,
        message_id=1,
        chat=SimpleNamespace(
            id=group_id, type="supergroup", title="合成群", username=""
        ),
        from_user=SimpleNamespace(
            id=user_id, first_name="成员", last_name="", username=""
        ),
        bot=SimpleNamespace(send_message=AsyncMock(), get_chat=AsyncMock()),
    )


class _NoCloseSession:
    """真实会话的**代理**，只把 ``close`` 藏起来。

    刻意不删除 ``AsyncSession.close``（那会让夹具自己的 ``async with`` 也坏掉）：
    ``_discard_pending`` 用 ``getattr(session, "close", None)`` 探测，代理返回 ``None``
    即等价于「这个会话没有 close」。真实 context 仍由中间件正常结束并清理。
    """

    close = None

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class _ForgetTestCase(unittest.IsolatedAsyncioTestCase):
    """真实 handler + 真实 service + 真实 SQLite；只替换发送边界与外部授权查询。"""

    async def asyncSetUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = handle.name
        handle.close()
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=SUPERGROUP_ID, title="合成当前群", settings={}))
            session.add(Group(id=OTHER_GROUP_ID, title="合成另一个群", settings={}))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    # ---- 播种与读取 --------------------------------------------------------

    async def _seed_fact(self, *, fingerprint: str, **overrides: Any) -> int:
        values: dict[str, Any] = {
            "scope": "group",
            "scope_id": SUPERGROUP_ID,
            "subject_user_id": MEMBER_ID,
            "fact_text": f"合成事实 {fingerprint}",
            "category": "preference",
            "confidence": 50,
            "confirm_count": 1,
            "first_seen_at": FIRST_SEEN_AT,
            "last_confirmed_at": CONFIRMED_AT,
            "source_kind": "passive",
            "source_message_id": None,
            "evidence_excerpt": "",
            "fingerprint": fingerprint,
            "status": "active",
        }
        values.update(overrides)
        async with self.session_factory() as session:
            fact = UserFact(**values)
            session.add(fact)
            await session.commit()
            return int(fact.id)

    async def _fresh_fact(self, fact_id: int) -> tuple[str, Any]:
        """fresh session 读持久字段——不读 ORM 旧 identity map。"""

        async with self.session_factory() as session:
            row = await session.get(UserFact, fact_id)
            if row is None:
                return "<gone>", None
            return str(row.status), row.last_confirmed_at

    async def _fresh_status(self, fact_id: int) -> str:
        return (await self._fresh_fact(fact_id))[0]

    async def _fresh_count(self, *, status: str) -> int:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(UserFact.id).where(
                    UserFact.scope_id == SUPERGROUP_ID,
                    UserFact.status == status,
                )
            )
            return len(list(rows))

    # ---- 语句记录（证明「没发删除 UPDATE」） --------------------------------

    @contextlib.contextmanager
    def _record_statements(self):
        statements: list[str] = []

        def _hook(conn, cursor, statement, parameters, context, executemany):
            statements.append(str(statement))

        event.listen(self.engine.sync_engine, "before_cursor_execute", _hook)
        try:
            yield statements
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", _hook)

    @contextlib.contextmanager
    def _race_before_update(self, *, fact_id: int, mode: str):
        """在**真实边界**上制造并发：删除 UPDATE 真正发出之前，用另一条连接改库。

        用 ``before_cursor_execute`` 挂在真实 engine 上，配 stdlib ``sqlite3`` 的独立
        连接（WAL 允许读写并存），因此不依赖任何被 mock 的产品函数返回值。
        """

        fired = {"count": 0}

        def _hook(conn, cursor, statement, parameters, context, executemany):
            if "UPDATE user_facts" not in str(statement):
                return
            if fired["count"]:
                return
            fired["count"] += 1
            con = sqlite3.connect(self._db_path)
            try:
                if mode == "soft":
                    con.execute(
                        "UPDATE user_facts SET status='deleted' WHERE id=?", (fact_id,)
                    )
                elif mode == "purge":
                    con.execute("DELETE FROM user_facts WHERE id=?", (fact_id,))
                else:  # pragma: no cover - 防御
                    raise AssertionError(f"未知的并发模式 {mode!r}")
                con.commit()
            finally:
                con.close()

        event.listen(self.engine.sync_engine, "before_cursor_execute", _hook)
        try:
            yield fired
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", _hook)

    # ---- 真链发送（只替换 transport） --------------------------------------

    async def _invoke_real_send(
        self,
        session: AsyncSession,
        text: str,
        sent: list[dict[str, Any]],
        *,
        admin: bool = False,
        transport_fault: BaseException | None = None,
    ) -> None:
        """**不**替换 ``commands._answer``：走真实渲染 + 真实发送助手。"""

        message = _message(text)

        async def _transport(body: object, **kwargs: object) -> Any:
            if transport_fault is not None:
                raise transport_fault
            sent.append(
                {"body": str(body), "options": {k: str(v) for k, v in kwargs.items()}}
            )
            return SimpleNamespace(message_id=99, chat=message.chat)

        message.answer = _transport
        self.cleanup_mock = AsyncMock(return_value=True)
        with patch.object(
            commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
        ), patch.object(
            commands, "memory_facts_enabled", return_value=True
        ), patch.object(
            commands, "_schedule_memory_command_cleanup", new=AsyncMock()
        ), patch.object(
            commands, "is_super_admin_user_id", return_value=False
        ), patch.object(
            commands,
            "is_group_admin_authorized",
            new=AsyncMock(return_value=admin),
        ), patch.object(
            telegram_utils,
            "schedule_message_auto_delete_durable",
            new=self.cleanup_mock,
            create=True,
        ):
            await commands.cmd_memory(message, session, _settings())

    # ---- DB 边界故障注入 ---------------------------------------------------

    @staticmethod
    def _boundary_effects(
        session: AsyncSession,
        *,
        execute_mode: str = "ok",
        commit_mode: str = "ok",
        rollback_mode: str = "ok",
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """在**真实** session 上按模式替换边界方法；只影响第一次 UPDATE / commit。

        ``*_mode`` 取值：``ok`` / ``fail_before`` / ``fail_after`` / ``cancel_before`` /
        ``cancel_after``（execute 与 commit），``ok`` / ``fail`` / ``cancel``（rollback）。
        """

        real_execute = session.execute
        real_commit = session.commit
        real_rollback = session.rollback
        calls = {"execute_update": 0, "commit": 0, "rollback": 0}

        async def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            if "UPDATE user_facts" in str(statement) and calls["execute_update"] == 0:
                calls["execute_update"] += 1
                if execute_mode == "cancel_before":
                    raise asyncio.CancelledError()
                if execute_mode == "fail_before":
                    raise RuntimeError("synthetic execute before dml")
                result = await real_execute(statement, *args, **kwargs)
                if execute_mode == "cancel_after":
                    raise asyncio.CancelledError()
                if execute_mode == "fail_after":
                    raise RuntimeError("synthetic execute after dml")
                return result
            return await real_execute(statement, *args, **kwargs)

        async def commit() -> None:
            calls["commit"] += 1
            if calls["commit"] == 1:
                if commit_mode == "cancel_before":
                    raise asyncio.CancelledError()
                if commit_mode == "fail_before":
                    raise RuntimeError("synthetic commit before persist")
                await real_commit()
                if commit_mode == "cancel_after":
                    raise asyncio.CancelledError()
                if commit_mode == "fail_after":
                    raise RuntimeError("synthetic commit after persist")
                return
            await real_commit()

        async def rollback() -> None:
            calls["rollback"] += 1
            if rollback_mode == "cancel":
                raise asyncio.CancelledError()
            if rollback_mode == "fail":
                raise RuntimeError("synthetic rollback")
            await real_rollback()

        return {"execute": execute, "commit": commit, "rollback": rollback}, calls

    async def _run_real_send(
        self,
        text: str,
        *,
        execute_mode: str = "ok",
        commit_mode: str = "ok",
        rollback_mode: str = "ok",
        admin: bool = False,
        race: tuple[int, str] | None = None,
        record: bool = False,
    ) -> dict[str, Any]:
        """真链发送，可选地在真实 session 上注入边界故障；返回观测结果。"""

        sent: list[dict[str, Any]] = []
        error: BaseException | None = None
        in_transaction: bool | None = None
        statements: list[str] = []
        calls: dict[str, int] = {}
        stack = contextlib.ExitStack()
        with stack:
            if record:
                statements = stack.enter_context(self._record_statements())
            if race is not None:
                stack.enter_context(
                    self._race_before_update(fact_id=race[0], mode=race[1])
                )
            async with self.session_factory() as session:
                effects, calls = self._boundary_effects(
                    session,
                    execute_mode=execute_mode,
                    commit_mode=commit_mode,
                    rollback_mode=rollback_mode,
                )
                for name, effect in effects.items():
                    stack.enter_context(patch.object(session, name, new=effect))
                try:
                    await self._invoke_real_send(session, text, sent, admin=admin)
                except BaseException as exc:  # noqa: BLE001 - 观察取消与阻断异常
                    error = exc
                # teardown 之前观察事务状态（取消/清理必须已经完成）。
                in_transaction = session.in_transaction()
        return {
            "sent": sent,
            "error": error,
            "in_transaction": in_transaction,
            "statements": statements,
            "calls": calls,
        }

    async def _run_through_middleware(
        self,
        text: str,
        *,
        class_side_effects: dict[str, Any],
        sent: list[dict[str, Any]],
        wrap_session: Any = None,
        admin: bool = False,
        transport_fault: BaseException | None = None,
    ) -> list[BaseException]:
        """真实 ``DbSessionMiddleware`` + 真实 handler + 真链发送。

        ``class_side_effects`` 打在上游基类 ``AsyncSession`` 上——这正是父代理探针用的
        注入面：生产 session 是它的子类，``super()`` 会命中被替换的实现。返回从
        ``middleware(...)`` 逃出来的异常列表。
        """

        errors: list[BaseException] = []

        async def handler(event_: object, data: dict[str, Any]) -> None:
            session = data["session"]
            if wrap_session is not None:
                session = wrap_session(session)
            await self._invoke_real_send(
                session, text, sent, admin=admin, transport_fault=transport_fault
            )

        middleware = DbSessionMiddleware(self.session_factory)
        with contextlib.ExitStack() as stack:
            for name, factory in class_side_effects.items():
                stack.enter_context(patch.object(AsyncSession, name, factory))
            try:
                await middleware(handler, object(), {})
            except BaseException as exc:  # noqa: BLE001 - 探针要记录逃出来的异常
                errors.append(exc)
        return errors

    # ---- 中间件故障包装 ----------------------------------------------------

    @staticmethod
    def _fault_wrappers(
        *,
        fail_commit: bool = False,
        fail_rollback: bool = False,
        fail_close_first: bool = False,
    ):
        """故障只在**第一次** commit / 每次 rollback / 第一次 close 上生效。

        close 刻意只失败第一次：那一次是服务自己 ``_discard_pending`` 的尝试；此后
        ``DbSessionMiddleware`` 的 ``async with`` 必须能真正关闭会话，否则
        ``__aexit__`` 的异常会替换掉被观察的异常，而且连接永远不会归还。
        """

        real_commit = AsyncSession.commit
        real_rollback = AsyncSession.rollback
        real_close = AsyncSession.close
        calls = {"commit": 0, "close": 0, "rollback": 0}

        async def commit(session: Any) -> None:
            calls["commit"] += 1
            if fail_commit and calls["commit"] == 1:
                raise RuntimeError("synthetic first commit")
            await real_commit(session)

        async def rollback(session: Any) -> None:
            calls["rollback"] += 1
            if fail_rollback:
                raise RuntimeError("synthetic rollback")
            await real_rollback(session)

        async def close(session: Any) -> None:
            calls["close"] += 1
            if fail_close_first and calls["close"] == 1:
                raise RuntimeError("synthetic first close")
            await real_close(session)

        return calls, {"commit": commit, "rollback": rollback, "close": close}

    # ---- 共用断言 ----------------------------------------------------------

    def _assert_unconfirmed_receipt(self, sent: list[dict[str, Any]]) -> None:
        self.assertEqual(len(sent), 1, f"应当只发一条回执，实际 {sent!r}")
        body = sent[0]["body"]
        self.assertIn(UNCONFIRMED_HEAD, body)
        self.assertIn(UNCONFIRMED_BODY, body)
        for phrase in RETIRED_TEXTS:
            self.assertNotIn(phrase, body, f"失败回执不得包含旧文案 {phrase!r}")
        for phrase in FORBIDDEN_IN_FAILURE:
            self.assertNotIn(phrase, body, f"失败回执不得包含 {phrase!r}")


# ---------------------------------------------------------------------------
# 一、红检资格段：只用既有产品符号
# ---------------------------------------------------------------------------


class MemoryForgetReceiptTests(_ForgetTestCase):
    """业务红检：真实 cmd_memory + 真实 middleware + 真实发送助手。"""

    async def test_forget_success_marks_deleted_and_refreshes_confirmed_at(self) -> None:
        fact = await self._seed_fact(fingerprint="success")

        result = await self._run_real_send("/memory forget 1")

        sent = result["sent"]
        self.assertEqual(len(sent), 1, f"应当只发一条回执，实际 {sent!r}")
        self.assertIn(FORGOTTEN_TEXT, sent[0]["body"])
        for phrase in RETIRED_TEXTS:
            self.assertNotIn(phrase, sent[0]["body"])
        status, confirmed_at = await self._fresh_fact(fact)
        self.assertEqual(status, "deleted", "成功删除必须是软删（status='deleted'）")
        self.assertIsNotNone(confirmed_at, "行必须仍在（软删，非物理删）")
        self.assertNotEqual(
            confirmed_at, CONFIRMED_AT, "单条删除的成功更新会刷新 last_confirmed_at"
        )

    async def test_forget_legacy_numbering_control_deletes_two_facts_in_a_row(self) -> None:
        """保留对照：编号是**实时列表位置**，不是稳定事实 ID。"""

        first = await self._seed_fact(fingerprint="pos-a")
        second = await self._seed_fact(fingerprint="pos-b")

        one = await self._run_real_send("/memory forget 1")
        two = await self._run_real_send("/memory forget 1")
        three = await self._run_real_send("/memory forget 1")

        self.assertIn(FORGOTTEN_TEXT, one["sent"][0]["body"])
        self.assertIn(FORGOTTEN_TEXT, two["sent"][0]["body"])
        self.assertEqual(await self._fresh_status(first), "deleted")
        self.assertEqual(await self._fresh_status(second), "deleted")
        self.assertIn(MISSING_OR_DENIED_TEXT, three["sent"][0]["body"])

    async def test_forget_missing_number_keeps_legacy_copy_and_sends_no_delete(self) -> None:
        fact = await self._seed_fact(fingerprint="missing")

        result = await self._run_real_send("/memory forget 5", record=True)

        sent = result["sent"]
        self.assertEqual(len(sent), 1, f"应当只发一条回执，实际 {sent!r}")
        self.assertIn(MISSING_OR_DENIED_TEXT, sent[0]["body"])
        for phrase in RETIRED_TEXTS:
            self.assertNotIn(phrase, sent[0]["body"])
        self.assertEqual(
            [s for s in result["statements"] if "UPDATE user_facts" in s],
            [],
            "缺 target 时不得发出删除 UPDATE（会 SELECT，但不进入删除服务）",
        )
        self.assertEqual(await self._fresh_status(fact), "active")

    async def test_forget_without_permission_uses_the_same_copy_and_no_delete(self) -> None:
        foreign = await self._seed_fact(
            fingerprint="denied", subject_user_id=OTHER_MEMBER_ID
        )

        result = await self._run_real_send("/memory forget 1", record=True, admin=False)

        sent = result["sent"]
        self.assertIn(MISSING_OR_DENIED_TEXT, sent[0]["body"])
        self.assertEqual(
            [s for s in result["statements"] if "UPDATE user_facts" in s],
            [],
            "无权时不得发出删除 UPDATE",
        )
        self.assertEqual(await self._fresh_status(foreign), "active")

    async def test_admin_forgets_another_members_fact_in_this_group(self) -> None:
        foreign = await self._seed_fact(
            fingerprint="admin-target", subject_user_id=OTHER_MEMBER_ID
        )

        result = await self._run_real_send("/memory forget 1", admin=True)

        self.assertIn(FORGOTTEN_TEXT, result["sent"][0]["body"])
        self.assertEqual(await self._fresh_status(foreign), "deleted")

    async def test_forget_only_touches_the_selected_fact_across_scopes(self) -> None:
        here = await self._seed_fact(fingerprint="scope-here")
        elsewhere = await self._seed_fact(
            fingerprint="scope-other-group", scope_id=OTHER_GROUP_ID
        )
        private = await self._seed_fact(
            fingerprint="scope-private", scope="private", scope_id=MEMBER_ID
        )
        foreign = await self._seed_fact(
            fingerprint="scope-foreign", subject_user_id=OTHER_MEMBER_ID
        )

        result = await self._run_real_send("/memory forget 1")

        self.assertIn(FORGOTTEN_TEXT, result["sent"][0]["body"])
        statuses = {
            "here": await self._fresh_status(here),
            "elsewhere": await self._fresh_status(elsewhere),
            "private": await self._fresh_status(private),
            "foreign": await self._fresh_status(foreign),
        }
        self.assertEqual(
            sorted(statuses.values()).count("deleted"), 1, f"必须只删一条：{statuses!r}"
        )
        self.assertEqual(statuses["foreign"], "active", "不得碰他人的事实")

    async def test_forget_concurrent_soft_delete_is_reported_as_unchanged(self) -> None:
        fact = await self._seed_fact(fingerprint="race-soft")

        result = await self._run_real_send(
            "/memory forget 1", race=(fact, "soft")
        )

        sent = result["sent"]
        self.assertEqual(len(sent), 1, f"应当只发一条回执，实际 {sent!r}")
        self.assertIn(UNCHANGED_TEXT, sent[0]["body"])
        for phrase in RETIRED_TEXTS:
            self.assertNotIn(phrase, sent[0]["body"])
        self.assertEqual(await self._fresh_status(fact), "deleted")

    async def test_forget_concurrent_purge_is_reported_as_unchanged(self) -> None:
        fact = await self._seed_fact(fingerprint="race-purge")

        result = await self._run_real_send(
            "/memory forget 1", race=(fact, "purge")
        )

        sent = result["sent"]
        self.assertEqual(len(sent), 1, f"应当只发一条回执，实际 {sent!r}")
        self.assertIn(UNCHANGED_TEXT, sent[0]["body"])
        for phrase in RETIRED_TEXTS:
            self.assertNotIn(phrase, sent[0]["body"])
        self.assertEqual(await self._fresh_status(fact), "<gone>")

    async def test_forget_execute_fault_before_dml_reports_unconfirmed(self) -> None:
        fact = await self._seed_fact(fingerprint="execute-before")

        result = await self._run_real_send("/memory forget 1", execute_mode="fail_before")

        self.assertIsNone(result["error"], "普通失败不得上抛")
        self._assert_unconfirmed_receipt(result["sent"])
        self.assertEqual(await self._fresh_status(fact), "active")

    async def test_forget_execute_fault_after_dml_reports_unconfirmed(self) -> None:
        fact = await self._seed_fact(fingerprint="execute-after")

        result = await self._run_real_send("/memory forget 1", execute_mode="fail_after")

        self.assertIsNone(result["error"], "普通失败不得上抛")
        self._assert_unconfirmed_receipt(result["sent"])
        self.assertEqual(await self._fresh_status(fact), "active")

    async def test_forget_commit_fault_before_persist_reports_unconfirmed(self) -> None:
        fact = await self._seed_fact(fingerprint="commit-before")

        result = await self._run_real_send("/memory forget 1", commit_mode="fail_before")

        self.assertIsNone(result["error"], "普通失败不得上抛")
        self._assert_unconfirmed_receipt(result["sent"])
        self.assertEqual(await self._fresh_status(fact), "active")

    async def test_forget_commit_fault_after_persist_may_have_persisted(self) -> None:
        """commit 已真实返回后再抛错：回执必须仍是 unconfirmed，且**不得**声称没生效。"""

        fact = await self._seed_fact(fingerprint="commit-after")

        result = await self._run_real_send("/memory forget 1", commit_mode="fail_after")

        self.assertIsNone(result["error"], "普通失败不得上抛")
        self._assert_unconfirmed_receipt(result["sent"])
        self.assertEqual(
            await self._fresh_status(fact), "deleted", "已持久化的变化不得被回执否认"
        )

    async def test_forget_cancel_before_execute_sends_nothing_and_cleans_up(self) -> None:
        fact = await self._seed_fact(fingerprint="cancel-before-execute")

        result = await self._run_real_send(
            "/memory forget 1", execute_mode="cancel_before"
        )

        self.assertIsInstance(result["error"], asyncio.CancelledError)
        self.assertEqual(result["sent"], [], "取消不得产生任何回执")
        self.assertFalse(result["in_transaction"], "teardown 之前必须已清理事务")
        self.assertEqual(await self._fresh_status(fact), "active")

    async def test_forget_cancel_before_commit_sends_nothing_and_cleans_up(self) -> None:
        fact = await self._seed_fact(fingerprint="cancel-before-commit")

        result = await self._run_real_send("/memory forget 1", commit_mode="cancel_before")

        self.assertIsInstance(result["error"], asyncio.CancelledError)
        self.assertEqual(result["sent"], [], "取消不得产生任何回执")
        self.assertFalse(result["in_transaction"], "teardown 之前必须已清理事务")
        self.assertEqual(await self._fresh_status(fact), "active")

    async def test_forget_cancel_after_persist_sends_nothing_and_keeps_changes(self) -> None:
        fact = await self._seed_fact(fingerprint="cancel-after-commit")

        result = await self._run_real_send("/memory forget 1", commit_mode="cancel_after")

        self.assertIsInstance(result["error"], asyncio.CancelledError)
        self.assertEqual(result["sent"], [], "取消不得产生任何回执")
        self.assertFalse(result["in_transaction"], "teardown 之前必须已清理事务")
        self.assertEqual(await self._fresh_status(fact), "deleted")

    async def test_forget_cleanup_cancellation_does_not_become_a_plain_failure(self) -> None:
        fact = await self._seed_fact(fingerprint="cleanup-cancel")

        result = await self._run_real_send(
            "/memory forget 1", execute_mode="fail_before", rollback_mode="cancel"
        )

        self.assertIsInstance(
            result["error"], asyncio.CancelledError, "清理期间的取消必须原样传播"
        )
        self.assertEqual(result["sent"], [], "取消不得产生任何回执")
        self.assertEqual(await self._fresh_status(fact), "active")

    async def test_forget_receipt_uses_the_fixed_autodelete_window(self) -> None:
        await self._seed_fact(fingerprint="autodelete")

        result = await self._run_real_send("/memory forget 1")

        self.assertEqual(len(result["sent"]), 1)
        self.assertTrue(self.cleanup_mock.await_count >= 1, "回执必须走 durable 自动删除调度")
        seconds = [
            call.args[1] for call in self.cleanup_mock.await_args_list if len(call.args) > 1
        ]
        self.assertEqual(seconds, [20], f"回执保留 20 秒，实际 {seconds!r}")

    # ---- 真实中间件下的清理故障 -------------------------------------------

    async def test_forget_triple_fault_blocks_the_compensation_commit(self) -> None:
        """commit 未真实执行 + rollback 失败 + close 失败 ⇒ 必须阻断补偿 commit。"""

        fact = await self._seed_fact(fingerprint="triple-fault")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(
            fail_commit=True, fail_rollback=True, fail_close_first=True
        )

        errors = await self._run_through_middleware(
            "/memory forget 1", class_side_effects=wrappers, sent=sent
        )

        self.assertEqual(len(errors), 1, f"应当上抛一次清理阻断异常，实际 {errors!r}")
        self.assertIsInstance(errors[0], MemorySessionCleanupUnsafeError)
        self.assertEqual(calls["commit"], 1, "第一次 commit 之后不得再发生补偿 commit")
        self._assert_unconfirmed_receipt(sent)
        self.assertEqual(await self._fresh_status(fact), "active", "残留不得被补写")
        self.assertEqual(await self._fresh_count(status="deleted"), 0)

    async def test_forget_rollback_fault_with_working_close_is_contained(self) -> None:
        fact = await self._seed_fact(fingerprint="rollback-only-fault")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(
            fail_commit=True, fail_rollback=True
        )

        errors = await self._run_through_middleware(
            "/memory forget 1", class_side_effects=wrappers, sent=sent
        )

        self.assertEqual(errors, [], "close 已证明丢弃事务时不应上抛")
        self._assert_unconfirmed_receipt(sent)
        self.assertEqual(calls["commit"], 2, "补偿 commit 仍会走一次（应是空操作）")
        self.assertEqual(await self._fresh_status(fact), "active")
        self.assertEqual(await self._fresh_count(status="deleted"), 0)

    async def test_forget_close_fault_once_then_real_close_leaves_no_residual(self) -> None:
        fact = await self._seed_fact(fingerprint="close-once-fault")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(
            fail_commit=True, fail_rollback=True, fail_close_first=True
        )

        errors = await self._run_through_middleware(
            "/memory forget 1", class_side_effects=wrappers, sent=sent
        )

        self.assertEqual(len(errors), 1, f"close 首次失败即无法证明已丢弃，实际 {errors!r}")
        self.assertIsInstance(errors[0], MemorySessionCleanupUnsafeError)
        self.assertEqual(calls["commit"], 1)
        self.assertGreaterEqual(calls["close"], 2, "真实 context 最终仍关闭了会话")
        self._assert_unconfirmed_receipt(sent)
        self.assertEqual(await self._fresh_status(fact), "active")
        self.assertEqual(await self._fresh_count(status="deleted"), 0)

    async def test_forget_session_without_close_is_treated_as_cleanup_unsafe(self) -> None:
        fact = await self._seed_fact(fingerprint="no-close")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(
            fail_commit=True, fail_rollback=True
        )

        errors = await self._run_through_middleware(
            "/memory forget 1",
            class_side_effects=wrappers,
            sent=sent,
            wrap_session=_NoCloseSession,
        )

        self.assertEqual(len(errors), 1, f"缺少 close 时无法证明已丢弃，实际 {errors!r}")
        self.assertIsInstance(errors[0], MemorySessionCleanupUnsafeError)
        self.assertEqual(calls["commit"], 1)
        self._assert_unconfirmed_receipt(sent)
        self.assertEqual(await self._fresh_status(fact), "active")
        self.assertEqual(await self._fresh_count(status="deleted"), 0)

    async def test_forget_receipt_send_fault_never_returns_normally(self) -> None:
        fact = await self._seed_fact(fingerprint="send-fault")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(
            fail_commit=True, fail_rollback=True, fail_close_first=True
        )

        errors = await self._run_through_middleware(
            "/memory forget 1",
            class_side_effects=wrappers,
            sent=sent,
            transport_fault=RuntimeError("synthetic transport"),
        )

        self.assertEqual(len(errors), 1, f"发送自己的故障也必须冒出去，实际 {errors!r}")
        self.assertIs(type(errors[0]), RuntimeError, "发送自身的故障应原样冒出")
        self.assertEqual(calls["commit"], 1, "发送失败时更不得发生补偿 commit")
        self.assertEqual(sent, [], "transport 抛错时不产生已发送记录")
        self.assertEqual(await self._fresh_status(fact), "active")

    async def test_forget_rollback_success_contains_the_failure(self) -> None:
        """commit 未真实执行 + rollback 成功 ⇒ 清理安全，照常发未确认回执、不上抛。"""

        fact = await self._seed_fact(fingerprint="rollback-success")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(fail_commit=True)

        errors = await self._run_through_middleware(
            "/memory forget 1", class_side_effects=wrappers, sent=sent
        )

        self.assertEqual(errors, [], "回滚成功即已证明事务被丢弃，不应上抛")
        self._assert_unconfirmed_receipt(sent)
        self.assertEqual(calls["commit"], 2, "补偿 commit 仍走一次（空操作）")
        self.assertEqual(await self._fresh_status(fact), "active")
        self.assertEqual(await self._fresh_count(status="deleted"), 0)

    async def test_forget_middleware_success_control_persists_normally(self) -> None:
        fact = await self._seed_fact(fingerprint="middleware-success")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers()

        errors = await self._run_through_middleware(
            "/memory forget 1", class_side_effects=wrappers, sent=sent
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(sent), 1)
        self.assertIn(FORGOTTEN_TEXT, sent[0]["body"])
        self.assertNotIn(UNCONFIRMED_HEAD, sent[0]["body"])
        self.assertEqual(calls["commit"], 2)
        self.assertEqual(await self._fresh_status(fact), "deleted")

    async def test_forget_usage_copy_is_unchanged(self) -> None:
        await self._seed_fact(fingerprint="usage")

        result = await self._run_real_send("/memory forget 0", record=True)

        body = result["sent"][0]["body"]
        self.assertIn(USAGE_HEAD, body)
        self.assertIn(USAGE_TAIL, body)
        self.assertEqual(
            [s for s in result["statements"] if "UPDATE user_facts" in s], []
        )


# ---------------------------------------------------------------------------
# 二、协议段：直接验证服务层新接口（不参与旧红检）
# ---------------------------------------------------------------------------


class MemoryForgetOutcomeProtocolTests(_ForgetTestCase):
    """``soft_delete_fact_outcome`` / legacy ``soft_delete_fact`` 的服务层契约。"""

    def setUp(self) -> None:
        if not _HAS_FORGET_OUTCOME:
            self.fail("B5 判别结果接口尚未实现（本段按设计不参与旧基线红检）")

    async def _call_outcome(self, fact_id: int) -> Any:
        async with self.session_factory() as session:
            return await soft_delete_fact_outcome(session, fact_id)

    async def test_same_fact_second_call_is_committed_but_unchanged(self) -> None:
        fact = await self._seed_fact(fingerprint="protocol-noop")

        first = await self._call_outcome(fact)
        status_after_first, confirmed_after_first = await self._fresh_fact(fact)
        second = await self._call_outcome(fact)
        status_after_second, confirmed_after_second = await self._fresh_fact(fact)

        self.assertTrue(first.committed)
        self.assertEqual(first.facts_soft_deleted, 1)
        self.assertFalse(first.switch_changed)
        self.assertIsInstance(second, MemoryWriteOutcome, "必须复用 B4 的判别结果类型")
        self.assertTrue(second.committed, "no-op 也是一次**成功**的事务")
        self.assertEqual(second.facts_soft_deleted, 0)
        self.assertEqual(status_after_first, "deleted")
        self.assertEqual(status_after_second, "deleted")
        self.assertEqual(
            confirmed_after_first,
            confirmed_after_second,
            "重复 no-op 不得刷新 last_confirmed_at",
        )

    async def test_missing_fact_id_is_committed_but_unchanged(self) -> None:
        outcome = await self._call_outcome(999999999)

        self.assertTrue(outcome.committed, "行不存在同样是已确认的零变更")
        self.assertEqual(outcome.facts_soft_deleted, 0)
        self.assertEqual(outcome.detail, "")

    async def test_invalid_fact_id_is_uncommitted_with_fixed_detail(self) -> None:
        async with self.session_factory() as session:
            outcome = await soft_delete_fact_outcome(session, "not-a-number")

        self.assertFalse(outcome.committed)
        self.assertEqual(outcome.detail, "invalid_fact_id")

    async def test_legacy_bool_wrapper_matches_the_new_outcome(self) -> None:
        fact = await self._seed_fact(fingerprint="protocol-legacy")

        async with self.session_factory() as session:
            first = await _ltm_soft_delete(session, fact)
        async with self.session_factory() as session:
            second = await _ltm_soft_delete(session, fact)
        async with self.session_factory() as session:
            missing = await _ltm_soft_delete(session, 999999999)

        self.assertIs(first, True)
        self.assertIs(second, False, "已确认的零变更必须返回 False")
        self.assertIs(missing, False)

    async def test_superseded_fact_is_deleted_by_the_same_where_clause(self) -> None:
        fact = await self._seed_fact(fingerprint="protocol-superseded", status="superseded")

        outcome = await self._call_outcome(fact)

        self.assertTrue(outcome.committed)
        self.assertEqual(outcome.facts_soft_deleted, 1)
        self.assertEqual(await self._fresh_status(fact), "deleted")

    async def test_success_refreshes_confirmed_at_only_when_a_row_changed(self) -> None:
        fact = await self._seed_fact(fingerprint="protocol-clock")

        await self._call_outcome(fact)
        _, after_change = await self._fresh_fact(fact)
        await self._call_outcome(fact)
        _, after_noop = await self._fresh_fact(fact)

        self.assertNotEqual(after_change, CONFIRMED_AT, "成功删除会刷新确认时间")
        self.assertEqual(after_noop, after_change, "no-op 不刷新确认时间")


async def _ltm_soft_delete(session: AsyncSession, fact_id: Any) -> bool:
    """调用 legacy ``soft_delete_fact``（导入放函数内，保持基线可 collect）。"""

    from bot.services import long_term_memory as ltm

    return await ltm.soft_delete_fact(session, fact_id)
