"""签到提醒：文案、今日签到人数 / 已签到名单，以及「同一时段只发一条」的幂等占位。

分工：

- ``render_checkin_reminder``：四段定时提醒的正文（定时任务与按钮回调刷新
  「人数 + 名单」都用它，保证文案只有一个来源）；
- ``render_checkin_roster``：名单那一行（HTML 转义、最多 ``CHECKIN_ROSTER_MAX_NAMES``
  个昵称、超出补「…等 N 人」）；
- ``build_checkin_reminder_keyboard`` / ``shop_deep_link``：提醒下方的两个按钮。
  签到走 callback（``checkin:v1``），商店走 **URL 深链** ``shop_<群号>``——
  菜单发到私聊，不在群里刷屏；
- ``count_checkins_today``：「今日已签到 N 人」，按 ``member_checkins`` 当天行数
  数（**本地** Asia/Shanghai 自然日，与 ``checkin_date`` 口径一致）；
- ``today_checkin_roster``：人数 + 名单一次给全（给渲染函数用，别让两处各查一半）；
- ``claim_reminder_slot`` / ``mark_reminder_sent`` / ``release_reminder_slot``：
  ``checkin_reminder_posts`` 上 (group_id, slot_key) 唯一索引的占位协议。

不在这里做的事：判断"现在该不该发"（那是 cron 的事）、真正发送（那是
``bot.tools.checkin_reminder`` 的事）。本模块不碰任何 LLM，也不改签到规则。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    CheckinReminderPost,
    MemberCheckin,
    TelegramDeleteJob,
)
from bot.services.checkin import (
    CHECKIN_BUTTON_TEXT,
    CHECKIN_CALLBACK_DATA,
    local_today,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

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

#: 提醒里第二个按钮「🛒 积分商店」。是 **URL 深链**，不是 callback：
#: 商店菜单走**私聊**，群里只留一个入口链接，免得菜单把群刷乱。
#: Telegram 不允许机器人主动私聊没聊过天的人，深链是唯一对"没私聊过机器人的人"
#: 也可用的做法（点一下就等于那人自己给机器人发了 /start）。
SHOP_BUTTON_TEXT = "🛒 积分商店"

#: 深链的 start payload：``shop_<群号>``。群号带负号（超级群 -100…），
#: Telegram 允许 payload 里出现 ``-`` 和 ``_``。
#: **与入群验证的 payload（``verify`` / ``verify_n…``）不共用前缀**，两边各自解析，
#: 互不干扰（见 ``parse_shop_start_payload`` 与 join_verification 的解析函数）。
SHOP_START_PAYLOAD_PREFIX = "shop_"

#: 提醒里最多列出的已签到昵称个数；超出的部分折成「…等 N 人」。
CHECKIN_ROSTER_MAX_NAMES = 20

#: Telegram 单条消息长度上限。名单是唯一会随人数增长的部分，渲染时按这个上限
#: 兜底裁剪（昵称里的 ``&``/``<`` 转义后会变长，所以量的是**转义后**的长度）。
TELEGRAM_MESSAGE_MAX_CHARS = 4096

#: 当天一个人都没有时的鼓励句。
CHECKIN_ROSTER_EMPTY_TEXT = "还没有人签到，来抢第一个 ☝️"


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


def render_checkin_roster(*, checked_in: int, names: Sequence[str] = ()) -> str:
    """「已签到（N）：A、B、C」那一行（HTML）。

    规则（紧凑、可复现）：

    - 昵称一律 ``html.escape``：生产里昵称可能是 ``<b>x</b>`` / ``a & b``，
      直接拼进 HTML 会被 Telegram 整条拒掉（can't parse entities），
      提醒就发不出去了；
    - 最多列 ``CHECKIN_ROSTER_MAX_NAMES`` 个，多出来的折成「…等 N 人」
      （N = 没列出来的**人数**）；
    - 括号里的 N 是当天总人数（``checked_in``），与「今日已签到 N 人」同一口径；
    - 名单为空且人数为 0 时给一句鼓励；名单为空但人数 > 0（理论上不会发生，
      比如名字被裁光）时返回空串，宁可不显示也不要写出自相矛盾的一行。
    """

    total = max(0, int(checked_in))
    listed = [str(name) for name in names if str(name or "").strip()]
    total = max(total, len(listed))
    if not listed:
        return "" if total > 0 else CHECKIN_ROSTER_EMPTY_TEXT
    shown = listed[:CHECKIN_ROSTER_MAX_NAMES]
    body = "、".join(escape(name) for name in shown)
    hidden = total - len(shown)
    if hidden > 0:
        body += f"…等 {hidden} 人"
    return f"已签到（{total}）：{body}"


def _compose_checkin_reminder(
    *, greeting: str, checked_in: int, names: Sequence[str]
) -> str:
    """把各段拼成正文；名单为空串时不留空行。"""

    roster_line = render_checkin_roster(checked_in=checked_in, names=names)
    lines = [
        f"<b>{greeting}，今天的签到提醒</b>",
        "连续签到第 N 天得 N 分（最高 10 分）；断签从 1 分重新开始，已得积分不清零。",
        f"今日已签到 <b>{max(0, int(checked_in))}</b> 人，点下面的按钮即可签到。",
    ]
    if roster_line:
        lines.append(roster_line)
    lines.append("本条提醒 10 分钟后自动删除。")
    return "\n".join(lines)


def render_checkin_reminder(
    *, slot: int, checked_in: int, names: Sequence[str] = ()
) -> str:
    """一条提醒的正文（HTML）。

    必含三件事：一句签到规则（连续第 N 天得 N 分，最高 10 分，断签从 1 分重来、
    已得积分不清零）、今日已签到人数（紧跟已签到名单）、以及"本条提醒 10 分钟后
    自动删除"。不 @所有人、不堆表情。

    名单已经按 ``CHECKIN_ROSTER_MAX_NAMES`` 截断并转义；这里再按 Telegram 4096
    字符上限兜底：超出就从名单**末尾**开始丢名字（丢到够短为止），保证正文一定发得出去。
    """

    normalized = normalize_slot(slot)
    greeting = SLOT_GREETINGS.get(normalized if normalized is not None else 0, "你好")
    roster = tuple(names)
    text = _compose_checkin_reminder(
        greeting=greeting, checked_in=checked_in, names=roster
    )
    while len(text) > TELEGRAM_MESSAGE_MAX_CHARS and roster:
        roster = roster[:-1]
        text = _compose_checkin_reminder(
            greeting=greeting, checked_in=checked_in, names=roster
        )
    return text


def shop_start_payload(group_id: int) -> str:
    """商店深链的 start payload：``shop_-1001234567890``。"""

    return f"{SHOP_START_PAYLOAD_PREFIX}{int(group_id)}"


def parse_shop_start_payload(payload: object) -> int | None:
    """把 ``shop_<群号>`` 解析成群号；不是商店 payload（含 ``verify…``）返回 None。

    只认 ``shop_`` 前缀 + 一个（可带负号的）十进制整数，群号 0 视为非法。
    独立于 ``join_verification.parse_private_verify_group_id``：两个前缀不重叠，
    任何一边改动都不会串到另一边。
    """

    normalized = str(payload or "").strip()
    if not normalized.startswith(SHOP_START_PAYLOAD_PREFIX):
        return None
    body = normalized[len(SHOP_START_PAYLOAD_PREFIX) :]
    digits = body[1:] if body.startswith("-") else body
    if not digits.isascii() or not digits.isdigit():
        return None
    value = int(body)
    return value if value != 0 else None


def shop_deep_link(*, bot_username: str, group_id: int | None = None) -> str | None:
    """商店深链；拿不到机器人用户名或群号时返回 None（调用方据此不画按钮）。"""

    username = str(bot_username or "").strip().lstrip("@")
    if not username or group_id is None:
        return None
    return f"https://t.me/{username}?start={shop_start_payload(int(group_id))}"


def build_checkin_reminder_keyboard(
    *, bot_username: str = "", group_id: int | None = None
) -> InlineKeyboardMarkup:
    """提醒下方的按钮：一行两个——「✅ 一键签到」｜「🛒 积分商店」。

    - 签到按钮的 ``callback_data`` 是固定常量 ``checkin:v1``，**不含用户 ID**：
      点击者身份只由 ``callback_query.from_user`` 决定；
    - 商店按钮是 **URL 深链** ``https://t.me/<bot>?start=shop_<群号>``（把菜单引到
      私聊，不在群里刷屏），没有 ``callback_data``；
    - **取不到机器人用户名**（``get_me`` 失败 / 没设 username）时干脆不放这个按钮，
      退化成只有一个签到按钮——绝不能让整条提醒因此发不出去。
    """

    buttons = [
        InlineKeyboardButton(
            text=CHECKIN_BUTTON_TEXT,
            callback_data=CHECKIN_CALLBACK_DATA,
        )
    ]
    url = shop_deep_link(bot_username=bot_username, group_id=group_id)
    if url:
        buttons.append(InlineKeyboardButton(text=SHOP_BUTTON_TEXT, url=url))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


@dataclass(frozen=True, slots=True)
class CheckinRoster:
    """今天本群的签到情况：总人数 + 按签到先后排好的昵称（最多 20 个）。"""

    count: int
    names: tuple[str, ...]


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


async def today_checkin_roster(
    session: AsyncSession,
    *,
    group_id: int,
    day: date | None = None,
    limit: int = CHECKIN_ROSTER_MAX_NAMES,
) -> CheckinRoster:
    """今天本群的签到人数 + 昵称名单（一次查全，渲染层不用自己拼）。

    - 顺序：按 ``member_checkins.id`` 升序 = 签到先后（最早在前）。id 单调递增且
      同一天一人只有一行，所以顺序**稳定可复现**，不受 ``created_at`` 秒级精度
      并列的影响；
    - 一人只出现一次：按 ``user_id`` 分组取最早的一行（表上
      ``(group_id, user_id, checkin_date)`` 唯一索引本来就保证一人一行，分组是双保险）；
    - 昵称取表里现成的 ``display_name``；万一为空（历史脏数据）就用 user_id 兜底，
      免得名单里出现「、」这种空条目；
    - ``limit`` 只截查询，``count`` 永远是**当天总人数**（超出的部分由
      ``render_checkin_roster`` 折成「…等 N 人」）。
    """

    today = (day or local_today()).isoformat()
    gid = int(group_id)
    count = await count_checkins_today(session, group_id=gid, day=day)
    rows = await session.execute(
        select(MemberCheckin.display_name, MemberCheckin.user_id)
        .where(
            MemberCheckin.group_id == gid,
            MemberCheckin.checkin_date == today,
        )
        .group_by(MemberCheckin.user_id)
        .order_by(func.min(MemberCheckin.id))
        .limit(max(1, int(limit)))
    )
    names = tuple(
        str(name or "").strip() or str(int(user_id))
        for name, user_id in rows.all()
    )
    return CheckinRoster(count=count, names=names)


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
    """发送成功后回填 message_id（按钮回调按它反查时段）并标记 ``delivered_at``。

    ``delivered_at``（B-27）是「占位真的变成了消息」的证据：进程在 claim 与 send
    之间被 SIGKILL / OOM / 容器驱逐时，只会留下一行 ``message_id=0`` 的空占位，
    而 ``(group_id, slot_key)`` 唯一索引会让该时段**永远不再发**、且无补发路径。
    :func:`reap_stale_reminder_slots` 靠它把超宽限期的空占位清掉并补发。
    """

    await session.execute(
        update(CheckinReminderPost)
        .where(
            CheckinReminderPost.group_id == int(group_id),
            CheckinReminderPost.slot_key == str(key),
        )
        .values(message_id=int(message_id), delivered_at=now_shanghai_naive())
    )


#: 空占位的宽限期（秒，B-27）。必须**远大于**一次「claim → 读名单 → 发消息」的正常
#: 耗时（秒级），否则会把「刚 claim、正在发」的行误判成空占位。
STALE_REMINDER_GRACE_SECONDS = 15 * 60


async def reap_stale_reminder_slots(
    session: AsyncSession,
    *,
    grace_seconds: int = STALE_REMINDER_GRACE_SECONDS,
    now: Any | None = None,
) -> list[tuple[int, str]]:
    """清理「claim 了但从没送达」的空占位，返回被释放的 ``(group_id, slot_key)``。

    判据是**三个都有**才删：``message_id = 0``（没回填过）、
    ``delivered_at IS NULL``（从没确认送达），再加 ``created_at`` 早于宽限期——
    这样「刚 claim 正在发」的行不会被误删。腾出来的时段会在同一轮里被
    :func:`claim_reminder_slot` 正常认领并补发。
    """

    stamp = now or now_shanghai_naive()
    cutoff = stamp - timedelta(seconds=max(0, int(grace_seconds)))
    try:
        rows = (
            await session.execute(
                select(
                    CheckinReminderPost.group_id, CheckinReminderPost.slot_key
                ).where(
                    CheckinReminderPost.message_id == 0,
                    CheckinReminderPost.delivered_at.is_(None),
                    CheckinReminderPost.created_at <= cutoff,
                )
            )
        ).all()
        stale = [(int(row[0]), str(row[1])) for row in rows]
        if not stale:
            return []
        await session.execute(
            delete(CheckinReminderPost).where(
                CheckinReminderPost.message_id == 0,
                CheckinReminderPost.delivered_at.is_(None),
                CheckinReminderPost.created_at <= cutoff,
            )
        )
        await session.commit()
    except Exception as exc:  # 清理失败只是「这一轮不补发」，绝不影响发提醒
        await session.rollback()
        log.warning("checkin reminder: stale slot reap failed | error=%s", exc)
        return []
    log.info(
        "checkin reminder: released %d stale undelivered slot(s) | groups=%s",
        len(stale),
        sorted({group_id for group_id, _slot in stale}),
    )
    return stale


async def backfill_durable_auto_delete(
    session_factory: Any,
    *,
    chat_id: int,
    message_id: int,
    due_at: Any,
) -> bool:
    """自动删除排队失败时的**持久兜底**（B-27）。

    ``schedule_message_auto_delete_durable`` 返回 ``False``（调度器未初始化 / 不健康）
    时，那条写着「本条提醒 10 分钟后自动删除」的消息会**永久**留在群里。
    这里直接往同一张持久表 ``telegram_delete_jobs`` 补一行，由常驻进程的清理
    worker 到点执行——与调度器走的是同一条队列、同一套幂等键。
    """

    normalized_chat_id = int(chat_id)
    normalized_message_id = int(message_id)
    if normalized_chat_id == 0 or normalized_message_id <= 0:
        return False
    due = due_at if isinstance(due_at, datetime) else now_shanghai_naive()
    due = due.replace(tzinfo=None)
    try:
        async with session_factory() as session:
            statement = sqlite_insert(TelegramDeleteJob).values(
                chat_id=normalized_chat_id,
                message_id=normalized_message_id,
                due_at=due,
                attempts=0,
                lease_until=None,
                last_error="",
                updated_at=now_shanghai_naive(),
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        TelegramDeleteJob.chat_id,
                        TelegramDeleteJob.message_id,
                    ],
                    set_={
                        "due_at": func.min(
                            TelegramDeleteJob.due_at, statement.excluded.due_at
                        ),
                        "updated_at": now_shanghai_naive(),
                    },
                )
            )
            await session.commit()
    except Exception as exc:
        log.warning(
            "checkin reminder: durable auto delete backfill failed | chat=%s | "
            "message=%s | error=%s",
            normalized_chat_id,
            normalized_message_id,
            exc,
        )
        return False
    log.warning(
        "checkin reminder: durable auto delete backfilled directly | chat=%s | "
        "message=%s",
        normalized_chat_id,
        normalized_message_id,
    )
    return True


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
