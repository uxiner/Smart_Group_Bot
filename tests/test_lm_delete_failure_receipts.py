"""B6：``/lm`` 单条群永久备忘删除按钮的可靠回执。

背景（父代理独立复现，基线产品 ``d9cdda2332e71e691a495f3f88891f6f1fed2147``）：
``callback_data = f"lmd:{memory_id}:{page}"`` 指向的删除按钮在若干可达的合成边界故障下
**完全不发回执**——用户点了按钮，什么也看不到：

1. ``commit`` 在**持久化之前**抛错 → 行仍在，异常上抛，``callback.answer`` 次数 0；
2. ``commit`` 已在**内层持久化之后**抛错 → 行已删，异常上抛，``callback.answer`` 次数 0；
3. 删除已确认、但列表刷新（空/非空两个 ``edit_text`` 分支）失败 → 异常上抛，0 回执。

另有两条口径缺陷：旧的 ``delete_permanent_memory`` 把**预读选中**的 ORM 行列表当作删除
证据，因此在「服务预读一行 → 另一个独立 session 删掉它并提交 → 本服务的 DELETE 命中 0 行
但 commit 正常返回」时，仍然会回「已删除记忆 #N」。

本文件按**红检资格**分两段：

* ``LmDeleteReceiptTests`` —— 只用**基线既有**产品符号（真实 ``on_memory_delete`` +
  真实 ``MemoryService`` + 真实临时 SQLite，只替换授权判定与最终外部 ``callback.answer`` /
  ``Message.edit_text``）。在冻结基线 ``d9cdda2`` 上这些用例必须是**业务语义
  AssertionError**，不能是导入失败或夹具错误。红检只跑这一段。
* ``PermanentMemoryDeletionOutcomeTests`` —— 直接验证服务层新接口
  （``delete_permanent_memory_outcome``）。基线不存在该符号，故**不参与旧红检**；
  这里用守卫式导入标注，运行时若缺符号会明确失败（不是 skip）。

替换边界：只替换**最终外部 transport**（``callback.answer`` / ``Message.edit_text``）、
**授权判定**与**精确的 DB 边界**（服务自己创建的会话上的 ``execute`` / ``commit`` /
``close`` 与 SQLite 写锁获取）。故障注入只作用于 ``MemoryService`` 自己通过工厂拿到的会话，
不污染夹具用于播种与 fresh 读取的会话。绝不发真实 Telegram 请求、绝不调用真实 LLM。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import BotConfig, Settings
from bot.db.engine import init_db
from bot.db.models import Group, GroupPermanentMemory
from bot.handlers import commands
from bot.services.memory import MemoryService

try:  # B6 新增符号：基线不存在 → 只有协议段受影响，业务段仍能 collect。
    from bot.services.memory import PermanentMemoryDeletionOutcome

    _delete_outcome_binding = getattr(
        MemoryService, "delete_permanent_memory_outcome", None
    )
    _HAS_DELETE_OUTCOME = _delete_outcome_binding is not None
except ImportError:  # pragma: no cover - 冻结基线
    PermanentMemoryDeletionOutcome = None  # type: ignore[assignment]
    _delete_outcome_binding = None  # type: ignore[assignment]
    _HAS_DELETE_OUTCOME = False

try:  # B6 新增私有助手：供 rowcount 严格类型的直接单元断言使用。
    from bot.services.memory import _pm_single_row_change
except ImportError:  # pragma: no cover - 冻结基线
    _pm_single_row_change = None  # type: ignore[assignment]


SUPERGROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009999999999
ADMIN_ID = 779
MEMBER_ID = 777

# 父代理任务书逐字给定的三条确定文案（B6 §4）。
UNCONFIRMED_TEXT = "未能确认这条群永久备忘是否已删除。请重新 /lm 查看。"
NO_CHANGE_TEXT = "记忆不存在或已删除"
DELETED_TEXT = "已删除记忆 #{memory_id}"
REFRESH_FAILED_TEXT = "记忆 #{memory_id} 已删除，但列表刷新失败，请重新 /lm 查看。"

# 既有文案，必须逐字保持。
PARAMS_ERROR_TEXT = "参数错误"
SESSION_NOT_READY_TEXT = "会话未就绪，请重新 /lm"
MESSAGE_GONE_TEXT = "消息已失效"
DENIED_TEXT = "仅群管理员可操作该列表"
UNAUTHORIZED_TEXT = "当前群组未授权"

# 被 B6 取代的旧刷新失败文案：任何路径都不得再出现（它把「已删」说成「请重试」）。
RETIRED_REFRESH_TEXT = "删除成功，但列表刷新失败，请重试 /lm"

# 不得出现在任何回执里的东西：异常文本、SQL、内部原因码、群标识、更强承诺。
FORBIDDEN_IN_RECEIPTS = (
    "synthetic",
    "RuntimeError",
    "DeletedInstance",
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "commit",
    "rowcount",
    "execute_failed",
    "commit_failed",
    "session_close_failed",
    "-1001234567890",
    "-1009999999999",
    "彻底删除",
    "无残留",
    "永不再记",
    "已持久生效",
    "数据库",
    "事务",
)


class _StubLLM:
    class main:
        model = "gemini/gemini-2.0-flash"

    async def compress(self, system: str, user_text: str) -> str:
        _ = system, user_text
        return ""


def _settings() -> Settings:
    return Settings(_env_file=None)


def _statement_kind(statement: object) -> str:
    """只为故障注入分类语句；不参与任何业务断言。"""

    text = str(statement).lower()
    if "group_permanent_memories" not in text:
        return ""
    head = text.lstrip()
    for kind in ("delete", "select", "insert", "update"):
        if head.startswith(kind):
            return kind
    return ""


class _Callback:
    """记录 ``callback.answer`` 的替身——**只**替换最终外部传输。"""

    def __init__(
        self,
        *,
        data: str,
        chat_id: int = SUPERGROUP_ID,
        user_id: int = ADMIN_ID,
        edit_fault: BaseException | None = None,
        answer_fault: BaseException | None = None,
    ) -> None:
        self.data = data
        self.from_user = _user(user_id)
        self.answers: list[dict[str, Any]] = []
        self.answer_attempts = 0
        self.edits: list[dict[str, Any]] = []
        self.message = _callback_message(
            chat_id=chat_id, edits=self.edits, edit_fault=edit_fault
        )
        self._answer_fault = answer_fault

    @property
    def texts(self) -> list[str]:
        return [str(item["text"]) for item in self.answers]

    async def answer(self, text: Any = None, **kwargs: Any) -> None:
        self.answer_attempts += 1
        if self._answer_fault is not None:
            raise self._answer_fault
        self.answers.append({"text": text, "kwargs": dict(kwargs)})


def _user(user_id: int) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=user_id, first_name="成员", last_name="", username="")


def _callback_message(
    *,
    chat_id: int,
    edits: list[dict[str, Any]],
    edit_fault: BaseException | None = None,
) -> Any:
    from types import SimpleNamespace

    async def edit_text(text: Any, **kwargs: Any) -> Any:
        if edit_fault is not None:
            raise edit_fault
        edits.append({"text": str(text), "kwargs": dict(kwargs)})
        return SimpleNamespace(message_id=2, chat=SimpleNamespace(id=chat_id))

    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup", title="合成当前群", username=""),
        message_id=1,
        reply_markup=None,
        edit_text=edit_text,
    )


class _FaultySessionFactory:
    """把故障注入限制在**服务自己**创建的会话上。

    支持两类注入：

    * ``execute_delete`` / ``execute_select`` / ``commit`` / ``close``：按模式在真实边界上抛错
      （``ok`` / ``fail_before`` / ``fail_after`` / ``cancel_before`` / ``cancel_after`` /
      ``fail_first``）。
    * ``race``：在**SQLite 写锁获取之前**（因此不在任何写事务里）用一条独立的 stdlib
      ``sqlite3`` 连接先把目标行删掉并提交——复现父代理「独立 writer 先删」的探针。
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        db_path: str,
        execute_delete: str = "ok",
        execute_select: str = "ok",
        commit: str = "ok",
        close: str | tuple[str, ...] = "ok",
        rollback: str | tuple[str, ...] = "ok",
        race_memory_id: int | None = None,
        barrier: Any = None,
    ) -> None:
        self._factory = factory
        self._db_path = db_path
        self._modes = {
            "execute_delete": execute_delete,
            "execute_select": execute_select,
            "commit": commit,
        }
        # ``close`` / ``rollback`` 都可以给一个按「第几次调用」生效的序列，最后一项一直重复。
        # 这样既能表达「第一次失败、第二次成功」的可恢复边界，也能表达「所有真实恢复通路永久失败」。
        plan = tuple(close) if isinstance(close, (tuple, list)) else (close,)
        # 兼容旧拼写：``fail_first`` / ``cancel_first`` = 第一次「已真 close 再报错」，之后恢复。
        _legacy_close = {
            "fail_first": ("fail_after", "ok"),
            "cancel_first": ("cancel_after", "ok"),
        }
        if len(plan) == 1 and plan[0] in _legacy_close:
            plan = _legacy_close[plan[0]]
        self._close_plan = plan
        self._rollback_plan = (
            tuple(rollback) if isinstance(rollback, (tuple, list)) else (rollback,)
        )
        # 真实 ``asyncio.Task.cancel()`` 用例的**事件边界**：``block_until_released`` 模式先
        # 通知 ``close_entered``，再真正挂在 ``close_gate`` 上等待（不用 sleep 同步）。
        self.close_gate = asyncio.Event()
        self.close_entered = asyncio.Event()
        # 记录 harness 主动抛出的每一个异常实例（带标签），让用例能断言「传播的就是**同一个**
        # 取消对象」，而不是只看类型。
        self.raised: list[tuple[str, BaseException]] = []
        self._race_memory_id = race_memory_id
        # 真并发用例的 barrier：让两个真实调用都**停在写锁获取之前**再一起放行。
        # 父代理要求「不在获得写锁之后等另一个 writer」，否则根本没制造出写锁争用。
        self._barrier = barrier
        self.sessions_created = 0
        # 供用例在 fixture teardown **之前**直接观测真实 session、并存取真实 close 以强制归还资源。
        self.sessions: list[AsyncSession] = []
        self.recovery_closes: list[Any] = []
        self.calls = {
            "delete_execute": 0,
            "select_execute": 0,
            "commit": 0,
            "close": 0,
            "rollback": 0,
            "write_lock": 0,
        }

    # 兼容 ``async_sessionmaker`` 的调用形状。
    def __call__(self, *args: Any, **kwargs: Any) -> AsyncSession:
        session = self._factory(*args, **kwargs)
        self.sessions_created += 1
        self.sessions.append(session)
        self._install(session)
        return session

    def _before_write_lock(self) -> None:
        target = self._race_memory_id
        self._race_memory_id = None
        if target is None:
            return
        connection = sqlite3.connect(self._db_path)
        try:
            connection.execute("DELETE FROM group_permanent_memories WHERE id=?", (target,))
            connection.commit()
        finally:
            connection.close()

    def _install(self, session: AsyncSession) -> None:
        factory = self
        real_execute = session.execute
        real_commit = session.commit
        real_close = session.close
        real_rollback = session.rollback
        real_acquire = getattr(session, "_acquire_write_lock", None)
        factory.recovery_closes.append(real_close)
        gate = {"used": False}

        def _fail(label: str, exc: BaseException) -> Any:
            """抛出并记录同一个异常实例——用例可据此断言「传播的就是这一个对象」。"""

            factory.raised.append((label, exc))
            raise exc

        async def execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            kind = _statement_kind(statement)
            if kind == "delete" and not factory.calls["delete_execute"]:
                factory.calls["delete_execute"] += 1
                mode = factory._modes["execute_delete"]
                if mode == "cancel_before":
                    _fail("delete_cancel_before", asyncio.CancelledError("synthetic delete cancelled"))
                if mode == "fail_before":
                    _fail("delete_fail_before", RuntimeError("synthetic delete statement before dml"))
                result = await real_execute(statement, *args, **kwargs)
                if mode == "cancel_after":
                    _fail("delete_cancel_after", asyncio.CancelledError("synthetic delete cancelled"))
                if mode == "fail_after":
                    _fail("delete_fail_after", RuntimeError("synthetic delete statement after dml"))
                return result
            if kind == "select" and not factory.calls["select_execute"]:
                factory.calls["select_execute"] += 1
                mode = factory._modes["execute_select"]
                if mode == "cancel_before":
                    _fail("select_cancel_before", asyncio.CancelledError("synthetic select cancelled"))
                if mode == "fail_before":
                    _fail("select_fail_before", RuntimeError("synthetic select statement failed"))
                result = await real_execute(statement, *args, **kwargs)
                if mode == "cancel_after":
                    _fail("select_cancel_after", asyncio.CancelledError("synthetic select cancelled"))
                return result
            return await real_execute(statement, *args, **kwargs)

        async def commit() -> None:
            factory.calls["commit"] += 1
            if factory.calls["commit"] == 1:
                mode = factory._modes["commit"]
                if mode == "cancel_before":
                    _fail("commit_cancel_before", asyncio.CancelledError("synthetic commit cancelled"))
                if mode == "fail_before":
                    _fail("commit_fail_before", RuntimeError("synthetic commit before persist"))
                await real_commit()
                if mode == "cancel_after":
                    _fail("commit_cancel_after", asyncio.CancelledError("synthetic commit cancelled"))
                if mode == "fail_after":
                    _fail("commit_fail_after", RuntimeError("synthetic commit after persist"))
                return
            await real_commit()

        async def close() -> None:
            factory.calls["close"] += 1
            index = min(factory.calls["close"], len(factory._close_plan)) - 1
            mode = factory._close_plan[index]
            # ``fail_before`` / ``cancel_before``：**真正归还连接之前**失败——这才是父代理
            # B1 观测到的边界（事务与 SQLite 全局写锁都还留在 session 上）。
            if mode == "fail_before":
                _fail("close_fail_before", RuntimeError("synthetic close before real close"))
            if mode == "cancel_before":
                _fail("close_cancel_before", asyncio.CancelledError("synthetic close cancelled"))
            if mode == "block_until_released":
                # 真实 ``Task.cancel()`` 的事件边界：先通知用例「已经挂在 cleanup 的 await 上」，
                # 再真挂住。用例此时取消任务，取消由真实事件循环派发（不是手工 raise，也不用 sleep）。
                factory.close_entered.set()
                await factory.close_gate.wait()
            await real_close()
            if mode == "fail_after":
                _fail("close_fail_after", RuntimeError("synthetic close"))
            if mode == "cancel_after":
                _fail("close_cancel_after", asyncio.CancelledError("synthetic close cancelled"))

        async def rollback() -> None:
            factory.calls["rollback"] += 1
            index = min(factory.calls["rollback"], len(factory._rollback_plan)) - 1
            mode = factory._rollback_plan[index]
            if mode == "fail_before":
                _fail("rollback_fail_before", RuntimeError("synthetic rollback"))
            if mode == "cancel_before":
                _fail("rollback_cancel_before", asyncio.CancelledError("synthetic rollback cancelled"))
            await real_rollback()
            if mode == "fail_after":
                _fail("rollback_fail_after", RuntimeError("synthetic rollback after real rollback"))
            if mode == "cancel_after":
                _fail("rollback_cancel_after", asyncio.CancelledError("synthetic rollback cancelled"))

        async def acquire_write_lock(*args: Any, **kwargs: Any) -> Any:
            factory.calls["write_lock"] += 1
            if factory._barrier is not None and not gate["used"]:
                # 每个真实 session 只在**第一次**写锁获取之前等 barrier。``commit`` 对已经
                # 持锁的 session 会再调一次 ``_acquire_write_lock``（框架里是重入早退），
                # 那不是一个新的争用点；若再等一次就只剩自己一个 party，会把自己挂死。
                gate["used"] = True
                # 仍在**真实写锁获取之前**：两个调用到齐后才一起冲锁。
                await factory._barrier.wait()
            factory._before_write_lock()
            return await real_acquire(*args, **kwargs)

        session.execute = execute  # type: ignore[method-assign]
        session.commit = commit  # type: ignore[method-assign]
        session.close = close  # type: ignore[method-assign]
        session.rollback = rollback  # type: ignore[method-assign]
        if real_acquire is not None:
            session._acquire_write_lock = acquire_write_lock  # type: ignore[method-assign]


