from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message

from bot.utils.command_catalog import bare_command, management_command_names
from bot.utils.telegram import schedule_message_auto_delete_durable

log = logging.getLogger(__name__)

_COMMAND_CLEANUP_SECONDS = 5


class ManagementCommandCleanupMiddleware(BaseMiddleware):
    """Remove an operator's management command line from the group after serving it.

    A support group scrolls: a pile of ``/ban 123``, ``/mute`` and ``/warnings``
    lines is pure noise once the answer is posted.  Only commands that exist for
    operators are touched (``/help``, ``/av`` and other member-facing entries are
    left alone), and only in groups - a DM has no audience to keep clean.

    The deletion goes through the durable queue, so a restart inside the five
    second window still removes the message.  Scheduling happens before the
    handler runs: the handler already holds the message contents in memory, and
    deferring it would let a slow or failing handler leak the line forever.
    """

    def __init__(self, seconds: int = _COMMAND_CLEANUP_SECONDS) -> None:
        self._seconds = max(1, int(seconds))
        self._commands = management_command_names()

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        try:
            await self._schedule_cleanup(event)
        except Exception:
            # Cleanup is cosmetic: never block the command itself on it.
            log.debug("management command cleanup scheduling failed", exc_info=True)
        return await handler(event, data)

    async def _schedule_cleanup(self, message: Message) -> None:
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
