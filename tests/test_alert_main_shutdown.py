"""main() 的**正常关停**与构造失败（正式回归）。

真实 ``Settings(_env_file=None)`` + ``os.environ.clear()``、真实 init_db（main 的
init_db 返回观察包装、``session_factory`` 仍是真���）、真实
``_initialize_runtime_services`` 与真实消费者；不手调 ``drain_once``、不 mock
``_run_with_background_supervision`` 与 main 的 finally、不在测试里先
``consumer.stop``。只隔离与本任务无关的外部设施（命令菜单、verify web、
轮询/维护协程、metadata 刷新）与要注入故障的那一步。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot.__main__ as main_module
from bot.config import Settings
from bot.db.engine import init_db
from bot.services import budget_alerts, group_budget
from bot.services import llm as llm_module
from bot.services import verify_web

_CONSUMER_TASK_QUALNAME = "BudgetAlertConsumer._run"
_KEEPER_SENTINEL = object()


async def _idle(*args, **kwargs):
    await asyncio.Event().wait()


async def _noop(*args, **kwargs):
    return None


class _IdleService:
    """与本任务无关的 service：构造成功，方法要么空转要么挂住。"""

    def __init__(self, *args, **kwargs):
        self.webhook_route_error = None
        self.webhook_runtime_failure = asyncio.Event()

    def __getattr__(self, name):
        return _idle if name in ("monitor", "run_forever") else _noop


class _Dispatcher(dict):
    def include_router(self, *args, **kwargs):
        return None


class MainShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sb-alert-shutdown-")
        self.addCleanup(self._td.cleanup)
        self.db_path = os.path.join(self._td.name, "main.db")
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.db_path}"
        )
        self._saved_env = dict(os.environ)
        os.environ.clear()
        self.addCleanup(self._restore_env)
        main_module.budget_alert_consumer_holder.clear()
        main_module._BUDGET_ALERT_BOT[0] = None
        self.addCleanup(main_module.budget_alert_consumer_holder.clear)
        self.addCleanup(group_budget.configure_alert_enqueuer, None)
        # 进程级成本护栏是**模块全局**，本文件必须还原进入时的原值 —— 否则
        # 一次真实 main 留下的护栏会带着已删掉的临时库污染后续套件。
        self._guard_before = llm_module.cost_guard_factory()
        self.addCleanup(llm_module.configure_cost_guard, self._guard_before)

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
        await self.engine.dispose()

    # ------------------------------------------------------------------
    def _settings(self) -> Settings:
        return Settings(  # type: ignore[call-arg]
            _env_file=None,
            super_admin_id=4242,
            database_url=f"sqlite+aiosqlite:///{self.db_path}",
        )

    def _consumer_tasks(self, baseline: set) -> list:
        return [
            task
            for task in asyncio.all_tasks()
            if not task.done()
            and task not in baseline
            and getattr(task.get_coro(), "__qualname__", "") == _CONSUMER_TASK_QUALNAME
        ]

    def _install_keeper(self, baseline: set):
        keeper = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=lambda *_: True,
            recipient="9", owner="keeper",
        )
        main_module.budget_alert_consumer_holder.append(keeper)
        keeper.start()
        marker = object()
        group_budget.configure_alert_enqueuer(marker)
        bot_marker = object()
        main_module._BUDGET_ALERT_BOT[0] = bot_marker
        baseline = set(asyncio.all_tasks())
        return keeper, marker, bot_marker, baseline

    async def _run_main(self, *, keeper: bool, constructor_error: bool = None):
        """跑真实 main（正常关停或构造失败），返回观察到的快照序列。"""

        holder = main_module.budget_alert_consumer_holder
        before = list(holder)
        hook = group_budget.alert_enqueuer()
        bot_before = main_module._BUDGET_ALERT_BOT[0]
        guard_before = llm_module.cost_guard_factory()
        keep = None
        if keeper:
            keep, hook, bot_before, baseline = self._install_keeper(baseline=set())
            before = list(holder)
        else:
            baseline = set(asyncio.all_tasks())
        observed: list[dict] = []
        constructor_calls = [0]
        owned_tasks: set = set()

        def snapshot(label: str) -> None:
            observed.append({
                "label": label,
                "live": len(self._consumer_tasks(baseline)),
                "owned_holder": sum(1 for c in holder if c not in before),
                "owned_tasks_nonempty": bool(owned_tasks),
                "owned_tasks_done": all(t.done() for t in owned_tasks),
                "keeper_alive": bool(keep and keep._task and not keep._task.done()),
                "keeper_in_holder": (keep in holder) if keep is not None else True,
                "hook_restored": group_budget.alert_enqueuer() is hook,
                "bot_restored": main_module._BUDGET_ALERT_BOT[0] is bot_before,
                # 进程级护栏：装配期必须真的换成本次的（阳性对照，否则"已还原"
                # 可能只是从头到尾都没装过），清理之后必须回到进入时的原值。
                "guard_installed": (
                    llm_module.cost_guard_factory() is not None
                    and llm_module.cost_guard_factory() is not guard_before
                ),
                "guard_is_baseline": llm_module.cost_guard_factory() is guard_before,
            })

        def capture_owned(label: str) -> None:
            """**同步**采集本次 owned 消费者的真实任务并当场做阳性断言。

            不 await、不返回协程 —— 它从同步构造路径（_FailingService.__init__）
            里也会被调用。断言"确实有一个活着的 owned 任务 + holder 差量 1"，
            否则后面的 for 循环会因为空集合而空绿。
            """

            tasks = self._consumer_tasks(baseline)
            self.assertEqual(
                len(tasks), 1,
                f"{label}: owned 消费者任务数不是 1（{len(tasks)}）",
            )
            self.assertTrue(
                all(not task.done() for task in tasks),
                f"{label}: 捕获到的 owned 消费者任务不是活的",
            )
            self.assertEqual(
                sum(1 for c in holder if c not in before), 1,
                f"{label}: owned holder 差量不是 1",
            )
            owned_tasks.update(tasks)
            snapshot(label)

        async def close():
            snapshot("bot_close")

        async def dispose():
            snapshot("engine_dispose")
            await self.engine.dispose()

        async def delivery(**kwargs):
            capture_owned("delivery_entered")
            await asyncio.sleep(0)

        class _FailingService(_IdleService):
            def __init__(self, *args, **kwargs):
                # 构造故障这条臂**从不进** delivery，阳性证据必须就地采集。
                capture_owned("constructor_entered")
                constructor_calls[0] += 1
                raise constructor_error

        bot = SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(
                id=1, username="fixture", full_name="F", first_name="F"
            )),
            session=SimpleNamespace(close=close),
        )
        services = [
            "TelegramCleanupScheduler", "ProactiveTopicService",
            "JoinVerificationSweeper", "PatrolService",
            "ScheduledMessageService", "GroupPermissionService",
            "ShopExpiryService", "RaidGuardService",
        ]
        replacements = {
            "load_bootstrap_settings": lambda: self._settings(),
            "validate_bootstrap_settings": lambda _s: None,
            "init_db": AsyncMock(return_value=(SimpleNamespace(dispose=dispose), self.session_factory)),
            "configure_logging": lambda *a, **k: None,
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
            "_arm_forced_exit_watchdog": lambda *a: SimpleNamespace(set=lambda: None),
            "run_resource_watchdog": _idle,
            "run_private_chat_history_maintenance": _idle,
            "run_search_record_maintenance": _idle,
            "run_long_term_memory_extraction": _idle,
            "run_long_term_memory_maintenance": _idle,
        }
        with ExitStack() as stack:
            for name, value in replacements.items():
                stack.enter_context(patch.object(main_module, name, value))
            for name in services:
                target = _FailingService if name == "ProactiveTopicService" and constructor_error else _IdleService
                stack.enter_context(patch.object(main_module, name, target))
            stack.enter_context(patch.object(verify_web, "VerifyWebServer", _IdleService))
            stack.enter_context(patch.object(
                main_module.MemoryService, "run_archive_maintenance", _idle
            ))
            stack.enter_context(patch.object(
                main_module.model_limits.PeriodicModelMetadataRefresh, "start",
                lambda self: asyncio.create_task(_idle()),
            ))
            error = None
            try:
                async with asyncio.timeout(20):
                    await main_module.main()
            except BaseException as exc:      # 构造失败臂要拿到**原**异常对象
                error = exc
        snapshot("returned")
        return {
            "observed": observed,
            "owned_tasks": owned_tasks,
            "keeper": keep,
            "constructor_calls": constructor_calls[0],
            "error": error,
        }

    def _assert_closed_snapshots(self, result: dict, *, keeper_expected: bool = False) -> None:
        """bot_close / engine_dispose / returned 三个**真实** snapshot 都要断言。

        owned 任务必须非空（否则是空绿）、且都已结束；live 0、owned holder 0、
        hook 与 bot 引用回基线；有 keeper 时它必须仍活着且仍在 holder 里。
        """

        owned = result["owned_tasks"]
        self.assertEqual(len(owned), 1, f"捕获到的 owned 任务不是 1 条：{owned}")
        by_label = {item["label"]: item for item in result["observed"]}
        # 阳性对照：装配期确实换上了本次的护栏。没有这一条，"清理后已还原"
        # 可能只是整轮都没装过（和 hook/bot 那两条的控制同一个道理）。
        self.assertTrue(
            any(item["guard_installed"] for item in result["observed"]),
            "整个启动过程里进程级成本护栏从未被换成本次装配的那个；"
            "那么后面的'已还原'断言是空绿",
        )
        for label in ("bot_close", "engine_dispose", "returned"):
            self.assertIn(label, by_label, f"缺少真实 snapshot：{label}")
            item = by_label[label]
            self.assertEqual(item["live"], 0, f"{label}: 消费者还活着")
            self.assertEqual(item["owned_holder"], 0, f"{label}: owned holder 未清零")
            self.assertTrue(item["owned_tasks_nonempty"], f"{label}: owned 任务为空")
            self.assertTrue(item["owned_tasks_done"], f"{label}: owned 任务未结束")
            self.assertTrue(item["hook_restored"], f"{label}: hook 未回基线")
            self.assertTrue(item["bot_restored"], f"{label}: bot 引用未回基线")
            self.assertTrue(
                item["guard_is_baseline"],
                f"{label}: 进程级成本护栏没有还原成进入时的原值 —— 它握着本次的"
                "会话工厂，engine 一 dispose，后续任何 LLM 调用都会去查一个已经"
                "关掉的库（跨套件就是上一套件删掉的临时库）",
            )
            if keeper_expected:
                self.assertTrue(item["keeper_alive"], f"{label}: keeper 不在了")
                self.assertTrue(item["keeper_in_holder"], f"{label}: keeper 被移出 holder")

    # ------------------------------------------------------------------
    async def test_normal_shutdown_without_keeper(self) -> None:
        result = await self._run_main(keeper=False)
        self.assertIsNone(result["error"])
        by_label = {s["label"]: s for s in result["observed"]}
        self.assertIn("bot_close", by_label)
        self.assertEqual(
            by_label["bot_close"]["live"], 0,
            "bot HTTP 关闭时本次消费者任务还在跑",
        )
        self.assertEqual(by_label["bot_close"]["owned_holder"], 0)
        self.assertTrue(by_label["bot_close"]["hook_restored"])
        self.assertTrue(by_label["bot_close"]["bot_restored"])
        self.assertEqual(by_label["engine_dispose"]["live"], 0)
        # 返回之后再确认：owned 任务真的结束、holder 回到 baseline。
        for task in result["owned_tasks"]:
            self.assertTrue(task.done() or task.cancelled(), "owned 任务还在跑")
        self.assertEqual(len(main_module.budget_alert_consumer_holder), 0)
        self._assert_closed_snapshots(result)

    async def test_normal_shutdown_preserves_a_pre_existing_keeper(self) -> None:
        result = await self._run_main(keeper=True)
        keeper = result["keeper"]
        try:
            self.assertIsNone(result["error"])
            by_label = {s["label"]: s for s in result["observed"]}
            self.assertTrue(
                by_label["bot_close"]["keeper_alive"],
                "正常关停把既有 keeper 一起停了",
            )
            self.assertEqual(by_label["bot_close"]["owned_holder"], 0)
            self.assertTrue(by_label["bot_close"]["hook_restored"])
            self.assertTrue(by_label["bot_close"]["bot_restored"])
            self.assertFalse(keeper._task.done())
            self.assertIn(keeper, main_module.budget_alert_consumer_holder)
        finally:
            await keeper.stop()
            main_module.budget_alert_consumer_holder.clear()
            group_budget.configure_alert_enqueuer(None)
        self._assert_closed_snapshots(result, keeper_expected=True)

    async def test_service_construction_failure_restores_hook_too(self) -> None:
        boom = RuntimeError("constructor exploded")
        result = await self._run_main(keeper=False, constructor_error=boom)
        self.assertIs(result["error"], boom, "原异常对象必须继续抛出")
        by_label = {s["label"]: s for s in result["observed"]}
        self.assertIn("bot_close", by_label)
        self.assertEqual(by_label["bot_close"]["live"], 0)
        self.assertEqual(by_label["bot_close"]["owned_holder"], 0)
        self.assertTrue(
            by_label["bot_close"]["hook_restored"],
            "构造失败路径只 stop 了消费者，没有还原登记钩子",
        )
        self.assertTrue(by_label["bot_close"]["bot_restored"])
        self.assertEqual(by_label["engine_dispose"]["live"], 0)
        for task in result["owned_tasks"]:
            self.assertTrue(task.done() or task.cancelled())
        self.assertEqual(len(main_module.budget_alert_consumer_holder), 0)
        self.assertEqual(result["constructor_calls"], 1, "构造故障没被调用到")
        self.assertEqual(
            sum(1 for i in result["observed"] if i["label"] == "constructor_entered"),
            1, "构造故障前没有就地采集阳性证据",
        )
        self._assert_closed_snapshots(result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()