class _LmDeleteTestCase(unittest.IsolatedAsyncioTestCase):
    """真实 handler + 真实 service + 真实 SQLite；只替换授权与最终外部传输。"""

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

    # ---- 播种与 fresh 读取 -------------------------------------------------

    async def _seed_memory(self, *, group_id: int = SUPERGROUP_ID, content: str) -> int:
        async with self.session_factory() as session:
            row = GroupPermanentMemory(group_id=group_id, content=content, created_by=ADMIN_ID)
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _fresh_ids(self, *, group_id: int = SUPERGROUP_ID) -> list[int]:
        """fresh session 读持久行——不读服务的 identity map。"""

        async with self.session_factory() as session:
            rows = await session.execute(
                select(GroupPermanentMemory.id)
                .where(GroupPermanentMemory.group_id == group_id)
                .order_by(GroupPermanentMemory.id.asc())
            )
            return [int(value) for value in rows.scalars().all()]

    async def _fresh_contents(self, *, group_id: int = SUPERGROUP_ID) -> list[str]:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(GroupPermanentMemory.content)
                .where(GroupPermanentMemory.group_id == group_id)
                .order_by(GroupPermanentMemory.id.asc())
            )
            return [str(value) for value in rows.scalars().all()]

    def _service(self, proxy: _FaultySessionFactory) -> MemoryService:
        return MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            _StubLLM(),
            session_factory=proxy,
        )

    # ---- 运行真实回调 ------------------------------------------------------

    async def _run(
        self,
        *,
        memory_id: int,
        data: str | None = None,
        chat_id: int = SUPERGROUP_ID,
        user_id: int = ADMIN_ID,
        execute_delete: str = "ok",
        execute_select: str = "ok",
        commit: str = "ok",
        close: str | tuple[str, ...] = "ok",
        rollback: str | tuple[str, ...] = "ok",
        race_memory_id: int | None = None,
        barrier: Any = None,
        edit_fault: BaseException | None = None,
        answer_fault: BaseException | None = None,
        authorized: bool | None = True,
        admin: bool = True,
        session_ready: bool = True,
        drop_message: bool = False,
    ) -> dict[str, Any]:
        proxy = _FaultySessionFactory(
            self.session_factory,
            db_path=self._db_path,
            execute_delete=execute_delete,
            execute_select=execute_select,
            commit=commit,
            close=close,
            rollback=rollback,
            race_memory_id=race_memory_id,
            barrier=barrier,
        )
        callback = _Callback(
            data=data if data is not None else f"lmd:{memory_id}:0",
            chat_id=chat_id,
            user_id=user_id,
            edit_fault=edit_fault,
            answer_fault=answer_fault,
        )
        if drop_message:
            callback.message = None

        error: BaseException | None = None
        handler_session: AsyncSession | None = None
        with patch.object(commands, "memory_holder", new=_Holder(self._service(proxy))), patch.object(
            commands, "is_super_admin_user_id", return_value=False
        ), patch.object(
            commands, "is_group_authorized", new=AsyncMock(return_value=authorized is not False)
        ), patch.object(
            commands,
            "is_group_admin_authorized",
            new=AsyncMock(return_value=admin),
        ):
            async with self.session_factory() as session:
                handler_session = session if session_ready else None
                try:
                    await commands.on_memory_delete(
                        callback, _settings(), handler_session
                    )
                except BaseException as exc:  # noqa: BLE001 - 观察取消与阻断异常
                    error = exc

        return {
            "callback": callback,
            "texts": callback.texts,
            "answer_attempts": callback.answer_attempts,
            "edits": callback.edits,
            "error": error,
            "calls": dict(proxy.calls),
            "sessions_created": proxy.sessions_created,
            "proxy": proxy,
        }

    # ---- 资源归还观测（必须在 fixture teardown 之前） -----------------------

    async def _assert_write_lock_available(self) -> None:
        """独立 session 的限时写入必须一次成功——证明 SQLite 全局写锁已归还。

        父代理的 B6-B1 probe 正是用「独立 writer 在 1 秒内写不进去」抓到丢锁的：
        写锁没还回去时第二个 writer 会一直等到 ``_acquire_write_lock`` 自己的 5 秒超时，
        因此这里用更短的 ``wait_for`` 先判失败（``asyncio.TimeoutError``）。
        """

        async def _write() -> None:
            async with self.session_factory() as session:
                session.add(
                    GroupPermanentMemory(
                        group_id=OTHER_GROUP_ID,
                        content="写锁探针",
                        created_by=ADMIN_ID,
                    )
                )
                await session.commit()

        await asyncio.wait_for(_write(), timeout=1.5)

    async def _assert_write_lock_still_held(self) -> None:
        with self.assertRaises(asyncio.TimeoutError):
            await self._assert_write_lock_available()

    # ---- 真实 DML 边界故障 -------------------------------------------------

    @contextlib.contextmanager
    def _dml_fault(self, *, after: bool) -> Any:
        """在 **真实 DML 边界**上让目标 DELETE 失败。

        这一层对两版代码都生效：旧实现走 ``session.delete(row)`` + commit 时的 ORM
        flush，新实现走原子的 Core ``DELETE``——两者最终都经同一条 cursor
        ``DELETE FROM group_permanent_memories ...``。父代理指出旧用例只 patch
        ``session.execute`` 并不覆盖旧 ORM flush，所以那两项旧红不是同故障对照。
        """

        state = {"count": 0}
        event_name = "after_cursor_execute" if after else "before_cursor_execute"

        def _hook(conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any) -> None:
            normalized = " ".join(str(statement).split()).lower()
            if not normalized.startswith("delete") or "group_permanent_memories" not in normalized:
                return
            state["count"] += 1
            if state["count"] > 1:
                return
            raise RuntimeError("synthetic dml failure")

        event.listen(self.engine.sync_engine, event_name, _hook)
        try:
            yield state
        finally:
            event.remove(self.engine.sync_engine, event_name, _hook)

    # ---- 断言辅助 ----------------------------------------------------------

    def _assert_receipt(
        self,
        result: dict[str, Any],
        *,
        expected: str,
        attempts: int = 1,
    ) -> None:
        """恰好一次回执尝试、恰好一条命中期望文本、且无「被取代的旧文案」。"""

        texts = result["texts"]
        self.assertIsNone(result["error"], f"意外异常：{result['error']!r}")
        self.assertEqual(result["answer_attempts"], attempts)
        self.assertEqual(len(texts), attempts)
        self.assertIn(expected, texts)
        for text in texts:
            self.assertNotIn(RETIRED_REFRESH_TEXT, text)
            for forbidden in FORBIDDEN_IN_RECEIPTS:
                self.assertNotIn(forbidden, text)

    def _assert_no_receipt(self, result: dict[str, Any]) -> None:
        self.assertEqual(result["answer_attempts"], 0)
        self.assertEqual(result["texts"], [])


