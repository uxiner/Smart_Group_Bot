"""Private operational alerts to the configured super administrator.

Failure handling in the enforcement paths must never be silent, and it must not
turn one database hiccup into a flood of private messages either. Alerts are
therefore deduplicated per ``kind`` with a cooldown, and every suppression or
delivery failure still leaves a log line — an alert that was not sent is never
invisible.

The alert text is assembled only from structured fields (ids, counters,
exception class names), never from member-supplied text, and every dynamic value
is HTML-escaped before it is sent.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

#: One alert per ``kind`` per window. Deliberately coarse: a database outage
#: fails an enforcement lookup for *every* message, and a private message per
#: message would be worse than useless. The per-occurrence detail stays in the
#: caller's log line.
ALERT_COOLDOWN_SECONDS = 300.0
#: Hard ceiling on how long an alert may delay the update that triggered it.
ALERT_TIMEOUT_SECONDS = 3.0

_LAST_ALERT_AT: dict[str, float] = {}


def reset_alert_cooldowns() -> None:
    """Test hook: forget every cooldown so the next alert is sent immediately."""

    _LAST_ALERT_AT.clear()


def alert_cooldown_remaining(kind: str, *, now: float | None = None) -> float:
    """Seconds until ``kind`` may alert again (0.0 = free to send)."""

    moment = time.monotonic() if now is None else float(now)
    last = _LAST_ALERT_AT.get(str(kind))
    if last is None:
        return 0.0
    return max(0.0, ALERT_COOLDOWN_SECONDS - (moment - last))


async def alert_super_admin(
    bot: Any,
    settings: Any,
    *,
    kind: str,
    summary: str,
    fields: dict[str, Any] | None = None,
) -> bool:
    """Private-message the super administrator; never raises.

    Returns True only when Telegram accepted the message. Every other outcome
    (no admin configured, no bot, cooldown, timeout, delivery error) is logged
    and reported as False so the caller can carry on: an alert must never break
    the enforcement path it is describing.
    """

    detail = " ".join(str(summary or "").split())
    rendered_fields = " ".join(
        f"{name}={value}" for name, value in (fields or {}).items()
    )
    context = f"{detail} {rendered_fields}".strip()
    admin_id = int(getattr(settings, "super_admin_id", 0) or 0)
    if admin_id <= 0:
        log.warning("ops alert skipped: no super_admin_id | kind=%s %s", kind, context)
        return False
    send = getattr(bot, "send_message", None)
    if not callable(send):
        log.warning(
            "ops alert skipped: bot cannot send messages | kind=%s %s",
            kind,
            context,
        )
        return False

    remaining = alert_cooldown_remaining(kind)
    if remaining > 0:
        log.warning(
            "ops alert suppressed by cooldown | kind=%s remaining=%.0fs %s",
            kind,
            remaining,
            context,
        )
        return False
    _LAST_ALERT_AT[str(kind)] = time.monotonic()

    lines = [f"⚠️ <b>{html.escape(str(kind))}</b>", html.escape(detail)]
    for name, value in (fields or {}).items():
        lines.append(
            f"{html.escape(str(name))}: <code>{html.escape(str(value))}</code>"
        )
    log.error("ops alert | kind=%s %s", kind, context)
    try:
        await asyncio.wait_for(
            send(chat_id=admin_id, text="\n".join(lines), parse_mode="HTML"),
            timeout=ALERT_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("ops alert delivery failed | kind=%s admin=%s", kind, admin_id)
        return False
    log.warning("ops alert delivered | kind=%s admin=%s %s", kind, admin_id, context)
    return True
