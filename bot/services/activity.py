"""每周活跃激励：让"在群里好好聊天"也能拿到积分，而不是只有签到有回报。

两块职责：

1. **日累计**（``record_message_activity``）：每条合格消息往 ``member_activity_daily``
   做一次 UPSERT。按天滚动累计是必须的——群消息只在 ``group_message_archive`` 里留
   7 天，周末再想统计一个完整自然周就已经晚了。防刷屏也在这里解决：``messages``
   **写入时就按每天 20 条封顶**，后面积分怎么聚合都不会把刷屏算成贡献。

   合格消息 = 真实成员的纯文本消息、去空白后长度 ≥ 2、不是命令（``/`` 开头）、
   不是机器人/频道身份发的、不是群管理员或超管发的（他们不参与评选）。

2. **每周结算**（``settle_weekly_activity``）：统计最近一个完整自然周（周一至周日，
   Asia/Shanghai）：

   - ``score = messages + 2 * active_days + replies_received``
   - 门槛：``active_days >= 3 且 messages >= 10``（不达标不发奖，榜单上也不出现）
   - 前 10 名分 77 分：第 1 名 25，第 2/3 名各 12，第 4–10 名各 4
   - 并列时按 ``messages`` 降序、再按 ``user_id`` 升序稳定排序

发奖写的是独立的 ``member_point_awards`` 流水，**不伪造签到行**：连续签到天数是从
签到日期集合倒推出来的，塞假签到会直接把别人的连击算错。``ref``
（``weekly-activity:2026-W40:<user_id>``）上有唯一索引，重复结算不会重复加分，
也不会抛异常。

全流程纯本地统计，没有任何 LLM 调用。
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import Context
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from html import escape

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import MemberActivityDaily, MemberPointAward
from bot.db.sqlite_session import is_database_locked_error
from bot.services import policy_runtime
from bot.services.checkin import local_today
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 口径常量
# ---------------------------------------------------------------------------

# 每天最多累计多少条有效消息（写入时就封顶，防刷屏）
MAX_DAILY_MESSAGES = 20
# 有效消息的最小长度（去空白后按字符数算）
MIN_MESSAGE_TEXT_LENGTH = 2
# 只有纯文本消息算数：表情/图片/语音这些占位文本不是"在聊天"
COUNTED_MESSAGE_TYPE = "text"

# 榜单长度与每周奖励总额（77 分是定死的预算，不要改）
WEEKLY_TOP_N = 10
WEEKLY_TOTAL_POINTS = 77
HEAD_REWARD_POINTS = {1: 25, 2: 12, 3: 12}
TAIL_REWARD_POINTS = 4

# 参与门槛：活跃天数与发言条数都要够
MIN_ACTIVE_DAYS = 3
MIN_WEEKLY_MESSAGES = 10

AWARD_REASON_WEEKLY_ACTIVITY = "weekly_activity"
AWARD_REF_PREFIX = "weekly-activity"


def _act():
    """当前生效的活跃激励快照（默认 = 改造前逐字相同）。"""

    return policy_runtime.activity_policy()


def reward_points_for_rank(rank: int) -> int:
    """第 N 名能拿多少分；不在奖励向量长度之内返回 0。

    奖励向量是唯一起点：榜单长度 (``weekly_top_n``) 与周总额
    (``weekly_total_points``) 都由它派生，不再单独存一份会互相矛盾的值。
    """

    policy = _act()
    position = int(rank)
    if position < 1 or position > policy.weekly_top_n:
        return 0
    return policy.weekly_reward_points[position - 1]


def activity_score(*, messages: int, active_days: int, replies_received: int) -> int:
    """得分公式：发言条数 + 活跃天数×2 + 被回复次数。"""

    return int(messages) + 2 * int(active_days) + int(replies_received)


def meets_threshold(*, messages: int, active_days: int) -> bool:
    """参与门槛：至少活跃 N 天且累计发言 M 条。"""

    policy = _act()
    return (
        int(active_days) >= policy.min_active_days
        and int(messages) >= policy.min_weekly_messages
    )


def is_countable_message(
    *,
    text: str,
    message_type: str = COUNTED_MESSAGE_TYPE,
    is_bot: bool = False,
    is_channel: bool = False,
    is_admin: bool = False,
) -> bool:
    """这条消息算不算"有效发言"。

    纯函数：判定逻辑全在这里，落库和统计都不再重复判断，方便测试也方便以后
    调口径。管理员/超管、机器人、频道身份一律不计（他们不参与评选）。
    """

    if is_bot or is_channel or is_admin:
        return False
    if str(message_type or "").strip().lower() != COUNTED_MESSAGE_TYPE:
        return False
    body = str(text or "").strip()
    if len(body) < _act().min_message_text_length:
        return False
    # 命令（/rank、/me …）不是聊天内容
    if body.startswith("/"):
        return False
    return True


# ---------------------------------------------------------------------------
# 日累计
# ---------------------------------------------------------------------------


async def _upsert_daily(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    activity_date: str,
    messages: int = 0,
    replies_received: int = 0,
    display_name: str = "",
    updated_at: datetime | None = None,
) -> None:
    """(群, 用户, 日期) 唯一一行；消息条数在 SQL 里就按上限截断。

    用 ``INSERT ... ON CONFLICT DO UPDATE`` 而不是"先查再写"：并发下不会两行。
    ``min(x, 20)`` 交给 SQLite 算，读取和写入之间没有可以被插队的窗口。
    """

    added_messages = max(0, min(int(messages), _act().max_daily_messages))
    added_replies = max(0, int(replies_received))
    stamp = updated_at or now_shanghai_naive()
    values: dict[str, object] = {
        "group_id": int(group_id),
        "user_id": int(user_id),
        "activity_date": str(activity_date),
        "messages": added_messages,
        "replies_received": added_replies,
        "display_name": str(display_name or "")[:255],
        "created_at": stamp,
        "updated_at": stamp,
    }
    updates: dict[str, object] = {
        # 当天的 20 条上限就在这里生效：第 21 条之后 messages 不再增长
        "messages": func.min(
            MemberActivityDaily.messages + added_messages, _act().max_daily_messages
        ),
        "replies_received": MemberActivityDaily.replies_received + added_replies,
        "updated_at": stamp,
    }
    if values["display_name"]:
        updates["display_name"] = values["display_name"]

    await session.execute(
        sqlite_insert(MemberActivityDaily)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["group_id", "user_id", "activity_date"],
            set_=updates,
        )
    )


async def record_message_activity(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    text: str = "",
    message_type: str = COUNTED_MESSAGE_TYPE,
    display_name: str = "",
    is_bot: bool = False,
    is_channel: bool = False,
    is_admin: bool = False,
    reply_to_user_id: int | None = None,
    now: object = None,
) -> bool:
    """记一条群消息的活跃度；不合格的消息直接返回 False（不写库）。

    合格消息会给发送者 ``messages +1``；如果这条是回复，还会给被回复的人
    ``replies_received +1``（自己回自己不算）。异常向上抛——容错放在
    :func:`record_message_activity_safe` / :func:`schedule_activity_record` 里，
    这样测试和排查时能看见真实错误。
    """

    if not is_countable_message(
        text=text,
        message_type=message_type,
        is_bot=is_bot,
        is_channel=is_channel,
        is_admin=is_admin,
    ):
        return False

    gid, uid = int(group_id), int(user_id)
    if uid == 0:
        return False
    day = local_today(now).isoformat()
    await _upsert_daily(
        session,
        group_id=gid,
        user_id=uid,
        activity_date=day,
        messages=1,
        display_name=display_name,
    )

    target = int(reply_to_user_id or 0)
    if target and target != uid:
        await _upsert_daily(
            session,
            group_id=gid,
            user_id=target,
            activity_date=day,
            replies_received=1,
        )
    return True


async def record_message_activity_safe(
    session: AsyncSession,
    **kwargs: object,
) -> bool:
    """:func:`record_message_activity` 的容错版本：失败只记日志，不抛异常。

    活跃度是"锦上添花"的统计，任何失败都不该影响用户看到的回复。
    """

    try:
        return await record_message_activity(session, **kwargs)  # type: ignore[arg-type]
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning(
            "activity record failed | group=%s user=%s",
            kwargs.get("group_id", "?"),
            kwargs.get("user_id", "?"),
            exc_info=True,
        )
        return False


# ---------------------------------------------------------------------------
# 后台写入（不阻塞回复路径）
# ---------------------------------------------------------------------------

_ACTIVITY_TASKS: set[asyncio.Task[None]] = set()
# SQLite 只有一个写者；重试一次足以吃掉偶发的锁竞争
_ACTIVITY_WRITE_ATTEMPTS = 2


async def _write_activity_sample(
    session_factory: async_sessionmaker[AsyncSession],
    kwargs: dict[str, object],
) -> None:
    """独立 session 落库并提交；失败只记日志（调用方是 fire-and-forget 任务）。"""

    for attempt in range(1, _ACTIVITY_WRITE_ATTEMPTS + 1):
        try:
            async with session_factory() as session:
                await record_message_activity(session, **kwargs)  # type: ignore[arg-type]
                await session.commit()
            return
        except asyncio.CancelledError:
            raise
        except OperationalError as exc:
            if attempt < _ACTIVITY_WRITE_ATTEMPTS and is_database_locked_error(exc):
                await asyncio.sleep(0.2)
                continue
            log.warning(
                "activity write failed | group=%s user=%s reason=%s",
                kwargs.get("group_id", "?"),
                kwargs.get("user_id", "?"),
                exc,
            )
            return
        except SQLAlchemyError as exc:
            log.warning(
                "activity write failed | group=%s user=%s reason=%s",
                kwargs.get("group_id", "?"),
                kwargs.get("user_id", "?"),
                exc,
            )
            return
        except Exception:
            log.exception(
                "activity write failed | group=%s user=%s",
                kwargs.get("group_id", "?"),
                kwargs.get("user_id", "?"),
            )
            return


def _track_activity_task(task: asyncio.Task[None]) -> None:
    _ACTIVITY_TASKS.add(task)
    task.add_done_callback(_ACTIVITY_TASKS.discard)


def schedule_activity_record(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    **kwargs: object,
) -> None:
    """把活跃度落库丢到后台任务，永不阻塞调用方（回复路径）。

    独立 session + 独立 context：主流程回滚、抛错、取消都不会牵连这条统计。
    """

    try:
        task = asyncio.create_task(
            _write_activity_sample(session_factory, dict(kwargs)),
            name=f"activity-record:{kwargs.get('group_id', '?')}",
            context=Context(),
        )
    except RuntimeError:
        # 没有运行中的事件循环（同步调用/退出中）：统计丢了就丢了
        log.debug("activity record skipped | no running event loop")
        return
    _track_activity_task(task)


async def drain_activity_tasks(timeout: float = 3.0) -> None:
    """尽力把还在排队/进行中的活跃度写入落完（机器人关停时调用）。"""

    tasks = {task for task in _ACTIVITY_TASKS if not task.done()}
    if not tasks:
        return
    log.info("draining activity writes | pending=%s", len(tasks))
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=1.0)


# ---------------------------------------------------------------------------
# 每周结算
# ---------------------------------------------------------------------------


def last_complete_week(now: object = None) -> tuple[date, date]:
    """最近一个**完整**的自然周：上一个周一到上一个周日（本地时间）。

    周一 09:00 发周报时，今天是新一周的周一，所以"上一周"就是昨天结束的那一周。
    """

    today = local_today(now)
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(days=7)
    return start, start + timedelta(days=6)


def week_key(week_start: date) -> str:
    """ISO 周标识（``2026-W40``），用作发奖幂等键的一部分。"""

    iso = week_start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def award_ref(week: str, user_id: int) -> str:
    """同一个用户、同一个周只发一次奖的幂等键。"""

    return f"{AWARD_REF_PREFIX}:{week}:{int(user_id)}"


@dataclass(frozen=True, slots=True)
class WeeklyActivityEntry:
    """活跃榜上的一行（只包含达标的人）。"""

    user_id: int
    display_name: str
    rank: int
    active_days: int
    messages: int
    replies_received: int
    score: int
    points: int = 0      # 这个名次对应的奖励分
    awarded: bool = False  # 本次结算是否真的写入了奖励流水


@dataclass(frozen=True, slots=True)
class WeeklyActivityResult:
    """一次周结算的结果，文案层拿到就能直接排版。"""

    group_id: int
    week_start: str          # YYYY-MM-DD（周一）
    week_end: str            # YYYY-MM-DD（周日）
    week: str                # 2026-W40
    entries: tuple[WeeklyActivityEntry, ...]
    participants: int        # 这一周有活跃记录的人数（含不达标的）
    qualified: int           # 达标人数
    total_points: int        # 榜单应发放的总额（满 10 人时是 77）
    awarded_points: int      # 本次实际入账的分（重复结算时为 0）


async def _week_totals(
    session: AsyncSession, *, group_id: int, start: date, end: date
) -> dict[int, tuple[int, int, int]]:
    """窗口内按成员聚合 (活跃天数, 发言条数, 被回复次数)。"""

    rows = await session.execute(
        select(
            MemberActivityDaily.user_id,
            func.count(),
            func.coalesce(func.sum(MemberActivityDaily.messages), 0),
            func.coalesce(func.sum(MemberActivityDaily.replies_received), 0),
        )
        .where(
            MemberActivityDaily.group_id == int(group_id),
            # activity_date 是 YYYY-MM-DD，字符串比较就是日期比较
            MemberActivityDaily.activity_date >= start.isoformat(),
            MemberActivityDaily.activity_date <= end.isoformat(),
        )
        .group_by(MemberActivityDaily.user_id)
    )
    return {
        int(user_id): (int(days or 0), int(messages or 0), int(replies or 0))
        for user_id, days, messages, replies in rows.all()
    }


async def _latest_display_names(
    session: AsyncSession, *, group_id: int, start: date, end: date
) -> dict[int, str]:
    """窗口内每个成员最后一次发言用的昵称（不依赖签到记录）。"""

    rows = await session.execute(
        select(
            MemberActivityDaily.user_id,
            MemberActivityDaily.display_name,
            MemberActivityDaily.updated_at,
        )
        .where(
            MemberActivityDaily.group_id == int(group_id),
            MemberActivityDaily.activity_date >= start.isoformat(),
            MemberActivityDaily.activity_date <= end.isoformat(),
        )
        .order_by(MemberActivityDaily.updated_at)
    )
    names: dict[int, str] = {}
    for user_id, name, _updated in rows.all():
        shown = str(name or "").strip()
        if shown:
            names[int(user_id)] = shown[:255]
    return names


def rank_week(
    totals: dict[int, tuple[int, int, int]],
    names: dict[int, str] | None = None,
) -> list[WeeklyActivityEntry]:
    """纯函数：按周汇总数据排出榜单并分配奖励分。

    ``totals`` 是 ``{user_id: (active_days, messages, replies_received)}``。
    先卡门槛，再按 得分 → 发言数 → user_id 排序（同分时确定，不会一次一个样），
    最后取前 10 名按名次发 25/12/12/4…。
    """

    display = names or {}
    scored: list[WeeklyActivityEntry] = []
    for user_id, (days, messages, replies) in totals.items():
        if not meets_threshold(messages=messages, active_days=days):
            continue
        scored.append(
            WeeklyActivityEntry(
                user_id=int(user_id),
                display_name=str(display.get(int(user_id)) or "") or f"成员{int(user_id)}",
                rank=0,
                active_days=int(days),
                messages=int(messages),
                replies_received=int(replies),
                score=activity_score(
                    messages=messages, active_days=days, replies_received=replies
                ),
            )
        )
    scored.sort(key=lambda item: (-item.score, -item.messages, item.user_id))
    return [
        replace(item, rank=position, points=reward_points_for_rank(position))
        for position, item in enumerate(scored[: _act().weekly_top_n], start=1)
    ]


async def _write_award(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    points: int,
    ref: str,
    created_at: datetime | None = None,
) -> bool:
    """写一条奖励流水；同一 (群, 用户, ref) 已经存在时返回 False（不报错）。

    ``ON CONFLICT DO NOTHING + RETURNING``：冲突时 SQLite 不返回行，所以既能
    判断"这次到底发没发出去"，又不会像裸 INSERT 那样撞唯一索引抛异常。
    """

    stmt = (
        sqlite_insert(MemberPointAward)
        .values(
            group_id=int(group_id),
            user_id=int(user_id),
            points=int(points),
            reason=AWARD_REASON_WEEKLY_ACTIVITY,
            ref=str(ref),
            created_at=created_at or now_shanghai_naive(),
        )
        .on_conflict_do_nothing(index_elements=["group_id", "user_id", "ref"])
        .returning(MemberPointAward.id)
    )
    written = (await session.execute(stmt)).scalar_one_or_none()
    return written is not None


async def settle_weekly_activity(
    session: AsyncSession,
    *,
    group_id: int,
    now: object = None,
    week_start: date | None = None,
    award: bool = True,
) -> WeeklyActivityResult:
    """结算一个完整自然周并发奖；重复调用安全（不会重复加分，也不会抛异常）。

    ``week_start`` 不给就用最近一个完整自然周（上周一）。``award=False`` 只算
    不发（dry-run / 报表预览用）。

    不在这里 commit：调用方决定事务边界（周报与命令行入口各自提交）。
    """

    gid = int(group_id)
    start = week_start or last_complete_week(now)[0]
    end = start + timedelta(days=6)
    key = week_key(start)

    totals = await _week_totals(session, group_id=gid, start=start, end=end)
    names = await _latest_display_names(session, group_id=gid, start=start, end=end)
    ranked = rank_week(totals, names)

    entries: list[WeeklyActivityEntry] = []
    awarded_points = 0
    for entry in ranked:
        written = False
        if award and entry.points > 0:
            written = await _write_award(
                session,
                group_id=gid,
                user_id=entry.user_id,
                points=entry.points,
                ref=award_ref(key, entry.user_id),
            )
        if written:
            awarded_points += entry.points
        entries.append(replace(entry, awarded=written))

    return WeeklyActivityResult(
        group_id=gid,
        week_start=start.isoformat(),
        week_end=end.isoformat(),
        week=key,
        entries=tuple(entries),
        participants=len(totals),
        qualified=len(ranked),
        total_points=sum(entry.points for entry in entries),
        awarded_points=awarded_points,
    )


# ---------------------------------------------------------------------------
# 展示（给群成员看的大白话，不出现表名/内部术语）
# ---------------------------------------------------------------------------

_MEDALS = ("🥇", "🥈", "🥉")


def _period_label(result: WeeklyActivityResult) -> str:
    return (
        f"{result.week_start[5:].replace('-', '/')}–"
        f"{result.week_end[5:].replace('-', '/')}"
    )


def render_activity_lines(result: WeeklyActivityResult) -> list[str]:
    """周报里的"上周活跃榜"段落（纯文本行，由外层统一包成 HTML 引用块）。

    只展示达标成员：得分和拿到的积分都要写清楚，规则也顺带说一句，免得群里
    猜"为什么我没上榜"。
    """

    lines = [f"🏅 上周活跃榜（{_period_label(result)}）"]
    if not result.entries:
        lines.append(
            "这一周还没有人达标（至少要活跃 3 天、聊满 10 句），下周继续加油～"
        )
        return lines

    for entry in result.entries:
        medal = _MEDALS[entry.rank - 1] if entry.rank <= len(_MEDALS) else f"{entry.rank}."
        lines.append(
            f"{medal} {escape(entry.display_name)} · 得分 {entry.score}"
            f" · +{entry.points} 分"
        )
    if result.awarded_points > 0:
        lines.append(
            f"本周一共发出 <b>{result.total_points}</b> 分，已经自动记到大家的积分账户里"
        )
    else:
        # 重复结算、或只算不发的预览：没有新入账，就别写成"刚刚发了"
        lines.append(
            f"本周一共发出 <b>{result.total_points}</b> 分（同一周只发一次，不会重复加）"
        )
    lines.append(
        "得分 = 发言条数 + 活跃天数×2 + 被别人回复的次数；"
        "每天发言超过 20 句不再继续累计"
    )
    return lines


__all__ = [
    "AWARD_REF_PREFIX",
    "COUNTED_MESSAGE_TYPE",
    "MAX_DAILY_MESSAGES",
    "MIN_ACTIVE_DAYS",
    "MIN_MESSAGE_TEXT_LENGTH",
    "MIN_WEEKLY_MESSAGES",
    "TAIL_REWARD_POINTS",
    "WEEKLY_TOTAL_POINTS",
    "WEEKLY_TOP_N",
    "WeeklyActivityEntry",
    "WeeklyActivityResult",
    "activity_score",
    "award_ref",
    "drain_activity_tasks",
    "is_countable_message",
    "last_complete_week",
    "meets_threshold",
    "rank_week",
    "record_message_activity",
    "record_message_activity_safe",
    "render_activity_lines",
    "reward_points_for_rank",
    "schedule_activity_record",
    "settle_weekly_activity",
    "week_key",
]
