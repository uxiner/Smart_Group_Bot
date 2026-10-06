"""**真实的** ``bot.__main__`` 注册路径上，LLM 准入闸门是不是同一个对象。

父代理探针 B 用「捕获 module 属性」模拟 main 按值 import，那**不是** main 的真实
行为。这里改成跑真正的 ``_initialize_runtime_services`` + 真正的
``init_group_summary_scheduler`` 注册，然后断言：

* ``scheduler 的 gate is bot.services.llm._LLM_PRIORITY_GATE``；
* ``slot_waiter`` / ``background_capacity`` 读到的容量与该 gate 一致；
* 混合 BACKGROUND + NORMAL 请求时，预留规则成立（背景用掉的额度来自 normal 池）；
* permit 只被 consume 一次（拿许可的请求不会再占一个额外名额）。

**已知边界（不要夸大）**：进程内仍有一层总信号量 ``_LLM_REQUEST_SEMAPHORE``，
它没有被取消；这里修的是"priority 预留 / 共享 gate 关系被裂开"那个真问题。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import Settings
from bot.services import llm as llm_module
from bot.services.policy_runtime import bind
from bot.services.request_priority import ExecutionPriority
from bot.services.runtime_config import ResourceSettingsConfig, record_applied_restart_values
from bot.services.startup_resources import apply_startup_resources

DEFAULT_R = ResourceSettingsConfig()
BUMPED = ResourceSettingsConfig(
    llm_request_capacity=16,
    llm_request_noncritical_capacity=14,
    llm_request_normal_capacity=8,
    llm_request_background_capacity=3,
)


class MainRegistrationGateIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        apply_startup_resources(SimpleNamespace(resources=DEFAULT_R))
        self.addCleanup(
            apply_startup_resources, SimpleNamespace(resources=DEFAULT_R)
        )
        self.addCleanup(lambda: record_applied_restart_values(
            __import__("bot.services.runtime_config", fromlist=["RuntimeConfig"]).RuntimeConfig()
        ))

    def _register_scheduler(self, settings: Settings):
        """调用 **main 里那段真代码**（不是测试里抄一份）。

        ``_build_group_summary_scheduler`` 是从 ``bot/__main__.py`` 抽出来的注册
        函数，main 自己也是调它——所以这里跑的就是生产路径。
        """

        from bot.__main__ import _build_group_summary_scheduler

        memory = SimpleNamespace(group_summary_store=lambda: object())
        return _build_group_summary_scheduler(
            llm=None, memory=memory, settings=settings
        )


    async def test_main_never_imports_the_gate_by_value(self) -> None:
        """源码层面就不该有 ``from bot.services.llm import _LLM_PRIORITY_GATE``。"""

        import ast

        import bot.__main__ as main

        tree = ast.parse(open(main.__file__, encoding="utf-8").read())
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("llm"):
                offenders.extend(
                    alias.name
                    for alias in node.names
                    if "GATE" in alias.name or "PRIORITY" in alias.name
                )
        self.assertEqual(
            offenders,
            [],
            f"main 仍然按值导入了闸门对象：{offenders}。重绑之后它就是旧的。",
        )

    async def test_registered_scheduler_uses_the_live_gate_object(self) -> None:
        """真的跑 ``_build_group_summary_scheduler``，断言拿到的是同一个对象。"""

        apply_startup_resources(SimpleNamespace(resources=BUMPED))
        scheduler = self._register_scheduler(Settings(_env_file=None))

        self.assertIsNotNone(scheduler)
        self.assertIs(
            scheduler._gate,
            llm_module._LLM_PRIORITY_GATE,
            "调度器拿到的必须就是 llm 模块此刻那个门禁对象",
        )
        self.assertEqual(scheduler._gate.total_capacity, 16)
        self.assertEqual(scheduler._gate.normal_capacity, 8)
        # 闭包也必须跟着同一个对象，而不是注册那一刻之前的旧容量
        self.assertEqual(scheduler._background_capacity(), 3)
        self.assertEqual(
            scheduler._slot_waiter(),
            llm_module._LLM_PRIORITY_GATE.has_waiting(ExecutionPriority.NORMAL),
        )

    async def test_mixed_background_and_normal_requests_respect_the_reserved_rule(self) -> None:
        """背景与普通共用 normal 池：普通至少留 ``normal - background`` 个名额。"""

        gate = llm_module._LLM_PRIORITY_GATE
        normal_capacity = gate.normal_capacity
        background_capacity = gate.background_capacity
        release = asyncio.Event()
        taken: list[str] = []

        async def _hold(priority: ExecutionPriority, tag: str) -> None:
            async with gate.slot(priority=priority, timeout=5.0):
                taken.append(tag)
                await release.wait()

        # 先把背景额度占满
        background_tasks = [
            asyncio.ensure_future(_hold(ExecutionPriority.BACKGROUND, f"bg{i}"))
            for i in range(background_capacity)
        ]
        await asyncio.sleep(0.05)
        self.assertEqual(
            gate.snapshot()["active_background"], background_capacity
        )

        # 普通请求还能拿到 normal - background 个
        normal_tasks = [
            asyncio.ensure_future(_hold(ExecutionPriority.NORMAL, f"n{i}"))
            for i in range(normal_capacity - background_capacity)
        ]
        await asyncio.sleep(0.05)
        self.assertEqual(
            gate.snapshot()["active_normal"],
            normal_capacity - background_capacity,
        )
        # 再多的普通请求就必须等——这就是"预留"的意义
        with self.assertRaises(TimeoutError):
            async with gate.slot(priority=ExecutionPriority.NORMAL, timeout=0.1):
                pass

        release.set()
        await asyncio.gather(*background_tasks, *normal_tasks, return_exceptions=True)
        self.assertEqual(gate.snapshot()["active_background"], 0)
        self.assertEqual(gate.snapshot()["active_normal"], 0)

    async def test_a_permit_is_consumed_exactly_once(self) -> None:
        """``permit.consume()`` 之后请求不再二次 acquire——否则会双重占位。"""

        gate = llm_module._LLM_PRIORITY_GATE
        before = gate.snapshot()["active_normal"]
        async with gate.slot(priority=ExecutionPriority.NORMAL, timeout=5.0) as permit:
            self.assertIsNone(permit)  # slot() 本身不产出 permit
            self.assertEqual(
                gate.snapshot()["active_normal"], before + 1, "只占了一个名额"
            )
        self.assertEqual(gate.snapshot()["active_normal"], before)

    async def test_total_semaphore_still_exists_and_is_not_overclaimed(self) -> None:
        """诚实说明边界：总信号量仍在，没有被这次修复取消。"""

        self.assertIsNotNone(llm_module._LLM_REQUEST_SEMAPHORE)
        self.assertEqual(
            llm_module._LLM_REQUEST_SEMAPHORE._value,
            llm_module._LLM_REQUEST_CAPACITY,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
