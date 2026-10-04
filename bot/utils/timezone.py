from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:
    SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def to_shanghai_datetime(
    value: datetime,
    *,
    assume_naive_tz: tzinfo | None = SHANGHAI_TZ,
) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=assume_naive_tz or SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ).replace(microsecond=0)


def to_shanghai_naive(
    value: datetime,
    *,
    assume_naive_tz: tzinfo | None = SHANGHAI_TZ,
) -> datetime:
    return to_shanghai_datetime(value, assume_naive_tz=assume_naive_tz).replace(tzinfo=None)


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ).replace(microsecond=0)


def now_shanghai_naive() -> datetime:
    return now_shanghai().replace(tzinfo=None)


def now_shanghai_naive_precise() -> datetime:
    """本地（Asia/Shanghai）朴素时间，**保留微秒**。

    :func:`now_shanghai_naive` 抹掉微秒是为了让"自然日 / 整分"这类语义稳定，
    拿它当幂等键的时钟会让同一秒内的两次操作撞 key（见
    :func:`bot.services.point_shop.purchase_stamp`）。只给"要区分同一秒内的
    先后"的地方用，不要改 :func:`now_shanghai` 的全局语义。
    """

    return datetime.now(SHANGHAI_TZ).replace(tzinfo=None)


def format_shanghai_timestamp(
    value: Any,
    *,
    assume_naive_tz: tzinfo | None = SHANGHAI_TZ,
    default: str = "unknown",
) -> str:
    if isinstance(value, datetime):
        return to_shanghai_datetime(value, assume_naive_tz=assume_naive_tz).strftime("%Y-%m-%d %H:%M:%S")

    text = str(value or "").strip()
    if not text:
        return default

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return text
    return to_shanghai_datetime(parsed, assume_naive_tz=assume_naive_tz).strftime("%Y-%m-%d %H:%M:%S")
