"""每日签到与积分：连续签到递增（1→10 封顶），断签从 1 分重来；积分可消费。

三条不变量：

- “一天只能签一次”靠 ``member_checkins`` 上 (群, 用户, 本地自然日) 的唯一索引；
- **可用积分 = 签到流水 SUM + 奖励流水 SUM − 消费流水 SUM**。三张表都是 append-only，
  没有“余额”列，所以不会出现余额和流水对不上的情况（要审计某人的分怎么没的，查流水即可）；
- 奖励（``member_point_awards``，如每周活跃激励）走独立流水，**不写进 member_checkins**：
  连续签到天数是把签到日期倒着数出来的，伪造签到行会把连击算错。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    GlobalBan,
    MemberCheckin,
    MemberPointAward,
    MemberPointSpend,
    UserWarning,
    Violation,
)
from bot.utils.timezone import now_shanghai_naive

# 连续第 N 天得 N 分，第 10 天起封顶；断签一天后重新从 1 分开始。
MAX_DAILY_POINTS = 10

# 消费原因（入库，便于以后审计/扩展别的消费场景）
SPEND_REASON_CHALLENGE = "moderation_challenge"
# 免除一次审核质询的价钱
CHALLENGE_SKIP_COST = 2


def available_from_ledgers(*, earned: int, awarded: int, spent: int) -> int:
    """可用积分的**唯一**定义：签到所得 + 奖励所得 − 已消费。

    其它地方（包括 /rank 的默认榜）要算可用余额都必须走这里，免得哪天加了新的
    积分来源，只有一部分页面跟着变。
    """

    return int(earned) + int(awarded) - int(spent)


@dataclass(frozen=True, slots=True)
class CheckinOutcome:
    """一次签到查询/写入的结果，供回执文案直接使用。"""

    already: bool           # 今天是否已经签过（写入时表示"这次没加分"）
    points_awarded: int     # 本次获得的分（重复签到为 0）
    total_points: int       # 累计获得
    spent_points: int       # 累计消耗
    available_points: int   # 当前可用
    total_days: int         # 累计签到天数
    streak: int             # 当前连续天数
    next_award: int         # 下一次签到能得几分
    capped: bool            # 是否已经到每日封顶


def local_today(now: object = None) -> date:
    """本地（上海）自然日。传入 now 便于测试注入。"""

    moment = now if now is not None else now_shanghai_naive()
    return moment.date() if hasattr(moment, "date") else moment


def award_for_streak(streak: int) -> int:
    """连续第 N 天得 min(N, 10) 分。断签后 streak 归 1，自然回到 1 分。"""

    return max(1, min(int(streak), MAX_DAILY_POINTS))


async def _dates(session: AsyncSession, group_id: int, user_id: int) -> set[str]:
    rows = await session.execute(
        select(MemberCheckin.checkin_date).where(
            MemberCheckin.group_id == int(group_id),
            MemberCheckin.user_id == int(user_id),
        )
    )
    return {str(value) for (value,) in rows.all()}


def _streak(days: set[str], today: date) -> int:
    """从今天往回数连续签到的天数。

    今天还没签时从昨天起算：连续记录不该因为"今天还没签"就先归零，
    否则用户每天早上看到的连续天数都是 0。
    """

    if today.isoformat() in days:
        anchor = today
    elif (today - timedelta(days=1)).isoformat() in days:
        anchor = today - timedelta(days=1)
    else:
        return 0
    streak = 0
    cursor = anchor
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


async def _sums(
    session: AsyncSession, group_id: int, user_id: int
) -> tuple[int, int, int, int]:
    """(累计签到所得, 累计奖励所得, 累计消耗, 签到天数)"""

    earned_row = (
        await session.execute(
            select(func.coalesce(func.sum(MemberCheckin.points), 0), func.count())
            .select_from(MemberCheckin)
            .where(
                MemberCheckin.group_id == int(group_id),
                MemberCheckin.user_id == int(user_id),
            )
        )
    ).one()
    awarded = (
        await session.execute(
            select(func.coalesce(func.sum(MemberPointAward.points), 0))
            .select_from(MemberPointAward)
            .where(
                MemberPointAward.group_id == int(group_id),
                MemberPointAward.user_id == int(user_id),
            )
        )
    ).scalar()
    spent = (
        await session.execute(
            select(func.coalesce(func.sum(MemberPointSpend.points), 0))
            .select_from(MemberPointSpend)
            .where(
                MemberPointSpend.group_id == int(group_id),
                MemberPointSpend.user_id == int(user_id),
            )
        )
    ).scalar()
    return (
        int(earned_row[0] or 0),
        int(awarded or 0),
        int(spent or 0),
        int(earned_row[1] or 0),
    )


async def summarize(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    now: object = None,
) -> CheckinOutcome:
    """只读：今天签没签、可用多少分、连续几天、下次可得几分。"""

    today = local_today(now)
    days = await _dates(session, group_id, user_id)
    earned, awarded, spent, total_days = await _sums(session, group_id, user_id)
    streak = _streak(days, today)
    # 累计获得 = 签到 + 奖励（不含消费），可用 = 累计获得 − 消费，两行对得上
    total = earned + awarded
    return CheckinOutcome(
        already=today.isoformat() in days,
        points_awarded=0,
        total_points=total,
        spent_points=spent,
        available_points=available_from_ledgers(
            earned=earned, awarded=awarded, spent=spent
        ),
        total_days=total_days,
        streak=streak,
        next_award=award_for_streak(streak + 1) if streak else 1,
        capped=streak >= MAX_DAILY_POINTS,
    )


async def available_points(
    session: AsyncSession, *, group_id: int, user_id: int
) -> int:
    """当前可用积分（质询卡决定要不要显示"消耗积分免除"时用）。"""

    earned, awarded, spent, _days = await _sums(session, group_id, user_id)
    return available_from_ledgers(earned=earned, awarded=awarded, spent=spent)


async def record_checkin(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    display_name: str = "",
    now: object = None,
) -> CheckinOutcome:
    """写入今天的签到并加分；今天已经有了就不加分，返回 already=True。

    连续第 N 天得 N 分（第 10 天起封顶 10 分）；中间断一天，连续天数归 1，
    下次签到自然回到 +1 分。
    """

    today = local_today(now)
    already = False
    award = 0
    existing = await _dates(session, group_id, user_id)
    if today.isoformat() not in existing:
        # 先算出"含今天"的连续天数，再决定这次加几分
        streak_with_today = _streak(existing | {today.isoformat()}, today)
        award = award_for_streak(streak_with_today)
        try:
            # SAVEPOINT：只回滚这一条插入，不牵连调用方在这个 session 里
            # 还没提交的其它改动
            async with session.begin_nested():
                session.add(
                    MemberCheckin(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        checkin_date=today.isoformat(),
                        points=award,
                        display_name=str(display_name or "")[:255],
                    )
                )
        except IntegrityError:
            # 唯一索引挡住重复签到：按"今天已签"处理
            already = True
            award = 0
    else:
        already = True

    outcome = await summarize(session, group_id=group_id, user_id=user_id, now=now)
    return CheckinOutcome(
        already=already,
        points_awarded=0 if already else award,
        total_points=outcome.total_points,
        spent_points=outcome.spent_points,
        available_points=outcome.available_points,
        total_days=outcome.total_days,
        streak=outcome.streak,
        next_award=outcome.next_award,
        capped=outcome.capped,
    )


async def spend_points(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    points: int,
    reason: str = "",
    ref: str | None = None,
) -> bool:
    """扣积分；余额不足或同一 ref 已扣过都返回 False（不抛异常）。

    ``ref`` 是幂等键（例如 ``challenge:12``）：唯一索引保证同一次质询只会被扣一次，
    连点两次按钮不会重复扣分。
    """

    cost = max(0, int(points))
    if cost <= 0:
        return False
    if await available_points(session, group_id=group_id, user_id=user_id) < cost:
        return False
    try:
        async with session.begin_nested():
            session.add(
                MemberPointSpend(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    points=cost,
                    reason=str(reason or "")[:64],
                    ref=ref,
                )
            )
    except IntegrityError:
        return False
    return True


# ---------------------------------------------------------------------------
# 积分榜（/rank）与个人档案（/me）
#
# 下面全是只读查询：不碰上面的签到/消费写入路径，也不加"余额"列，
# 所以可用积分仍然等于签到流水减去消费流水。
# ---------------------------------------------------------------------------

# 个人档案里"近 N 天被审核命中"的统计窗口
VIOLATION_WINDOW_DAYS = 30

# 积分榜默认展示的名次数
RANK_LIMIT = 10


@dataclass(frozen=True, slots=True)
class RankEntry:
    """积分榜上的一行。"""

    user_id: int
    display_name: str
    points: int   # 参与排序的分值：默认榜=可用积分，本周榜=本周获得
    days: int     # 参与排序的签到天数（并列时的第二关键字）


@dataclass(frozen=True, slots=True)
class RankBoard:
    """一次 /rank 查询的结果，文案层拿到就能直接排版。"""

    mode: str                        # "all"（默认，按可用积分）或 "week"
    entries: tuple[RankEntry, ...]   # 已按名次排好，最多 RANK_LIMIT 条
    caller_rank: int                 # 调用者名次（1 起；调用者没记录也会算出来）
    caller_points: int               # 调用者参与排序的分值
    caller_available: int            # 调用者可用积分（"可用 N 分"那行用）
    members: int                     # 参与本次排名的总人数
    has_data: bool                   # 本群有没有对应口径的签到数据


@dataclass(frozen=True, slots=True)
class MemberProfile:
    """一个人在本群的档案：积分 + 签到 + 违规/封禁。"""

    available_points: int
    total_points: int
    spent_points: int
    streak: int
    total_days: int
    signed_today: bool
    next_award: int
    warning_count: int
    recent_violations: int
    window_days: int
    group_banned: bool
    globally_banned: bool

    @property
    def banned(self) -> bool:
        """群内封禁和全局封禁都算"在封禁名单里"。"""

        return self.group_banned or self.globally_banned


def _week_start(today: date) -> date:
    """本周一（本地自然日，日历周从周一算起）。"""

    return today - timedelta(days=today.weekday())


async def _earned_by_user(
    session: AsyncSession, group_id: int, *, since: str | None = None
) -> dict[int, tuple[int, int]]:
    """按成员聚合 (获得的积分, 签到天数)；``since`` 非空时只看该本地日（含）之后。"""

    stmt = (
        select(
            MemberCheckin.user_id,
            func.coalesce(func.sum(MemberCheckin.points), 0),
            func.count(),
        )
        .select_from(MemberCheckin)
        .where(MemberCheckin.group_id == int(group_id))
    )
    if since is not None:
        # checkin_date 是 YYYY-MM-DD，字符串比较就是日期比较
        stmt = stmt.where(MemberCheckin.checkin_date >= str(since))
    rows = await session.execute(stmt.group_by(MemberCheckin.user_id))
    return {
        int(user_id): (int(points or 0), int(days or 0))
        for user_id, points, days in rows.all()
    }


async def _spent_by_user(session: AsyncSession, group_id: int) -> dict[int, int]:
    """按成员聚合已消耗积分。"""

    rows = await session.execute(
        select(
            MemberPointSpend.user_id,
            func.coalesce(func.sum(MemberPointSpend.points), 0),
        )
        .where(MemberPointSpend.group_id == int(group_id))
        .group_by(MemberPointSpend.user_id)
    )
    return {int(user_id): int(points or 0) for user_id, points in rows.all()}


async def _awards_by_user(
    session: AsyncSession,
    group_id: int,
    *,
    since: datetime | None = None,
) -> dict[int, int]:
    """按成员聚合奖励所得（每周活跃激励之类）；``since`` 非空时只看该时刻之后。"""

    stmt = (
        select(
            MemberPointAward.user_id,
            func.coalesce(func.sum(MemberPointAward.points), 0),
        )
        .where(MemberPointAward.group_id == int(group_id))
        .group_by(MemberPointAward.user_id)
    )
    if since is not None:
        stmt = stmt.where(MemberPointAward.created_at >= since)
    rows = await session.execute(stmt)
    return {int(user_id): int(points or 0) for user_id, points in rows.all()}


async def _display_names(session: AsyncSession, group_id: int) -> dict[int, str]:
    """每个成员最近一条签到记录的昵称（签到时填的展示名）。"""

    latest = (
        select(
            MemberCheckin.user_id.label("user_id"),
            func.max(MemberCheckin.checkin_date).label("last_day"),
        )
        .where(MemberCheckin.group_id == int(group_id))
        .group_by(MemberCheckin.user_id)
        .subquery()
    )
    rows = await session.execute(
        select(MemberCheckin.user_id, MemberCheckin.display_name)
        .join(
            latest,
            (MemberCheckin.user_id == latest.c.user_id)
            & (MemberCheckin.checkin_date == latest.c.last_day),
        )
        .where(MemberCheckin.group_id == int(group_id))
    )
    return {int(user_id): str(name or "") for user_id, name in rows.all()}


def _rank_order(item: tuple[int, int, int]) -> tuple[int, int, int]:
    """排序关键字：分高的在前，并列看签到天数，再并列按 user_id 稳定排序。"""

    user_id, points, days = item
    return (-points, -days, user_id)


async def build_rank_board(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    week: bool = False,
    now: object = None,
    limit: int = RANK_LIMIT,
) -> RankBoard:
    """排本群积分榜，并算出调用者自己的名次。

    默认榜按**可用积分**（签到流水 + 奖励流水 − 消费流水）；``week=True`` 只累加
    本周一（Asia/Shanghai）以来**获得**的积分（含本周到账的奖励）。并列时签到天数
    多的在前，再按 user_id 稳定排序。调用者即使不在榜内也会拿到名次：没有任何记录
    时排在所有有效记录之后（榜里不会出现这条合成记录，它只出现在"你：第 X 名"那行）。
    """

    gid, uid = int(group_id), int(user_id)
    overall = await _earned_by_user(session, gid)
    awarded = await _awards_by_user(session, gid)
    spent = await _spent_by_user(session, gid)
    available = {
        member: available_from_ledgers(
            earned=overall.get(member, (0, 0))[0],
            awarded=awarded.get(member, 0),
            spent=spent.get(member, 0),
        )
        # 只靠奖励拿分（没签到过）的人也要出现在榜上
        for member in set(overall) | set(awarded)
    }

    if week:
        week_monday = _week_start(local_today(now))
        weekly = await _earned_by_user(session, gid, since=week_monday.isoformat())
        week_awards = await _awards_by_user(
            session, gid, since=datetime.combine(week_monday, time.min)
        )
        scored = {
            member: (
                weekly.get(member, (0, 0))[0] + week_awards.get(member, 0),
                weekly.get(member, (0, 0))[1],
            )
            for member in set(weekly) | set(week_awards)
        }
        mode = "week"
    else:
        scored = {
            member: (available[member], overall.get(member, (0, 0))[1])
            for member in available
        }
        mode = "all"

    ranked = sorted(
        (
            (member, points, days)
            for member, (points, days) in scored.items()
        ),
        key=_rank_order,
    )

    names = await _display_names(session, gid)
    entries = tuple(
        RankEntry(
            user_id=member,
            display_name=names.get(member) or str(member),
            points=points,
            days=days,
        )
        for member, points, days in ranked[: max(1, int(limit))]
    )
    if uid in scored:
        caller_rank = next(
            rank
            for rank, (member, _points, _days) in enumerate(ranked, start=1)
            if member == uid
        )
    else:
        # 调用者没有任何记录：真实记录（签到天数都 ≥1）全都排在它前面。
        # 这里不把它塞进 entries——榜只列真实数据，它只出现在"你：第 X 名"那行。
        caller_rank = len(ranked) + 1
    return RankBoard(
        mode=mode,
        entries=entries,
        caller_rank=caller_rank,
        caller_points=scored.get(uid, (0, 0))[0],
        caller_available=available.get(uid, 0),
        members=len(scored),
        has_data=bool(scored),
    )


def _violation_cutoff(now: object = None) -> datetime:
    """「近 30 天」的统计起点。

    ``violations.created_at`` 存的是 UTC，所以本地（上海）时间要先减 8 小时再
    往回推，否则 00:00-08:00 的命中会被算错一天。
    """

    if now is None:
        moment = datetime.now(timezone.utc).replace(tzinfo=None)
    elif isinstance(now, datetime):
        moment = now - timedelta(hours=8)
    else:
        moment = datetime.combine(now, time.min) - timedelta(hours=8)
    return moment - timedelta(days=VIOLATION_WINDOW_DAYS)


async def member_profile(
    session: AsyncSession, *, group_id: int, user_id: int, now: object = None
) -> MemberProfile:
    """只读：把 /me 要的积分、签到、违规和封禁一次查齐。"""

    gid, uid = int(group_id), int(user_id)
    outcome = await summarize(session, group_id=gid, user_id=uid, now=now)
    warning = (
        (
            await session.execute(
                select(UserWarning)
                .where(UserWarning.group_id == gid, UserWarning.user_id == uid)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    recent = (
        await session.execute(
            select(func.count())
            .select_from(Violation)
            .where(
                Violation.group_id == gid,
                Violation.user_id == uid,
                Violation.created_at >= _violation_cutoff(now),
            )
        )
    ).scalar()
    globally = (
        await session.execute(
            select(func.count()).select_from(GlobalBan).where(GlobalBan.user_id == uid)
        )
    ).scalar()
    return MemberProfile(
        available_points=outcome.available_points,
        total_points=outcome.total_points,
        spent_points=outcome.spent_points,
        streak=outcome.streak,
        total_days=outcome.total_days,
        signed_today=outcome.already,
        next_award=outcome.next_award,
        warning_count=int(getattr(warning, "count", 0) or 0),
        recent_violations=int(recent or 0),
        window_days=VIOLATION_WINDOW_DAYS,
        group_banned=bool(getattr(warning, "is_banned", False)),
        globally_banned=bool(globally),
    )
