"""补测试缺口 C2-05：两个 ``__main__.py`` 挂载的中间件 + ``/av`` 私聊限流器。

``AUDIT-C`` C2-05 记录这三个**生产必经**模块零测试::

    $ grep -rn "track_group_member_cached" tests/ | wc -l          -> 0
    $ grep -rl "logging_mw" tests/ | wc -l                         -> 0
    $ grep -rn "AVPrivateRateLimiter" tests/ | wc -l -> 0

``bot/__main__.py:548`` ``dispatcher.message.outer_middleware(MemberRosterMiddleware(...))``
与 ``:555`` ``dispatcher.message.middleware(LoggingMiddleware())`` 每条消息都过；
``tests/test_command_entrypoints.py:84`` 那个名字里带 ``rate_limit`` 的用例**从未触发过
限流器**——``/av`` 私聊每人每小时 10 次这道闸门零验证。

本文件**只补用例，不改任何行为**。中间件部分走真 SQLite（真 ``init_db`` + 真
MemberGroupMember 行），限流器部分按报告要求**注入 clock**而不是依赖真实
``time.monotonic()``（它是模块级单例，测试必须自给时钟）。
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import GroupMember
from bot.middlewares.logging_mw import LoggingMiddleware
from bot.middlewares.member_roster import MemberRosterMiddleware
from bot.services.av_query_limits import (
    AVPrivateRateLimiter,
    rate_limit_minutes,
)


def _group_message(user_id: int = 555) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=11,
        chat=SimpleNamespace(id=-100123, type="supergroup", title="群"),
        from_user=SimpleNamespace(
            id=user_id,
            is_bot=False,
            full_name="张三",
            username="zhangsan",
        ),
        sender_chat=None,
        text="在吗",
        caption=None,
        content_type="text",
    )


class MemberRosterMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """``bot/__main__.py:548`` —— 每条群消息都过。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        from bot.services.authz import authorize_group
        from bot.services.patrol import _roster_cache

        _roster_cache.clear()
        async with self.session_factory() as session:
            await authorize_group(session, -100123)
            await session.commit()

    async def asyncTearDown(self) -> None:
        from bot.services.patrol import _roster_cache

        _roster_cache.clear()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def test_handler_is_called_and_the_sender_is_upserted(self) -> None:
        seen: list[object] = []

        async def handler(event, data):
            seen.append(event)
            return "handled"

        middleware = MemberRosterMiddleware(self.session_factory)
        result = await middleware(handler, _group_message(), {})

        self.assertEqual(result, "handled")
        self.assertEqual(len(seen), 1)
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(GroupMember).where(
                        GroupMember.group_id == -100123,
                        GroupMember.user_id == 555,
                    )
                )
            ).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].full_name, "张三")

    async def test_a_roster_failure_never_blocks_the_message(self) -> None:
        """``member_roster.py:44-50`` 的 ``except Exception: log.debug`` 兜底。"""

        async def handler(event, data):
            return "handled"

        def boom(*_args, **_kwargs):
            raise RuntimeError("roster db down")

        middleware = MemberRosterMiddleware(self.session_factory)
        with patch(
            "bot.middlewares.member_roster.track_group_member_cached", new=boom
        ):
            result = await middleware(handler, _group_message(), {})

        self.assertEqual(result, "handled")

    async def test_non_group_traffic_is_left_alone(self) -> None:
        calls: list[object] = []

        async def handler(event, data):
            return "handled"

        middleware = MemberRosterMiddleware(self.session_factory)
        with patch(
            "bot.middlewares.member_roster.track_group_member_cached",
            new=AsyncMock(side_effect=lambda *a: calls.append(a)),
        ):
            private = _group_message()
            private.chat = SimpleNamespace(id=555, type="private", title=None)
            self.assertEqual(await middleware(handler, private, {}), "handled")
        self.assertEqual(calls, [])

    async def test_unchanged_profile_does_not_write_to_the_database_again(self) -> None:
        """``track_group_member_cached`` 的进程级缓存短路（middlewares 里没有这层）。"""

        async def handler(event, data):
            return "handled"

        middleware = MemberRosterMiddleware(self.session_factory)
        await middleware(handler, _group_message(), {})
        with patch(
            "bot.services.patrol.track_group_member",
            new=AsyncMock(side_effect=AssertionError("必须走缓存短路")),
        ) as tracked:
            self.assertEqual(
                await middleware(handler, _group_message(), {}), "handled"
            )
        tracked.assert_not_awaited()

        # 资料变了才重新落库。
        with patch(
            "bot.services.patrol.track_group_member", new=AsyncMock()
        ) as tracked:
            renamed = _group_message()
            renamed.from_user = SimpleNamespace(
                id=555, is_bot=False, full_name="张三改", username="zhangsan"
            )
            await middleware(handler, renamed, {})
        tracked.assert_awaited_once()


class LoggingMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """``bot/__main__.py:555`` —— 每条消息都过。"""

    def _event(self) -> SimpleNamespace:
        event = _group_message()
        event.bot = SimpleNamespace(token="42:TEST")
        return event

    async def test_handler_runs_and_the_log_context_is_cleared(self) -> None:
        from bot.utils.logging_setup import get_log_context

        async def handler(_event, _data):
            self.assertEqual(get_log_context().get("chat_id"), "-100123")
            return "handled"

        with self.assertLogs("bot.middlewares.logging_mw", level=logging.INFO) as logs:
            result = await LoggingMiddleware()(handler, self._event(), {"event_update": SimpleNamespace(update_id=9)})

        self.assertEqual(result, "handled")
        self.assertTrue(any("收到消息" in line for line in logs.output))
        self.assertTrue(any("处理完成" in line for line in logs.output))
        # finally 分支把上下文还原成了 ContextVar 的默认值，事件循环上不留残留。
        self.assertEqual(get_log_context()["chat_id"], "-")

    async def test_flow_id_is_a_stable_crc32_of_the_update_coordinates(self) -> None:
        first = LoggingMiddleware._build_flow_id(9, -100123, 11)
        self.assertEqual(first, LoggingMiddleware._build_flow_id(9, -100123, 11))
        self.assertNotEqual(first, LoggingMiddleware._build_flow_id(10, -100123, 11))
        self.assertEqual(len(first), 6)

    async def test_handler_exception_is_logged_and_re_raised(self) -> None:
        async def handler(_event, _data):
            raise RuntimeError("handler blew up")

        with self.assertLogs("bot.middlewares.logging_mw", level=logging.INFO) as logs:
            with self.assertRaises(RuntimeError):
                await LoggingMiddleware()(handler, self._event(), {})
        self.assertTrue(any("状态=失败" in line for line in logs.output))

    async def test_handler_exception_still_clears_the_log_context(self) -> None:
        from bot.utils.logging_setup import get_log_context

        async def handler(_event, _data):
            raise RuntimeError("handler blew up")

        with self.assertLogs("bot.middlewares.logging_mw", level=logging.INFO):
            with self.assertRaises(RuntimeError):
                await LoggingMiddleware()(handler, self._event(), {})
        self.assertEqual(get_log_context()["flow_id"], "-")