class _Holder:
    """``memory_holder`` 的替身：只提供 ``get()``。"""

    def __init__(self, service: MemoryService) -> None:
        self._service = service

    def get(self) -> MemoryService:
        return self._service

    def get_optional(self) -> MemoryService:
        return self._service


# ======================================================================
# 业务语义段：只用基线既有符号，在冻结基线上必须红（intended AssertionError）
# ======================================================================


class LmDeleteReceiptTests(_LmDeleteTestCase):
    # ---- 守门：既有正确路径 ------------------------------------------------

    async def test_last_memory_deletion_reports_deleted_and_empties_the_list(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id)

        self._assert_receipt(result, expected=DELETED_TEXT.format(memory_id=memory_id))
        self.assertEqual(await self._fresh_ids(), [])
        self.assertEqual(len(result["edits"]), 1)

    async def test_deletion_with_other_memories_left_reports_deleted(self) -> None:
        first = await self._seed_memory(content="合成备忘 1")
        second = await self._seed_memory(content="合成备忘 2")

        result = await self._run(memory_id=first)

        self._assert_receipt(result, expected=DELETED_TEXT.format(memory_id=first))
        self.assertEqual(await self._fresh_ids(), [second])
        self.assertEqual(len(result["edits"]), 1)

    async def test_unknown_memory_id_reports_missing_or_deleted(self) -> None:
        await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=987654321)

        self._assert_receipt(result, expected=NO_CHANGE_TEXT)
        self.assertEqual(len(await self._fresh_ids()), 1)
        self.assertEqual(result["edits"], [])

    async def test_cross_group_memory_id_is_rejected_without_touching_the_other_group(
        self,
    ) -> None:
        foreign = await self._seed_memory(group_id=OTHER_GROUP_ID, content="另一个群的备忘")

        result = await self._run(memory_id=foreign)

        self._assert_receipt(result, expected=NO_CHANGE_TEXT)
        self.assertEqual(await self._fresh_ids(group_id=OTHER_GROUP_ID), [foreign])
        self.assertEqual(await self._fresh_ids(), [])

    async def test_malformed_callback_payload_reports_params_error(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        too_short = await self._run(memory_id=memory_id, data="lmd:1")
        self._assert_receipt(too_short, expected=PARAMS_ERROR_TEXT)

        non_positive = await self._run(memory_id=memory_id, data="lmd:-3:0")
        self._assert_receipt(non_positive, expected=PARAMS_ERROR_TEXT)

        self.assertEqual(len(await self._fresh_ids()), 1)

    async def test_missing_session_reports_session_not_ready(self) -> None:
        await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=1, session_ready=False)

        self._assert_receipt(result, expected=SESSION_NOT_READY_TEXT)
        self.assertEqual(len(await self._fresh_ids()), 1)

    async def test_missing_message_reports_message_gone(self) -> None:
        await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=1, drop_message=True)

        self._assert_receipt(result, expected=MESSAGE_GONE_TEXT)

    async def test_non_admin_member_is_denied(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, admin=False, user_id=MEMBER_ID)

        self._assert_receipt(result, expected=DENIED_TEXT)
        self.assertEqual(await self._fresh_ids(), [memory_id])

    async def test_unauthorized_group_is_rejected(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, authorized=False)

        self._assert_receipt(result, expected=UNAUTHORIZED_TEXT)
        self.assertEqual(await self._fresh_ids(), [memory_id])

    async def test_second_click_of_the_same_button_reports_no_change(self) -> None:
        """稳定 ID：同一个 ``lmd:<id>:<page>`` 再点一次不会删掉另一条。"""

        first = await self._seed_memory(content="合成备忘 1")
        second = await self._seed_memory(content="合成备忘 2")

        first_run = await self._run(memory_id=first)
        self._assert_receipt(first_run, expected=DELETED_TEXT.format(memory_id=first))
        self.assertEqual(await self._fresh_ids(), [second])

        # 页内位置会变（原来 #2 变成 #1），但按钮载荷里的 ID 不变。
        second_run = await self._run(memory_id=first, data=f"lmd:{first}:0")

        self._assert_receipt(second_run, expected=NO_CHANGE_TEXT)
        self.assertEqual(await self._fresh_ids(), [second])

    async def test_loaded_row_attributes_stay_readable_after_commit(self) -> None:
        """``expire_on_commit=False``：commit 之后读已加载属性不会抛 Detached/过期错误。"""

        memory_id = await self._seed_memory(content="合成备忘 1")

        async with self.session_factory() as session:
            row = await session.get(GroupPermanentMemory, memory_id)
            self.assertIsNotNone(row)
            await session.commit()
            self.assertEqual(str(row.content), "合成备忘 1")

    async def test_legacy_delete_by_stable_id_is_unchanged(self) -> None:
        first = await self._seed_memory(content="合成备忘 1")
        second = await self._seed_memory(content="合成备忘 2")

        service = self._service(_FaultySessionFactory(self.session_factory, db_path=self._db_path))
        deleted = await service.delete_permanent_memory(SUPERGROUP_ID, f"#{first}")

        self.assertIsInstance(deleted, list)
        self.assertEqual([int(row.id) for row in deleted], [first])
        self.assertEqual(await self._fresh_ids(), [second])

    async def test_legacy_delete_by_keyword_and_blank_target_are_unchanged(self) -> None:
        first = await self._seed_memory(content="合成备忘 alpha")
        second = await self._seed_memory(content="合成备忘 beta")

        service = self._service(_FaultySessionFactory(self.session_factory, db_path=self._db_path))
        blank = await service.delete_permanent_memory(SUPERGROUP_ID, "   ")
        self.assertEqual(blank, [])

        by_keyword = await service.delete_permanent_memory(SUPERGROUP_ID, "alpha")
        self.assertEqual([int(row.id) for row in by_keyword], [first])
        self.assertEqual(await self._fresh_ids(), [second])

        no_hit = await service.delete_permanent_memory(SUPERGROUP_ID, "gamma")
        self.assertEqual(no_hit, [])

    async def test_legacy_delete_propagates_transaction_failures(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")
        proxy = _FaultySessionFactory(
            self.session_factory, db_path=self._db_path, commit="fail_before"
        )
        service = self._service(proxy)

        with self.assertRaises(RuntimeError):
            await service.delete_permanent_memory(SUPERGROUP_ID, f"#{memory_id}")

        self.assertEqual(await self._fresh_ids(), [memory_id])

    # ---- 红检：写入未确认 ------------------------------------------------

    async def test_commit_failure_before_persist_reports_unconfirmed(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, commit="fail_before")

        self._assert_receipt(result, expected=UNCONFIRMED_TEXT)
        self.assertEqual(await self._fresh_ids(), [memory_id])
        self.assertEqual(result["edits"], [])
        for text in result["texts"]:
            self.assertNotIn(NO_CHANGE_TEXT, text)
            self.assertNotIn("没有生效", text)

    async def test_commit_failure_after_persist_reports_unconfirmed(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, commit="fail_after")

        self._assert_receipt(result, expected=UNCONFIRMED_TEXT)
        # 行**确实**已经删掉了——所以绝不能说「失败未生效」。
        self.assertEqual(await self._fresh_ids(), [])
        for text in result["texts"]:
            self.assertNotIn("没有生效", text)
            self.assertNotIn("未删除", text)

    async def test_delete_statement_failure_reports_unconfirmed(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        # 真实 DML 边界故障：新旧两版代码都必经的那条 cursor DELETE。这样旧实现的
        # ORM ``session.delete`` + flush 也会命中同一故障，红检才是同故障对照。
        with self._dml_fault(after=False) as seen:
            result = await self._run(memory_id=memory_id)

        self.assertEqual(seen["count"], 1)
        self._assert_receipt(result, expected=UNCONFIRMED_TEXT)
        self.assertEqual(await self._fresh_ids(), [memory_id])

    async def test_delete_statement_failure_after_dml_reports_unconfirmed(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        with self._dml_fault(after=True) as seen:
            result = await self._run(memory_id=memory_id)

        self.assertEqual(seen["count"], 1)
        self._assert_receipt(result, expected=UNCONFIRMED_TEXT)
        # 语句发出过、但事务从未 commit 就被关闭回滚：行仍在，所以只能报未确认。
        self.assertEqual(await self._fresh_ids(), [memory_id])

    async def test_unconfirmed_delete_neither_refreshes_nor_retries(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, commit="fail_before")

        self._assert_receipt(result, expected=UNCONFIRMED_TEXT)
        self.assertEqual(result["edits"], [])
        self.assertEqual(result["calls"]["delete_execute"], 1)
        self.assertEqual(result["calls"]["select_execute"], 0)
        # 只有删除服务那一枚会话；没有为了刷新而另开会话。
        self.assertEqual(result["sessions_created"], 1)

    # ---- 红检：写入已确认、但刷新失败 --------------------------------------

    async def test_list_refresh_failure_after_confirmed_delete_acknowledges_it(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, execute_select="fail_before")

        self._assert_receipt(
            result, expected=REFRESH_FAILED_TEXT.format(memory_id=memory_id)
        )
        self.assertEqual(await self._fresh_ids(), [])
        self.assertEqual(result["edits"], [])

    async def test_empty_branch_edit_failure_after_confirmed_delete_acknowledges_it(
        self,
    ) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(
            memory_id=memory_id, edit_fault=RuntimeError("synthetic edit")
        )

        self._assert_receipt(
            result, expected=REFRESH_FAILED_TEXT.format(memory_id=memory_id)
        )
        self.assertEqual(await self._fresh_ids(), [])

    async def test_non_empty_branch_edit_failure_after_confirmed_delete_acknowledges_it(
        self,
    ) -> None:
        first = await self._seed_memory(content="合成备忘 1")
        await self._seed_memory(content="合成备忘 2")

        result = await self._run(
            memory_id=first, edit_fault=RuntimeError("synthetic edit")
        )

        self._assert_receipt(
            result, expected=REFRESH_FAILED_TEXT.format(memory_id=first)
        )
        self.assertEqual(len(await self._fresh_ids()), 1)

    # ---- 取消：任何阶段都不得被 cleanup 异常替换 ---------------------------

    async def test_cancellation_before_commit_propagates_without_receipt(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, commit="cancel_before")

        self._assert_no_receipt(result)
        self.assertIsInstance(result["error"], asyncio.CancelledError)
        self.assertEqual(await self._fresh_ids(), [memory_id])

    async def test_cancellation_after_commit_persisted_propagates(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, commit="cancel_after")

        self._assert_no_receipt(result)
        self.assertIsInstance(result["error"], asyncio.CancelledError)

    async def test_cancellation_during_delete_statement_propagates(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, execute_delete="cancel_before")

        self._assert_no_receipt(result)
        self.assertIsInstance(result["error"], asyncio.CancelledError)
        self.assertEqual(await self._fresh_ids(), [memory_id])

    async def test_cancellation_during_list_refresh_propagates_without_receipt(
        self,
    ) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, execute_select="cancel_before")

        self._assert_no_receipt(result)
        self.assertIsInstance(result["error"], asyncio.CancelledError)

    async def test_cancellation_during_close_after_confirmed_commit_propagates(
        self,
    ) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, close="cancel_first")

        self._assert_no_receipt(result)
        self.assertIsInstance(result["error"], asyncio.CancelledError)

    # ---- 红检：context 退出失败不得推翻已确认结果 --------------------------

    async def test_close_failure_after_confirmed_commit_keeps_the_confirmed_result(
        self,
    ) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, close="fail_first")

        self._assert_receipt(result, expected=DELETED_TEXT.format(memory_id=memory_id))
        self.assertEqual(await self._fresh_ids(), [])
        for text in result["texts"]:
            self.assertNotIn(UNCONFIRMED_TEXT, text)

    async def test_close_failure_on_a_no_change_delete_keeps_no_change(self) -> None:
        await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=987654321, close="fail_first")

        self._assert_receipt(result, expected=NO_CHANGE_TEXT)
        self.assertEqual(len(await self._fresh_ids()), 1)

    # ---- 红检：独立 writer / 并发 -----------------------------------------

    async def test_independent_writer_wins_the_id_and_this_call_reports_no_change(
        self,
    ) -> None:
        """写锁获取前另一个连接先删掉这一行：本调用真实 rowcount 为 0。"""

        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(memory_id=memory_id, race_memory_id=memory_id)

        self._assert_receipt(result, expected=NO_CHANGE_TEXT)
        self.assertEqual(await self._fresh_ids(), [])

    # ---- 外部传输边界 ------------------------------------------------------

    async def test_answer_transport_failure_propagates_once_and_is_not_retried(
        self,
    ) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        result = await self._run(
            memory_id=memory_id, answer_fault=RuntimeError("synthetic answer")
        )

        self.assertIsInstance(result["error"], RuntimeError)
        self.assertEqual(result["answer_attempts"], 1)
        self.assertEqual(await self._fresh_ids(), [])


