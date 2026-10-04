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

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import ColumnElement, DateTime, func, insert, literal, select
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


# ---------------------------------------------------------------------------
# 回执文案：`/checkin` 命令与「✅ 一键签到」按钮**共用同一个渲染函数**
#
# 为什么抽出来：两条入口各写一份文案，改口径时必然有一边忘了改，用户就会在命令
# 和按钮里看到两份不一致的回执。这里只留一个"结果 → 文案"的出口：
# ``checkin_receipt()`` 取数，``render_checkin_receipt()`` 出 HTML 卡片（命令用），
# ``render_checkin_toast()`` 出按钮的轻提示（toast 有长度限制，是压缩版）。
# 注意：抽取只是搬家，`/checkin` 的文案逐字未变（tests/test_checkin.py 会盯着）。
# ---------------------------------------------------------------------------

# 「✅ 一键签到」按钮的固定 callback_data。**绝不能**把用户 ID 编进来：
# 点击者身份只由 callback_query.from_user 决定；一旦把 ID 写进按钮，
# 谁点的按钮谁就可能被算成别人签到。
CHECKIN_CALLBACK_DATA = "checkin:v1"

CHECKIN_BUTTON_TEXT = "✅ 一键签到"

# answer_callback_query 的 toast 上限：Telegram 限制 200 字符，这里留余量。
CHECKIN_TOAST_MAX_CHARS = 180


@dataclass(frozen=True, slots=True)
class CheckinReceipt:
    """回执的字段视图：命令与按钮两条入口都从这里取数字。"""

    already: bool      # 今天是否已经签过
    day: int           # 本次签到是连续第几天
    awarded: int       # 本次获得的分
    available: int     # 当前可用分
    streak: int        # 当前连续天数
    total_days: int    # 累计签到天数
    next_award: int    # 下一次可得几分
    capped: bool       # 是否已到每日封顶


def checkin_receipt(outcome: CheckinOutcome) -> CheckinReceipt:
    """把一次签到结果收敛成回执字段（**唯一**的取数出口）。"""

    return CheckinReceipt(
        already=bool(outcome.already),
        day=int(outcome.streak),
        awarded=int(outcome.points_awarded),
        available=int(outcome.available_points),
        streak=int(outcome.streak),
        total_days=int(outcome.total_days),
        next_award=int(outcome.next_award),
        capped=bool(outcome.capped),
    )


def render_checkin_receipt(outcome: CheckinOutcome) -> str:
    """`/checkin` 的完整回执（HTML）。抽取前后逐字一致。"""

    receipt = checkin_receipt(outcome)
    if receipt.already:
        return (
            "<b>今天已经签过了</b>\n"
            f"可用 <b>{receipt.available}</b> 分｜连续 {receipt.streak} 天"
            f"｜共签到 {receipt.total_days} 天\n"
            f"明天 0 点后再来，可得 +{receipt.next_award} 分。"
        )
    return (
        f"<b>签到成功 · 第 {receipt.day} 天 +{receipt.awarded} 分</b>\n"
        f"可用 <b>{receipt.available}</b> 分｜连续 {receipt.streak} 天"
        f"｜共签到 {receipt.total_days} 天\n"
        + (
            "已连续 10 天以上，每天都是满额 +10 分。"
            if receipt.capped
            else f"明天签到可得 +{receipt.next_award} 分。"
        )
    )


def render_checkin_toast(outcome: CheckinOutcome) -> str:
    """「一键签到」按钮的回执轻提示（纯文本一行，不再往群里发消息）。

    与 `/checkin` 的卡片同源（都走 ``checkin_receipt``），只是压成一行：
    toast 太长会被 Telegram 截断，而群里再补一条回执就是刷屏。
    """

    receipt = checkin_receipt(outcome)
    if receipt.already:
        return (
            f"今天已经签过了｜可用 {receipt.available} 分"
            f"｜连续 {receipt.streak} 天｜明天可得 +{receipt.next_award} 分"
        )
    return (
        f"签到成功 · 第 {receipt.day} 天 +{receipt.awarded} 分"
        f"｜可用 {receipt.available} 分｜连续 {receipt.streak} 天"
    )