class AVPrivateRateLimiterTests(unittest.TestCase):
    """``/av`` 私聊限流：首次放行 / 第 N+1 次拒绝 / 注入 clock 推进窗口。

    ``AVPrivateRateLimiter`` 是模块级单例的同款实例，**必须**注入 clock，
    不能依赖真实 ``time.monotonic()``（否则用例既慢又不确定）。
    """

    def test_first_ten_calls_pass_and_the_eleventh_is_rejected(self) -> None:
        now = [1000.0]
        limiter = AVPrivateRateLimiter(limit=10, window_seconds=3600.0, clock=lambda: now[0])

        for index in range(10):
            allowed, retry_after = limiter.allow(7)
            self.assertTrue(allowed, index)
            self.assertEqual(retry_after, 0)

        allowed, retry_after = limiter.allow(7)
        self.assertFalse(allowed)
        self.assertEqual(retry_after, 3600)

    def test_the_window_rolls_forward_with_the_injected_clock(self) -> None:
        now = [1000.0]
        limiter = AVPrivateRateLimiter(limit=10, window_seconds=3600.0, clock=lambda: now[0])
        for _ in range(10):
            self.assertTrue(limiter.allow(7)[0])

        now[0] += 1800.0  # 才过一半，最老那条还在窗口里
        self.assertFalse(limiter.allow(7)[0])

        now[0] += 1801.0  # 最老那条已滑出窗口
        self.assertTrue(limiter.allow(7)[0])

    def test_retry_after_counts_down_as_the_window_slides(self) -> None:
        now = [1000.0]
        limiter = AVPrivateRateLimiter(limit=1, window_seconds=600.0, clock=lambda: now[0])
        self.assertTrue(limiter.allow(7)[0])
        self.assertEqual(limiter.allow(7), (False, 600))
        now[0] += 100.0
        self.assertEqual(limiter.allow(7), (False, 500))
        now[0] += 499.0
        self.assertEqual(limiter.allow(7), (False, 1))  # 向上取整，至少 1 秒
        now[0] += 1.0
        self.assertTrue(limiter.allow(7)[0])  # 最老那条滑出窗口

    def test_blocked_queries_do_not_consume_a_slot(self) -> None:
        now = [1000.0]
        limiter = AVPrivateRateLimiter(limit=2, window_seconds=600.0, clock=lambda: now[0])
        self.assertTrue(limiter.allow(7)[0])
        self.assertTrue(limiter.allow(7)[0])
        self.assertTrue(limiter.blocked(7)[0])
        self.assertTrue(limiter.blocked(7)[0])
        now[0] += 601.0
        self.assertFalse(limiter.blocked(7)[0])
        self.assertTrue(limiter.allow(7)[0])

    def test_limits_are_per_user(self) -> None:
        now = [1000.0]
        limiter = AVPrivateRateLimiter(limit=1, window_seconds=600.0, clock=lambda: now[0])
        self.assertTrue(limiter.allow(7)[0])
        self.assertFalse(limiter.allow(7)[0])
        self.assertTrue(limiter.allow(8)[0])

    def test_anonymous_sender_is_never_blocked(self) -> None:
        limiter = AVPrivateRateLimiter(limit=1, window_seconds=600.0)
        for _ in range(50):
            self.assertEqual(limiter.allow(0), (True, 0))
        self.assertEqual(limiter.blocked(0), (False, 0))

    def test_the_idle_user_ceiling_does_not_grow_without_bound(self) -> None:
        """``_drop_idle_users`` 只清**已过期**的队列；活跃用户不受影响。"""

        now = [1000.0]
        limiter = AVPrivateRateLimiter(
            limit=5, window_seconds=60.0, max_users=16, clock=lambda: now[0]
        )
        for user_id in range(1, 201):  # 0 是匿名发送者，不占槽
            limiter.allow(user_id)
        # 同一时刻进来的都是活跃用户，字典按人数增长是预期行为。
        self.assertEqual(len(limiter._hits), 200)

        # 窗口过去之后，它们变成 idle，下一轮新建用户时才会被回收。
        now[0] += 120.0
        limiter.allow(9999)
        self.assertLessEqual(len(limiter._hits), 16)

    def test_rate_limit_minutes_rounds_up_and_never_returns_zero(self) -> None:
        self.assertEqual(rate_limit_minutes(0), 1)
        self.assertEqual(rate_limit_minutes(1), 1)
        self.assertEqual(rate_limit_minutes(60), 1)
        self.assertEqual(rate_limit_minutes(61), 2)
        self.assertEqual(rate_limit_minutes(3600), 60)
