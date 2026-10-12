"""启动包装器的两条真实用例：正常返回三元组 / 失败清理本次消费者。

用**真实 Settings**（`_env_file=None` + 清空环境，不读任何本机配置），真实
init_db；只 mock 远端模型元数据预取和日志，不 patch 消费者、也不 patch
``RuntimeConfigManager.initialize`` 以外的启动路径。

反投 7918848 时，第一条必须绿、第二条必须在**清理断言**上红（缺 API 报错不算）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import bot.__main__ as main_module
from bot.config import Settings
from bot.db.engine import init_db
from bot.services import budget_alerts, group_budget
from bot.services import llm as llm_module

def _current_guard(configure_cost_guard):
    """取出进程级 cost guard 的**实际内容**（它是模块全局，没有公开读取器）。"""

    from bot.services import llm as llm_module

    for name in dir(llm_module):
        if name.endswith("GUARD") or name.endswith("_COST_GUARD"):
            value = getattr(llm_module, name)
            if value is not None and not isinstance(value, type):
                return value
    raise AssertionError("找不到进程级 cost guard 全局")


def _hook_readonly():
    """只读登记钩子；旧树没有 ``alert_enqueuer()`` 时回退读模块全局。

    纯读取，不写产品、不 patch consumer —— 只是为了让把这份回归反投到旧树时
    红在**行为断言**上，而不是红在一个 AttributeError 上。
    """

    getter = getattr(group_budget, "alert_enqueuer", None)
    if callable(getter):
        return getter()
    return getattr(group_budget, "_ALERT_ENQUEUER", None)


_ENV_KEYS = (
    "TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "ADMIN_ID", "SUPER_ADMIN_ID",
)


class StartupCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self._saved_env = dict(os.environ)
        os.environ.clear()          # 真正清空；只 pop 4 个 key 不等于没有本机配置
        self.addCleanup(self._restore_env)
        main_module.budget_alert_consumer_holder.clear()
        main_module._BUDGET_ALERT_BOT[0] = None
        self.addCleanup(main_module.budget_alert_consumer_holder.clear)
        # 进程级成本护栏是模块全局。本文件**直接**驱动初始化器、不走真正的关停
        # 路径（成功装配后它本来就该留着，由关停去摘），所以这里必须自己还原
        # 进入时的原值：否则一次成功用例留下的护栏会带着本用例已经删掉的临时库
        # 污染后续套件（真实 main 的还原由 tests/test_alert_main_shutdown.py 与
        # tests/test_alert_main_failure_cleanup.py 覆盖）。
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
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    def _settings(self, super_admin_id: int = 4242) -> Settings:
        return Settings(_env_file=None, super_admin_id=super_admin_id)  # type: ignore[call-arg]

    async def _initialize(self, settings: Settings):
        with patch.object(
            main_module, "_prefetch_model_context_metadata", AsyncMock()
        ), patch.object(main_module, "configure_logging", lambda *a, **k: None):
            return await main_module._initialize_runtime_services(
                settings=settings, session_factory=self.session_factory
            )

    def _live_consumer_tasks(self) -> list:
        return [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and getattr(task.get_coro(), "__name__", "") == "_run"
        ]

    # ------------------------------------------------------------------
    async def test_normal_startup_returns_a_real_triple(self) -> None:
        result = await self._initialize(self._settings())
        try:
            self.assertIsInstance(result, tuple, type(result))
            self.assertEqual(len(result), 3)
            runtime_config, llm, memory = result
            self.assertIsNotNone(runtime_config)
            self.assertIsNotNone(llm)
            self.assertIsNotNone(memory)
            # 消费者真的装了并且跑着。
            self.assertEqual(len(main_module.budget_alert_consumer_holder), 1)
            consumer = main_module.budget_alert_consumer_holder[0]
            self.assertIsNotNone(consumer._task)
            self.assertEqual(consumer._recipient_id, "4242")
            # 阳性对照：本次装配**确实**换上了进程级成本护栏（成功启动把它留给
            # 关停去摘；摘除本身由 test_alert_main_shutdown.py 覆盖）。
            self.assertIsNot(
                llm_module.cost_guard_factory(), self._guard_before,
                "成功启动没有装上进程级成本护栏",
            )
            self.assertIsNotNone(llm_module.cost_guard_factory())
        finally:
            for consumer in list(main_module.budget_alert_consumer_holder):
                await consumer.stop()
            main_module.budget_alert_consumer_holder.clear()

    async def test_failure_after_startup_cleans_up_only_this_run(self) -> None:
        boom = RuntimeError("injected during runtime config initialize")
        holder = main_module.budget_alert_consumer_holder

        # 先放一个"此前成功启动留下"的消费者 + 它的登记钩子，验证不被误停。
        keeper = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=lambda *_: True, recipient="9", owner="keeper"
        )
        holder.append(keeper)
        keeper.start()
        # "此前成功启动"的登记钩子：它属于 keeper 那次成功，不该被本次失败清掉。
        marker_hook = object()
        group_budget.configure_alert_enqueuer(marker_hook)
        hook_before = _hook_readonly()
        self.assertIs(hook_before, marker_hook)

        initialize = AsyncMock(side_effect=boom)
        with patch.object(
            main_module, "_prefetch_model_context_metadata", AsyncMock()
        ), patch.object(main_module, "configure_logging", lambda *a, **k: None), \
             patch.object(
                 main_module.RuntimeConfigManager, "initialize", initialize
             ):
            with self.assertRaises(RuntimeError) as caught:
                await self._initialize(self._settings())

        self.assertIs(caught.exception, boom, "原异常必须继续抛出")
        self.assertEqual(initialize.await_count, 1, "注入点没被走到")
        self.assertIn(keeper, holder, "既有成功消费者被误停了")
        self.assertIsNotNone(
            keeper._task, "既有消费者的任务被误停了"
        )
        self.assertFalse(keeper._task.done(), "既有消费者的任务已经结束")
        self.assertIs(
            _hook_readonly(), hook_before,
            "既有登记钩子被无条件清掉了",
        )
        self.assertIs(
            llm_module.cost_guard_factory(), self._guard_before,
            "失败的启动把进程级成本护栏留在一个已经被清理掉的会话工厂上",
        )
        self.assertEqual(
            len(holder), 1, f"本次创建的消费者没被摘除：{holder}"
        )
        async with asyncio.timeout(5):
            while len(self._live_consumer_tasks()) > 1:
                await asyncio.sleep(0)

        await keeper.stop()
        holder.clear()


    async def test_budget_assembly_failure_after_start_still_cleans_up(self) -> None:
        """``shared_service`` 在 consumer.start **之后**抛 → 必须清理。

        这条异常被内层的预算 ``except Exception`` 吞掉并降级成
        UnavailableCostGuard，初始化**正常返回三元组** —— 外层包装器看不到失败，
        所以清理必须发生在那条 except 里。降级本身要保留：清成 fail-open 才是
        真的把护栏关掉了。
        """

        holder = main_module.budget_alert_consumer_holder
        keeper = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=lambda *_: True, recipient="9", owner="keeper"
        )
        holder.append(keeper)
        keeper.start()
        marker_hook = object()
        group_budget.configure_alert_enqueuer(marker_hook)
        baseline = len(holder)
        live_baseline = len(self._live_consumer_tasks())

        def _boom(*args, **kwargs):
            raise RuntimeError("budget shared_service blew up after consumer start")

        with patch.object(
            main_module, "_prefetch_model_context_metadata", AsyncMock()
        ), patch.object(main_module, "configure_logging", lambda *a, **k: None), \
             patch.object(group_budget, "shared_service", _boom):
            result = await self._initialize(self._settings())
            # 降级必须真的生效：optional 拒绝、安全不拦。
            from bot.services.llm import configure_cost_guard
            guard = _current_guard(configure_cost_guard)
            self.assertTrue(
                guard.__class__.__name__ == "UnavailableCostGuard",
                f"降级 guard 类型不对：{guard!r}",
            )
            optional = await guard.admit(
                group_id=1, purpose="optional", stage="main",
                provider="slot-a", model="m", prompt_tokens=1, max_output_tokens=1,
            )
            self.assertFalse(optional.admitted, "降级后 optional 必须 fail-closed")
            safety = await guard.admit(
                group_id=1, purpose="safety", stage="moderation",
                provider="slot-a", model="m", prompt_tokens=1, max_output_tokens=1,
            )
            self.assertTrue(safety.admitted, "降级后安全调用必须照常放行")

        # 降级仍然生效：optional fail-closed、安全可用。
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        self.assertEqual(len(holder), baseline, "本次创建的消费者没被摘除")
        self.assertIs(_hook_readonly(), marker_hook, "登记钩子没被还原")
        async with asyncio.timeout(5):
            while len(self._live_consumer_tasks()) > live_baseline:
                await asyncio.sleep(0)
        self.assertIsNotNone(keeper._task)
        self.assertFalse(keeper._task.done(), "既有 keeper 被误停了")
        await keeper.stop()
        holder.clear()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
