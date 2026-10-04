from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from aiogram.utils.web_app import safe_parse_webapp_init_data

log = logging.getLogger(__name__)

MAX_INIT_DATA_AGE_SECONDS = 10 * 60
MAX_INIT_DATA_FUTURE_SECONDS = 60

#: A-06 缓解：initData 是一枚 10 分钟内可无限重放、不可撤销的 bearer。这里给它加
#: 一份服务端预算，让"拿到 header 就能一直打"变成"拿到 header 只能在预算内打"。
#: 面板单次打开会并发打出 ~10 个请求（session / groups / 每个群的 8 类资源），
#: 所以窗口按请求数而不是"一次性"计：一次性 nonce 会让这些并发请求的后 9 个
#: 直接 401，把面板打死。
#: 真正的修法是换发自有会话（见 FIX-p1-upstream.md 的方案段），需要改前端协议。
INIT_DATA_WINDOW_SECONDS = 60.0
INIT_DATA_WINDOW_REQUESTS = 120
INIT_DATA_MAX_REQUESTS = 600
INIT_DATA_LEDGER_MAX_ENTRIES = 4096

#: digest -> (窗口起点, 窗口内已用次数, 窗口内累计次数)
_INIT_DATA_REPLAY_LEDGER: dict[bytes, tuple[float, int, int]] = {}


def _init_data_digest(init_data: str) -> bytes:
    """Never key the ledger by the credential itself."""

    return hashlib.sha256(init_data.encode("utf-8")).digest()


def _forget_expired_init_data(now: float) -> None:
    stale = [
        digest
        for digest, (window_start, _window, _total) in _INIT_DATA_REPLAY_LEDGER.items()
        if now - window_start > MAX_INIT_DATA_AGE_SECONDS
    ]
    for digest in stale:
        _INIT_DATA_REPLAY_LEDGER.pop(digest, None)
    while len(_INIT_DATA_REPLAY_LEDGER) > INIT_DATA_LEDGER_MAX_ENTRIES:
        oldest = min(
            _INIT_DATA_REPLAY_LEDGER.items(),
            key=lambda item: item[1][0],
        )[0]
        _INIT_DATA_REPLAY_LEDGER.pop(oldest, None)


def consume_init_data_budget(init_data: str) -> tuple[int, int]:
    """Charge one request against ``init_data``'s replay budget.

    Returns ``(window_remaining, total_remaining)``, negative once the budget is
    gone.  The caller rejects the request when either goes negative, so exactly
    ``INIT_DATA_WINDOW_REQUESTS`` requests are served per window.  Charges are
    only made for initData that already passed signature and age verification,
    so forged garbage can never exhaust a legitimate user's budget.
    """

    now = time.monotonic()
    digest = _init_data_digest(init_data)
    window_start, window_count, total_count = _INIT_DATA_REPLAY_LEDGER.get(
        digest, (now, 0, 0)
    )
    if now - window_start > INIT_DATA_WINDOW_SECONDS:
        window_start, window_count = now, 0
    window_count += 1
    total_count += 1
    _INIT_DATA_REPLAY_LEDGER[digest] = (window_start, window_count, total_count)
    _forget_expired_init_data(now)
    return (
        INIT_DATA_WINDOW_REQUESTS - window_count,
        INIT_DATA_MAX_REQUESTS - total_count,
    )


def reset_init_data_replay_budget() -> None:
    """Test/运维用：清空重放预算表。"""

    _INIT_DATA_REPLAY_LEDGER.clear()


def _auth_error(
    *,
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> web.HTTPException:
    payload = json.dumps(
        {"ok": False, "error": {"code": code, "message": message}},
        ensure_ascii=False,
    )
    exception_type: type[web.HTTPException]
    if status == 403:
        exception_type = web.HTTPForbidden
    elif status == 429:
        exception_type = web.HTTPTooManyRequests
    else:
        exception_type = web.HTTPUnauthorized
    response_headers = {"Cache-Control": "no-store"}
    if headers:
        response_headers.update(headers)
    return exception_type(
        text=payload,
        content_type="application/json",
        headers=response_headers,
    )


def _auth_timestamp(value: Any) -> float:
    timestamp: float
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.timestamp()
    elif isinstance(value, bool):
        raise ValueError("invalid auth_date")
    elif isinstance(value, (int, float)):
        timestamp = float(value)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("missing auth_date")
        timestamp = float(text)
    if not math.isfinite(timestamp):
        raise ValueError("invalid auth_date")
    return timestamp


async def require_super_admin(
    request: web.Request,
    bot_token: str,
    super_admin_id: int,
) -> Any:
    """Authenticate a fresh Telegram Mini App session and return its user.

    The custom ``tma`` authorization scheme carries Telegram's raw initData.
    Signature verification always happens before trusting its timestamp or user.
    """

    user = await require_authenticated_user(request, bot_token)
    try:
        user_id = int(user.id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _auth_error(
            status=401,
            code="invalid_user",
            message="Telegram 用户信息无效。",
        ) from exc
    if not super_admin_id or user_id != int(super_admin_id):
        raise _auth_error(
            status=403,
            code="super_admin_required",
            message="仅最高管理员可访问此页面。",
        )
    return user


async def require_authenticated_user(
    request: web.Request,
    bot_token: str,
) -> Any:
    """Authenticate a fresh Telegram Mini App session without assigning a role."""
    authorization = str(request.headers.get("Authorization") or "").strip()
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "tma" or not parts[1].strip():
        raise _auth_error(
            status=401,
            code="authorization_required",
            message="请从 Telegram Mini App 重新打开管理页面。",
        )

    try:
        parsed = safe_parse_webapp_init_data(bot_token, parts[1].strip())
        auth_timestamp = _auth_timestamp(parsed.auth_date)
    except (ValueError, AttributeError, TypeError, OverflowError, OSError) as exc:
        raise _auth_error(
            status=401,
            code="invalid_init_data",
            message="Telegram 会话签名或时间校验失败。",
        ) from exc

    age_seconds = datetime.now(timezone.utc).timestamp() - auth_timestamp
    if age_seconds > MAX_INIT_DATA_AGE_SECONDS:
        raise _auth_error(
            status=401,
            code="init_data_expired",
            message="Telegram 会话已过期，请重新打开管理页面。",
        )
    if age_seconds < -MAX_INIT_DATA_FUTURE_SECONDS:
        raise _auth_error(
            status=401,
            code="init_data_from_future",
            message="Telegram 会话时间异常，请重新打开管理页面。",
        )

    user = getattr(parsed, "user", None)
    try:
        int(user.id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise _auth_error(
            status=401,
            code="invalid_user",
            message="Telegram 用户信息无效。",
        ) from exc

    # A-06 缓解：验签 + 时效都过了，才给这枚 initData 记一次重放预算。伪造的
    # initData 在上面就被拒掉了，不会消耗任何人的预算。
    window_remaining, total_remaining = consume_init_data_budget(parts[1].strip())
    if window_remaining < 0 or total_remaining < 0:
        log.warning(
            "mini app initData replay budget exhausted | user=%s window_remaining=%s total_remaining=%s",
            getattr(user, "id", "-"),
            window_remaining,
            total_remaining,
        )
        raise _auth_error(
            status=429,
            code="init_data_replay_budget_exhausted",
            message="本次 Telegram 会话的请求额度已用完，请重新打开管理页面。",
            headers={"Retry-After": str(int(INIT_DATA_WINDOW_SECONDS))},
        )
    return user
