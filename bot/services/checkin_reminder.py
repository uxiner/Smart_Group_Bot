"""签到提醒：文案、今日签到人数，以及「同一时段只发一条」的幂等占位。

分工：

- ``render_checkin_reminder``：四段定时提醒的正文（定时任务与按钮回调刷新人数
  都用它，保证文案只有一个来源）；
- ``count_checkins_today``：「今日已签到 N 人」，按 ``member_checkins`` 当天行数
  数（**本地** Asia/Shanghai 自然日，与 ``checkin_date`` 口径一致）；
- ``claim_reminder_slot`` / ``mark_reminder_sent`` / ``release_reminder_slot``：
  ``checkin_reminder_posts`` 上 (group_id, slot_key) 唯一索引的占位协议。

不在这里做的事：判断"现在该不该发"（那是 cron 的事）、真正发送（那是
``bot.tools.checkin_reminder`` 的事）。本模块不碰任何 LLM，也不改签到规则。
"""

from __future__ import annotations

from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import CheckinReminderPost, MemberCheckin
from bot.services.checkin import (
    CHECKIN_BUTTON_TEXT,
    CHECKIN_CALLBACK_DATA,
    local_today,
)

#: 每天四段提醒，取值就是**本地**时段（Asia/Shanghai）。`--slot` 只允许这几个值。
REMINDER_SLOTS: tuple[int, ...] = (9, 12, 15, 18)

#: 每段的开头必须不一样（早上好/中午好/下午好/晚上好）。
SLOT_GREETINGS: dict[int, str] = {
    9: "早上好",
    12: "中午好",
    15: "下午好",
    18: "晚上好",
}

#: 提醒发出 10 分钟后自动删除。走持久删除调度器（写 telegram_delete_jobs），
#: 重启不会丢任务——不许用内存里的 sleep / asyncio 定时器。
REMINDER_AUTO_DELETE_SECONDS = 60 * 10


def normalize_slot(slot: object) -> int | None:
    """把时段收敛成 9/12/15/18；非法（含 None、"abc"）返回 None。"""

    try:
        value = int(slot)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value in REMINDER_SLOTS else None


def slot_key(day: date, slot: int) -> str:
    """幂等键：本地自然日 + 本地时段，形如 ``2026-10-01:9``。"""

    return f"{day.isoformat()}:{int(slot)}"


def render_checkin_reminder(*, slot: int, checked_in: int) -> str:
    """一条提醒的正文（HTML）。

    必含三件事：一句签到规则（连续第 N 天得 N 分，最高 10 分，断签从 1 分重来、
    已得积分不清零）、今日已签到人数、以及"本条提醒 10 分钟后自动删除"。
    不 @所有人、不堆表情。
    """

    normalized = normalize_slot(slot)
    greeting = SLOT_GREETINGS.get(normalized if normalized is not None else 0, "你好")
    return (
        f"<b>{greeting}，今天的签到提醒</b>\n"
        "连续签到第 N 天得 N 分（最高 10 分）；断签从 1 分重新开始，已得积分不清零。\n"
        f"今日已签到 <b>{max(0, int(checked_in))}</b> 人，点下面的按钮即可签到。\n"
        "本条提醒 10 分钟后自动删除。"
    )


def build_checkin_reminder_keyboard() -> InlineKeyboardMarkup:
    """提醒下方的「✅ 一键签到」按钮。

    ``callback_data`` 是固定常量，**不含任何用户 ID**：点击者身份只由
    ``callback_query.from_user`` 决定。
    """

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CHECKIN_BUTTON_TEXT,
                    callback_data=CHECKIN_CALLBACK_DATA,
                )
            ]
        ]
    )


async def count_checkins_today(
    session: AsyncSession,
    *,
    group_id: int,
    day: date | None = None,
) -> int:
    """本群今天已签到人数（``member_checkins`` 当天行数，本地自然日）。"""

    today = (day or local_today()).isoformat()
    count = (
        await session.execute(
            select(func.count())
            .select_from(MemberCheckin)
            .where(
                MemberCheckin.group_id == int(group_id),
                MemberCheckin.checkin_date == today,
            )
        )
    ).scalar()
    return int(count or 0)


async def claim_reminder_slot(
    session: AsyncSession,
    *,
    group_id: int,
    key: str,
) -> bool:
    """为 (群, slot_key) 占位；True = 这个时段还没发过，可以发。

    占位发生在**发送之前**（``INSERT ... ON CONFLICT DO NOTHING + RETURNING``）：
    已有该行时 SQLite 不返回行 → False。所以 cron 重试、运维手动重跑、两个进程
    同时跑，都只有一个能发出。不在这里 commit，事务边界由调用方决定。
    """

    statement = (
        sqlite_insert(CheckinReminderPost)
        .values(group_id=int(group_id), slot_key=str(key), message_id=0)
        .on_conflict_do_nothing(index_elements=["group_id", "slot_key"])
        .returning(CheckinReminderPost.id)
    )
    claimed = (await session.execute(statement)).scalar_one_or_none()
    return claimed is not None


async def mark_reminder_sent(
    session: AsyncSession,
    *,
    group_id: int,
    key: str,
    message_id: int,
) -> None:
    """发送成功后回填 message_id（按钮回调按它反查时段）。"""

    await session.execute(
        update(CheckinReminderPost)
        .where(
            CheckinReminderPost.group_id == int(group_id),
            CheckinReminderPost.slot_key == str(key),
        )
        .values(message_id=int(message_id))
    )


async def release_reminder_slot(
    session: AsyncSession,
    *,
    group_id: int,
    key: str,
) -> None:
    """发送失败时撤掉占位，让重试还能补发：没发出去 ≠ 已发过。"""

    await session.execute(
        delete(CheckinReminderPost).where(
            CheckinReminderPost.group_id == int(group_id),
            CheckinReminderPost.slot_key == str(key),
        )
    )


async def find_reminder_slot(
    session: AsyncSession,
    *,
    group_id: int,
    message_id: int,
) -> int | None:
    """按 (群, message_id) 反查这条提醒是哪个时段发的；不是提醒消息返回 None。"""

    if int(message_id) <= 0:
        return None
    key = (
        await session.execute(
            select(CheckinReminderPost.slot_key)
            .where(
                CheckinReminderPost.group_id == int(group_id),
                CheckinReminderPost.message_id == int(message_id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if key is None:
        return None
    _day, _separator, slot_text = str(key).partition(":")
    return normalize_slot(slot_text)