async def spend_points(
    session: AsyncSession,
    *,
    group_id: int,
    user_id: int,
    points: int,
    reason: str = "",
    ref: str,
    extra_guard: ColumnElement[bool] | None = None,
) -> bool:
    """扣积分；余额不足或同一 ref 已扣过都返回 False（业务失败不抛异常）。

    ``ref`` 是**必填**的幂等键（例如 ``challenge:12``）：唯一索引保证同一次质询
    只会被扣一次，连点两次按钮不会重复扣分。

    F-054：以前 ``ref`` 默认 ``None``，而 SQLite 的唯一索引把 NULL 视为互不相等，
    于是"没传 ref"的调用完全没有幂等保护。为了让问题在开发期就暴露，这里把
    ``ref`` 改成必填参数，并对空串显式抛 ``ValueError``（宁可调用点当场报错，
    也不要静默地少一层去重）。

    余额检查与扣分是**同一条写语句**：可用积分是"签到 + 奖励 − 消费"的 SUM 派生值，
    先 SELECT 余额再 INSERT 消费行的话，两个不同 ``ref`` 的并发消费会各自读到同一份
    旧余额、双双通过检查，把余额扣成负数。这里把条件直接写进
    ``INSERT ... SELECT ... WHERE 余额 >= cost``，整条语句在 SQLite 的写锁之下执行，
    ``rowcount`` 为 0 就代表余额不足（或并发下已被别人扣掉）。

    ``extra_guard``（可选）把"每日次数上限"这类**计数型**闸门也变成同一条语句的
    ``WHERE`` 条件，而不是先 ``SELECT COUNT`` 再判：``/draw`` 的每日 10 次上限
    之前是 check-then-act，25 个并发请求会各自读到同一份计数、双双通过，把
    上限变成 25 次（抽奖的期望值是净赚，所以这是直接的积分泄漏）。守卫表达式
    必须只依赖当前行可见的数据（典型写法是「当日某个 ref 前缀的行数 < 上限」的
    ``scalar_subquery``），这样它和余额条件一样在写锁下判完。``None`` = 不加
    额外条件，行为与以前完全一致。
    """

    idempotency_key = str(ref or "").strip()
    if not idempotency_key:
        raise ValueError(
            "spend_points 需要非空的 ref 幂等键："
            "SQLite 的唯一索引把 NULL 当作互不相等，ref=None 等于没有去重"
        )

    cost = max(0, int(points))
    if cost <= 0:
        return False
    gid, uid = int(group_id), int(user_id)
    earned = (
        select(func.coalesce(func.sum(MemberCheckin.points), 0))
        .where(MemberCheckin.group_id == gid, MemberCheckin.user_id == uid)
        .scalar_subquery()
    )
    awarded = (
        select(func.coalesce(func.sum(MemberPointAward.points), 0))
        .where(MemberPointAward.group_id == gid, MemberPointAward.user_id == uid)
        .scalar_subquery()
    )
    spent = (
        select(func.coalesce(func.sum(MemberPointSpend.points), 0))
        .where(MemberPointSpend.group_id == gid, MemberPointSpend.user_id == uid)
        .scalar_subquery()
    )
    # 余额守卫和 extra_guard 在**同一条**语句里判完：并发下后到的那些请求看到的
    # 是前一笔已经提交的行数，不会拿着同一份旧计数一起通过。
    condition = earned + awarded - spent >= cost
    if extra_guard is not None:
        condition = condition & extra_guard
    statement = (
        sqlite_insert(MemberPointSpend)
        .from_select(
            # F-050：Core 的 INSERT ... SELECT 不会套用 Python 侧列默认值，所以
            # created_at 必须显式带上，且用本地（Asia/Shanghai）时钟，和
            # member_checkins / member_entitlements / member_point_awards 同口径。
            ("group_id", "user_id", "points", "reason", "ref", "created_at"),
            select(
                literal(gid),
                literal(uid),
                literal(cost),
                literal(str(reason or "")[:64]),
                literal(idempotency_key[:64]),
                literal(now_shanghai_naive(), type_=DateTime),
            ).where(condition),
        )
        # F-053 验收修补（SQLite 原子性）：这里原来包在 ``session.begin_nested()``
        # 里捕获 IntegrityError，但 pysqlite/aiosqlite 驱动对 SAVEPOINT 支持不完整，
        # 保存点会**提前提交外层事务**（已实证）。后果是：扣分一旦写入，外层
        # ``commit()`` 失败再 rollback 也撤不回来 —— 用户白扣分、拿不到权益，
        # 提示却写着"这次没有扣分"。改成原生 ``ON CONFLICT DO NOTHING``：
        # 不抛异常、不改变事务边界，幂等仍由唯一索引保证，
        # ``rowcount == 1`` 依旧是"这次真的扣到了"的判据。
        .on_conflict_do_nothing()
    )
    result = await session.execute(statement)
    return int(getattr(result, "rowcount", 0) or 0) == 1


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
