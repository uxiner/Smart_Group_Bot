"""启动资源的装配契约：真的换容量、真的拒绝活动任务、真的可重复调用。

覆盖四件事：

1. **模块级 gate 真用新容量准入**——不是只 assert schema 存了值，而是真的并发跑
   一次，看同时在飞的请求数被新容量卡住。
2. **有活动请求时拒绝重配**——``StartupResourceBusy``，且**不**产生新闸门、不泄漏
   slot（老 gate 的持有者仍然正常释放）。
3. **幂等**：同样的配置再调一次是 no-op，不会新建资源实例。
4. **restart 契约**：``restart_pending`` 只在字段真的变了时出现，且只含字段名。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from bot.services import policy_runtime
from bot.services.runtime_config import RuntimeConfig
from bot.services.startup_resources import (
    StartupResourceBusy,
    apply_startup_resources,
    resource_health_report,
    telegram_session_limits,
)

#: 改完必须还原的模块级全局（这个测试会真的动进程状态）。
_ORIGINAL_CAPACITY: int | None = None


def _config(**resources_overrides: object) -> RuntimeConfig:
    base = RuntimeConfig()
    payload = base.storage_payload()
    resources = {**base.resources.model_dump(), **resources_overrides}
    payload["resources"] = resources
    return RuntimeConfig.model_validate(payload)


async def _hold_slot(gate: Any, release: asyncio.Event) -> None:
    """占住一个**普通**名额直到 ``release`` 被 set。

    显式传 ``NORMAL``：不传的话 ``slot()`` 会取当前上下文优先级（默认 CRITICAL），
    计数就落到 ``active_critical`` 去了，测不到普通容量。
    """

    from bot.services.request_priority import ExecutionPriority

    async with gate.slot(priority=ExecutionPriority.NORMAL, timeout=5.0):
        await release.wait()


async def _wait_for(predicate: Any, *, timeout: float = 2.0) -> None:
    """轮询到条件成立为止（信号量准入不是同步的）。"""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


class StartupAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        global _ORIGINAL_CAPACITY
        from bot.services import llm as llm_module

        _ORIGINAL_CAPACITY = llm_module._LLM_REQUEST_CAPACITY
        policy_runtime.unbind()

    def tearDown(self) -> None:
        from bot.services import llm as llm_module

        # 还原到默认容量，免得影响同进程里后续跑的测试。
        restore = _config(
            llm_request_capacity=_ORIGINAL_CAPACITY or 8,
            llm_request_noncritical_capacity=(_ORIGINAL_CAPACITY or 8) - 1,
            llm_request_normal_capacity=4,
            llm_request_background_capacity=2,
        )
        apply_startup_resources(restore)
        policy_runtime.unbind()

    def test_defaults_are_a_no_op_on_a_fresh_process(self) -> None:
        report = apply_startup_resources(RuntimeConfig())
        self.assertEqual(
            report.changed,
            (),
            "默认配置在干净进程上不应触发任何重配",
        )
        self.assertIn("bot.parse_mode", report.restart_required)
        self.assertIn("resources.llm_request_capacity", report.restart_required)

    def test_applying_the_same_config_twice_is_idempotent(self) -> None:
        config = _config(
            llm_request_capacity=12,
            llm_request_noncritical_capacity=10,
            llm_request_normal_capacity=6,
            llm_request_background_capacity=2,
        )
        first = apply_startup_resources(config)
        self.assertIn("resources.llm_request_capacity", first.changed)
        second = apply_startup_resources(config)
        self.assertEqual(second.changed, (), "重复装配必须是 no-op")
        self.assertIn("resources.llm_request_capacity", resource_health_report()["llm"]["capacity"] and "resources.llm_request_capacity")
        self.assertEqual(resource_health_report()["llm"]["capacity"], 12)

    def test_rejects_a_config_that_would_starve_the_critical_slot(self) -> None:
        with self.assertRaises(Exception):
            # noncritical == total ⇒ 一个关键名额都不剩
            _config(
                llm_request_capacity=8,
                llm_request_noncritical_capacity=8,
                llm_request_normal_capacity=4,
                llm_request_background_capacity=2,
            )
        with self.assertRaises(Exception):
            # background 吃掉 normal 之后不足 2 个回复名额
            _config(
                llm_request_capacity=8,
                llm_request_noncritical_capacity=7,
                llm_request_normal_capacity=3,
                llm_request_background_capacity=2,
            )


class GateAdmissionTests(unittest.IsolatedAsyncioTestCase):
    """真的用新容量跑一次准入，而不是只检查存进去的数。"""

    async def asyncSetUp(self) -> None:
        from bot.services import llm as llm_module

        self._original_capacity = llm_module._LLM_REQUEST_CAPACITY
        policy_runtime.unbind()

    async def asyncTearDown(self) -> None:
        apply_startup_resources(
            _config(
                llm_request_capacity=self._original_capacity,
                llm_request_noncritical_capacity=max(
                    1, self._original_capacity - 1
                ),
                llm_request_normal_capacity=4,
                llm_request_background_capacity=2,
            )
        )
        policy_runtime.unbind()

    async def test_the_module_gate_really_admits_with_the_configured_capacity(self) -> None:
        from bot.services import llm as llm_module
        from bot.services.request_priority import ExecutionPriority

        apply_startup_resources(
            _config(
                # 最小可表达的一档：5 = 1 关键 + 1 非关键 + 2 普通 + 1 背景
                llm_request_capacity=5,
                llm_request_noncritical_capacity=4,
                llm_request_normal_capacity=3,
                llm_request_background_capacity=1,
            )
        )
        gate = llm_module._LLM_PRIORITY_GATE
        self.assertEqual(gate.total_capacity, 5)
        self.assertEqual(gate.noncritical_capacity, 4)
        self.assertEqual(llm_module._LLM_REQUEST_CAPACITY, 5)
        self.assertEqual(llm_module._LLM_REQUEST_SEMAPHORE._value, 5)

        # 三个普通请求同时占满后，第四个在 0.1s 内拿不到名额。
        held: list[Any] = []
        release = asyncio.Event()
        try:
            for _ in range(3):
                held.append(asyncio.ensure_future(_hold_slot(gate, release)))
            await _wait_for(lambda: gate.snapshot()["active_normal"] == 3)
            self.assertEqual(gate.snapshot()["active_normal"], 3)
            # 第 4 个普通请求必须拿不到名额（普通容量就是 3）。
            with self.assertRaises(TimeoutError):
                async with gate.slot(priority=ExecutionPriority.NORMAL, timeout=0.1):
                    pass
        finally:
            release.set()
            await asyncio.gather(*held, return_exceptions=True)
        # 归还后名额必须全部回来——这就是"slot 不泄漏"。
        self.assertEqual(llm_module._LLM_REQUEST_SEMAPHORE._value, 5)
        self.assertEqual(gate.snapshot()["active_normal"], 0)

    async def test_reconfiguring_while_requests_are_in_flight_is_refused(self) -> None:
        from bot.services import llm as llm_module
        from bot.services.request_priority import ExecutionPriority

        config = _config(
            llm_request_capacity=5,
            llm_request_noncritical_capacity=4,
            llm_request_normal_capacity=3,
            llm_request_background_capacity=1,
        )
        apply_startup_resources(config)
        gate = llm_module._LLM_PRIORITY_GATE
        original_gate = gate
        original_semaphore = llm_module._LLM_REQUEST_SEMAPHORE

        release = asyncio.Event()
        holder = asyncio.ensure_future(_hold_slot(gate, release))
        await _wait_for(lambda: gate.snapshot()["active_normal"] == 1)
        self.assertEqual(gate.snapshot()["active_normal"], 1)
        self.assertTrue(gate.in_use())
        try:
            with self.assertRaises(StartupResourceBusy):
                apply_startup_resources(
                    _config(
                        llm_request_capacity=7,
                        llm_request_noncritical_capacity=6,
                        llm_request_normal_capacity=4,
                        llm_request_background_capacity=2,
                    )
                )
        finally:
            release.set()
            await asyncio.gather(holder, return_exceptions=True)
        # 拒绝之后**不能**留下半新半旧的闸门。
        self.assertIs(llm_module._LLM_PRIORITY_GATE, original_gate)
        self.assertIs(llm_module._LLM_REQUEST_SEMAPHORE, original_semaphore)
        self.assertEqual(llm_module._LLM_REQUEST_SEMAPHORE._value, 5)


class RestartContractTests(unittest.TestCase):
    def test_restart_pending_names_only_fields_that_really_changed(self) -> None:
        base = RuntimeConfig()
        unchanged = RuntimeConfig.model_validate(base.storage_payload())
        self.assertEqual(base.restart_changed_paths(unchanged), [])

        payload = base.storage_payload()
        payload["bot"]["parse_mode"] = "Markdown"
        self.assertIn(
            "bot.parse_mode",
            RuntimeConfig.model_validate(payload).restart_changed_paths(base),
        )

        resources = dict(base.resources.model_dump())
        resources["pending_reply_execution_capacity"] = 6
        payload = base.storage_payload()
        payload["resources"] = resources
        self.assertEqual(
            RuntimeConfig.model_validate(payload).restart_changed_paths(base),
            ["resources.pending_reply_execution_capacity"],
        )

    def test_a_hot_field_change_does_not_ask_for_a_restart(self) -> None:
        base = RuntimeConfig()
        payload = base.storage_payload()
        payload["economy"]["tag_price_7d"] = 40
        payload["private_chat"]["per_user_daily_limit"] = 120
        self.assertEqual(
            RuntimeConfig.model_validate(payload).restart_changed_paths(base),
            [],
        )


class TelegramSessionLimitTests(unittest.TestCase):
    def test_session_limits_follow_the_runtime_config(self) -> None:
        policy_runtime.unbind()
        try:
            self.assertEqual(
                telegram_session_limits(),
                {
                    "total_capacity": 64,
                    "noncritical_capacity": 60,
                    "normal_capacity": 44,
                    "privileged_timeout_seconds": 8.0,
                    "critical_admission_timeout_seconds": 1.5,
                    "high_admission_timeout_seconds": 4.0,
                    "normal_admission_timeout_seconds": 15.0,
                },
            )
        finally:
            policy_runtime.unbind()

    def test_a_bound_config_changes_the_session_capacities(self) -> None:
        from bot.config import Settings

        settings = Settings(_env_file=None)
        settings.resources = settings.resources.model_copy(
            update={"telegram_total_capacity": 32, "telegram_normal_capacity": 20}
        )
        policy_runtime.bind(settings)
        try:
            limits = telegram_session_limits()
            self.assertEqual(limits["total_capacity"], 32)
            self.assertEqual(limits["normal_capacity"], 20)
        finally:
            policy_runtime.unbind()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
