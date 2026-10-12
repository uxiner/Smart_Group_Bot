"""崩溃后**持久状态恢复**合同。

被测对象不是"进程怎么崩的"，而是**崩溃之后留在库里的状态**如何被下一次真实
启动恢复。所以 seed 行是直接 ORM 写入的持久状态（下面写明），不冒称模拟真实
SIGKILL 的端到端崩溃。

两臂：

* ``dispatch_started=True`` —— "已经把消息交给 Telegram、但确认没写回去"：
  租约过期后**不自动重发**（重发就是管理员收到第二条），落 ``unknown``；
* ``dispatch_started=False`` —— "领取了但还没开始发送"：租约过期后回到
  ``pending``，由**真实**后台消费者 claim 并发一次，落 ``sent``、租约字段清空、
  attempts 由 1 变 2。

恢复必须由 main 装配里真正 ``await`` 的 ``recover_expired_leases()`` 与
``mark_uncertain_dispatches()`` 完成 —— 本文件**不**直接调这两个方法、不手动
drain/recover、不 new 消费者、不替换消费者。``send_message`` 是唯一的网络
替身。

关键点：**不能只验证 initializer**。恢复动作一完成状态就已经是对的了，若不去
观察消费者是否真的工作过，把 ``BudgetAlertConsumer.start`` 换成空实现也能"通过"。
所以这里用 SQLAlchemy 的 ``after_cursor_execute`` 观察**真实消费者**的 claim
查询（``budget_alert_events`` + ``status IN`` + ``LIMIT``；本文件自己的持久读
查询不带这两个条件，不会被误计），必须真的观测到多轮 claim 才放行。
``run_update_delivery`` 是被替身的外部 polling 入口，用来说"外部世界还在动"，
**不是**消费者入口。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import event, select

import bot.__main__ as main_module
from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import BudgetAlertEvent
from bot.services import budget_alerts, group_budget, verify_web
from bot.utils.timezone import now_shanghai_naive

_GROUP = -1001234567890
_ADMIN = 4242
#: 消费者 _claim 的查询特征（真实 SELECT）；本文件的持久读查询不带 LIMIT。
_CLAIM_SQL_MARKERS = ("budget_alert_events", "status IN", "LIMIT")
_CONSUMER_TASK_QUALNAME = "BudgetAlertConsumer._run"


async def _idle(*args, **kwargs):
    await asyncio.Event().wait()


class _IdleService:
    """与本任务无关的 service：构造成功，monitor/loop 类方法挂住，其余空转。"""

    def __init__(self, *args, **kwargs):
        self.webhook_route_error = None
        self.webhook_runtime_failure = asyncio.Event()

    def __getattr__(self, name):
        if name in ("monitor", "run_forever"):
            return _idle

        async def _noop(*a, **k):
            return None

        return _noop


class _Dispatcher(dict):
    def include_router(self, *args, **kwargs):
        return None


class RestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sb-alert-restart-")
        self.addCleanup(self._td.cleanup)
        self.db_path = os.path.join(self._td.name, "main.db")
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.db_path}"
        )
        self._extra_engines = []
        self._saved_env = dict(os.environ)
        os.environ.clear()
        self.addCleanup(self._restore_env)
        main_module.budget_alert_consumer_holder.clear()
        main_module._BUDGET_ALERT_BOT[0] = None
        self.addCleanup(main_module.budget_alert_consumer_holder.clear)
        self.addCleanup(group_budget.configure_alert_enqueuer, None)

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)

    async def asyncTearDown(self) -> None:
        for consumer in list(main_module.budget_alert_consumer_holder):
            await consumer.stop()
        main_module.budget_alert_consumer_holder.clear()
        main_module._BUDGET_ALERT_BOT[0] = None
        group_budget.configure_alert_enqueuer(None)
        group_budget.reset_shared_service()
        for engine in self._extra_engines:
            await engine.dispose()
        await self.engine.dispose()

    # ------------------------------------------------------------------
    def _settings(self) -> Settings:
        return Settings(  # type: ignore[call-arg]
            _env_file=None,
            super_admin_id=_ADMIN,
            database_url=f"sqlite+aiosqlite:///{self.db_path}",
        )

    async def _seed(self, *, dispatch_started: bool) -> int:
        """直接 ORM 写入一条**崩溃后**留在库里的行（不是模拟 SIGKILL）。"""

        expired = now_shanghai_naive() - timedelta(seconds=5)
        async with self.session_factory() as session:
            async with session.begin():
                row = BudgetAlertEvent(
                    group_id=_GROUP,
                    budget_date=now_shanghai_naive().date().isoformat(),
                    limit_kind="token",
                    threshold_kind="pause",
                    threshold_value=100,
                    used_value=100,
                    status=budget_alerts.STATUS_SENDING,
                    dispatch_started=dispatch_started,
                    attempts=1,
                    lease_owner="crashed-process",
                    lease_expires_at=expired,
                    next_attempt_at=expired,
                    summary=f"synthetic recovery fixture group={_GROUP}",
                )
                session.add(row)
                await session.flush()
                return int(row.id)

    async def _row(self, event_id: int):
        async with self.session_factory() as session:
            return (
                await session.scalars(
                    select(BudgetAlertEvent).where(BudgetAlertEvent.id == event_id)
                )
            ).first()

    def _consumer_tasks(self, baseline: set) -> list:
        return [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task not in baseline
            and getattr(task.get_coro(), "__qualname__", "")
            == _CONSUMER_TASK_QUALNAME
        ]

    async def _run_main(self, *, expect_status: str, expect_claims: int = 2) -> dict:
        """跑一次真实 main 到正常关停。

        必须先观测到**真实消费者**的多轮 claim 查询（SQL 层观察），才放行持久读
        断言；否则把消费者停掉也能"通过"。
        """

        sent: list[dict] = []
        claims = {"n": 0}
        claim_seen = asyncio.Event()
        baseline = set(asyncio.all_tasks())
        owned_at_entry: dict = {}
        holder_before = list(main_module.budget_alert_consumer_holder)
        hook_before = group_budget.alert_enqueuer()
        bot_before = main_module._BUDGET_ALERT_BOT[0]
        stopped: dict = {}

        def observe(engine) -> None:
            sync_engine = engine.sync_engine

            def _after(conn, cursor, statement, parameters, context, executemany):
                text = str(statement).upper()
                if all(marker.upper() in text for marker in _CLAIM_SQL_MARKERS):
                    claims["n"] += 1
                    if claims["n"] >= expect_claims:
                        claim_seen.set()

            event.listen(sync_engine, "after_cursor_execute", _after)
            observed.append((sync_engine, _after))

        observed: list = []

        async def close():
            stopped["bot_close"] = {
                "live": len(self._consumer_tasks(baseline)),
                "owned": sum(
                    1 for c in main_module.budget_alert_consumer_holder
                    if c not in holder_before
                ),
                "hook": group_budget.alert_enqueuer() is hook_before,
                "bot": main_module._BUDGET_ALERT_BOT[0] is bot_before,
            }

        async def delivery(**kwargs):
            live = self._consumer_tasks(baseline)
            owned_at_entry["tasks"] = live
            # 本次 main 装配出来的消费者实例：从 holder 差量里取，**不**替换
            # 消费者的任何方法。
            fresh = [
                c for c in main_module.budget_alert_consumer_holder
                if c not in holder_before
            ]
            if fresh:
                consumer_instance["consumer"] = fresh[0]
            # 存活状态在**捕获当下**记录：main 之后会正常停掉它，那时再看
            # done 就变成"当然结束了"。
            owned_at_entry["all_alive"] = bool(live) and all(
                not task.done() for task in live
            )
            owned_at_entry["holder"] = sum(
                1 for c in main_module.budget_alert_consumer_holder
                if c not in holder_before
            )
            # 等真实消费者的多轮 claim —— 不是等状态变对（状态可能在消费者还没
            # 跑过之前就已经被 initializer 改对了）。
            async with asyncio.timeout(30):
                await claim_seen.wait()
            return None

        async def send_message(*args, **kwargs):
            sent.append(
                {
                    "chat_id": args[0] if args else None,
                    "text": args[1] if len(args) > 1 else "",
                }
            )
            return SimpleNamespace(message_id=len(sent))

        bot = SimpleNamespace(
            send_message=send_message,
            me=AsyncMock(
                return_value=SimpleNamespace(
                    id=1, username="fixture", full_name="F", first_name="F"
                )
            ),
            session=SimpleNamespace(close=close),
        )
        real_init_db = init_db
        engine_box: list = []

        async def observing_init_db(*args, **kwargs):
            # 每次 main 都开**自己的**引擎（不是 self.engine）：两轮 main 因此
            # 各自持有、各自 dispose。
            engine, factory = await real_init_db(
                f"sqlite+aiosqlite:///{self.db_path}"
            )
            engine_box.append(engine)
            self._extra_engines.append(engine)
            observe(engine)

            async def dispose():
                # 绑定的是**本次**真实引擎（此前的 dispose 闭包引用了不在词法
                # 作用域里的 engine，NameError 被 main 的 bounded cleanup 吞掉，
                # 测试只断言调用前的快照，于是照样绿）。
                stopped["engine_dispose"] = {
                    "live": len(self._consumer_tasks(baseline)),
                    "owned": sum(
                        1 for c in main_module.budget_alert_consumer_holder
                        if c not in holder_before
                    ),
                    "hook": group_budget.alert_enqueuer() is hook_before,
                    "bot": main_module._BUDGET_ALERT_BOT[0] is bot_before,
                }
                try:
                    await engine.dispose()
                except BaseException as exc:
                    # main 的 bounded cleanup 会把这里的异常吞掉；必须自己
                    # 留下来并在断言里检查，否则"调用过"会被当成"释放成功"。
                    stopped.setdefault("cleanup_errors", []).append(
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    stopped["disposed_completed"] = 1

            return SimpleNamespace(dispose=dispose), factory

        replacements = {
            "load_bootstrap_settings": lambda: self._settings(),
            "validate_bootstrap_settings": lambda _s: None,
            "init_db": observing_init_db,
            "configure_logging": lambda *a, **kw: None,
            "_prefetch_model_context_metadata": AsyncMock(),
            "dp": _Dispatcher(),
            "create_bot": lambda _s: bot,
            "_build_group_summary_scheduler": lambda **kw: None,
            "_register_update_middlewares": lambda *a: None,
            "_publish_bot_command_menu": AsyncMock(),
            "restore_vote_ban_tasks": AsyncMock(),
            "log_process_identity": lambda: None,
            "log_enforcement_switch_state": lambda _s: None,
            "run_update_delivery": delivery,
            "_arm_forced_exit_watchdog": lambda *a: SimpleNamespace(
                set=lambda: None
            ),
        }
        idle_services = (
            "TelegramCleanupScheduler", "ProactiveTopicService",
            "JoinVerificationSweeper", "PatrolService",
            "ScheduledMessageService", "GroupPermissionService",
            "ShopExpiryService", "RaidGuardService",
        )
        consumer_instance = {}
        try:
            with ExitStack() as stack:
                for name, value in replacements.items():
                    stack.enter_context(patch.object(main_module, name, value))
                for name in idle_services:
                    stack.enter_context(
                        patch.object(main_module, name, _IdleService)
                    )
                stack.enter_context(
                    patch.object(verify_web, "VerifyWebServer", _IdleService)
                )
                stack.enter_context(
                    patch.object(
                        main_module.MemoryService, "run_archive_maintenance", _idle
                    )
                )
                stack.enter_context(
                    patch.object(
                        main_module.model_limits.PeriodicModelMetadataRefresh,
                        "start",
                        lambda self: asyncio.create_task(_idle()),
                    )
                )
                async with asyncio.timeout(45):
                    await main_module.main()
        finally:
            for sync_engine, listener in observed:
                event.remove(sync_engine, "after_cursor_execute", listener)

        return {
            "sent": sent,
            "claims": claims["n"],
            "owned_at_entry": owned_at_entry,
            "stopped": stopped,
            "consumer": consumer_instance.get("consumer"),
            "engine": engine_box[0] if engine_box else None,
        }

    def _assert_entry_saw_a_live_consumer(self, result: dict) -> None:
        tasks = result["owned_at_entry"].get("tasks") or []
        self.assertEqual(
            len(tasks), 1,
            f"外部入口被走到时消费者任务数不是 1（claims={result['claims']}）",
        )
        self.assertTrue(
            result["owned_at_entry"]["all_alive"],
            f"捕获当下消费者任务不是活的（claims={result['claims']}）",
        )
        self.assertEqual(
            result["owned_at_entry"]["holder"], 1,
            "外部入口被走到时 owned holder 不是 1",
        )

    def _assert_shutdown(self, result: dict) -> None:
        stopped = result["stopped"]
        self.assertEqual(
            stopped.get("cleanup_errors", []), [],
            f"引擎清理抛了异常却被 main 的 bounded cleanup 吞掉："
            f"{stopped.get('cleanup_errors')}",
        )
        self.assertEqual(
            stopped.get("disposed_completed"), 1,
            "engine.dispose 没有真正 await 完成",
        )
        self.assertIn("bot_close", stopped, "bot.session.close 没被调用")
        self.assertEqual(stopped["bot_close"]["live"], 0, "bot 关停时消费者还活着")
        self.assertEqual(stopped["bot_close"]["owned"], 0, "bot 关停时 owned 未清零")
        self.assertTrue(stopped["bot_close"]["hook"], "bot 关停时 hook 未回基线")
        self.assertTrue(stopped["bot_close"]["bot"], "bot 关停时 bot 引用未回基线")
        self.assertIn("engine_dispose", stopped, "engine.dispose 没被调用")
        self.assertEqual(stopped["engine_dispose"]["live"], 0)
        self.assertEqual(stopped["engine_dispose"]["owned"], 0)
        self.assertTrue(stopped["engine_dispose"]["hook"], "dispose 时 hook 未回基线")
        self.assertTrue(stopped["engine_dispose"]["bot"], "dispose 时 bot 引用未回基")
        # 捕获下来的 owned 任务必须**非空且每个都结束**（不只靠 live 扫描）。
        owned = result["owned_at_entry"]["tasks"]
        self.assertTrue(owned, "没捕获到 owned 消费者任务")
        for task in owned:
            self.assertTrue(
                task.done() or task.cancelled(),
                "main 关停之后 owned 消费者任务仍在跑",
            )
        self.assertEqual(len(main_module.budget_alert_consumer_holder), 0)
        self.assertIsNone(main_module._BUDGET_ALERT_BOT[0])

    # ------------------------------------------------------------------
    async def test_dispatched_but_unconfirmed_becomes_unknown_and_is_never_resent(self) -> None:
        event_id = await self._seed(dispatch_started=True)

        first = await self._run_main(expect_status=budget_alerts.STATUS_UNKNOWN)
        self._assert_entry_saw_a_live_consumer(first)
        self._assert_shutdown(first)
        self.assertGreaterEqual(first["claims"], 2, first["claims"])
        self.assertEqual(first["sent"], [], "已开始发送的行被自动重发了")
        row = await self._row(event_id)
        self.assertEqual(str(row.status), budget_alerts.STATUS_UNKNOWN)
        self.assertTrue(bool(row.dispatch_started))
        self.assertEqual(int(row.attempts), 1, "unknown 行不该再累加尝试次数")
        self.assertEqual(str(row.lease_owner), "", "unknown 行的租约 owner 没清空")
        self.assertIsNone(row.lease_expires_at, "unknown 行的租约没清空")
        self.assertIsNone(row.sent_at, "unknown 行不能有送达时间")
        first_consumer = first["consumer"]
        first_task = first["owned_at_entry"]["tasks"][0]

        group_budget.reset_shared_service()
        second = await self._run_main(expect_status=budget_alerts.STATUS_UNKNOWN)
        self._assert_entry_saw_a_live_consumer(second)
        self._assert_shutdown(second)
        self.assertGreaterEqual(second["claims"], 2, second["claims"])
        self.assertEqual(second["sent"], [], "第二次启动又重发了 unknown 行")
        again = await self._row(event_id)
        self.assertEqual(str(again.status), budget_alerts.STATUS_UNKNOWN)
        self.assertEqual(int(again.attempts), 1, "attempts 被改动了")
        self.assertEqual(str(again.lease_owner), "")
        self.assertIsNone(again.lease_expires_at)
        self.assertTrue(bool(again.dispatch_started), "dispatch_started 被改动了")
        self.assertIsNone(again.sent_at, "unknown 行在第二轮多了送达时间")
        self.assertIsNone(row.sent_at)
        # 两次 main 必须是重新装配，不是同一个消费者/同一个引擎。
        self.assertIsNot(second["consumer"], first_consumer)
        self.assertTrue(first_task.done(), "第一次的消费者任务还没结束")
        self.assertIsNot(second["engine"], first["engine"])

    async def test_undispatched_expired_lease_is_resent_exactly_once(self) -> None:
        event_id = await self._seed(dispatch_started=False)

        first = await self._run_main(expect_status=budget_alerts.STATUS_SENT)
        self._assert_entry_saw_a_live_consumer(first)
        self._assert_shutdown(first)
        self.assertGreaterEqual(first["claims"], 2, first["claims"])
        self.assertEqual(len(first["sent"]), 1, first["sent"])
        self.assertEqual(int(first["sent"][0]["chat_id"]), _ADMIN)
        self.assertIn(str(_GROUP), first["sent"][0]["text"])
        self.assertIn("token", first["sent"][0]["text"])
        self.assertIn("pause", first["sent"][0]["text"])
        row = await self._row(event_id)
        self.assertEqual(str(row.status), budget_alerts.STATUS_SENT)
        self.assertEqual(
            int(row.attempts), 2, "被重新领取一次后 attempts 应由 1 变 2"
        )
        self.assertEqual(str(row.lease_owner), "", "租约 owner 没清空")
        self.assertIsNone(row.lease_expires_at, "租约过期时间没清空")
        self.assertIsNotNone(row.sent_at)
        first_consumer = first["consumer"]
        first_task = first["owned_at_entry"]["tasks"][0]

        group_budget.reset_shared_service()
        second = await self._run_main(expect_status=budget_alerts.STATUS_SENT)
        self._assert_entry_saw_a_live_consumer(second)
        self._assert_shutdown(second)
        self.assertGreaterEqual(second["claims"], 2, second["claims"])
        self.assertEqual(second["sent"], [], "已 sent 的事件被重复发送")
        again = await self._row(event_id)
        self.assertEqual(str(again.status), budget_alerts.STATUS_SENT)
        self.assertEqual(int(again.attempts), 2, "第二次启动又累加了尝试次数")
        self.assertEqual(str(again.lease_owner), "")
        self.assertIsNone(again.lease_expires_at)
        self.assertTrue(bool(again.dispatch_started), "dispatch_started 被改动了")
        self.assertIsNotNone(again.sent_at)
        self.assertEqual(
            again.sent_at, row.sent_at,
            "第二轮改写了送达时间 —— 说明发生了重复发送",
        )
        self.assertIsNot(second["consumer"], first_consumer)
        self.assertTrue(first_task.done(), "第一次的消费者任务还没结束")
        self.assertIsNot(second["engine"], first["engine"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()