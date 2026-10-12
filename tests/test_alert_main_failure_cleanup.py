"""main() 的三条启动失败臂：create_bot / 身份 bootstrap / warm cache。

真实 ``Settings(_env_file=None)`` + ``os.environ.clear()``、真实 init_db、真实
``_initialize_runtime_services`` 与真实消费者；只隔离 bootstrap 读取、远端模型
元数据预取、日志、dispatcher 以及要注入故障的那一步。**不** mock 初始化函数、
不 mock 消费者本体。

每条臂都断言：注入确实被调用到、**原**异常对象继续传播、engine dispose 当刻
消费者**任务**已结束（不只看 holder —— stop() 之后 ``_task`` 变 None 会遮住
仍在跑的任务）、holder 回到基线、登记钩子与 bot 引用回到基线。

``asyncTearDown`` 另外在临时库被拆掉**之前**释放本次 main 真装上的
``MemoryService``，并把进程级 ``memory_holder`` 还原成进入时的原对象（可能是
``None``，也可能是上游留下的真实服务）：main 的 ``finally`` 只 shutdown 服务、
不还原 holder，泄漏出去的服务绑着已删的临时库，会让后续审核套件走错分支
（Linux 集成里 ``tests/test_group_moderation_confidence.py`` 的 24 例就是这么红的）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot.__main__ as main_module
from bot.config import Settings
from bot.services import budget_alerts, group_budget
from bot.services import llm as llm_module
from bot.services import memory_holder as memory_holder_module

_log = logging.getLogger(__name__)

KEEPER_SENTINEL = object()


def _live_consumer_tasks() -> list:
    """真实的消费者后台任务（``_run`` 协程），不看 ``_task`` 字段。"""

    return [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and getattr(task.get_coro(), "__qualname__", "")
        == "BudgetAlertConsumer._run"
    ]


class MainFailureCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        _DB_PATH[0] = self._db_path
        self.engine, self.session_factory = await init_db_engine()
        self._saved_env = dict(os.environ)
        os.environ.clear()
        self.addCleanup(self._restore_env)
        self._extra_engines = []
        main_module.budget_alert_consumer_holder.clear()
        main_module._BUDGET_ALERT_BOT[0] = None
        self.addCleanup(main_module.budget_alert_consumer_holder.clear)
        self.addCleanup(group_budget.configure_alert_enqueuer, None)
        # 进程级成本护栏是模块全局：本文件跑的是真实 main，必须还原进入时的原值
        # （一次失败启动留下的护栏会带着已删掉的临时库污染后续套件）。
        self._guard_before = llm_module.cost_guard_factory()
        self.addCleanup(llm_module.configure_cost_guard, self._guard_before)
        # 进程级 memory holder 同样是模块全局：本文件跑真实 main，真的会走到
        # ``memory_holder.init(memory)``；而 main 无论正常还是失败退出都只会
        # shutdown 这个服务、**不**把 holder 还原。于是后续套件会拿到一个绑着
        # 已删临时库的 MemoryService（Linux 集成里 test_group_moderation_confidence
        # 的 24 例就是这么红的：holder 非空 ⇒ 走 archive 路径 ⇒ 拿不到合成 message.date）。
        # 捕获进入时的原对象，在临时资源被拆掉**之前**还原它（不是无条件置 None）。
        self._memory_holder_before = memory_holder_module._instance

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
        # 必须在 engine.dispose() 与删除临时库文件**之前**：本次 main 装上的
        # MemoryService 握着的正是本次的会话工厂。
        await self._release_installed_memory_service()
        await self.engine.dispose()
        for extra in self._extra_engines:
            await extra.dispose()          # main 自己 init_db 出来的也要还回去
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _release_installed_memory_service(self) -> None:
        """释放本次 main 真装上的 MemoryService，并还原进入时的 holder 原对象。

        main 的 ``finally`` 已经 shutdown 过一次；这里再兜一次是有界的幂等清理
        （三次故障臂的退出路径不同，也不能假设上游一定走过 finally）。清理本身
        失败只记日志，不能用清理异常盖住真实断言；holder 一定还原成进入时的那个
        对象（可能是 None，也可能是上游留下的真实服务）。
        """

        current = memory_holder_module._instance
        if current is self._memory_holder_before:
            return
        try:
            shutdown = getattr(current, "shutdown", None)
            if current is not None and callable(shutdown):
                try:
                    await asyncio.wait_for(shutdown(), timeout=5.0)
                except Exception as exc:  # noqa: BLE001 - 清理失败不能盖住真实断言
                    _log.warning(
                        "memory service shutdown during test cleanup failed: %r", exc
                    )
        finally:
            memory_holder_module._instance = self._memory_holder_before

    async def _run_main(self, *, create_bot_impl=None, me_side_effect=None,
                        warm_side_effect=None, service_error=None) -> dict:
        """跑真实 main()，只替换要注入故障的那一步与无关的外部设施。"""

        observed = {"disposed": [], "bot_closed": [], "create_bot": 0,
                    "guard_installed": False}
        guard_before = llm_module.cost_guard_factory()

        real_init_db = main_module.init_db

        async def observing_init_db(*args, **kwargs):
            engine, session_factory = await real_init_db(*args, **kwargs)
            self._extra_engines.append(engine)   # main 自己建的也要还回去
            real_dispose = engine.dispose

            async def dispose(*a, **k):
                # 记录 dispose 当刻的真实状态：消费者任务是否已结束、holder 多少。
                observed["disposed"].append({
                    "live_tasks": len(_live_consumer_tasks()),
                    "holder": len(main_module.budget_alert_consumer_holder),
                    # 护栏必须**先于**引擎被摘掉：它握着这个会话工厂。
                    "guard_is_baseline": llm_module.cost_guard_factory() is guard_before,
                    # 阳性对照：这次失败启动**真的**把 MemoryService 装上了进程级
                    # holder（否则 tearDown 里的"还原原对象"就是空转）。
                    "memory_installed": (
                        memory_holder_module._instance is not self._memory_holder_before
                    ),
                })
                return await real_dispose(*a, **k)

            # AsyncEngine.dispose 是只读属性：用代理对象包一层。
            return SimpleNamespace(dispose=dispose), session_factory

        me_mock = AsyncMock(
            side_effect=me_side_effect
        ) if me_side_effect is not None else AsyncMock(
            return_value=SimpleNamespace(
                id=1, username="fixture", full_name="F", first_name="F"
            )
        )
        self.injected = {"me": me_mock}
        bot_obj = SimpleNamespace(
            me=me_mock,
            session=SimpleNamespace(close=self._closing(observed)),
        )

        def _create_bot(_settings):
            observed["create_bot"] += 1
            if create_bot_impl is not None:
                return create_bot_impl(_settings)
            return bot_obj

        dp = _FakeDispatcher()
        from bot import loader as loader_module

        self.observed = observed
        self.me_mock = me_mock

        async def observing_prefetch(*args, **kwargs):
            """阳性对照：真实的初始化已经跑到这里，本次护栏**确实**装上了。

            这一步排在预算护栏装配之后、三个故障注入点之前，所以它看到的是
            "本次装配的护栏"。没有这个对照，最后那句"已还原"在"从头到尾都没装过"
            的实现上也会绿。
            """

            current = llm_module.cost_guard_factory()
            observed["guard_installed"] = (
                current is not None and current is not guard_before
            )
        # 注入 warm 故障的那一臂 side_effect 抛错；其它臂 **转发到真实实现**
        # （正控），不是拿一个什么都不做的 AsyncMock 假装跑过。
        real_warm = main_module.warm_privileged_operator_cache
        if warm_side_effect is not None:
            warm_mock = AsyncMock(side_effect=warm_side_effect)
        else:
            warm_mock = AsyncMock(side_effect=real_warm)
        self.warm_mock = warm_mock
        with patch.object(main_module, "load_bootstrap_settings",
                          lambda: self._settings()), \
             patch.object(main_module, "validate_bootstrap_settings", lambda s: None), \
             patch.object(main_module, "init_db", observing_init_db), \
             patch.object(main_module, "create_bot", _create_bot), \
             patch.object(main_module, "_prefetch_model_context_metadata",
                          observing_prefetch), \
             patch.object(main_module, "configure_logging", lambda *a, **k: None), \
             patch.object(main_module, "log_process_identity", lambda: None), \
             patch.object(main_module, "log_enforcement_switch_state", lambda s: None), \
             patch.object(main_module, "warm_privileged_operator_cache",
                          warm_mock), \
             patch.object(main_module, "_register_update_middlewares", lambda *a, **k: None), \
             patch.object(loader_module, "create_bot", _create_bot), \
             patch.object(main_module, "dp", dp):
            await main_module.main()
        return observed

    def _closing(self, observed=None):
        async def _close():
            sink = observed if observed is not None else self.observed
            sink["bot_closed"].append({
                "live_tasks": len(_live_consumer_tasks()),
                "holder": len(main_module.budget_alert_consumer_holder),
            })
            return None

        return _close

    def _settings(self) -> Settings:
        # main 会**真的** init_db；不指定就会写开发目录下的 data/bot.db，
        # 与本用例的 tearDown 完全脱钩 —— 跨用例共享、泄漏、留副作用。
        return Settings(  # type: ignore[call-arg]
            _env_file=None,
            super_admin_id=4242,
            database_url=f"sqlite+aiosqlite:///{_DB_PATH[0]}",
        )

    async def _install_keeper(self):
        keeper = budget_alerts.BudgetAlertConsumer(
            self.session_factory, sender=lambda *_: True, recipient="9", owner="keeper"
        )
        main_module.budget_alert_consumer_holder.append(keeper)
        keeper.start()
        marker = object()
        group_budget.configure_alert_enqueuer(marker)
        main_module._BUDGET_ALERT_BOT[0] = KEEPER_SENTINEL
        return keeper, marker

    # ------------------------------------------------------------------
    async def test_create_bot_failure_cleans_up(self) -> None:
        keeper, marker = await self._install_keeper()
        baseline = len(_live_consumer_tasks())

        boom = RuntimeError("create_bot exploded")

        def _boom(_settings):
            raise boom

        with self.assertRaises(RuntimeError) as caught:
            await self._run_main(create_bot_impl=_boom)
        self.assertIs(caught.exception, boom, "不是**原**那个异常对象")
        self.assertEqual(self.observed["create_bot"], 1, "create_bot 没被调用到")
        self.assertEqual(
            len(main_module.budget_alert_consumer_holder), 1,
            "holder 没回到 baseline（只剩 keeper）",
        )
        self.assertTrue(self.observed["disposed"], "engine.dispose 没被调用")
        self.assertEqual(self.observed["disposed"][0]["live_tasks"], baseline)
        self.assertTrue(
            self.observed["disposed"][0]["guard_is_baseline"],
            "engine 都 dispose 了，进程级成本护栏还挂在本次的会话工厂上",
        )
        self.assertTrue(
            self.observed["disposed"][0]["memory_installed"],
            "这次失败启动没有真的把 MemoryService 装上进程级 holder",
        )
        async with asyncio.timeout(5):
            while len(_live_consumer_tasks()) > baseline:
                await asyncio.sleep(0)
        self.assertEqual(len(_live_consumer_tasks()), baseline)
        self.assertIs(_hook(), marker)
        self.assertIs(main_module._BUDGET_ALERT_BOT[0], KEEPER_SENTINEL)
        self.assertFalse(keeper._task.done(), "既有 keeper 被误停了")
        self.assertTrue(
            self.observed["guard_installed"],
            "整个失败启动里进程级成本护栏从未被换成本次装配的那个",
        )
        self.assertIs(
            llm_module.cost_guard_factory(), self._guard_before,
            "create_bot 失败时进程级成本护栏没还原",
        )
        await keeper.stop()
        main_module.budget_alert_consumer_holder.clear()

    async def test_identity_bootstrap_failure_cleans_up_before_bot_close(self) -> None:
        keeper, marker = await self._install_keeper()
        baseline = len(_live_consumer_tasks())
        boom = RuntimeError("bot.me exploded")
        with self.assertRaises(RuntimeError) as caught:
            await self._run_main(me_side_effect=boom)
        self.assertIs(caught.exception, boom, "不是**原**那个异常对象")
        self.assertEqual(
            self.me_mock.await_count, 1, "bot.me 注入点的调用次数不对"
        )
        self.assertTrue(self.observed["bot_closed"], "bot.session.close 没被调用")
        self.assertEqual(
            self.observed["bot_closed"][0]["live_tasks"], baseline,
            "bot HTTP 关闭时消费者任务还在跑",
        )
        self.assertTrue(self.observed["disposed"], "engine.dispose 没被调用")
        self.assertEqual(self.observed["disposed"][0]["live_tasks"], baseline)
        await _assert_cleanup(self, keeper, marker, baseline)

    async def test_warm_cache_failure_cleans_up_and_disposes_engine(self) -> None:
        keeper, marker = await self._install_keeper()
        baseline = len(_live_consumer_tasks())
        boom = RuntimeError("warm cache exploded")
        with self.assertRaises(RuntimeError) as caught:
            await self._run_main(warm_side_effect=boom)
        self.assertIs(caught.exception, boom, "不是**原**那个异常对象")
        self.assertEqual(
            self.warm_mock.await_count, 1, "warm 注入点的调用次数不对"
        )
        self.assertTrue(self.observed["disposed"], "warm 失败时 engine 也要 dispose")
        self.assertEqual(self.observed["disposed"][0]["live_tasks"], baseline)
        await _assert_cleanup(self, keeper, marker, baseline)



def _hook():
    getter = getattr(group_budget, "alert_enqueuer", None)
    if callable(getter):
        return getter()
    return getattr(group_budget, "_ALERT_ENQUEUER", None)


async def _assert_cleanup(case, keeper, marker, baseline):
    async with asyncio.timeout(5):
        while len(_live_consumer_tasks()) > baseline:
            await asyncio.sleep(0)
    assert len(_live_consumer_tasks()) == baseline
    assert len(main_module.budget_alert_consumer_holder) == 1
    assert _hook() is marker
    assert main_module._BUDGET_ALERT_BOT[0] is KEEPER_SENTINEL
    assert not keeper._task.done()
    # 阳性对照：装配期确实把护栏换成了本次的那个（否则下面的"已还原"是空绿）。
    assert case.observed["guard_installed"], (
        "整个失败启动里进程级成本护栏从未被换成本次装配的那个"
    )
    assert llm_module.cost_guard_factory() is case._guard_before, (
        "失败清理没有还原进程级成本护栏：它握着 main 自己建的会话工厂，"
        "engine 已经 dispose，之后任何 LLM 调用都会去查一个不存在的库"
    )
    # 阳性对照：这次失败启动确实装上了 MemoryService（还原由 tearDown 负责，
    # 见 tests/test_b13b_rework_v5.py 的同进程顺序回归）。
    assert case.observed["disposed"][0]["memory_installed"], (
        "这次失败启动没有真的把 MemoryService 装上进程级 holder"
    )
    await keeper.stop()
    main_module.budget_alert_consumer_holder.clear()


class _FakeDispatcher(dict):
    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None

        return _noop


async def init_db_engine():
    from bot.db.engine import init_db

    return await init_db(f"sqlite+aiosqlite:///{_DB_PATH[0]}")


_DB_PATH: list[str] = [""]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()