"""父代理验收阻断项的回归用例（A/C/E/G + 重启标签一致性）。

这里每条都对应一个**被父代理探针真实复现**的问题，`verified=true` 意为"问题
复现"而不是"通过"。所以本文件里的断言全部写成**修复后**应有的行为：先看它们在
修复前是红的（见各用例 docstring 里的「修复前」说明），再确认现在是绿的。

覆盖：

* **A** 部分占用也算忙：容量 3 拿掉 1 个（``_value==2``）必须拒绝换闸门；
  覆盖 LLM 主闸门、总信号量、tokenizer、群待回复、TTS 合成/转码/私聊、AV。
* **C** 原子性：任一闸门忙 → 整体拒绝，且**所有**对象 id / 容量 / 绑定快照不变。
* **E** ``restart_pending`` 累积：改热字段不清空、改回实际运行值才消除。
* **G** 忙诊断分列 active / available / waiters，不再把剩余许可当 in_flight。
* **D** restart 标签与真实行为一致：保存后热读侧仍返回**本进程在跑的值**。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from bot.config import Settings
from bot.services import policy_runtime
from bot.services.runtime_config import (
    ResourceSettingsConfig,
    RuntimeConfig,
    applied_restart_values,
    record_applied_restart_values,
)
from bot.services.startup_resources import (
    StartupResourceBusy,
    apply_startup_resources,
    gate_snapshot,
    probe_semaphore,
)


def _resources(**overrides: object) -> ResourceSettingsConfig:
    base = ResourceSettingsConfig()
    return base.model_copy(update=overrides)


def _config(**overrides: object) -> SimpleNamespace:
    return SimpleNamespace(resources=_resources(**overrides))


def _identity() -> dict:
    """当前进程里每个闸门对象的 id + 容量（用来证明"一个都没动"）。"""

    from bot.handlers import group
    from bot.services import av_search, doubao_tts, llm as llm_module, private_tts

    return {
        "llm_gate": id(llm_module._LLM_PRIORITY_GATE),
        "llm_gate_total": llm_module._LLM_PRIORITY_GATE.total_capacity,
        "llm_sem": id(llm_module._LLM_REQUEST_SEMAPHORE),
        "llm_capacity": llm_module._LLM_REQUEST_CAPACITY,
        "tokenizer": id(llm_module._LLM_TOKENIZER_THREAD_SLOTS),
        "tokenizer_capacity": llm_module._LLM_TOKENIZER_THREAD_CAPACITY,
        "pending": id(group._PENDING_REPLY_EXECUTION_SEMAPHORE),
        "pending_capacity": group._PENDING_REPLY_EXECUTION_CAPACITY,
        "tts_syn": id(doubao_tts._TTS_SYNTHESIS_SEMAPHORE),
        "tts_syn_capacity": doubao_tts._TTS_SYNTHESIS_CONCURRENCY,
        "tts_trn": id(doubao_tts._TTS_TRANSCODE_SEMAPHORE),
        "tts_trn_capacity": doubao_tts._TTS_TRANSCODE_CONCURRENCY,
        "tts_priv": id(private_tts._private_tts_semaphore),
        "tts_priv_capacity": private_tts.PRIVATE_TTS_CONCURRENCY,
        "av": id(av_search._AV_QUERY_SEMAPHORE),
        "av_capacity": av_search._AV_QUERY_CONCURRENCY,
    }


async def _hold(semaphore: asyncio.Semaphore, count: int) -> None:
    for _ in range(count):
        await semaphore.acquire()


def _release(semaphore: asyncio.Semaphore, count: int) -> None:
    for _ in range(count):
        semaphore.release()


class _GateFixture(unittest.IsolatedAsyncioTestCase):
    """每个用例前后把进程级闸门恢复到默认值。"""

    async def asyncSetUp(self) -> None:
        apply_startup_resources(_config())
        policy_runtime.unbind()
        self.addCleanup(policy_runtime.unbind)
        self._held: list[tuple[asyncio.Semaphore, int]] = []

    async def asyncTearDown(self) -> None:
        for semaphore, count in self._held:
            _release(semaphore, count)
        self._held.clear()
        apply_startup_resources(_config())

    async def _hold(self, semaphore: asyncio.Semaphore, count: int) -> None:
        await _hold(semaphore, count)
        self._held.append((semaphore, count))


class PartialOccupancyTests(_GateFixture):
    """A：部分占用必须算忙。修复前只看 ``_value <= 0``，3 拿 1 会被放行。"""

    async def test_probe_reports_active_available_waiters_separately(self) -> None:
        """G：诊断分列三个数。修复前把 ``_value`` 标成 in_flight，占满时打印 0。"""

        semaphore = asyncio.Semaphore(3)
        await semaphore.acquire()
        probe = probe_semaphore(name="p", semaphore=semaphore, capacity=3)
        self.assertEqual(
            (probe.active, probe.available, probe.waiters),
            (1, 2, 0),
            "容量 3 拿 1 个必须是 active=1 / available=2",
        )
        self.assertTrue(probe.busy)
        self.assertIn("active=1", probe.describe())
        self.assertIn("available=2", probe.describe())
        self.assertNotIn("in_flight=", probe.describe())
        semaphore.release()

        full = asyncio.Semaphore(2)
        await _hold(full, 2)
        probe = probe_semaphore(name="p", semaphore=full, capacity=2)
        self.assertEqual(
            (probe.active, probe.available), (2, 0), "占满时 active 必须是 2 而不是 0"
        )
        self.assertNotIn("in_flight=0", probe.describe())
        _release(full, 2)

    async def test_av_gate_with_one_of_three_held_refuses_reconfiguration(self) -> None:
        from bot.services import av_search

        before = _identity()
        await self._hold(av_search._AV_QUERY_SEMAPHORE, 1)  # 3 → 2，不是 0

        with self.assertRaises(StartupResourceBusy):
            apply_startup_resources(_config(av_query_concurrency=5))

        self.assertEqual(_identity(), before, "拒绝后不能动任何对象")
        self.assertEqual(av_search._AV_QUERY_CONCURRENCY, 3)
        self.assertEqual(int(av_search._AV_QUERY_SEMAPHORE._value), 2)

    async def test_every_gate_refuses_on_partial_occupancy(self) -> None:
        from bot.handlers import group
        from bot.services import doubao_tts, private_tts
        import bot.services.llm as llm_module

        cases = [
            ("tts_synthesis", doubao_tts._TTS_SYNTHESIS_SEMAPHORE, {"tts_synthesis_concurrency": 5}),
            ("tts_transcode", doubao_tts._TTS_TRANSCODE_SEMAPHORE, {"tts_transcode_concurrency": 5}),
            ("tts_private", private_tts._private_tts_semaphore, {"tts_private_concurrency": 5}),
            ("pending_reply", group._PENDING_REPLY_EXECUTION_SEMAPHORE, {"pending_reply_execution_capacity": 6}),
        ]
        for name, semaphore, override in cases:
            with self.subTest(gate=name):
                before = _identity()
                await self._hold(semaphore, 1)
                with self.assertRaises(StartupResourceBusy):
                    apply_startup_resources(_config(**override))
                self.assertEqual(_identity(), before, f"{name} 被拒绝后对象被动了")
                _release(semaphore, 1)
                self._held.remove((semaphore, 1))

    async def test_llm_gate_with_one_of_eight_held_refuses(self) -> None:
        import bot.services.llm as llm_module
        from bot.services.request_priority import ExecutionPriority

        before = _identity()
        await self._hold(llm_module._LLM_REQUEST_SEMAPHORE, 1)
        # 同时占住 priority gate 的一条车道，模拟"真的有一个请求在跑"
        async with llm_module._LLM_PRIORITY_GATE.slot(
            priority=ExecutionPriority.NORMAL, timeout=5.0
        ):
            with self.assertRaises(StartupResourceBusy):
                apply_startup_resources(
                    _config(
                        llm_request_capacity=16,
                        llm_request_noncritical_capacity=14,
                        llm_request_normal_capacity=8,
                        llm_request_background_capacity=3,
                    )
                )
        self.assertEqual(_identity(), before)

    async def test_a_gate_that_is_not_being_changed_does_not_block(self) -> None:
        """不变式 4：只对"真的要换"的闸门报忙。"""

        from bot.services import av_search

        before = _identity()
        await self._hold(av_search._AV_QUERY_SEMAPHORE, 1)
        # 只改群待回复，AV 保持不动 → 不该因为 AV 忙就拒绝
        report = apply_startup_resources(_config(pending_reply_execution_capacity=6))
        self.assertEqual(report.changed, ("resources.pending_reply_execution_capacity",))
        self.assertEqual(_identity()["av"], before["av"])

    async def test_idle_process_can_reconfigure_and_stays_idempotent(self) -> None:
        report = apply_startup_resources(_config(av_query_concurrency=5))
        self.assertIn("resources.av_query_concurrency", report.changed)
        again = apply_startup_resources(_config(av_query_concurrency=5))
        self.assertEqual(again.changed, (), "同样的配置再调一次必须是 no-op")


class AtomicApplyTests(_GateFixture):
    """C：先探测全部闸门，任一忙就整体拒绝。修复前先换 LLM 再在 AV 上抛。"""

    async def test_refusal_leaves_every_object_and_binding_untouched(self) -> None:
        import bot.services.llm as llm_module
        from bot.services import av_search

        settings = Settings(_env_file=None)
        policy_runtime.bind(settings)
        before = _identity()
        # AV 占满：它是装配顺序里的最后一个，旧实现在此之前已经把 LLM 换掉了
        await self._hold(av_search._AV_QUERY_SEMAPHORE, 3)

        with self.assertRaises(StartupResourceBusy):
            apply_startup_resources(
                _config(
                    llm_request_capacity=16,
                    llm_request_noncritical_capacity=14,
                    llm_request_normal_capacity=8,
                    llm_request_background_capacity=3,
                    llm_tokenizer_concurrency=3,
                    pending_reply_execution_capacity=5,
                    av_query_concurrency=4,
                )
            )

        after = _identity()
        self.assertEqual(after, before, "拒绝后至少有一个对象被替换了")
        self.assertEqual(llm_module._LLM_PRIORITY_GATE.total_capacity, 8)
        self.assertEqual(llm_module._LLM_REQUEST_CAPACITY, 8)
        self.assertIs(
            policy_runtime.bound_settings(), settings, "拒绝不能动绑定快照"
        )

    async def test_late_busy_does_not_leak_a_half_applied_state(self) -> None:
        """"晚失败"：前几个闸门都空闲，只有最后一个忙——仍必须整体不动。"""

        from bot.services import av_search

        before = _identity()
        await self._hold(av_search._AV_QUERY_SEMAPHORE, 3)
        with self.assertRaises(StartupResourceBusy):
            apply_startup_resources(
                _config(
                    llm_request_capacity=16,
                    llm_request_noncritical_capacity=14,
                    llm_request_normal_capacity=8,
                    llm_request_background_capacity=3,
                    pending_reply_execution_capacity=5,
                    av_query_concurrency=4,
                )
            )
        self.assertEqual(_identity(), before)


class RestartPendingAccumulationTests(unittest.TestCase):
    """E：提示要累积，不能被一次热字段保存清空。"""

    def setUp(self) -> None:
        self._base = RuntimeConfig()
        self._applied = record_applied_restart_values(self._base)

    def tearDown(self) -> None:
        record_applied_restart_values(RuntimeConfig())

    def _bumped(self, **extra: object) -> RuntimeConfig:
        payload = self._base.storage_payload()
        resources = {
            **self._base.resources.model_dump(),
            "llm_request_capacity": 16,
            "llm_request_noncritical_capacity": 14,
            "llm_request_normal_capacity": 8,
            "llm_request_background_capacity": 3,
            **extra,
        }
        payload["resources"] = resources
        return RuntimeConfig.model_validate(payload)

    def test_pending_accumulates_across_saves(self) -> None:
        bumped = self._bumped()
        first = bumped.restart_pending_paths(self._applied)
        self.assertIn("resources.llm_request_capacity", first)

        # 只改热字段的第二次保存
        hot_only = self._bumped(moderation_throttle_burst=4)
        second = hot_only.restart_pending_paths(self._applied)
        self.assertEqual(
            second,
            first,
            "只改热字段不该把「还没重启」的提示清掉",
        )

    def test_saving_the_same_restart_value_again_is_not_pending(self) -> None:
        self.assertEqual(self._base.restart_pending_paths(self._applied), [])

    def test_reverting_to_the_running_value_clears_the_hint(self) -> None:
        payload = self._base.storage_payload()
        resources = {**self._base.resources.model_dump(), "moderation_throttle_burst": 4}
        payload["resources"] = resources
        reverted = RuntimeConfig.model_validate(payload)
        self.assertEqual(
            reverted.restart_pending_paths(self._applied),
            [],
            "改回实际运行值就该消除提示（不是单调集合）",
        )

    def test_applied_snapshot_defaults_to_schema_values_on_a_fresh_process(self) -> None:
        record_applied_restart_values(self._base)
        values = applied_restart_values()
        self.assertEqual(values["resources.llm_request_capacity"], 8)
        self.assertEqual(values["bot.parse_mode"], self._base.bot.parse_mode)


class RestartLabelMatchesBehaviourTests(unittest.TestCase):
    """D：``reload_kind`` 必须与真实行为一致，两种都合法，但不能"标 restart 却热生效"。

    本轮采用的划分：

    * **restart** = 在启动时装进某个长寿命对象（准入 gate、Session 的连接池、
      worker 池、有界队列、tokenizer 槽位）。它们读**本进程装配的那一份**。
    * **hot** = 每次操作现读（各级超时、租约、批量、阈值）。它们读期望值，保存
      即生效。

    之前 webhook update timeout / lease / polling timeout 被标成 restart 却是热读，
    于是"保存成功"与"实际生效"两件事对不上号——这才是真问题。
    """

    def tearDown(self) -> None:
        record_applied_restart_values(RuntimeConfig())
        policy_runtime.unbind()

    def _bind_saved(self, **resource_overrides: object) -> RuntimeConfig:
        """模拟：加载默认值 → 运维保存一组新值 → 热更绑定。"""

        record_applied_restart_values(RuntimeConfig())
        base = RuntimeConfig()
        settings = Settings(_env_file=None)
        base.apply_to_settings(settings, apply_prompts=False)
        payload = base.storage_payload()
        payload["resources"] = {**base.resources.model_dump(), **resource_overrides}
        saved = RuntimeConfig.model_validate(payload)
        saved.apply_to_settings(settings, apply_prompts=False)
        policy_runtime.bind(settings)
        return saved

    def test_cold_capacities_do_not_change_before_a_restart(self) -> None:
        from bot.services.startup_resources import telegram_session_limits
        from bot.services.verify_web import webhook_budget

        before = {
            "telegram": telegram_session_limits()["total_capacity"],
            "workers": webhook_budget()["max_concurrent_updates"],
        }
        saved = self._bind_saved(
            telegram_total_capacity=128,
            webhook_max_concurrent_updates=16,
        )
        after = {
            "telegram": telegram_session_limits()["total_capacity"],
            "workers": webhook_budget()["max_concurrent_updates"],
        }
        self.assertEqual(after, before, "冷容量在重启前不能偷偷生效")
        pending = saved.restart_pending_paths(applied_restart_values())
        self.assertIn("resources.telegram_total_capacity", pending)
        self.assertIn("resources.webhook_max_concurrent_updates", pending)

    def test_hot_timeouts_apply_immediately(self) -> None:
        from bot.services.update_delivery import polling_limits
        from bot.services.startup_resources import telegram_session_limits
        from bot.services.verify_web import inbox_limits, webhook_budget

        self._bind_saved(
            telegram_critical_admission_timeout_seconds=0.9,
            telegram_privileged_timeout_seconds=6.0,
            webhook_update_timeout_seconds=200.0,
            webhook_http_response_timeout_seconds=260.0,
            webhook_inbox_lease_seconds=400.0,
            polling_timeout_seconds=45,
        )
        self.assertEqual(webhook_budget()["update_timeout_seconds"], 200.0)
        self.assertEqual(webhook_budget()["http_response_timeout_seconds"], 260.0)
        self.assertEqual(inbox_limits()["lease_seconds"], 400.0)
        self.assertEqual(polling_limits()["timeout_seconds"], 45)
        self.assertEqual(
            telegram_session_limits()["critical_admission_timeout_seconds"], 0.9
        )
        self.assertEqual(telegram_session_limits()["privileged_timeout_seconds"], 6.0)

    def test_the_lease_and_budget_correlation_is_enforced_on_save(self) -> None:
        """热/冷混合之后，关联约束依然必须在**期望值**上成立。"""

        import pydantic

        base = RuntimeConfig()
        payload = base.storage_payload()
        payload["resources"] = {
            **base.resources.model_dump(),
            "webhook_inbox_lease_seconds": 100.0,   # < 最大的端到端预算 150
        }
        with self.assertRaises(pydantic.ValidationError):
            RuntimeConfig.model_validate(payload)

    def test_a_valid_cold_plus_hot_combination_is_accepted(self) -> None:
        base = RuntimeConfig()
        payload = base.storage_payload()
        payload["resources"] = {
            **base.resources.model_dump(),
            "webhook_max_concurrent_updates": 16,
            "telegram_total_capacity": 128,
            "webhook_update_timeout_seconds": 200.0,
            "webhook_http_response_timeout_seconds": 260.0,
            "webhook_inbox_lease_seconds": 400.0,
        }
        combined = RuntimeConfig.model_validate(payload)
        self.assertEqual(combined.resources.webhook_max_concurrent_updates, 16)
        self.assertGreaterEqual(
            combined.resources.webhook_inbox_lease_seconds,
            max(
                combined.resources.webhook_update_timeout_seconds,
                combined.resources.webhook_security_update_timeout_seconds,
            ),
        )


class HealthSnapshotTests(unittest.TestCase):
    def test_snapshot_exposes_active_separately(self) -> None:
        report = gate_snapshot()
        self.assertIn("llm_priority_gate", report)
        self.assertIn("active", report["llm_priority_gate"])
        self.assertIn("available", report["llm_priority_gate"])
        self.assertIn("waiters", report["llm_priority_gate"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
