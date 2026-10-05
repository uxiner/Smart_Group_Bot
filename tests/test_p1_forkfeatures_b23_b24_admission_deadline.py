"""修复批 P1-2 / B-23 + B-24：摘要入场的等待预算与「执行硬超时」名实一致。

复现的原缺陷（``AUDIT-B`` B-23 / B-24）：

* **B-23** ``_run_group_inner`` 的入场等待直接用**满额** ``queue_wait_seconds``，
  而 ``_claim_next`` 已经用 ``dirty_since`` 记过一次入场计时并做过一次过期判定——
  同一份预算花两遍，中间还夹着 ``read_snapshot`` / ``fit_summary_prompt``。
  两个任务都卡在入场等待时，它们**占着** ``_running`` 的执行槽，
  ``_pump`` 的并发判定（``len(self._running) < limit``，limit=2）就让整条流水线停 30s。
* **B-24** ``asyncio.timeout(cfg.deadline_seconds)`` 的块内**只有**
  ``background_summary_completion`` 一个调用；前置的 content_revision / load /
  coverage_intact / pending_count / read_snapshot 与后置的 publish 全在 timeout 之外。
  DB 一卡，任务远超 15s 仍占着执行槽，``_note_model_concurrency`` 的 finally 也不执行。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from bot.services.group_summary import (
    GroupSummaryConfig,
    GroupSummaryScheduler,
)
from tests.test_group_summary import FakeLLM, FakeStore, _rows


class _RecordingGate:
    """记录每次 ``acquire_permit`` 拿到的 timeout。"""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.background_capacity = 2

    async def acquire_permit(self, *, priority, timeout):  # noqa: ANN001
        self.timeouts.append(float(timeout))
        return SimpleNamespace(consume=lambda: None, release=lambda: None, release_unconsumed=lambda: None)


class _SlowStore(FakeStore):
    """前置 DB 读卡住：``content_revision`` 永远不返回。"""

    def __init__(self, rows) -> None:  # noqa: ANN001
        super().__init__({1: rows})
        self.revision_calls = 0

    async def content_revision(self, group_id: int) -> int:  # noqa: ANN001
        self.revision_calls += 1
        await asyncio.sleep(30)
        return 0


class AdmissionBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_budget_is_the_remaining_queue_wait(self) -> None:
        """B-23：入场等待只能用「剩余预算」，不能再来一份满额。"""

        cfg = GroupSummaryConfig(
            enabled=True,
            queue_wait_seconds=30.0,
            min_refresh_seconds=0.0,
            deadline_seconds=60.0,
        )
        gate = _RecordingGate()
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=FakeStore({1: _rows(1, 600)}),
            config_provider=lambda: cfg,
            slot_waiter=None,
            gate=gate,
        )
        # 手工构造一个「已经等了 25 秒」的 pending：dirty_since 是入场计时的起点。
        scheduler.notify(1)
        pending = scheduler._pending[1]
        pending.dirty_since = scheduler._clock() - 25.0

        outcome = await scheduler.run_group(1, cfg)

        self.assertEqual(outcome, "published")
        self.assertEqual(len(gate.timeouts), 1)
        self.assertLessEqual(
            gate.timeouts[0],
            6.0,
            "30s 的 queue_wait 已经用掉 25s，入场等待只能拿剩下的 5s（原来又给了 30s）",
        )
        self.assertGreaterEqual(gate.timeouts[0], 0.05, "下限仍是 50ms")

    async def test_fresh_pending_still_gets_the_full_budget(self) -> None:
        """回归：刚入队的 pending 没消耗过预算，拿到满额。"""

        cfg = GroupSummaryConfig(
            enabled=True,
            queue_wait_seconds=30.0,
            min_refresh_seconds=0.0,
            deadline_seconds=60.0,
        )
        gate = _RecordingGate()
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=FakeStore({1: _rows(1, 600)}),
            config_provider=lambda: cfg,
            slot_waiter=None,
            gate=gate,
        )
        scheduler.notify(1)

        self.assertEqual(await scheduler.run_group(1, cfg), "published")
        self.assertAlmostEqual(gate.timeouts[0], 30.0, delta=0.1)

    async def test_missing_pending_does_not_crash_the_budget(self) -> None:
        """没有 pending 记录时按「未消耗预算」处理（回归：直接调 run_group 的路径）。"""

        cfg = GroupSummaryConfig(
            enabled=True,
            queue_wait_seconds=10.0,
            min_refresh_seconds=0.0,
            deadline_seconds=60.0,
        )
        gate = _RecordingGate()
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=FakeStore({1: _rows(1, 600)}),
            config_provider=lambda: cfg,
            slot_waiter=None,
            gate=gate,
        )
        self.assertEqual(await scheduler.run_group(1, cfg), "published")
        self.assertAlmostEqual(gate.timeouts[0], 10.0, delta=0.1)


class HardDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_covers_the_db_front_matter(self) -> None:
        """B-24：DB 卡住时任务必须在 deadline_seconds 内退出，不能占着执行槽。"""

        cfg = GroupSummaryConfig(
            enabled=True,
            queue_wait_seconds=30.0,
            min_refresh_seconds=0.0,
            deadline_seconds=0.2,
        )
        store = _SlowStore(_rows(1, 600))
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=store,
            config_provider=lambda: cfg,
            slot_waiter=None,
            gate=None,
        )
        scheduler.notify(1)

        outcome = await asyncio.wait_for(scheduler.run_group(1, cfg), timeout=5.0)

        self.assertEqual(outcome, "deadline_exceeded")
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["deadline_exceeded_total"], 1)
        self.assertEqual(snapshot["failure_total"], 1)
        # 执行槽必须已经释放（否则 _pump 会认为并发已满、整条流水线停摆）
        self.assertNotIn(1, scheduler._running)
        self.assertEqual(store.revision_calls, 1)

    async def test_normal_run_still_publishes_within_the_deadline(self) -> None:
        """回归：正常路径不受影响。"""

        cfg = GroupSummaryConfig(
            enabled=True,
            min_refresh_seconds=0.0,
            deadline_seconds=30.0,
        )
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=FakeStore({1: _rows(1, 600)}),
            config_provider=lambda: cfg,
            slot_waiter=None,
            gate=None,
        )
        scheduler.notify(1)

        self.assertEqual(await scheduler.run_group(1, cfg), "published")
        self.assertEqual(scheduler.snapshot()["deadline_exceeded_total"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
