"""摘要入场/执行期限分离（第二轮 B）：真实门禁与真实 run() 循环的探针。

守住三件事：

1. **有界入场归 queue_wait**：拿许可的等待有上限，超时算 `admission_timeout`（不是执行
   期限超时、不是失败退避）；拿到许可后的模型/重试/fallback 才算执行期限。
2. **许可只被接手一次**：调度器预取的许可交给实际请求任务释放（取消不合作也不提前归还），
   同一路径**不会二次 acquire**（真实门禁 2/2 占用下仍能完成 = 无死锁）。
3. **回复压力不算摘要的入场等待**：压力期间有界唤醒、不判 `queue_expired`、不无端退避，
   压力解除后**不需要新通知**也能自己继续。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import BotConfig
from bot.services.group_summary import (
    GroupSummaryConfig,
    GroupSummaryScheduler,
)
from bot.services.llm import LLMService
from bot.services.request_priority import ExecutionPriority, ReservedCapacityGate
from tests.test_group_summary import FakeLLM, FakeStore, _rows


def _chat_resp(text: str = "摘要正文") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=[]))],
        usage=None,
    )


class _BlockingGate:
    """入场永远超时的门禁替身（只实现调度器用到的接口）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.background_capacity = 2

    async def acquire_permit(self, *, priority, timeout):  # noqa: ANN001
        self.calls += 1
        await asyncio.sleep(min(0.05, timeout))
        raise TimeoutError("no slot")


class _RecordingPermit:
    def __init__(self) -> None:
        self.consumed = False
        self.released = False

    def consume(self):  # noqa: ANN201
        self.consumed = True
        return self

    def release(self) -> None:
        self.released = True

    def release_unconsumed(self) -> None:
        if not self.consumed:
            self.released = True


class _GateStub:
    def __init__(self, permit) -> None:  # noqa: ANN001
        self.permit = permit
        self.background_capacity = 2

    async def acquire_permit(self, *, priority, timeout):  # noqa: ANN001
        return self.permit


class _PermitAwareLLM:
    def __init__(self, permit) -> None:  # noqa: ANN001
        self.permit = permit
        self.calls = 0

    async def background_summary_completion(self, messages, **kwargs):  # noqa: ANN003
        self.calls += 1
        taken = kwargs.get("permit")
        if taken is not None:
            owned = taken.consume()
            if owned is not None:
                owned.release()
        return "摘要正文：讨论了显卡行情与装机建议，结论是等下一波。"


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_wait_is_bounded_and_is_not_an_execution_timeout(self) -> None:
        store = FakeStore({1: _rows(1, 600)})
        gate = _BlockingGate()
        cfg = GroupSummaryConfig(
            enabled=True,
            queue_wait_seconds=0.2,
            min_refresh_seconds=0.0,
            deadline_seconds=5.0,
        )
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=store,
            config_provider=lambda: cfg,
            slot_waiter=None,
            gate=gate,
        )
        scheduler.notify(1)

        outcome = await scheduler.run_group(1, cfg)

        self.assertEqual(outcome, "admission_timeout")
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["admission_timeout_total"], 1)
        self.assertEqual(snapshot["deadline_exceeded_total"], 0)
        self.assertEqual(snapshot["failure_total"], 0)
        # 排回队尾稍后再来（有界退避），不是丢弃
        self.assertIn(1, scheduler._pending)
        self.assertIsNotNone(scheduler._next_wake_in(cfg))

    async def test_permit_is_consumed_once_and_unconsumed_release_is_safe(self) -> None:
        permit = _RecordingPermit()
        rows = _rows(1, 600)
        store = FakeStore({1: rows})
        llm = _PermitAwareLLM(permit)
        cfg = GroupSummaryConfig(enabled=True, min_refresh_seconds=0.0)
        scheduler = GroupSummaryScheduler(
            llm=llm,
            store=store,
            config_provider=lambda: cfg,
            slot_waiter=None,
            gate=_GateStub(permit),
        )
        scheduler.notify(1)

        outcome = await scheduler.run_group(1, cfg)

        self.assertEqual(outcome, "published")
        self.assertTrue(permit.consumed)
        self.assertTrue(permit.released)

    async def test_permit_not_taken_by_the_model_is_released_by_the_scheduler(self) -> None:
        permit = _RecordingPermit()
        store = FakeStore({1: _rows(1, 600)})
        cfg = GroupSummaryConfig(enabled=True, min_refresh_seconds=0.0)
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),  # 不接受 permit（测试替身）
            store=store,
            config_provider=lambda: cfg,
            slot_waiter=None,
            gate=_GateStub(permit),
        )
        scheduler.notify(1)

        outcome = await scheduler.run_group(1, cfg)

        self.assertEqual(outcome, "published")
        self.assertFalse(permit.consumed)
        self.assertTrue(permit.released)

    async def test_real_gate_transfer_does_not_deadlock_with_all_slots_taken(self) -> None:
        """真实门禁：背景 2/2 全占，把其中一张交给模型调用也必须能完成（不二次 acquire）。"""

        gate = ReservedCapacityGate(
            total_capacity=8,
            noncritical_capacity=7,
            normal_capacity=4,
            background_capacity=2,
        )
        first = await gate.acquire_permit(
            priority=ExecutionPriority.BACKGROUND, timeout=1.0
        )
        second = await gate.acquire_permit(
            priority=ExecutionPriority.BACKGROUND, timeout=1.0
        )
        model = BotConfig().main_model
        llm = LLMService(model, model, compress=model)
        try:
            with patch(
                "bot.services.llm.litellm.acompletion",
                new=AsyncMock(return_value=_chat_resp("摘要")),
            ), patch.object(llm, "chat_configuration_issue", return_value=""):
                with patch("bot.services.llm._LLM_PRIORITY_GATE", gate):
                    text = await asyncio.wait_for(
                        llm.background_summary_completion(
                            [{"role": "user", "content": "x"}],
                            permit=second,
                        ),
                        timeout=5.0,
                    )
            self.assertTrue(text)
            # 请求任务接手并释放了自己那张；第一张仍在手里。
            self.assertEqual(gate.snapshot()["active_background"], 1)
        finally:
            first.release_unconsumed()
            second.release_unconsumed()
        self.assertEqual(gate.snapshot()["active_background"], 0)

    async def test_two_concurrent_summaries_through_the_real_gate(self) -> None:
        gate = ReservedCapacityGate(
            total_capacity=8,
            noncritical_capacity=7,
            normal_capacity=4,
            background_capacity=2,
        )
        store = FakeStore({1: _rows(1, 600), 2: _rows(2, 600)})
        cfg = GroupSummaryConfig(
            enabled=True, min_refresh_seconds=0.0, global_concurrency=8
        )
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=store,
            config_provider=lambda: cfg,
            slot_waiter=None,
            gate=gate,
            background_capacity=lambda: gate.background_capacity,
        )
        scheduler.notify(1)
        scheduler.notify(2)

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                scheduler.run_group(1, cfg),
                scheduler.run_group(2, cfg),
            ),
            timeout=5.0,
        )

        self.assertEqual(sorted(outcomes), ["published", "published"])
        self.assertEqual(scheduler.snapshot()["admission_timeout_total"], 0)
        self.assertEqual(gate.snapshot()["active_background"], 0)
        self.assertEqual(scheduler._effective_concurrency(cfg), 2)


class ReplyPressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_pressure_never_expires_the_queue_and_resumes_alone(self) -> None:
        """真实 run() 探针：只 notify 一次，压力超过 queue_wait，之后不再发消息也要跑。"""

        store = FakeStore({1: _rows(1, 600)})
        llm = FakeLLM()
        pressure = {"on": True}

        async def _release_pressure() -> None:
            await asyncio.sleep(0.35)
            pressure["on"] = False

        cfg = GroupSummaryConfig(
            enabled=True,
            queue_wait_seconds=0.1,  # 压力时间明显超过它
            min_refresh_seconds=0.0,
            failure_backoff_seconds=2.0,  # retry_poll = 1.0s（有界休眠）
        )
        scheduler = GroupSummaryScheduler(
            llm=llm,
            store=store,
            config_provider=lambda: cfg,
            slot_waiter=lambda: pressure["on"],
        )
        scheduler.notify(1)
        run_task = asyncio.create_task(scheduler.run())
        release_task = asyncio.create_task(_release_pressure())
        try:
            deadline = asyncio.get_running_loop().time() + 6.0
            while not llm.calls and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
        finally:
            await release_task
            scheduler._closed = True
            scheduler._wake_event.set()
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

        self.assertTrue(llm.calls, "压力解除后必须自己继续（不需要新通知）")
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["queue_expired_total"], 0)
        self.assertGreaterEqual(snapshot["skipped_reply_waiting_total"], 1)
        self.assertEqual(snapshot["failure_total"], 0)

    async def test_next_wake_in_is_bounded_while_pending_under_pressure(self) -> None:
        store = FakeStore({1: _rows(1, 600)})
        cfg = GroupSummaryConfig(
            enabled=True, queue_wait_seconds=30.0, failure_backoff_seconds=2.0
        )
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=store,
            config_provider=lambda: cfg,
            slot_waiter=lambda: True,
        )
        scheduler.notify(1)

        self.assertIsNone(scheduler._claim_next(cfg))

        wake_in = scheduler._next_wake_in(cfg)
        self.assertIsNotNone(wake_in)
        self.assertLessEqual(wake_in, 1.0)
        # 压力期间不消耗入场预算
        pending = scheduler._pending[1]
        self.assertIsNotNone(pending.dirty_since)
        self.assertEqual(scheduler.snapshot()["queue_expired_total"], 0)


if __name__ == "__main__":
    unittest.main()
