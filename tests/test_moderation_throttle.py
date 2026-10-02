"""F-021：审核送审整形闸（按群/按成员）与它对判定质量的中立性。

三条最要紧的口径，任何一条坏了都算"拿误伤/漏判换成本"，所以都有专门用例：

1. **正常聊天完全不受影响**：成员在一个窗口里的前 ``burst`` 条消息零延迟送审，
   别人刷屏也不会让他的消息被推迟。
2. **超限只推迟、不降级**：被整形的消息照样走一次**完整的审核模型判定**，
   绝不是"跳过大模型退化成本地正则"；本地确定性命中更是零延迟、零模型调用。
3. **一条都不丢**：每次 ``acquire`` 都会返回（可能等过、也可能因为等待名额已满而
   放弃整形直接送审），没有任何"超限就丢弃"的分支。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.config import ModerationConfig
from bot.db.models import ModerationRule
from bot.services.moderation import ModerationService
from bot.services.moderation_throttle import (
    DEFAULT_BURST,
    DEFAULT_MAX_WAITERS,
    DEFAULT_MAX_WAIT_SECONDS,
    DEFAULT_SPACING_SECONDS,
    ModerationAdmissionGate,
)


class _FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _RecordingSleeper:
    """把"睡眠"记下来并推进假时钟，测试跑得飞快。"""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        self.clock.advance(float(seconds))


def _gate(clock: _FakeClock, sleeper) -> ModerationAdmissionGate:
    return ModerationAdmissionGate(clock=clock, sleep=sleeper)


class AdmissionGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_normal_members_first_messages_are_never_delayed(self) -> None:
        clock = _FakeClock()
        sleeper = _RecordingSleeper(clock)
        gate = _gate(clock, sleeper)

        outcomes = [await gate.acquire(-100, 7) for _ in range(DEFAULT_BURST)]

        self.assertEqual(sleeper.calls, [], "正常频率的消息一条都不该等")
        self.assertFalse(any(outcome.shaped for outcome in outcomes))
        self.assertTrue(all(outcome.waited_seconds == 0.0 for outcome in outcomes))

    async def test_the_next_message_in_a_burst_waits_instead_of_being_dropped(
        self,
    ) -> None:
        clock = _FakeClock()
        sleeper = _RecordingSleeper(clock)
        gate = _gate(clock, sleeper)
        for _ in range(DEFAULT_BURST):
            await gate.acquire(-100, 7)

        outcome = await gate.acquire(-100, 7)

        self.assertTrue(outcome.shaped, "超预算的消息要被整形")
        self.assertEqual(sleeper.calls, [DEFAULT_SPACING_SECONDS])
        self.assertAlmostEqual(outcome.waited_seconds, DEFAULT_SPACING_SECONDS)
        # 仍然是一条可以立刻送审的准入（不是拒绝/丢弃）
        self.assertFalse(outcome.bypassed)

    async def test_a_flood_is_spaced_and_every_message_gets_through(self) -> None:
        clock = _FakeClock()
        sleeper = _RecordingSleeper(clock)
        gate = _gate(clock, sleeper)

        outcomes = [await gate.acquire(-100, 7) for _ in range(20)]

        self.assertEqual(len(outcomes), 20, "一条都不能少")
        self.assertTrue(
            all(outcome.waited_seconds <= DEFAULT_MAX_WAIT_SECONDS for outcome in outcomes)
        )
        self.assertGreater(gate.shaped_total, 0, "连发必须真的触发整形")
        # 单条最多等 max_wait：不会为了整形把一条消息无限期拖住
        self.assertLessEqual(gate.max_observed_wait, DEFAULT_MAX_WAIT_SECONDS)

    async def test_the_waiter_cap_keeps_the_update_channel_open(self) -> None:
        """同时等待的审核全局封顶，超出的直接立刻送审（只是不再整形）。"""

        clock = _FakeClock()
        release = asyncio.Event()
        started: list[float] = []

        async def _blocking_sleep(seconds: float) -> None:
            started.append(float(seconds))
            await release.wait()

        gate = ModerationAdmissionGate(clock=clock, sleep=_blocking_sleep)
        for _ in range(DEFAULT_BURST):
            await gate.acquire(-100, 7)

        tasks = [asyncio.create_task(gate.acquire(-100, 7)) for _ in range(6)]
        await asyncio.sleep(0.05)

        self.assertEqual(gate.snapshot()["waiters"], float(DEFAULT_MAX_WAITERS))
        self.assertEqual(len(started), DEFAULT_MAX_WAITERS)
        self.assertEqual(gate.bypassed_total, 6 - DEFAULT_MAX_WAITERS)

        release.set()
        outcomes = await asyncio.gather(*tasks)
        self.assertEqual(len(outcomes), 6, "等待名额满了也不能丢消息")
        self.assertEqual(gate.snapshot()["waiters"], 0.0, "取消/结束后名额必须归还")

    async def test_cancelling_a_waiter_gives_the_slot_back(self) -> None:
        clock = _FakeClock()
        release = asyncio.Event()

        async def _blocking_sleep(seconds: float) -> None:
            await release.wait()

        gate = ModerationAdmissionGate(clock=clock, sleep=_blocking_sleep)
        for _ in range(DEFAULT_BURST):
            await gate.acquire(-100, 7)

        task = asyncio.create_task(gate.acquire(-100, 7))
        await asyncio.sleep(0.05)
        self.assertEqual(gate.snapshot()["waiters"], 1.0)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(gate.snapshot()["waiters"], 0.0)

    async def test_one_members_flood_does_not_delay_anybody_else(self) -> None:
        clock = _FakeClock()
        sleeper = _RecordingSleeper(clock)
        gate = _gate(clock, sleeper)
        for _ in range(DEFAULT_BURST):
            await gate.acquire(-100, 7)
        await gate.acquire(-100, 7)  # 7 号开始被整形
        sleeper.calls.clear()

        other_member = await gate.acquire(-100, 8)
        other_group = await gate.acquire(-200, 7)

        self.assertEqual(sleeper.calls, [], "整形必须按 (群, 成员) 隔离，不能连坐别人")
        self.assertFalse(other_member.shaped)
        self.assertFalse(other_group.shaped)

    async def test_system_paths_without_a_sender_are_never_shaped(self) -> None:
        clock = _FakeClock()
        sleeper = _RecordingSleeper(clock)
        gate = _gate(clock, sleeper)
        for _ in range(DEFAULT_BURST + 5):
            await gate.acquire(-100, 7)
        sleeper.calls.clear()

        no_sender = await gate.acquire(-100, 0)
        no_group = await gate.acquire(0, 7)

        self.assertEqual(sleeper.calls, [])
        self.assertFalse(no_sender.shaped)
        self.assertFalse(no_group.shaped)

    async def test_the_state_map_stays_bounded(self) -> None:
        clock = _FakeClock()
        sleeper = _RecordingSleeper(clock)
        gate = ModerationAdmissionGate(clock=clock, sleep=sleeper, max_keys=32)

        for user_id in range(1, 200):
            clock.advance(30.0)
            await gate.acquire(-100, user_id)

        self.assertLessEqual(gate.snapshot()["tracked_keys"], 32.0)


# --------------------------------------------------------------------------- #
# 与 ModerationService 的集成：整形不许改变判定
# --------------------------------------------------------------------------- #


class _NoAutoflush:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> "_RowsResult":
        return self

    def all(self) -> list[object]:
        return self.rows


class _FakeSession:
    def __init__(self, rules: list[ModerationRule]) -> None:
        self.no_autoflush = _NoAutoflush()
        self.execute = AsyncMock(return_value=_RowsResult(list(rules)))
        self.get = AsyncMock(return_value=None)
        self.commit = AsyncMock()


class ModerationAdmissionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    LLM_RULE = ModerationRule(
        id=7,
        group_id=-100,
        rule_type="llm",
        pattern="No advertising",
        action="ban",
        enabled=True,
    )
    KEYWORD_RULE = ModerationRule(
        id=9,
        group_id=-100,
        rule_type="keyword",
        pattern="加V领取",
        action="delete",
        enabled=True,
    )

    def _setup(self, rules, response: str):
        clock = _FakeClock()
        sleeper = _RecordingSleeper(clock)
        gate = ModerationAdmissionGate(clock=clock, sleep=sleeper)
        llm = SimpleNamespace(moderation=AsyncMock(return_value=response))
        service = ModerationService(
            ModerationConfig(), llm, admission_gate=gate
        )
        return service, llm, gate, sleeper, _FakeSession(rules)

    async def test_a_throttled_message_still_gets_a_full_model_verdict(self) -> None:
        service, llm, gate, sleeper, session = self._setup(
            [self.LLM_RULE],
            '{"violated": true, "rule_id": 7, "reason": "广告", "confidence": 0.95}',
        )
        # 先把这名成员的额度用完，让这次送审真的被整形
        for _ in range(DEFAULT_BURST):
            await gate.acquire(-100, 7)
        sleeper.calls.clear()

        verdict = await service.evaluate(
            session, -100, "加V领取优惠券", sender_id=7
        )

        self.assertEqual(sleeper.calls, [DEFAULT_SPACING_SECONDS], "超限只推迟送审")
        llm.moderation.assert_awaited_once()
        self.assertTrue(verdict.violated)
        self.assertTrue(verdict.conclusive)
        self.assertEqual(verdict.confidence, 0.95)
        self.assertFalse(verdict.deterministic, "走的必须是模型判定，不是本地规则")

    async def test_an_ordinary_member_is_never_delayed(self) -> None:
        service, llm, _gate, sleeper, session = self._setup(
            [self.LLM_RULE], '{"violated": false, "reason": "正常聊天", "confidence": 0.0}'
        )

        verdict = await service.evaluate(session, -100, "今天天气不错", sender_id=7)

        self.assertEqual(sleeper.calls, [], "正常频率完全不受影响")
        llm.moderation.assert_awaited_once()
        self.assertFalse(verdict.violated)
        self.assertTrue(verdict.conclusive)

    async def test_one_members_flood_does_not_delay_another_members_audit(self) -> None:
        service, _llm, gate, sleeper, session = self._setup(
            [self.LLM_RULE], '{"violated": false, "reason": "正常聊天"}'
        )
        for _ in range(DEFAULT_BURST + 2):
            await gate.acquire(-100, 7)
        sleeper.calls.clear()

        await service.evaluate(session, -100, "闲聊", sender_id=8)

        self.assertEqual(sleeper.calls, [])

    async def test_a_local_keyword_hit_is_never_delayed_or_sent_to_the_model(self) -> None:
        service, llm, gate, sleeper, session = self._setup(
            [self.KEYWORD_RULE], '{"violated": false}'
        )
        for _ in range(DEFAULT_BURST + 2):
            await gate.acquire(-100, 7)
        sleeper.calls.clear()

        verdict = await service.evaluate(session, -100, "加V领取优惠券", sender_id=7)

        self.assertTrue(verdict.violated)
        self.assertTrue(verdict.deterministic)
        self.assertEqual(sleeper.calls, [], "确定性命中零延迟，不该排队")
        llm.moderation.assert_not_awaited()

    async def test_a_system_path_is_never_shaped(self) -> None:
        """申诉复核 / 巡检 / 入群筛查（sender_id=0）不参与整形。"""

        service, llm, gate, sleeper, session = self._setup(
            [self.LLM_RULE], '{"violated": false, "reason": "正常聊天"}'
        )
        for _ in range(DEFAULT_BURST + 2):
            await gate.acquire(-100, 7)
        sleeper.calls.clear()

        await service.evaluate(session, -100, "复核这条消息", sender_id=0)

        self.assertEqual(sleeper.calls, [], "非成员触发路径保持原行为")
        llm.moderation.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
