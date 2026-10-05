"""群健康与审核质量报表：把"机器人这几天判得准不准"变成能看的数字。

为什么需要它：审核模型会把群里的日常话题（存储/套餐/邀请/白名单）读成广告，
而"判错了几次"过去只能靠翻日志发现。这里把三个口径落成固定报表：

- **命中**：violations 表（消息级审核）
- **放行/误伤**：ban_audit_events 里 action='clear' 的记录（复核改判、管理员放行）
- **边缘判定**：置信度 < MARGINAL_CONFIDENCE 的命中——最可能误伤的那一类

时区坑：``violations.created_at`` 与 ``ban_audit_events.created_at`` 是 UTC 朴素时间，
``group_message_archive.sent_at`` 和 ``member_checkins.checkin_date`` 是本地（+8）——
两者不能直接同窗口比较，下面的 _utc_since / _local_since 就是干这个的。

全部只读，离线跑，不参与审核主链路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Sequence

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    AuthorizedGroup,
    BanAuditEvent,
    Group,
    GroupMessageArchive,
    JoinVerification,
    MemberCheckin,
    MemberPointSpend,
    ModerationRule,
    Violation,
)
from bot.services.checkin import local_today
from bot.utils.security import escape_html
from bot.utils.timezone import now_shanghai_naive

# 置信度低于这个值 = "边缘判定"：模型说违规但自己也不确定，最可能误伤
MARGINAL_CONFIDENCE = 0.9
LOCAL_UTC_OFFSET_HOURS = 8

# 置信度分档：调 moderation.high_confidence_threshold 之前，先看命中实际落在哪一档。
# 键是稳定的机器名（报表/测试都用它），值是给人看的区间标签。
CONFIDENCE_BANDS: tuple[tuple[str, str], ...] = (
    ("lt_0_5", "&lt;0.5"),
    ("0_5_0_7", "0.5–0.7"),
    ("0_7_0_9", "0.7–0.9"),
    ("ge_0_9", "≥0.9"),
)


def confidence_band(value: float) -> str:
    """把 0–1 的置信度归到 ``CONFIDENCE_BANDS`` 的某一档。"""

    numeric = float(value)
    if numeric < 0.5:
        return "lt_0_5"
    if numeric < 0.7:
        return "0_5_0_7"
    if numeric < 0.9:
        return "0_7_0_9"
    return "ge_0_9"


def _utc_since(days: int) -> datetime:
    return now_shanghai_naive() - timedelta(hours=LOCAL_UTC_OFFSET_HOURS) - timedelta(days=max(1, int(days)))


def _local_since(days: int) -> datetime:
    return now_shanghai_naive() - timedelta(days=max(1, int(days)))


def _local_since_date(days: int) -> str:
    return (local_today() - timedelta(days=max(1, int(days)))).isoformat()


@dataclass(frozen=True, slots=True)
class ModerationQuality:
    days: int
    total: int = 0
    by_action: dict[str, int] = field(default_factory=dict)
    by_rule: list[tuple[str, int]] = field(default_factory=list)
    members: int = 0
    repeat_members: int = 0
    marginal: int = 0
    confident: int = 0
    no_confidence: int = 0
    cleared_appeal: int = 0
    cleared_admin: int = 0
    top_reasons: list[tuple[str, int]] = field(default_factory=list)
    join_bans: int = 0
    bans_by_source: dict[str, int] = field(default_factory=dict)
    pending_challenges: int = 0
    # 有置信度的命中按 CONFIDENCE_BANDS 分档计数（无置信度的不进任何档）
    confidence_bands: dict[str, int] = field(default_factory=dict)
    # 落在 moderation.high_confidence_threshold 之上的命中数（默认 0.9）
    high_confidence_hits: int = 0
    high_confidence_threshold: float = MARGINAL_CONFIDENCE

    @property
    def cleared(self) -> int:
        return int(self.cleared_appeal) + int(self.cleared_admin)

    @property
    def false_positive_rate(self) -> float | None:
        """被复核/管理员放行的比例。没有任何放行记录时返回 0.0（而不是 None）。"""

        if self.total <= 0:
            return None
        return self.cleared / self.total

    @property
    def high_confidence_ratio(self) -> float | None:
        """高置信命中占窗口内全部命中的比例。

        分母是全部命中（含没有置信度的历史行）——那正是运营要回答的问题：
        "把阈值调到多少，才不会让大部分命中都落进不可用的区间"。
        """

        if self.total <= 0:
            return None
        return self.high_confidence_hits / self.total


@dataclass(frozen=True, slots=True)
class ActivitySummary:
    days: int
    messages: int = 0
    senders: int = 0
    checkin_count: int = 0
    checkin_members: int = 0
    points_awarded: int = 0
    top_members: list[tuple[str, int]] = field(default_factory=list)


async def _rule_labels(session: AsyncSession, group_id: int) -> dict[int, str]:
    rows = await session.execute(
        select(ModerationRule.id, ModerationRule.rule_type, ModerationRule.pattern).where(
            ModerationRule.group_id == int(group_id)
        )
    )
    labels: dict[int, str] = {}
    for rule_id, rule_type, pattern in rows.all():
        text = " ".join(str(pattern or "").split())
        labels[int(rule_id)] = f"[{rule_type}] {text[:28]}"
    return labels


async def collect_quality(
    session: AsyncSession,
    *,
    group_id: int,
    days: int = 7,
    high_threshold: float | None = None,
) -> ModerationQuality:
    """窗口内的审核质量：命中构成、边缘判定、放行（误伤）数。

    ``high_threshold`` 是运行时配置的 ``moderation.high_confidence_threshold``；
    不传就按默认 0.9 统计"高置信占比"。
    """

    gid = int(group_id)
    window = max(1, int(days))
    confidence_threshold = (
        MARGINAL_CONFIDENCE if high_threshold is None else float(high_threshold)
    )
    utc_since = _utc_since(window)

    total = (
        await session.execute(
            select(func.count())
            .select_from(Violation)
            .where(Violation.group_id == gid, Violation.created_at >= utc_since)
        )
    ).scalar() or 0

    action_rows = await session.execute(
        select(Violation.action_taken, func.count())
        .where(Violation.group_id == gid, Violation.created_at >= utc_since)
        .group_by(Violation.action_taken)
    )
    by_action = {str(action or "?"): int(count) for action, count in action_rows.all()}

    members = (
        await session.execute(
            select(func.count(func.distinct(Violation.user_id))).where(
                Violation.group_id == gid, Violation.created_at >= utc_since
            )
        )
    ).scalar() or 0

    # F-032：过去这里把窗口内**每一行**的 confidence 整列拉进 Python 再统计。报表是
    # 每群 8 次窗口查询里最贵的一次（命中数大时是 O(窗口内命中数) 的行传输 + Python
    # 循环）。改成 SQL 侧一次聚合出全部 7 个计数，返回值逐个等价。
    confidence_row = (
        await session.execute(
            select(
                func.count(),
                func.sum(case((Violation.confidence.is_(None), 1), else_=0)),
                func.sum(
                    case(
                        (
                            and_(
                                Violation.confidence.isnot(None),
                                Violation.confidence >= confidence_threshold,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            and_(
                                Violation.confidence.isnot(None),
                                Violation.confidence < MARGINAL_CONFIDENCE,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            and_(
                                Violation.confidence.isnot(None),
                                Violation.confidence >= MARGINAL_CONFIDENCE,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            ).where(
                Violation.group_id == gid, Violation.created_at >= utc_since
            )
        )
    ).one()
    total_confidence = int(confidence_row[0] or 0)
    no_confidence = int(confidence_row[1] or 0)
    high_hits = int(confidence_row[2] or 0)
    marginal = int(confidence_row[3] or 0)
    confident = int(confidence_row[4] or 0)
    bands = {key: 0 for key, _label in CONFIDENCE_BANDS}
    if total_confidence:
        # 区间分布仍要在 Python 里算：confidence_band 的边界是按相邻 band 的中点
        # 动态算出来的（CONFIDENCE_BANDS 可被配置改写），SQL 里复刻不了。
        band_rows = await session.execute(
            select(Violation.confidence).where(
                Violation.group_id == gid,
                Violation.created_at >= utc_since,
                Violation.confidence.isnot(None),
            )
        )
        for (value,) in band_rows.all():
            bands[confidence_band(float(value))] += 1

    labels = await _rule_labels(session, gid)
    rule_rows = await session.execute(
        select(Violation.rule_id, func.count())
        .where(Violation.group_id == gid, Violation.created_at >= utc_since)
        .group_by(Violation.rule_id)
        .order_by(func.count().desc())
        .limit(4)
    )
    by_rule = [
        (labels.get(int(rule_id), "规则已删除") if rule_id is not None else "未标注规则", int(count))
        for rule_id, count in rule_rows.all()
    ]

    repeat_members = 0
    if members:
        # F-032：同一条 GROUP BY 的结果过去整列拉进 Python 只为数"出现 >1 次的人"。
        # 改成 SQL 侧对子查询计数，行数不再随窗口内命中数增长。
        repeat_members = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(
                        select(Violation.user_id)
                        .where(
                            Violation.group_id == gid,
                            Violation.created_at >= utc_since,
                        )
                        .group_by(Violation.user_id)
                        .having(func.count() > 1)
                        .subquery()
                    )
                )
            ).scalar()
            or 0
        )

    reason_rows = await session.execute(
        select(Violation.verdict_reason, func.count())
        .where(
            Violation.group_id == gid,
            Violation.created_at >= utc_since,
            Violation.verdict_reason != "",
        )
        .group_by(Violation.verdict_reason)
        .order_by(func.count().desc())
        .limit(3)
    )
    top_reasons = [(str(reason)[:24], int(count)) for reason, count in reason_rows.all()]

    clear_rows = await session.execute(
        select(BanAuditEvent.source, func.count())
        .where(
            BanAuditEvent.group_id == gid,
            BanAuditEvent.action == "clear",
            BanAuditEvent.created_at >= utc_since,
        )
        .group_by(BanAuditEvent.source)
    )
    cleared_by_source = {str(source or ""): int(count) for source, count in clear_rows.all()}

    ban_rows = await session.execute(
        select(BanAuditEvent.source, func.count())
        .where(
            BanAuditEvent.group_id == gid,
            BanAuditEvent.action == "ban",
            BanAuditEvent.created_at >= utc_since,
        )
        .group_by(BanAuditEvent.source)
    )
    ban_by_source = {str(source or "未知"): int(count) for source, count in ban_rows.all()}
    join_bans = int(ban_by_source.get("profile_screening", 0))

    pending = (
        await session.execute(
            select(func.count())
            .select_from(JoinVerification)
            .where(
                JoinVerification.group_id == gid,
                JoinVerification.kind == "moderation",
                JoinVerification.status == "pending",
            )
        )
    ).scalar() or 0

    return ModerationQuality(
        days=window,
        total=int(total),
        by_action=by_action,
        by_rule=by_rule,
        members=int(members),
        repeat_members=int(repeat_members),
        marginal=marginal,
        confident=confident,
        no_confidence=no_confidence,
        cleared_appeal=int(cleared_by_source.get("appeal_recheck", 0)),
        cleared_admin=int(cleared_by_source.get("admin_review", 0)),
        top_reasons=top_reasons,
        join_bans=int(join_bans),
        bans_by_source=ban_by_source,
        pending_challenges=int(pending),
        confidence_bands=bands,
        high_confidence_hits=int(high_hits),
        high_confidence_threshold=confidence_threshold,
    )


async def collect_activity(
    session: AsyncSession, *, group_id: int, days: int = 7
) -> ActivitySummary:
    """窗口内的活跃度：消息量、发言人数、签到与积分。"""

    gid = int(group_id)
    window = max(1, int(days))
    local_since = _local_since(window)
    since_date = _local_since_date(window)

    messages = (
        await session.execute(
            select(func.count())
            .select_from(GroupMessageArchive)
            .where(
                GroupMessageArchive.group_id == gid,
                GroupMessageArchive.sent_at >= local_since,
            )
        )
    ).scalar() or 0
    senders = (
        await session.execute(
            select(func.count(func.distinct(GroupMessageArchive.sender_id))).where(
                GroupMessageArchive.group_id == gid,
                GroupMessageArchive.sent_at >= local_since,
            )
        )
    ).scalar() or 0

    checkin_rows = await session.execute(
        select(
            func.count(),
            func.count(func.distinct(MemberCheckin.user_id)),
            func.coalesce(func.sum(MemberCheckin.points), 0),
        )
        .select_from(MemberCheckin)
        .where(
            MemberCheckin.group_id == gid,
            MemberCheckin.checkin_date >= since_date,
        )
    )
    checkin_count, checkin_members, points_awarded = checkin_rows.one()

    earned = (
        select(
            MemberCheckin.user_id.label("user_id"),
            func.sum(MemberCheckin.points).label("earned"),
        )
        .where(
            MemberCheckin.group_id == gid,
            MemberCheckin.checkin_date >= since_date,
        )
        .group_by(MemberCheckin.user_id)
        .subquery()
    )
    spent = (
        select(
            MemberPointSpend.user_id.label("user_id"),
            func.sum(MemberPointSpend.points).label("spent"),
        )
        .where(MemberPointSpend.group_id == gid)
        .group_by(MemberPointSpend.user_id)
        .subquery()
    )
    board_rows = await session.execute(
        select(
            earned.c.user_id,
            (earned.c.earned - func.coalesce(spent.c.spent, 0)).label("points"),
        )
        .select_from(earned)
        .outerjoin(spent, spent.c.user_id == earned.c.user_id)
        .order_by((earned.c.earned - func.coalesce(spent.c.spent, 0)).desc())
        .limit(3)
    )
    top_members: list[tuple[str, int]] = []
    for user_id, points in board_rows.all():
        name_row = await session.execute(
            select(MemberCheckin.display_name)
            .where(MemberCheckin.user_id == int(user_id), MemberCheckin.group_id == gid)
            .order_by(MemberCheckin.id.desc())
            .limit(1)
        )
        display = str(name_row.scalar_one_or_none() or "") or f"成员{int(user_id)}"
        top_members.append((display[:18], int(points or 0)))

    return ActivitySummary(
        days=window,
        messages=int(messages),
        senders=int(senders),
        checkin_count=int(checkin_count or 0),
        checkin_members=int(checkin_members or 0),
        points_awarded=int(points_awarded or 0),
        top_members=top_members,
    )


def _rate_text(quality: ModerationQuality) -> str:
    rate = quality.false_positive_rate
    if rate is None:
        return "本期没有审核命中"
    return f"{rate * 100:.0f}%（{quality.cleared}/{quality.total} 条命中被改判放行）"


def _blockquote(title: str, lines: list[str]) -> str:
    """标题 + 可展开引用块。

    管理命令的 _render_action_response 对带 <blockquote> 的正文原样透传，
    所以这里的排版不会被二次包装破坏。
    """

    body = "\n".join(line for line in lines if str(line).strip())
    return f"<b>{title}</b>\n<blockquote expandable>{body}</blockquote>"


def render_quality_report(
    quality: ModerationQuality,
    activity: ActivitySummary | None = None,
    *,
    activity_lines: Sequence[str] | None = None,
) -> str:
    """审核质量报表（Telegram HTML；标题 + 可展开明细）。

    ``activity_lines`` 是外部（每周活跃激励结算）算好的"上周活跃榜"文案行，
    这里只负责排版，不重复统计也不发奖。
    """

    title = f"审核质量 · 近 {quality.days} 天"
    lines: list[str] = []
    if quality.total <= 0:
        lines.append("本期没有任何审核命中。")
    else:
        action_text = "｜".join(
            f"{name} {count}" for name, count in sorted(quality.by_action.items())
        )
        lines.append(f"命中 <b>{quality.total}</b> 条（{action_text}）")
        lines.append(
            f"涉及 <b>{quality.members}</b> 位成员，其中复犯 {quality.repeat_members} 人"
        )
        lines.append(
            f"边缘判定（置信 &lt; {MARGINAL_CONFIDENCE:g}）<b>{quality.marginal}</b> 条"
            f"，高置信 {quality.confident} 条"
        )
        if any(quality.confidence_bands.values()):
            band_text = "｜".join(
                f"{label} {quality.confidence_bands.get(key, 0)}"
                for key, label in CONFIDENCE_BANDS
            )
            lines.append(f"置信度分档：{band_text}")
            ratio = quality.high_confidence_ratio
            if ratio is not None:
                lines.append(
                    f"高于高置信阈值（{quality.high_confidence_threshold:g}）："
                    f"<b>{quality.high_confidence_hits}</b> 条，"
                    f"占全部命中 {ratio * 100:.0f}%"
                )
        if quality.no_confidence:
            lines.append(
                f"（另有 {quality.no_confidence} 条命中没有模型置信度："
                "本地正则/关键词命中、图片守卫，或置信度字段上线前的历史行）"
            )
        lines.append(
            f"被放行/判定误伤：<b>{quality.cleared}</b> 条"
            f"（模型复核 {quality.cleared_appeal}｜管理员 {quality.cleared_admin}）"
            f" → 误伤率 {_rate_text(quality)}"
        )
        if quality.by_rule:
            lines.append("命中最多的规则：")
            lines.extend(
                f"· {escape_html(label)} — {count} 条" for label, count in quality.by_rule
            )
        if quality.top_reasons:
            lines.append("模型给的理由（Top3）：")
            lines.extend(
                f"· {escape_html(reason)} — {count} 条"
                for reason, count in quality.top_reasons
            )
        ban_text = f"入群资料拦截 {quality.join_bans} 人"
        others = {
            source: count
            for source, count in quality.bans_by_source.items()
            if source != "profile_screening" and count
        }
        if others:
            ban_text += "｜其它封禁：" + "、".join(
                f"{source} {count}" for source, count in sorted(others.items())
            )
        lines.append(f"{ban_text}｜待完成质询 {quality.pending_challenges} 个")
    if activity is not None:
        lines.append("")
        lines.append(f"【群活跃 · 近 {activity.days} 天】")
        lines.append(
            f"消息 <b>{activity.messages}</b> 条｜发言成员 <b>{activity.senders}</b> 人"
        )
        lines.append(
            f"签到 {activity.checkin_count} 人次（{activity.checkin_members} 人）"
            f"，共发出 {activity.points_awarded} 分"
        )
        if activity.top_members:
            medals = ["🥇", "🥈", "🥉"]
            board = "｜".join(
                f"{medals[index] if index < 3 else ''}{escape_html(name)} {points} 分"
                for index, (name, points) in enumerate(activity.top_members)
            )
            lines.append(f"本周积分榜：{board}")
    if activity_lines:
        lines.append("")
        lines.extend(str(line) for line in activity_lines if str(line).strip())
    return _blockquote(title, lines)


async def render_group_quality(
    session: AsyncSession,
    *,
    group_id: int,
    days: int = 7,
    activity_lines: Sequence[str] | None = None,
    high_threshold: float | None = None,
) -> str:
    """一次取齐并渲染（命令与周报共用）。"""

    quality = await collect_quality(
        session,
        group_id=group_id,
        days=days,
        high_threshold=high_threshold,
    )
    activity = await collect_activity(session, group_id=group_id, days=days)
    return render_quality_report(quality, activity, activity_lines=activity_lines)


async def authorized_group_ids(session: AsyncSession) -> list[int]:
    """当前有效的授权群（周报要发给谁）。"""

    rows = await session.execute(
        select(AuthorizedGroup.group_id)
        .join(Group, Group.id == AuthorizedGroup.group_id, isouter=True)
        .where(AuthorizedGroup.bot_present == True)  # noqa: E712
        .order_by(AuthorizedGroup.group_id)
    )
    return [int(value) for (value,) in rows.all()]


__all__ = [
    "ActivitySummary",
    "CONFIDENCE_BANDS",
    "MARGINAL_CONFIDENCE",
    "ModerationQuality",
    "authorized_group_ids",
    "collect_activity",
    "collect_quality",
    "confidence_band",
    "render_group_quality",
    "render_quality_report",
]
