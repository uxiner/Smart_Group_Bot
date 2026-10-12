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
from sqlalchemy import and_, delete, func, literal, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    CheckinReminderPost,
    MemberCheckin,
    TelegramDeleteJob,
)
from bot.services import policy_runtime
from bot.services.checkin import (
    CHECKIN_CALLBACK_DATA,
    checkin_button_text,
    local_today,
)
from bot.services.operations_guard import PersistedGuard, guard_predicate, verify_guard
from bot.services.operations_receipts import (
    RECEIPT_STATE_DISPATCHING,
    RECEIPT_STATE_SENT,
    RECEIPT_STATE_UNKNOWN,
    TASK_CHECKIN_REMINDER,
    ambiguous_send_failure,
    claim_business_dispatch,
    definitely_not_delivered,
    load_receipt,
    load_receipts,
    record_receipt,
    release_business_dispatch,
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


def _rem():
    """当前生效的签到提醒快照（默认 = 改造前逐字相同）。"""

    return policy_runtime.checkin_reminder_policy()


def reminder_slots() -> tuple[int, ...]:
    """当前允许的提醒时段（本地小时）。

    **这只约束「命令/CLI 接受哪些 --slot」**；真正"几点发"由**外部 cron** 决定
    （``bot/tools/checkin_reminder.py`` 的 docstring 里有样例 crontab）。改了这里
    不会让 cron 自己改时间，必须同步改 crontab——见 docs/configuration.md。
    """

    return _rem().slots


def reminder_auto_delete_seconds() -> int:
    """提醒发出后多久自动删除（0 = 不自动删）。"""

    return _rem().auto_delete_seconds


def shop_button_text() -> str:
    """商店按钮文字（可配文案；``shop_`` start payload 是协议常量，不可配）。"""

    return policy_runtime.display_policy().shop_button_text or SHOP_BUTTON_TEXT


def normalize_slot(slot: object) -> int | None:
    """把时段收敛成 9/12/15/18；非法（含 None、"abc"）返回 None。"""

    try:
        value = int(slot)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value in reminder_slots() else None


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
    shown = listed[: _rem().roster_max_names]
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
    greeting = (
        _rem().greeting_for(normalized if normalized is not None else 0) or "你好"
    )
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
            text=checkin_button_text(),
            callback_data=CHECKIN_CALLBACK_DATA,
        )
    ]
    url = shop_deep_link(bot_username=bot_username, group_id=group_id)
    if url:
        buttons.append(
            InlineKeyboardButton(text=shop_button_text(), url=url)
        )
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
    limit: int | None = None,
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

    limit = _rem().roster_max_names if limit is None else int(limit)
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
    guard: PersistedGuard | None = None,
) -> bool:
    """为 (群, slot_key) 占位；True = 这个时段还没发过，可以发。

    占位发生在**发送之前**（``INSERT ... ON CONFLICT DO NOTHING + RETURNING``）：
    已有该行时 SQLite 不返回行 → False。所以 cron 重试、运维手动重跑、两个进程
    同时跑，都只有一个能发出。不在这里 commit，事务边界由调用方决定。

    ``guard``（B13b 返工 R-B1）：非空时把持久准入谓词编进**同一条 INSERT 的
    WHERE**（``INSERT ... SELECT ... WHERE <guard>``）。这是"读了来源之后、真正
    出站之前"被切换来源/撤权挡住的那个线性化点：谓词与占位是同一条语句、同一次
    writer 准入。谓词不通过时**不会插入占位行**（返回 False），调用方据此判定
    "本来源已被退役"，不会误当成"别人发过了"。
    """

    if guard is None:
        statement = (
            sqlite_insert(CheckinReminderPost)
            .values(group_id=int(group_id), slot_key=str(key), message_id=0)
            .on_conflict_do_nothing(index_elements=["group_id", "slot_key"])
            .returning(CheckinReminderPost.id)
        )
    else:
        statement = (
            sqlite_insert(CheckinReminderPost)
            .from_select(
                ["group_id", "slot_key", "message_id"],
                select(literal(int(group_id)), literal(str(key)), literal(0)).where(
                    guard_predicate(guard)
                ),
            )
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
    grace_seconds: int | None = None,
    now: Any | None = None,
) -> list[tuple[int, str]]:
    """清理「claim 了但从没送达」的空占位，返回被释放的 ``(group_id, slot_key)``。

    判据是**三个都有**才删：``message_id = 0``（没回填过）、
    ``delivered_at IS NULL``（从没确认送达），再加 ``created_at`` 早于宽限期——
    这样「刚 claim 正在发」的行不会被误删。腾出来的时段会在同一轮里被
    :func:`claim_reminder_slot` 正常认领并补发。

    **R1：有出站证据的时段绝不回收。** 消息真的发出去、但回填
    ``mark_reminder_sent`` 失败（或进程死在发送与回填之间）时，占位看起来仍然是
    "空"的；只看三条件就会把它放回去，下一轮再发一条。所以这里再查一次
    ``operations_business_receipts``：凡是 ``(group_id, slot_key)`` 上已经有
    ``sent``/``unknown`` 证据的，一律保留（未知同样不能重发）。
    """

    grace_seconds = (
        _rem().stale_grace_seconds if grace_seconds is None else int(grace_seconds)
    )

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
        by_group: dict[int, list[str]] = {}
        for group_id, key in stale:
            by_group.setdefault(group_id, []).append(key)
        receipted: set[tuple[int, str]] = set()
        for group_id, keys in by_group.items():
            found = await load_receipts(
                session,
                task_id=TASK_CHECKIN_REMINDER,
                group_id=group_id,
                business_keys=keys,
            )
            receipted.update((group_id, key) for key in found)
        if receipted:
            log.warning(
                "checkin reminder: kept %d slot(s) with outbound receipt | "
                "groups=%s",
                len(receipted),
                sorted({group_id for group_id, _slot in receipted}),
            )
        stale = [pair for pair in stale if pair not in receipted]
        if not stale:
            return []
        await session.execute(
            delete(CheckinReminderPost).where(
                CheckinReminderPost.message_id == 0,
                CheckinReminderPost.delivered_at.is_(None),
                CheckinReminderPost.created_at <= cutoff,
                or_(
                    *[
                        and_(
                            CheckinReminderPost.group_id == group_id,
                            CheckinReminderPost.slot_key == key,
                        )
                        for group_id, key in stale
                    ]
                ),
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


# --- 运营任务中心（B13a）共用的单群发送原语 --------------------------------
#
# CLI（``bot.tools.checkin_reminder``）与运营任务中心必须走**同一段**发送链：
# 占位 → 发送 → 回填 → 排自动删除。把这段抽成服务函数，任何一侧都不会绕过
# 占位幂等或自己发明一套锁。这里不做进程级调度，也不打印任何东西。

CHECKIN_OUTCOME_SENT = "sent"
CHECKIN_OUTCOME_ALREADY_DELIVERED = "already_delivered"
CHECKIN_OUTCOME_SEND_FAILED = "send_failed"
CHECKIN_OUTCOME_MARK_FAILED = "mark_failed"

#: 占位台账的三种状态：没有行 / 有空占位（还没发出去）/ 已经发出过。
SLOT_STATE_MISSING = "missing"
SLOT_STATE_PLACEHOLDER = "placeholder"
SLOT_STATE_DELIVERED = "delivered"

AUTO_DELETE_QUEUED = "queued"
AUTO_DELETE_BACKFILLED = "backfilled"
AUTO_DELETE_UNCONFIRMED = "unconfirmed"
AUTO_DELETE_NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class CheckinSlotOutcome:
    """一次「给某个群发某个时段提醒」的结果。

    ``outbound_unknown``：外部结果**未知**。投递异常里包含了"其实已经发出去、
    只是回包丢了/超时"这种情况，所以``send_failed``不等于"没发出去"。写侧
    （运营任务中心）据它决定"禁止安全重发"，CLI 仍按既有策略释放占位重试。
    """

    group_id: int
    slot: int
    slot_key: str
    status: str
    reason: str = ""
    message_id: int = 0
    checked_in: int = 0
    auto_delete: str = AUTO_DELETE_NOT_APPLICABLE
    outbound_unknown: bool = False
    error: str = ""


async def reminder_outbound_receipt(
    session: AsyncSession,
    *,
    group_id: int,
    key: str,
) -> Any:
    """(群, slot_key) 上的出站证据（``None`` = 没有证据，不代表"没发过"）。

    ``sent`` 与 ``unknown`` 都表示"已经有可能出站"，调用方一律不得重发。
    """

    return await load_receipt(
        session,
        task_id=TASK_CHECKIN_REMINDER,
        group_id=int(group_id),
        business_key=str(key),
    )


async def reminder_slot_state(
    session: AsyncSession,
    *,
    group_id: int,
    key: str,
) -> str:
    """(群, slot_key) 的台账状态：missing / placeholder / delivered。"""

    row = (
        await session.execute(
            select(
                CheckinReminderPost.message_id,
                CheckinReminderPost.delivered_at,
            )
            .where(
                CheckinReminderPost.group_id == int(group_id),
                CheckinReminderPost.slot_key == str(key),
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return SLOT_STATE_MISSING
    message_id = int(row[0] or 0)
    delivered_at = row[1]
    if message_id > 0 or delivered_at is not None:
        return SLOT_STATE_DELIVERED
    return SLOT_STATE_PLACEHOLDER


async def send_checkin_slot(
    session: AsyncSession,
    bot: Any,
    *,
    group_id: int,
    slot: int,
    auto_delete_seconds: int,
    queue_auto_delete: Any,
    backfill_auto_delete: Any,
    session_factory: Any = None,
    bot_username: str = "",
    protect_outbound: bool = False,
    guard: "PersistedGuard | None" = None,
) -> CheckinSlotOutcome:
    """给单个群发该时段的签到提醒；返回结构化结果，由调用方决定怎么记账。

    ``queue_auto_delete`` / ``backfill_auto_delete`` 由调用方注入（CLI 传自己的
    模块级函数，保持既有测试的 patch 点；运营任务中心传同一组真实函数），
    这样"发送"与"自动删除排队"两条链在所有入口都是同一份实现。
    ``backfill_auto_delete`` 的调用约定是 ``(session_factory, *, chat_id,
    message_id, due_at)``（持久兜底自己开事务，所以需要工厂而不是当前 session）。

    ``protect_outbound``（**运营任务中心入口传 True**）：真实出站**之前**先抢一条
    持久 ``dispatching`` 保护并 commit，抢到的入口才发。这样进程死在发送与记账
    之间、或出站后确认写入失败，保护行都在库里，reaper / 常驻 runner / CLI 都
    不会再发第二条。发送异常时按 :func:`definitely_not_delivered` 反选：只有能
    证明"明确没出站"（发送前失败、Telegram 明确拒收）才释放保护与占位，其余
    一律保留保护并记 ``unknown``。

    不传 ``protect_outbound`` 的 legacy CLI 入口保持既有 at-least-once 策略
    （非超时异常释放占位以便重跑补发）；但只要有 ``sent``/``unknown``/
    ``dispatching`` 证据存在，任何入口都不会再发。
    """

    gid = int(group_id)
    key = slot_key(local_today(), slot)
    receipt = await reminder_outbound_receipt(session, group_id=gid, key=key)
    if receipt is not None:
        # R1：这个时段已经有持久证据。``sent``/``unknown`` 表示"可能已经发出去"，
        # ``dispatching`` 表示另一个入口正在发（或死在发送与记账之间）：三种情况
        # 下本入口一律不许再发。占位可能因为回填失败而看起来还是"空"的。
        if receipt.state == RECEIPT_STATE_SENT:
            reason = "outbound_receipt_confirmed"
        elif receipt.state == RECEIPT_STATE_DISPATCHING:
            reason = "outbound_dispatch_protected"
        else:
            reason = "outbound_receipt_unknown"
        return CheckinSlotOutcome(
            group_id=gid,
            slot=int(slot),
            slot_key=key,
            status=CHECKIN_OUTCOME_ALREADY_DELIVERED,
            reason=reason,
            message_id=int(receipt.external_message_id or 0),
            outbound_unknown=receipt.state != RECEIPT_STATE_SENT,
        )
    state = await reminder_slot_state(session, group_id=gid, key=key)
    if state == SLOT_STATE_DELIVERED:
        return CheckinSlotOutcome(
            group_id=gid,
            slot=int(slot),
            slot_key=key,
            status=CHECKIN_OUTCOME_ALREADY_DELIVERED,
            reason="slot_already_delivered",
        )

    # 出站前的准备（名单 → 正文 → 键盘）放在抢占**之前**：这一步失败可以证明没有
    # 出站，既不需要保护也不需要占位，如实返回"可安全重试"的失败。
    try:
        roster = await today_checkin_roster(session, group_id=gid)
        body = render_checkin_reminder(
            slot=int(slot), checked_in=roster.count, names=roster.names
        )
        markup = build_checkin_reminder_keyboard(
            bot_username=bot_username, group_id=gid
        )
    except Exception as exc:
        await session.rollback()
        log.exception("checkin reminder payload build failed | group=%s", gid)
        return CheckinSlotOutcome(
            group_id=gid,
            slot=int(slot),
            slot_key=key,
            status=CHECKIN_OUTCOME_SEND_FAILED,
            reason="pre_send_error",
            outbound_unknown=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    claimed = await claim_reminder_slot(session, group_id=gid, key=key, guard=guard)
    await session.commit()
    if not claimed:
        if guard is not None:
            # 只用 verify_guard 给"为什么没抢到"分类（谓词复核 / 别人持有）；
            # 复核自己出错时按不通过处理 —— 绝不因为读不出来就发。
            try:
                allowed = await verify_guard(session, guard)
            except Exception:
                allowed = False
            if not allowed:
                # 占位语句里的持久准入谓词没过（管理员在本轮读了来源之后切换/撤权）：
                # 这一条提醒**没有**发出，也没有留下占位。如实报告"来源已退役"，而
                # 不是伪装成"别人发过了"。
                await session.rollback()
                return CheckinSlotOutcome(
                    group_id=gid,
                    slot=int(slot),
                    slot_key=key,
                    status=CHECKIN_OUTCOME_SEND_FAILED,
                    reason="guard_blocked",
                    checked_in=roster.count,
                    outbound_unknown=False,
                )
        # 有别的进程/本轮另一次 claim 抢到了这个时段：不是我们发，也不再重复发。
        return CheckinSlotOutcome(
            group_id=gid,
            slot=int(slot),
            slot_key=key,
            status=CHECKIN_OUTCOME_ALREADY_DELIVERED,
            reason="slot_claimed_elsewhere",
        )

    protected = False
    if protect_outbound:
        try:
            claim = await claim_business_dispatch(
                session,
                task_id=TASK_CHECKIN_REMINDER,
                group_id=gid,
                business_key=key,
                detail="checkin reminder outbound dispatch claim",
                guard=guard,
            )
            await session.commit()
        except Exception as exc:
            # 保护写不进去就绝不发送：宁可这轮失败，也不要发出一条没有保护的提醒。
            await session.rollback()
            log.exception("checkin reminder dispatch claim failed | group=%s", gid)
            try:
                await release_reminder_slot(session, group_id=gid, key=key)
                await session.commit()
            except Exception:
                await session.rollback()
                log.exception("checkin reminder slot release failed | group=%s", gid)
            return CheckinSlotOutcome(
                group_id=gid,
                slot=int(slot),
                slot_key=key,
                status=CHECKIN_OUTCOME_SEND_FAILED,
                reason="dispatch_claim_failed",
                outbound_unknown=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        if claim is None:
            if guard is not None:
                # R2（返工 v3）：抢不到可能是**写边界复核不通过**（撤权、换配置、
                # 取消），也可能是别的入口正持有保护。复核查询自己出错时按保守
                # 协议同样不发（走下面的 dispatch_claim_failed 分支由调用方重试）。
                try:
                    allowed = await verify_guard(session, guard)
                except Exception as exc:
                    await session.rollback()
                    return CheckinSlotOutcome(
                        group_id=gid,
                        slot=int(slot),
                        slot_key=key,
                        status=CHECKIN_OUTCOME_SEND_FAILED,
                        reason="dispatch_claim_failed",
                        outbound_unknown=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                if not allowed:
                    try:
                        await session.rollback()
                        await release_reminder_slot(session, group_id=gid, key=key)
                        await session.commit()
                    except Exception:
                        await session.rollback()
                        log.exception(
                            "checkin reminder slot release failed | group=%s", gid
                        )
                    return CheckinSlotOutcome(
                        group_id=gid,
                        slot=int(slot),
                        slot_key=key,
                        status=CHECKIN_OUTCOME_SEND_FAILED,
                        reason="guard_blocked",
                        checked_in=roster.count,
                        outbound_unknown=False,
                    )
            # 别的入口已经持有这条业务键的保护：本次绝不发送；占位留给赢家回填，
            # 我们也不撤销它。
            return CheckinSlotOutcome(
                group_id=gid,
                slot=int(slot),
                slot_key=key,
                status=CHECKIN_OUTCOME_ALREADY_DELIVERED,
                reason="outbound_dispatch_protected",
                outbound_unknown=True,
            )
        protected = True

    sent_message: Any = None
    try:
        sent_message = await bot.send_message(
            gid,
            body,
            parse_mode="HTML",
            reply_markup=markup,
        )
    except Exception as exc:  # 投递异常：区分"明确未发"与"可能已发"
        message = f"{type(exc).__name__}: {exc}"
        if definitely_not_delivered(exc):
            # Telegram 明确拒收（4xx）：可以证明没有出站，释放保护与占位，允许重试。
            try:
                await session.rollback()
                if protected:
                    await release_business_dispatch(
                        session,
                        task_id=TASK_CHECKIN_REMINDER,
                        group_id=gid,
                        business_key=key,
                    )
                await release_reminder_slot(session, group_id=gid, key=key)
                await session.commit()
                reason = "send_rejected"
            except Exception:
                await session.rollback()
                log.exception("checkin reminder slot release failed | group=%s", gid)
                reason = "release_failed"
            log.warning("checkin reminder send rejected | group=%s | %s", gid, exc)
            return CheckinSlotOutcome(
                group_id=gid,
                slot=int(slot),
                slot_key=key,
                status=CHECKIN_OUTCOME_SEND_FAILED,
                reason=reason,
                checked_in=roster.count,
                outbound_unknown=False,
                error=message,
            )
        if ambiguous_send_failure(exc) or protected:
            # R1：超时/断连下消息**可能已经送达**；在受保护路径上，任何认不出来的
            # 异常同样无法证明没有出站。两者都保留占位与保护，写一条 ``unknown``
            # 出站证据（独立事务先落盘），所有入口都看得见。
            unknown_reason = (
                "send_timeout_unknown"
                if ambiguous_send_failure(exc)
                else "send_result_unknown"
            )
            try:
                await session.rollback()
                await record_receipt(
                    session,
                    task_id=TASK_CHECKIN_REMINDER,
                    group_id=gid,
                    business_key=key,
                    state=RECEIPT_STATE_UNKNOWN,
                    detail="checkin reminder send result unknown",
                )
                await session.commit()
                reason = unknown_reason
            except Exception:
                await session.rollback()
                log.exception(
                    "checkin reminder unknown receipt record failed | group=%s", gid
                )
                reason = "receipt_failed"
            log.warning(
                "checkin reminder send result unknown | group=%s | %s", gid, exc
            )
            return CheckinSlotOutcome(
                group_id=gid,
                slot=int(slot),
                slot_key=key,
                status=CHECKIN_OUTCOME_SEND_FAILED,
                reason=reason,
                checked_in=roster.count,
                outbound_unknown=True,
                error=message,
            )
        # legacy CLI 入口（没有出站前保护）：保持既有 at-least-once 策略，释放占位
        # 让重跑补发；``outbound_unknown`` 仍标 True —— 释放占位是"允许未来重试"，
        # 不等于"断言没发出去"。
        try:
            await session.rollback()
            await release_reminder_slot(session, group_id=gid, key=key)
            await session.commit()
            reason = "send_error"
        except Exception:
            await session.rollback()
            log.exception("checkin reminder slot release failed | group=%s", gid)
            reason = "release_failed"
        log.warning("checkin reminder send failed | group=%s | %s", gid, exc)
        return CheckinSlotOutcome(
            group_id=gid,
            slot=int(slot),
            slot_key=key,
            status=CHECKIN_OUTCOME_SEND_FAILED,
            reason=reason,
            checked_in=roster.count,
            outbound_unknown=True,
            error=message,
        )

    message_id = int(getattr(sent_message, "message_id", 0) or 0)
    try:
        # R1：先把"真实出站"写进持久证据（独立事务 commit），再去回填业务台账。
        # 顺序不能反：``mark_reminder_sent`` 失败时占位不会被回退（见下），
        # 若此时还没有证据，过期占位回收或 CLI 重跑就会再发一条。
        await record_receipt(
            session,
            task_id=TASK_CHECKIN_REMINDER,
            group_id=gid,
            business_key=key,
            state=RECEIPT_STATE_SENT,
            external_message_id=message_id,
            detail="checkin reminder delivered",
        )
        await session.commit()
    except Exception as exc:
        # 证据没能落盘：消息却已经出站。保守处理——不回退占位、不重发，如实标未知。
        await session.rollback()
        log.exception("checkin reminder receipt record failed | group=%s", gid)
        return CheckinSlotOutcome(
            group_id=gid,
            slot=int(slot),
            slot_key=key,
            status=CHECKIN_OUTCOME_MARK_FAILED,
            reason="receipt_failed",
            message_id=message_id,
            checked_in=roster.count,
            outbound_unknown=True,
            error=f"{type(exc).__name__}: {exc}",
        )
    try:
        await mark_reminder_sent(
            session, group_id=gid, key=key, message_id=message_id
        )
        await session.commit()
    except Exception as exc:
        # 消息已经出站了：绝不重发、也绝不释放占位（那等于允许再发一条）。
        await session.rollback()
        log.warning("checkin reminder mark sent failed | group=%s | %s", gid, exc)
        return CheckinSlotOutcome(
            group_id=gid,
            slot=int(slot),
            slot_key=key,
            status=CHECKIN_OUTCOME_MARK_FAILED,
            reason="mark_failed",
            message_id=message_id,
            checked_in=roster.count,
            outbound_unknown=True,
            error=f"{type(exc).__name__}: {exc}",
        )

    auto_delete = AUTO_DELETE_NOT_APPLICABLE
    queued = await queue_auto_delete(sent_message, int(auto_delete_seconds))
    if not queued:
        log.error("checkin reminder auto delete not queued | group=%s", gid)
        if session_factory is None:
            # 没有工厂就没法做持久兜底：如实标成未确认，不假装补写成功。
            log.error("checkin reminder auto delete backfill unavailable | group=%s", gid)
            backfilled = False
        else:
            try:
                backfilled = await backfill_auto_delete(
                    session_factory,
                    chat_id=gid,
                    message_id=message_id,
                    due_at=now_shanghai_naive()
                    + timedelta(seconds=int(auto_delete_seconds)),
                )
            except Exception:
                log.exception(
                    "checkin reminder auto delete backfill failed | group=%s", gid
                )
                backfilled = False
        auto_delete = AUTO_DELETE_BACKFILLED if backfilled else AUTO_DELETE_UNCONFIRMED
    else:
        auto_delete = AUTO_DELETE_QUEUED

    return CheckinSlotOutcome(
        group_id=gid,
        slot=int(slot),
        slot_key=key,
        status=CHECKIN_OUTCOME_SENT,
        reason="sent",
        message_id=message_id,
        checked_in=roster.count,
        auto_delete=auto_delete,
    )
