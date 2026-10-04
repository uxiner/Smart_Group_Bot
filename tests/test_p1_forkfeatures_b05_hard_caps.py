"""修复批 P1-2 / B-05：内存限流器的「上限」必须真的是硬上限。

复现的原缺陷（``AUDIT-B`` B-05）：

两处同一个模式——字典到达 ``max_keys`` / ``max_users`` 时调用一次清理，然后
**无论清理结果如何都插入新 key**。所以「上限」只是「触发一次清理的机会」，
不是内存上界：

* ``moderation_throttle.ModerationAdmissionGate._state_for``
* ``av_query_limits.AVPrivateRateLimiter._prune``

修法：清理后仍达上限就**不建新桶**，按「放弃整形 / 不计数」处理
（bypass 语义与模块顶部「绝不丢消息 / 绝不拒绝」的承诺一致：守住的是内存，
不是准入）。AV 限流的返回签名是 ``(allowed, retry_after)``，所以超容量时
返回 ``(True, 0)``——放行但**不计**这一枪。
"""

from __future__ import annotations

import asyncio
import unittest

from bot.services.av_query_limits import AVPrivateRateLimiter
from bot.services.moderation_throttle import ModerationAdmissionGate


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ModerationThrottleHardCapTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_dict_never_exceeds_max_keys(self) -> None:
        clock = _Clock()
        gate = ModerationAdmissionGate(
            burst=1,
            spacing_seconds=60.0,
            max_wait_seconds=0.0,
            max_keys=16,
            clock=clock,
            sleep=lambda _delay: asyncio.sleep(0),
        )
        # 500 个**互不相同**的 (群, 人) key，且时钟不动 → 一个也回收不掉。
        for index in range(500):
            clock.now += 0.001
            await gate.acquire(group_id=-1000 - index, user_id=index + 1)

        self.assertLessEqual(
            len(gate._states), 16, "状态字典必须在 max_keys 以内（原来会一路涨到 500）"
        )

    async def test_overflow_bypasses_instead_of_rejecting(self) -> None:
        """超容量时是「立刻送审」，不是拒绝。"""

        clock = _Clock()
        gate = ModerationAdmissionGate(
            burst=1,
            spacing_seconds=60.0,
            max_wait_seconds=30.0,
            max_keys=16,
            clock=clock,
            sleep=lambda _delay: asyncio.sleep(0),
        )
        outcomes = []
        for index in range(40):
            clock.now += 0.001
            outcomes.append(
                await gate.acquire(group_id=-1000 - index, user_id=index + 1)
            )
        overflow = outcomes[20:]
        self.assertTrue(overflow)
        self.assertTrue(
            all(not outcome.delayed for outcome in overflow),
            "超容量后不得再等待整形",
        )
        self.assertGreaterEqual(gate.bypassed_total, 1, "bypass 必须被计数")

    async def test_existing_key_still_works_after_overflow(self) -> None:
        """老 key 继续按 GCRA 整形（容量保护不能误伤已在跟踪的桶）。"""

        clock = _Clock()
        gate = ModerationAdmissionGate(
            burst=1,
            spacing_seconds=60.0,
            max_wait_seconds=30.0,
            max_keys=16,
            clock=clock,
            sleep=lambda _delay: asyncio.sleep(0),
        )
        first = await gate.acquire(group_id=-1, user_id=1)
        self.assertFalse(first.delayed)
        for index in range(40):
            clock.now += 0.001
            await gate.acquire(group_id=-2000 - index, user_id=index + 1)
        second = await gate.acquire(group_id=-1, user_id=1)
        self.assertTrue(
            second.delayed or second.bypassed,
            "已在跟踪的桶必须继续被整形（GCRA 语义不变）",
        )

    async def test_idle_keys_are_reclaimed_before_bypassing(self) -> None:
        """腾得出位置时照旧回收新 key（不无谓 bypass）。"""

        clock = _Clock()
        gate = ModerationAdmissionGate(
            burst=1,
            spacing_seconds=1.0,
            max_wait_seconds=0.0,
            max_keys=16,
            clock=clock,
            sleep=lambda _delay: asyncio.sleep(0),
        )
        for index in range(16):
            clock.now += 0.001
            await gate.acquire(group_id=-1000 - index, user_id=index + 1)
        self.assertEqual(len(gate._states), 16)
        # 时钟走过清理窗口 → 老 key 全部过期
        clock.now += 3600.0
        await gate.acquire(group_id=-9999, user_id=9999)
        self.assertEqual(
            len(gate._states), 1, "过期 key 应被回收，新 key 正常入桶（不需要 bypass）"
        )


class AVRateLimiterHardCapTests(unittest.TestCase):
    def test_user_dict_never_exceeds_max_users(self) -> None:
        clock = _Clock()
        limiter = AVPrivateRateLimiter(limit=10, max_users=16, clock=clock)
        for index in range(500):
            clock.now += 0.001
            limiter.allow(index + 1)
        self.assertLessEqual(
            len(limiter._hits), 16, "限流字典必须在 max_users 以内"
        )

    def test_overflow_allows_without_counting(self) -> None:
        clock = _Clock()
        limiter = AVPrivateRateLimiter(limit=2, max_users=16, clock=clock)
        for index in range(16):
            clock.now += 0.001
            limiter.allow(index + 1)
        # 容量已满：放行，但**不建桶、不计数**
        allowed, retry_after = limiter.allow(9999)
        self.assertTrue(allowed, "超容量不得拒绝用户请求（限流器自身不能挡人）")
        self.assertEqual(retry_after, 0)
        self.assertNotIn(9999, limiter._hits)
        self.assertEqual(len(limiter._hits), 16)

    def test_blocked_also_respects_the_cap(self) -> None:
        clock = _Clock()
        limiter = AVPrivateRateLimiter(limit=2, max_users=16, clock=clock)
        for index in range(16):
            clock.now += 0.001
            limiter.allow(index + 1)
        blocked, retry_after = limiter.blocked(9999)
        self.assertFalse(blocked, "未知用户无从判断额度 → 不限流")
        self.assertEqual(retry_after, 0)

    def test_normal_window_limiting_is_unchanged(self) -> None:
        clock = _Clock()
        limiter = AVPrivateRateLimiter(limit=2, window_seconds=100.0, max_users=16, clock=clock)
        self.assertEqual(limiter.allow(1), (True, 0))
        clock.now += 1.0
        self.assertEqual(limiter.allow(1), (True, 0))
        clock.now += 1.0
        allowed, retry_after = limiter.allow(1)
        self.assertFalse(allowed, "额度用满后必须被限流")
        self.assertGreater(retry_after, 0)
        # 窗口滑过 → 恢复
        clock.now += 200.0
        self.assertEqual(limiter.allow(1), (True, 0))

    def test_idle_users_are_reclaimed_before_bypassing(self) -> None:
        clock = _Clock()
        limiter = AVPrivateRateLimiter(limit=10, window_seconds=100.0, max_users=16, clock=clock)
        for index in range(16):
            clock.now += 0.001
            limiter.allow(index + 1)
        self.assertEqual(len(limiter._hits), 16)
        clock.now += 10_000.0
        self.assertEqual(limiter.allow(9999), (True, 0))
        self.assertEqual(len(limiter._hits), 1, "过期用户应被回收")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