# ======================================================================
# 服务层协议段：基线不存在新符号，因此不参与旧红检
# ======================================================================


    # ---- B1：cleanup 边界与资源归还（父代理独立验收 blocker） ---------------
    #
    # 父代理真 init_db probe：DELETE 真实执行、``commit`` wrapper 在**真 commit 之前**
    # 抛错、``close`` 第一次在**真 close 之前**抛错 ⇒ 旧实现只 close 不 rollback，
    # 于是方法返回 unconfirmed 之后 session 仍 ``in_transaction()`` 且**仍占着
    # SQLite 全局写锁**（独立 writer 1 秒内写不进去）。下面每条都在 fixture teardown
    # **之前**观测真实 session，并用限时写入证明写锁到底是还了还是没还。

    async def test_unconfirmed_delete_rolls_back_and_releases_the_write_lock(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 B1")

        result = await self._run(
            memory_id=memory_id, commit="fail_before", close="fail_before"
        )

        self._assert_receipt(result, expected=UNCONFIRMED_TEXT)
        proxy = result["proxy"]
        self.assertEqual(proxy.calls["rollback"], 1, "失败路径必须先真 rollback")
        self.assertFalse(
            proxy.sessions[-1].in_transaction(), "事务必须已被真实 rollback 终结"
        )
        self.assertEqual(await self._fresh_ids(), [memory_id], "未确认的写入不得落库")
        await self._assert_write_lock_available()

    async def test_close_failure_before_the_real_close_is_retried_and_released(
        self,
    ) -> None:
        """第一次「真 close 之前」失败不算终点：有界后备必须把资源真的还回去。"""

        memory_id = await self._seed_memory(content="合成备忘 B1")

        result = await self._run(
            memory_id=memory_id, commit="fail_before", close=("fail_before", "ok")
        )

        self._assert_receipt(result, expected=UNCONFIRMED_TEXT)
        proxy = result["proxy"]
        self.assertEqual(proxy.calls["close"], 2, "必须重试一次真实 close")
        self.assertFalse(proxy.sessions[-1].in_transaction())
        self.assertEqual(await self._fresh_ids(), [memory_id])
        await self._assert_write_lock_available()

    async def test_every_recovery_path_failing_is_disclosed_and_not_pretended_safe(
        self,
    ) -> None:
        """rollback 与 close 的**所有**真实通路都永久失败 ⇒ 必须 fail-closed。

        既不能装作已安全清理，也不能因为 cleanup 故障就把「未确认」说成别的结局：
        这里锁**仍在**泄漏会话手上，只有显式归还才释放。
        """

        memory_id = await self._seed_memory(content="合成备忘 B1")

        result = await self._run(
            memory_id=memory_id,
            commit="fail_before",
            close="fail_before",
            rollback="fail_before",
        )

        self._assert_receipt(result, expected=UNCONFIRMED_TEXT)
        proxy = result["proxy"]
        self.assertEqual(proxy.calls["rollback"], 2, "rollback 尝试必须有界：两次")
        self.assertTrue(
            proxy.sessions[-1].in_transaction(),
            "所有恢复通路都失败时事务仍未终结，必须如实暴露而不是假装干净",
        )
        self.assertEqual(await self._fresh_ids(), [memory_id])
        await self._assert_write_lock_still_held()

        # 显式归还（父代理 probe 的第二步）之后锁必须立刻可用——证明前面确实是被占着。
        await proxy.recovery_closes[-1]()
        await self._assert_write_lock_available()

    async def test_confirmed_delete_is_not_overturned_by_a_close_failure_after_the_real_close(
        self,
    ) -> None:
        """已确认的持久化真值不因 cleanup 故障被改写（持久 true 与资源 true 分开）。"""

        memory_id = await self._seed_memory(content="合成备忘 B1")

        # 第一次 close 在**真正归还连接之后**抛错，后备 close 正常：删除已确认，资源也算归还。
        result = await self._run(memory_id=memory_id, close=("fail_after", "ok"))

        self._assert_receipt(
            result, expected=DELETED_TEXT.format(memory_id=memory_id)
        )
        self.assertEqual(await self._fresh_ids(), [])
        await self._assert_write_lock_available()

    async def test_cancellation_before_the_real_close_propagates_and_releases_the_lock(
        self,
    ) -> None:
        memory_id = await self._seed_memory(content="合成备忘 B1")

        result = await self._run(
            memory_id=memory_id, close=("cancel_before", "ok")
        )

        self.assertIsInstance(result["error"], asyncio.CancelledError)
        self.assertEqual(result["answer_attempts"], 0, "取消不得补发任何回执")
        self.assertEqual(await self._fresh_ids(), [], "已确认删除仍然持久")
        await self._assert_write_lock_available()

    async def test_original_cancellation_is_not_replaced_by_a_cleanup_failure(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 B1")

        # 第一次 close 取消、后备 close 也失败 ⇒ cleanup 两次都没归还连接；但 commit 早已
        # 成功并释放写锁，所以这里既不能伪造成功回执，也不能让 RuntimeError 顶掉调用方的取消。
        result = await self._run(
            memory_id=memory_id, close=("cancel_before", "fail_before")
        )

        self.assertIsInstance(
            result["error"],
            asyncio.CancelledError,
            "cleanup 的 RuntimeError 绝不能替换掉调用方的取消",
        )
        self.assertEqual(result["answer_attempts"], 0)
        self.assertEqual(await self._fresh_ids(), [], "已确认删除仍然持久")
        await self._assert_write_lock_available()

    async def test_rollback_failure_does_not_hide_the_unconfirmed_receipt(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 B1")

        result = await self._run(
            memory_id=memory_id, commit="fail_before", rollback="fail_before"
        )

        self._assert_receipt(result, expected=UNCONFIRMED_TEXT)
        self.assertEqual(await self._fresh_ids(), [memory_id])
        await self._assert_write_lock_available()


class MemoryDeleteCleanupCancellationTests(_LmDeleteTestCase):
    """B6 rev 3：cleanup 的可恢复取消必须走完恢复，新取消不得被吞成普通回执。

    父代理的 cumulative-cleanup probe 实测两条缺口：

    * 正在传播原始取消 A 时，cleanup 第一次 ``close`` 的取消会让实现**提前返回**，
      放弃后面那次本来可以成功的 ``close`` —— 事务与 SQLite 全局写锁都留在 session 上；
    * 没有原始取消时，cleanup 新发生的取消被吞成普通失败，handler 照常回一条未确认回执。

    这里用真实 ``init_db`` + 真实 SQLite + 真实 handler/service，只替换授权与最终外部
    transport；资源归还一律在 fixture teardown **之前**观测（限时独立 writer）。
    """

    def _raised(self, proxy: _FaultySessionFactory, label: str) -> BaseException:
        """取回 harness 抛出的**那一个**异常实例（不是同类型的新对象）。"""

        for name, exc in proxy.raised:
            if name == label:
                return exc
        self.fail(f"harness 没有记录 {label}；实际记录：{[n for n, _ in proxy.raised]}")

    async def test_original_cancellation_survives_cleanup_and_releases_the_resources(
        self,
    ) -> None:
        """原取消 A + rollback 永久失败 + close 首取消 B/次成功 ⇒ 传播 A、资源真归还。"""

        memory_id = await self._seed_memory(content="合成备忘 rev3 原取消")

        result = await self._run(
            memory_id=memory_id,
            commit="cancel_before",
            rollback="fail_before",
            close=("cancel_before", "ok"),
        )

        proxy = result["proxy"]
        original = self._raised(proxy, "commit_cancel_before")
        self.assertIs(
            result["error"], original, "传播的必须是原始取消 A 这个对象，不被 cleanup 的 B 取代"
        )
        self.assertEqual(result["answer_attempts"], 0, "取消不得补发任何回执")
        self.assertEqual(proxy.calls["rollback"], 2, "rollback 尝试必须有界：两次")
        self.assertGreaterEqual(
            proxy.calls["close"], 2, "第一次 cleanup 取消不得放弃后面仍可成功的 close"
        )
        self.assertFalse(
            proxy.sessions[-1].in_transaction(), "可恢复路径必须在 teardown 前真正终结事务"
        )
        self.assertEqual(await self._fresh_ids(), [memory_id], "未确认的写入不得落库")
        await self._assert_write_lock_available()

    async def test_new_cleanup_cancellation_is_propagated_not_swallowed_as_a_receipt(
        self,
    ) -> None:
        """普通 commit 错误 + rollback 失败 + close 首取消 B/次成功 ⇒ 传播 B、零回执。"""

        memory_id = await self._seed_memory(content="合成备忘 rev3 新取消")

        result = await self._run(
            memory_id=memory_id,
            commit="fail_before",
            rollback="fail_before",
            close=("cancel_before", "ok"),
        )

        proxy = result["proxy"]
        cancel_b = self._raised(proxy, "close_cancel_before")
        self.assertIs(
            result["error"],
            cancel_b,
            "没有原始取消时，cleanup 新发生的取消必须原样传播，不能被吞成普通业务结果",
        )
        self.assertEqual(result["answer_attempts"], 0, "取消不得产生普通回执")
        self.assertEqual(result["texts"], [], "取消不得刷新、不得回未确认")
        self.assertEqual(proxy.calls["delete_execute"], 1, "不得重复删")
        self.assertEqual(proxy.calls["commit"], 1, "不得补发补偿 commit")
        self.assertEqual(proxy.calls["rollback"], 2)
        self.assertGreaterEqual(proxy.calls["close"], 2)
        self.assertFalse(proxy.sessions[-1].in_transaction())
        self.assertEqual(await self._fresh_ids(), [memory_id], "未确认的写入不得落库")
        await self._assert_write_lock_available()

    async def test_recoverable_cleanup_cancel_then_success_still_releases_resources(
        self,
    ) -> None:
        """控制组：cleanup 首轮 rollback 取消/次轮成功 + close 首次异常/次次成功。"""

        memory_id = await self._seed_memory(content="合成备忘 rev3 控制组")

        result = await self._run(
            memory_id=memory_id,
            commit="fail_before",
            rollback=("cancel_before", "ok"),
            close=("fail_before", "ok"),
        )

        proxy = result["proxy"]
        cancel_b = self._raised(proxy, "rollback_cancel_before")
        self.assertIs(result["error"], cancel_b, "新取消必须原样传播")
        self.assertEqual(result["answer_attempts"], 0)
        self.assertEqual(
            proxy.calls["rollback"], 2, "第一次 rollback 取消后仍必须走到第二次并真的回滚"
        )
        self.assertEqual(proxy.calls["close"], 2, "第一次 close 异常后仍必须走到第二次")
        self.assertFalse(proxy.sessions[-1].in_transaction())
        self.assertEqual(await self._fresh_ids(), [memory_id])
        await self._assert_write_lock_available()

    async def test_real_task_cancellation_at_the_cleanup_await_boundary(self) -> None:
        """真实 ``Task.cancel()`` 在 cleanup 的 await 边界触发（不用 sleep 同步）。

        取消由事件循环在 ``close_gate.wait()`` 上派发，不是手工 ``raise``；随后 cleanup
        仍必须走完第二次 ``close`` 并把事务与写锁在 teardown 前还回去。
        """

        memory_id = await self._seed_memory(content="合成备忘 rev3 真实取消")

        proxy = _FaultySessionFactory(
            self.session_factory,
            db_path=self._db_path,
            commit="fail_before",
            rollback="fail_before",
            close=("block_until_released", "ok"),
        )
        service = self._service(proxy)

        call = asyncio.ensure_future(
            service.delete_permanent_memory_outcome(SUPERGROUP_ID, memory_id)
        )
        # 等到任务**真的挂在 cleanup 的 await 上**（事件边界，不用 sleep 猜时机）。
        await asyncio.wait_for(proxy.close_entered.wait(), timeout=1.5)
        self.assertFalse(call.done(), "取消必须发生在 cleanup 的 await 边界上")

        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertTrue(call.cancelled(), "任务必须以取消结束，而不是返回一个普通结局")

        self.assertEqual(proxy.calls["rollback"], 2)
        self.assertGreaterEqual(proxy.calls["close"], 2, "取消后仍必须走完可用的 close")
        self.assertFalse(
            proxy.sessions[-1].in_transaction(), "teardown 之前事务必须已真正终结"
        )
        self.assertEqual(await self._fresh_ids(), [memory_id], "未确认的写入不得落库")
        await self._assert_write_lock_available()


class PermanentMemoryDeletionOutcomeTests(_LmDeleteTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        if not _HAS_DELETE_OUTCOME:
            self.fail(
                "服务层缺少 delete_permanent_memory_outcome / "
                "PermanentMemoryDeletionOutcome"
            )

    def _outcome_service(self, **modes: Any) -> MemoryService:
        return self._service(
            _FaultySessionFactory(self.session_factory, db_path=self._db_path, **modes)
        )

    async def test_confirmed_delete_reports_one_changed_row(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        outcome = await self._outcome_service().delete_permanent_memory_outcome(
            SUPERGROUP_ID, memory_id
        )

        self.assertIsInstance(outcome, PermanentMemoryDeletionOutcome)
        self.assertTrue(outcome.confirmed_deleted)
        self.assertFalse(outcome.confirmed_no_change)
        self.assertEqual(int(outcome.changed_rows), 1)
        self.assertEqual(await self._fresh_ids(), [])

    async def test_missing_id_reports_confirmed_no_change_with_zero_rows(self) -> None:
        outcome = await self._outcome_service().delete_permanent_memory_outcome(
            SUPERGROUP_ID, 987654321
        )

        self.assertTrue(outcome.confirmed_no_change)
        self.assertFalse(outcome.confirmed_deleted)
        self.assertEqual(int(outcome.changed_rows), 0)

    async def test_cross_group_id_reports_no_change_and_leaves_the_row(self) -> None:
        foreign = await self._seed_memory(group_id=OTHER_GROUP_ID, content="另一个群的备忘")

        outcome = await self._outcome_service().delete_permanent_memory_outcome(
            SUPERGROUP_ID, foreign
        )

        self.assertTrue(outcome.confirmed_no_change)
        self.assertEqual(await self._fresh_ids(group_id=OTHER_GROUP_ID), [foreign])

    async def test_non_positive_or_malformed_id_is_a_confirmed_no_change(self) -> None:
        service = self._outcome_service()

        for bad in (0, -1, "not-a-number", None):
            outcome = await service.delete_permanent_memory_outcome(SUPERGROUP_ID, bad)
            self.assertTrue(outcome.confirmed_no_change, f"bad={bad!r} -> {outcome!r}")
            self.assertEqual(int(outcome.changed_rows), 0)

        self.assertEqual(await self._fresh_ids(), [])

    async def test_commit_failure_is_unconfirmed_and_never_fabricates_zero(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        outcome = await self._outcome_service(commit="fail_after").delete_permanent_memory_outcome(
            SUPERGROUP_ID, memory_id
        )

        self.assertEqual(outcome.status, "unconfirmed")
        self.assertFalse(outcome.confirmed_deleted)
        self.assertFalse(outcome.confirmed_no_change)

    async def test_statement_failure_is_unconfirmed(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        outcome = await self._outcome_service(
            execute_delete="fail_before"
        ).delete_permanent_memory_outcome(SUPERGROUP_ID, memory_id)

        self.assertEqual(outcome.status, "unconfirmed")

    async def test_rowcount_that_is_not_zero_or_one_is_unconfirmed(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        class _AnomalousResult:
            rowcount = 7

        proxy = _FaultySessionFactory(self.session_factory, db_path=self._db_path)
        service = self._service(proxy)

        real_execute = AsyncSession.execute

        async def execute(self_session: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
            if _statement_kind(statement) == "delete":
                return _AnomalousResult()
            return await real_execute(self_session, statement, *args, **kwargs)

        with patch.object(AsyncSession, "execute", new=execute):
            outcome = await service.delete_permanent_memory_outcome(
                SUPERGROUP_ID, memory_id
            )

        self.assertEqual(outcome.status, "unconfirmed")

    async def test_close_failure_does_not_overturn_a_confirmed_delete(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        outcome = await self._outcome_service(close="fail_first").delete_permanent_memory_outcome(
            SUPERGROUP_ID, memory_id
        )

        self.assertTrue(outcome.confirmed_deleted)
        self.assertEqual(await self._fresh_ids(), [])

    async def test_independent_writer_first_yields_changed_rows_zero(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        outcome = await self._outcome_service(
            race_memory_id=memory_id
        ).delete_permanent_memory_outcome(SUPERGROUP_ID, memory_id)

        self.assertTrue(outcome.confirmed_no_change)
        self.assertEqual(int(outcome.changed_rows), 0)

    async def test_cancellation_is_never_converted_into_an_outcome(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        with self.assertRaises(asyncio.CancelledError):
            await self._outcome_service(commit="cancel_before").delete_permanent_memory_outcome(
                SUPERGROUP_ID, memory_id
            )

        self.assertEqual(await self._fresh_ids(), [memory_id])

    async def test_cancellation_survives_a_failing_cleanup(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")

        with self.assertRaises(asyncio.CancelledError):
            await self._outcome_service(
                commit="cancel_before", close="fail_first"
            ).delete_permanent_memory_outcome(SUPERGROUP_ID, memory_id)

    async def test_two_concurrent_real_deletes_yield_exactly_one_deletion(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 1")
        # 两个真实调用都停在**写锁获取之前**再一起放行：否则根本没有写锁争用，
        # 「恰好一次删除」就可能是串行执行的假象。
        barrier = asyncio.Barrier(2)
        service = self._outcome_service(barrier=barrier)

        first, second = await asyncio.gather(
            service.delete_permanent_memory_outcome(SUPERGROUP_ID, memory_id),
            service.delete_permanent_memory_outcome(SUPERGROUP_ID, memory_id),
            return_exceptions=True,
        )

        outcomes = [
            item for item in (first, second) if not isinstance(item, BaseException)
        ]
        deleted = [item for item in outcomes if item.confirmed_deleted]
        self.assertEqual(len(deleted), 1, f"恰好一次确认删除：{outcomes!r}")
        self.assertEqual(int(deleted[0].changed_rows), 1)
        for item in outcomes:
            if item.confirmed_deleted:
                continue
            self.assertTrue(
                item.confirmed_no_change,
                f"第二次调用只能是确认零变更（不得再报一次删除）：{item!r}",
            )
        self.assertEqual(await self._fresh_ids(), [])

    async def test_legacy_still_returns_a_list_and_keeps_its_exception_behaviour(
        self,
    ) -> None:
        memory_id = await self._seed_memory(content="合成备忘")
        service = self._outcome_service()

        deleted = await service.delete_permanent_memory(SUPERGROUP_ID, f"#{memory_id}")
        self.assertIsInstance(deleted, list)
        self.assertEqual(await self._fresh_ids(), [])

        survivor = await self._seed_memory(content="另一条合成备忘")
        with self.assertRaises(RuntimeError):
            await self._outcome_service(
                commit="fail_before"
            ).delete_permanent_memory(SUPERGROUP_ID, f"#{survivor}")

        self.assertEqual(await self._fresh_ids(), [survivor])

    async def test_deletion_outcome_uses_the_atomic_group_and_id_condition(self) -> None:
        """群边界与稳定 ID 必须在同一条 DML 条件里（无预读 SELECT）。"""

        memory_id = await self._seed_memory(content="合成备忘 1")
        statements: list[str] = []

        @contextlib.contextmanager
        def _record() -> Any:
            from sqlalchemy import event

            def _hook(conn, cursor, statement, parameters, context, executemany):
                statements.append(str(statement))

            event.listen(self.engine.sync_engine, "before_cursor_execute", _hook)
            try:
                yield
            finally:
                event.remove(self.engine.sync_engine, "before_cursor_execute", _hook)

        with _record():
            outcome = await self._outcome_service().delete_permanent_memory_outcome(
                SUPERGROUP_ID, memory_id
            )

        self.assertTrue(outcome.confirmed_deleted)
        deletes = [item for item in statements if item.lstrip().upper().startswith("DELETE")]
        self.assertEqual(len(deletes), 1)
        normalized = " ".join(deletes[0].split())
        self.assertIn("group_id =", normalized)
        self.assertIn("id =", normalized)
        self.assertFalse(
            [item for item in statements if item.lstrip().upper().startswith("SELECT")],
            f"不应有预读 SELECT：{statements}",
        )


class MemoryDeleteHandlerDefenceTests(_LmDeleteTestCase):
    """B2：handler 只能信**真实 DTO 的 ``status``×``changed_rows`` 相容性**。

    父代理实测旧分支：``object()`` 替身 → ``AttributeError`` + 0 回执；两个真值属性
    都为真 → ``no_change``；真 ``DTO('deleted', 0)`` → success 并刷新；
    ``DTO('no_change', 1)`` → ``no_change``。下面把每一种形状都钉死为「一次未确认、
    且不刷新」。

    这里用 service 替身**专证 handler 防守**——真实事务与并发覆盖仍由业务段与
    ``PermanentMemoryDeletionOutcomeTests`` 的真 SQLite 用例承担，替身不替代它们。
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        if not _HAS_DELETE_OUTCOME:
            self.fail(
                "服务层缺少 delete_permanent_memory_outcome / "
                "PermanentMemoryDeletionOutcome"
            )
        if getattr(commands, "_classify_memory_delete_outcome", None) is None:
            self.fail("handler 缺少 _classify_memory_delete_outcome")

    async def _run_stub(
        self, outcome: Any, *, delete_fault: BaseException | None = None
    ) -> dict[str, Any]:
        async def _delete(group_id: int, memory_id: int) -> Any:
            if delete_fault is not None:
                raise delete_fault
            return outcome

        service = SimpleNamespace(
            delete_permanent_memory_outcome=_delete,
            list_permanent_memories=AsyncMock(return_value=[]),
        )
        callback = _Callback(data="lmd:1:0", chat_id=SUPERGROUP_ID, user_id=ADMIN_ID)
        error: BaseException | None = None
        with patch.object(commands, "memory_holder", new=_Holder(service)), patch.object(
            commands, "is_super_admin_user_id", return_value=False
        ), patch.object(
            commands, "is_group_authorized", new=AsyncMock(return_value=True)
        ), patch.object(
            commands, "is_group_admin_authorized", new=AsyncMock(return_value=True)
        ):
            async with self.session_factory() as session:
                try:
                    await commands.on_memory_delete(callback, _settings(), session)
                except BaseException as exc:  # noqa: BLE001 - 观察传播
                    error = exc

        return {
            "callback": callback,
            "texts": callback.texts,
            "answer_attempts": callback.answer_attempts,
            "error": error,
            "service": service,
        }

    def _unclassifiable_outcomes(self) -> list[Any]:
        class _ExplodingStatus:
            @property
            def status(self) -> str:
                raise RuntimeError("synthetic attribute failure")

            changed_rows = 1

        class _ExplodingCount:
            status = "deleted"

            @property
            def changed_rows(self) -> int:
                raise RuntimeError("synthetic count failure")

        return [
            object(),
            SimpleNamespace(),
            SimpleNamespace(status="deleted"),
            SimpleNamespace(changed_rows=1),
            _ExplodingStatus(),
            _ExplodingCount(),
            SimpleNamespace(status="deleted", changed_rows=0),
            SimpleNamespace(status="deleted", changed_rows=2),
            SimpleNamespace(status="deleted", changed_rows=True),
            SimpleNamespace(status="deleted", changed_rows=False),
            SimpleNamespace(status="deleted", changed_rows=1.0),
            SimpleNamespace(status="no_change", changed_rows=1),
            SimpleNamespace(status="no_change", changed_rows=False),
            SimpleNamespace(status="unconfirmed", changed_rows=0),
            SimpleNamespace(status="unconfirmed", changed_rows=1),
            SimpleNamespace(status="weird", changed_rows=1),
        ]

    async def test_unclassifiable_outcomes_answer_unconfirmed_once_without_refreshing(
        self,
    ) -> None:
        for outcome in self._unclassifiable_outcomes():
            with self.subTest(outcome=repr(outcome)):
                result = await self._run_stub(outcome)
                self.assertIsNone(result["error"], f"不得抛错：{result['error']!r}")
                self.assertEqual(result["answer_attempts"], 1, "必须恰好一次回执")
                self.assertEqual(result["texts"], [UNCONFIRMED_TEXT])
                self.assertEqual(
                    result["service"].list_permanent_memories.await_count,
                    0,
                    "未确认时不得刷新列表",
                )

    async def test_conflicting_real_dtos_are_unconfirmed(self) -> None:
        for status, rows in (
            ("deleted", 0),
            ("deleted", 2),
            ("no_change", 1),
            ("unconfirmed", 0),
            ("unconfirmed", 1),
            ("weird", 1),
        ):
            with self.subTest(status=status, changed_rows=rows):
                outcome = PermanentMemoryDeletionOutcome(
                    status=status, changed_rows=rows
                )
                result = await self._run_stub(outcome)
                self.assertIsNone(result["error"])
                self.assertEqual(result["texts"], [UNCONFIRMED_TEXT])
                self.assertEqual(
                    result["service"].list_permanent_memories.await_count, 0
                )

    async def test_valid_dtos_still_take_their_normal_path(self) -> None:
        deleted = await self._run_stub(
            PermanentMemoryDeletionOutcome(status="deleted", changed_rows=1)
        )
        self.assertEqual(deleted["texts"], [DELETED_TEXT.format(memory_id=1)])
        self.assertEqual(deleted["service"].list_permanent_memories.await_count, 1)

        no_change = await self._run_stub(
            PermanentMemoryDeletionOutcome(status="no_change", changed_rows=0)
        )
        self.assertEqual(no_change["texts"], [NO_CHANGE_TEXT])
        self.assertEqual(
            no_change["service"].list_permanent_memories.await_count,
            0,
            "确认零变更不得刷新",
        )

    async def test_service_exception_is_unconfirmed_once(self) -> None:
        result = await self._run_stub(
            None, delete_fault=RuntimeError("synthetic service failure")
        )
        self.assertIsNone(result["error"])
        self.assertEqual(result["answer_attempts"], 1)
        self.assertEqual(result["texts"], [UNCONFIRMED_TEXT])
        self.assertEqual(result["service"].list_permanent_memories.await_count, 0)

    async def test_classifier_fault_still_answers_unconfirmed_once(self) -> None:
        def _explode(outcome: Any) -> str:
            raise RuntimeError("synthetic classifier failure")

        with patch.object(commands, "_classify_memory_delete_outcome", new=_explode):
            result = await self._run_stub(
                PermanentMemoryDeletionOutcome(status="deleted", changed_rows=1)
            )

        self.assertIsNone(result["error"])
        self.assertEqual(result["answer_attempts"], 1)
        self.assertEqual(result["texts"], [UNCONFIRMED_TEXT])
        self.assertEqual(result["service"].list_permanent_memories.await_count, 0)


class PermanentMemoryTargetValidationTests(_LmDeleteTestCase):
    """B3：新 service 只接**真实 int（bool 不算）**正整数稳定 ID，且 rowcount 严检。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        if not _HAS_DELETE_OUTCOME:
            self.fail(
                "服务层缺少 delete_permanent_memory_outcome / "
                "PermanentMemoryDeletionOutcome"
            )
        if _pm_single_row_change is None:
            self.fail("服务层缺少 _pm_single_row_change")

    async def test_rowcount_only_trusts_real_integer_zero_or_one(self) -> None:
        self.assertEqual(_pm_single_row_change(SimpleNamespace(rowcount=0)), 0)
        self.assertEqual(_pm_single_row_change(SimpleNamespace(rowcount=1)), 1)

        class _ExplodingRowcount:
            @property
            def rowcount(self) -> Any:
                raise RuntimeError("synthetic rowcount failure")

        for raw in (
            True,
            False,
            0.5,
            1.5,
            -1,
            2,
            7,
            None,
            "1",
            "",
            float("nan"),
            float("inf"),
            -float("inf"),
            _ExplodingRowcount(),
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(
                    _pm_single_row_change(SimpleNamespace(rowcount=raw)),
                    f"raw={raw!r} 不得被折叠成 0/1，也不得抛转换错误",
                )

    async def test_illegal_memory_id_never_touches_any_row(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 B3")
        proxy = _FaultySessionFactory(self.session_factory, db_path=self._db_path)
        service = self._service(proxy)

        for bad in (True, False, 1.5, float("inf"), float("nan"), -1, 0, "1", None, [1], object()):
            with self.subTest(bad=bad):
                outcome = await service.delete_permanent_memory_outcome(
                    SUPERGROUP_ID, bad
                )
                self.assertEqual(outcome.status, "no_change", f"bad={bad!r}")
                self.assertEqual(outcome.detail, "invalid_target")
                self.assertEqual(int(outcome.changed_rows), 0)
                self.assertEqual(proxy.calls["delete_execute"], 0)
                self.assertEqual(proxy.calls["select_execute"], 0)
                self.assertEqual(proxy.calls["commit"], 0)
                self.assertEqual(
                    proxy.sessions_created, 0, "非法参数不得创建 session"
                )

        self.assertEqual(
            await self._fresh_ids(), [memory_id], "stable ID 1 必须完好无损"
        )

    async def test_illegal_group_id_never_touches_any_row(self) -> None:
        memory_id = await self._seed_memory(content="合成备忘 B3")
        proxy = _FaultySessionFactory(self.session_factory, db_path=self._db_path)
        service = self._service(proxy)

        for bad in (True, False, 1.5, float("nan"), "x", None):
            with self.subTest(bad=bad):
                outcome = await service.delete_permanent_memory_outcome(bad, memory_id)
                self.assertEqual(outcome.detail, "invalid_target")
                self.assertEqual(proxy.sessions_created, 0)
                self.assertEqual(proxy.calls["delete_execute"], 0)

        self.assertEqual(await self._fresh_ids(), [memory_id])

    async def test_negative_group_id_is_still_a_real_delete(self) -> None:
        foreign = await self._seed_memory(
            group_id=OTHER_GROUP_ID, content="合成备忘 B3 另群"
        )
        proxy = _FaultySessionFactory(self.session_factory, db_path=self._db_path)
        service = self._service(proxy)

        outcome = await service.delete_permanent_memory_outcome(
            OTHER_GROUP_ID, foreign
        )

        self.assertTrue(outcome.confirmed_deleted)
        self.assertEqual(outcome.changed_rows, 1)
        self.assertIsNot(type(outcome.changed_rows), bool)
        self.assertEqual(proxy.calls["delete_execute"], 1)
        self.assertEqual(await self._fresh_ids(group_id=OTHER_GROUP_ID), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
