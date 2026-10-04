"""A-08：名册中间件的失败必须可见，且不能拖住消息处理。

``MemberRosterMiddleware`` 注册在 ``DbSessionMiddleware`` **之前**的
``outer_middleware`` 位置，用自己的 session factory，所以每条群消息会多开一个
session / 一个独立写事务；这块写入的异常又被 ``except Exception: log.debug``
整个吞掉。DEBUG 不是默认输出级别，等于：schema 漂移、字段超长、连接池耗尽、
写锁超时这类**全面性**故障一点痕迹都不留，巡逻服务会安静地对着一张停止更新
的 ``group_members`` 表工作。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.middlewares import member_roster
from bot.middlewares.member_roster import MemberRosterMiddleware
from bot.services.resource_health import resource_health_snapshot


def _group_message(chat_id: int = -100) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(id=555, full_name="Member", username="m"),
        sender_chat=None,
    )


class MemberRosterFailureIsVisibleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset = getattr(member_roster, "reset_member_roster_failure_state", None)
        if callable(reset):
            reset()
            self.addCleanup(reset)

    @staticmethod
    def _middleware() -> MemberRosterMiddleware:
        return MemberRosterMiddleware(SimpleNamespace())

    async def test_failure_is_logged_at_warning(self) -> None:
        handler = AsyncMock(return_value="handled")
        event = _group_message()

        with (
            patch.object(
                member_roster,
                "track_group_member_cached",
                new=AsyncMock(side_effect=RuntimeError("no such column: nick_name")),
            ),
            self.assertLogs(member_roster.log.name, level="WARNING") as logs,
        ):
            result = await self._middleware()(handler, event, {})

        self.assertEqual(result, "handled", "名册写入失败绝不能影响消息处理")
        self.assertTrue(
            any("member roster tracking failed" in line for line in logs.output),
            f"失败必须留下 WARNING 证据，实际日志：{logs.output}",
        )

    async def test_failure_counts_feed_resource_health(self) -> None:
        handler = AsyncMock(return_value=None)
        event = _group_message()

        with patch.object(
            member_roster,
            "track_group_member_cached",
            new=AsyncMock(side_effect=RuntimeError("database is locked")),
        ):
            await self._middleware()(handler, event, {})
            await self._middleware()(handler, event, {})

        snapshot = resource_health_snapshot()
        self.assertGreaterEqual(
            int(snapshot["resources"].get("member_roster", {}).get("consecutive_failures", 0)),
            2,
            "连续失败必须能被健康快照看到，否则巡逻面对停更的表毫无察觉",
        )

        with patch.object(
            member_roster, "track_group_member_cached", new=AsyncMock(return_value=None)
        ):
            await self._middleware()(handler, event, {})

        snapshot = resource_health_snapshot()
        self.assertEqual(
            snapshot["resources"].get("member_roster", {}).get("consecutive_failures"),
            0,
        )

    async def test_repeated_failures_are_throttled_per_chat(self) -> None:
        handler = AsyncMock(return_value=None)
        event = _group_message(chat_id=-100)

        with (
            patch.object(
                member_roster,
                "track_group_member_cached",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            self.assertLogs(member_roster.log.name, level="WARNING") as logs,
        ):
            for _ in range(50):
                await self._middleware()(handler, event, {})

        self.assertEqual(
            len(logs.output),
            1,
            f"同一群的失败日志必须按冷却节流，实际打了 {len(logs.output)} 条",
        )

    async def test_slow_write_is_abandoned_instead_of_blocking_the_message(self) -> None:
        started = asyncio.Event()
        observed_timeout: list[float] = []

        async def _hang(*_args: object, **_kwargs: object) -> None:
            started.set()
            await asyncio.sleep(30)

        real_timeout = asyncio.timeout

        def _recording_timeout(delay: float) -> object:
            observed_timeout.append(delay)
            return real_timeout(delay)

        handler = AsyncMock(return_value="handled")
        event = _group_message()

        with (
            patch.object(member_roster, "track_group_member_cached", new=_hang),
            patch.object(
                member_roster.asyncio,
                "timeout",
                new=_recording_timeout,
                create=True,
            ),
            self.assertLogs(member_roster.log.name, level="WARNING"),
        ):
            result = await asyncio.wait_for(
                self._middleware()(handler, event, {}),
                timeout=3.0,
            )
            self.assertTrue(started.is_set())

        self.assertEqual(result, "handled")
        self.assertEqual(
            observed_timeout,
            [member_roster.MEMBER_ROSTER_WRITE_TIMEOUT_SECONDS],
            "这次 best-effort 写必须有短超时，否则写锁最长 30s 期间每条消息都排队",
        )

    async def test_non_group_events_are_untouched(self) -> None:
        handler = AsyncMock(return_value="handled")
        event = _group_message()
        event.chat = SimpleNamespace(id=555, type="private")
        tracked = AsyncMock()

        with patch.object(member_roster, "track_group_member_cached", new=tracked):
            await self._middleware()(handler, event, {})

        tracked.assert_not_awaited()
