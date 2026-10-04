"""Group member roster maintenance from message traffic.

The Bot API cannot enumerate chat members, so the profile patrol scans the
group_members table instead. This outer middleware upserts a roster row for
every group message sender (cheap: a process-level cache skips the write
unless the visible profile changed). Joins/leaves are tracked by the
membership handlers; retained dialogue history seeds the roster once per
group inside the patrol service.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services.patrol import track_group_member_cached
from bot.services.resource_health import register_resource_health_provider

log = logging.getLogger(__name__)

#: 名册写入是 best-effort 的旁路，不能把消息处理拖住。engine 的 pool_timeout 是
#: 1.0s，所以这里留一点余量：1.5s 内拿不到连接/写锁就直接放弃这一条。
MEMBER_ROSTER_WRITE_TIMEOUT_SECONDS = 1.5
#: 同一个群的失败日志按这个间隔节流，否则一台坏掉的机器能把日志刷爆。
MEMBER_ROSTER_FAILURE_LOG_COOLDOWN_SECONDS = 60.0
MEMBER_ROSTER_LOGGED_CHATS_MAX = 512

_roster_failure_log_at: dict[int, float] = {}
_roster_consecutive_failures = 0


def _member_roster_resource_health_snapshot() -> dict[str, Any]:
    return {
        "consecutive_failures": _roster_consecutive_failures,
        "tracked_chat_count": len(_roster_failure_log_at),
    }


def _note_roster_success() -> None:
    global _roster_consecutive_failures
    if _roster_consecutive_failures:
        _roster_consecutive_failures = 0


def _note_roster_failure(chat_id: int, user_id: object) -> None:
    """Count the failure and log it at WARNING, throttled per chat.

    这条写入之前是 ``except Exception: log.debug(...)``：schema 漂移、字段超长、
    连接池耗尽、写锁超时这类全面性故障一点痕迹都不留，巡逻服务会安静地对着一张
    停止更新的 group_members 表工作。DEBUG 默认根本不输出，等于静默丢弃。
    """

    global _roster_consecutive_failures
    _roster_consecutive_failures += 1
    now = time.monotonic()
    last_logged = _roster_failure_log_at.get(chat_id)
    if (
        last_logged is not None
        and now - last_logged < MEMBER_ROSTER_FAILURE_LOG_COOLDOWN_SECONDS
    ):
        return
    if len(_roster_failure_log_at) >= MEMBER_ROSTER_LOGGED_CHATS_MAX:
        _roster_failure_log_at.clear()
    _roster_failure_log_at[chat_id] = now
    log.warning(
        "member roster tracking failed | chat=%s user=%s consecutive_failures=%d",
        chat_id,
        user_id,
        _roster_consecutive_failures,
        exc_info=True,
    )


def reset_member_roster_failure_state() -> None:
    """Test/运维用：清掉连续失败计数与节流表。"""

    global _roster_consecutive_failures
    _roster_consecutive_failures = 0
    _roster_failure_log_at.clear()


register_resource_health_provider("member_roster", _member_roster_resource_health_snapshot)


class MemberRosterMiddleware(BaseMiddleware):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        chat = getattr(event, "chat", None)
        user = getattr(event, "from_user", None)
        if (
            chat is not None
            and getattr(chat, "type", "") in ("group", "supergroup")
            and user is not None
            and getattr(event, "sender_chat", None) is None
        ):
            try:
                async with asyncio.timeout(MEMBER_ROSTER_WRITE_TIMEOUT_SECONDS):
                    await track_group_member_cached(self.session_factory, chat.id, user)
            except asyncio.CancelledError:
                raise
            except Exception:
                _note_roster_failure(chat.id, getattr(user, "id", "-"))
            else:
                _note_roster_success()
        return await handler(event, data)
