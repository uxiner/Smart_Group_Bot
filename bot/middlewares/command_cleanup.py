from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.services.authz import is_group_admin_authorized, is_super_admin_user_id
from bot.utils.command_catalog import bare_command, management_command_names
from bot.utils.telegram import schedule_message_auto_delete_durable

log = logging.getLogger(__name__)

_COMMAND_CLEANUP_SECONDS = 5


class ManagementCommandCleanupMiddleware(BaseMiddleware):
    """Remove an authorized operator's management command line once it is served.

    A support group scrolls: a pile of ``/ban 123``, ``/mute`` and ``/warnings``
    lines is pure noise once the answer is posted.  Only commands that exist for
    operators are touched (``/help``, ``/av`` and other member-facing entries are
    left alone), and only in groups - a DM has no audience to keep clean.

    Narrow on purpose (F-048): deleting a message is irreversible, so cleanup
    only happens when the sender is **positively confirmed** to be someone whose
    management command the bot actually serves — the configured super admin or a
    durably delegated group admin (exactly the identities
    ``ensure_group_admin_permission`` accepts).  Anyone can type ``/ban 555`` as
    ordinary text, and a Telegram chat administrator who was never delegated
    only gets "权限不足" from the handler, so both keep their message: 宁可漏删，
    不可误删.  No identity lookup runs for messages that do not parse as an
    operator command in the first place.

    The deletion goes through the durable queue, so a restart inside the five
    second window still removes the message.  Scheduling happens before the
    handler runs: the handler already holds the message contents in memory, and
    deferring it would let a slow or failing handler leak the line forever.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        seconds: int = _COMMAND_CLEANUP_SECONDS,
    ) -> None:
        self.session_factory = session_factory
        self._seconds = max(1, int(seconds))
        self._commands = management_command_names()

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        try:
            await self._schedule_cleanup(event, data)
        except Exception:
            # Cleanup is cosmetic: never block the command itself on it.
            log.debug("management command cleanup scheduling failed", exc_info=True)
        return await handler(event, data)

    async def _sender_is_operator(
        self,
        data: dict[str, Any],
        *,
        chat_id: int,
        user_id: int,
    ) -> bool:
        """True only when this sender is *confirmed* to run management commands."""

        settings = data.get("settings")
        if settings is not None and is_super_admin_user_id(user_id, settings):
            return True

        if self.session_factory is None:
            # Without the delegated-admin lookup we can only confirm the super
            # admin; everyone else keeps their message (漏删 > 误删).
            log.info(
                "management command cleanup cannot confirm an operator without a "
                "session factory; message kept | chat=%s user=%s",
                chat_id,
                user_id,
            )
            return False

        try:
            async with self.session_factory() as session:
                authorized = await is_group_admin_authorized(
                    session,
                    int(chat_id),
                    int(user_id),
                )
                await session.commit()
        except Exception:
            # Never delete on an unverifiable permission lookup.
            log.warning(
                "management command cleanup authorization lookup failed; "
                "message kept | chat=%s user=%s",
                chat_id,
                user_id,
                exc_info=True,
            )
            return False
        return bool(authorized)

    async def _schedule_cleanup(
        self,
        message: Message,
        data: dict[str, Any],
    ) -> None:
        chat = getattr(message, "chat", None)
        if chat is None or getattr(chat, "type", "") not in ("group", "supergroup"):
            return
        sender = getattr(message, "from_user", None)
        if sender is None or getattr(sender, "is_bot", False):
            return
        message_id = int(getattr(message, "message_id", 0) or 0)
        text = (getattr(message, "text", None) or "").lstrip()
        command = bare_command(text)
        if message_id <= 0 or not command or command not in self._commands:
            return
        if not await self._sender_is_operator(
            data,
            chat_id=int(chat.id),
            user_id=int(sender.id),
        ):
            log.info(
                "management command cleanup skipped for unconfirmed sender | "
                "chat=%s message=%s user=%s command=/%s",
                chat.id,
                message_id,
                sender.id,
                command,
            )
            return

        accepted = await schedule_message_auto_delete_durable(message, self._seconds)
        log.info(
            "management command cleanup | chat=%s message=%s command=/%s "
            "auto_delete=%ss scheduled=%s",
            chat.id,
            message_id,
            command,
            self._seconds,
            accepted,
        )
