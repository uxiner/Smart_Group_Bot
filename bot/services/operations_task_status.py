"""只读运营任务状态聚合（第三优先第一批）。

本模块只做**读取与纯计算**：不初始化任何任务、不发送、不认领 occurrence、不结算
积分、不写库。所有对外时间都带 ``Asia/Shanghai`` 偏移；无法观测的事实一律返回
``None`` + ``reason``，绝不猜 0 或猜时间。

三条口径红线：

1. **水位不是成功。** ``scheduled_messages.last_run_at`` 在创建与重新启用时也会被
   锚定，只有携带 ``last_message_id`` 才说明「发出去过」，而连它也**不带发送完成
   时刻**（表里没有 delivered_at）。因此成功证据必须区分 ``basis`` 与 ``reason``。
2. **占位不是成功。** ``weekly_report_posts.message_id = 0`` /
   ``checkin_reminder_posts.message_id = 0`` 是「先占位、发完再回填」的占位行；
   ``created_at`` 只是登记时刻。签到用 ``delivered_at`` 作为首选证据。
3. **来源只报程序实现。** 外部 cron 的登记情况未知（API 不读宿主 crontab、不跑
   shell、不猜部署计划），一律 ``external_schedule_known = False`` +
   ``external_schedule_reason = "external_schedule_unknown"``；定时消息按既有
   scheduler 口径纯计算下一次计划，算不出来就给 reason，绝不用 ``now + 间隔``
   伪造 ``next_run_at``。

契约细节见 ``docs/third-priority-batch1-delivery.md``。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import func, or_, select

from bot.db.models import (
    CheckinReminderPost,
    GroupSummary,
    GroupSummaryFailureState,
    MemberEntitlement,
    MemberPointAward,
    ScheduledMessage,
    ScheduledMessageOccurrence,
    WeeklyReportPost,
)
from bot.services.activity import AWARD_REASON_WEEKLY_ACTIVITY, AWARD_REF_PREFIX
from bot.services.checkin_reminder import reminder_slots
from bot.services.patrol import parse_schedule_time
from bot.services.task_policy import SOURCE_INTERNAL, slot_moment
from bot.services.scheduled_messages import _MIN_INTERVAL_MINUTES
from bot.utils.timezone import SHANGHAI_TZ, now_shanghai_naive, to_shanghai_datetime

#: 固定六类任务的稳定顺序（API 与前端都依赖它）。
TASK_IDS: tuple[str, ...] = (
    "scheduled_messages",
    "group_summary",
    "checkin_reminder",
    "activity_award",
    "weekly_report",
    "shop_expire",
)

TASK_TITLES: dict[str, str] = {
    "scheduled_messages": "定时消息",
    "group_summary": "群摘要",
    "checkin_reminder": "签到提醒",
    "activity_award": "周活跃结算",
    "weekly_report": "周报",
    "shop_expire": "商店到期处理",
}

#: 外部 cron 的登记情况不在程序可观测范围内。
EXTERNAL_SCHEDULE_UNKNOWN = "external_schedule_unknown"

#: 水位字段的固定说明：它是调度用锚点，不是投递证据。
SCHEDULE_WATERMARK_MEANING = (
    "调度水位：创建与重新启用定时消息时会以当前时刻锚定，只有投递成功才会推进；"
    "它本身不是投递成功的证据。"
)

#: 周报台账的登记时刻口径说明。
WEEK_REGISTERED_AT_MEANING = (
    "该周报台账的登记时刻（work row 创建时间），不是投递完成时刻；"
    "表内没有 delivered_at，因此不把登记时刻当作发送完成时间。"
)

#: 窗口说明：列表是「最近若干条」的展示上限，**事实**（计数/成功/水位/最早计划）
#: 一律取自全群台账，不受窗口影响。
_RECENT_WINDOW_MEANING = (
    "列表只给最近若干条用于展示；计数、成功证据、水位与最早计划都按全群台账计算，"
    "不受该展示上限影响。"
)

#: ``last_failure.count`` 的单位标注（不同任务的 count 含义不同）。
_COUNT_MEANING_SCHEDULED_RETRY = "scheduled_messages_pending_retry_occurrences"
_COUNT_MEANING_SUMMARY_FAILURES = "group_summary_cumulative_failures"

#: 单群单次响应里的记录上限：只报「最近若干条 + 总数」，不无限返回。
_ENTRY_LIMIT = 20
_POST_LIMIT = 10
_AWARD_SCAN_LIMIT = 500
#: 逐行流式的批大小：避免整群 ORM 对象/正文一次性进内存，也不做无限列表累积。
_STREAM_BATCH = 200

_REASON_NOT_RECORDED = "not_recorded"
_REASON_DELIVERY_TIME_NOT_RECORDED = "delivery_time_not_recorded"
_REASON_PLACEHOLDER_ONLY = "placeholder_only"
_REASON_NO_DELIVERY_EVIDENCE = "no_delivery_evidence"
_REASON_NO_SUCCESS_LEDGER = "no_success_ledger"
_REASON_EVENT_DRIVEN = "event_driven"
_REASON_RUNTIME_NOT_OBSERVED = "runtime_not_observed"
_REASON_DUE_NOW = "due_now_pending_delivery"
_REASON_PLANNED = "planned_by_scheduler"
#: B13b：周报的周期计划默认关闭（现网也没有周报 cron）。
_REASON_PLAN_DISABLED = "plan_disabled"
#: B13b 返工（R-B3）：锚点已经超出执行容差窗口（停机太久）。runner 会跳过这一期
#: 并把锚点推进到严格未来，所以状态页报的是**下一个**未来时刻，不报过去的时刻。
_REASON_PLAN_MISSED = "plan_window_missed"
#: 活动结算没有独立自动计划：人工在运营中心发起，或随周报披露一起结算。
_REASON_MANUAL_ONLY = "manual_only"
#: 内部 runner 归它管，但当前生效时段是空的（配置被清空），真的没有下一时刻。
_REASON_NO_SLOTS_CONFIGURED = "no_slots_configured"


# --------------------------------------------------------------------- 纯计算


def scheduled_message_next_plan(
    entry: Any, *, now: datetime | None = None, configured: bool | None = None
) -> tuple[datetime | None, str]:
    """返回 ``(下一次计划时刻, reason)``；纯函数，绝不修改 ``entry``。

    与 ``bot/services/scheduled_messages.scheduled_message_occurrence_at`` 同口径：
    同一个 ``now`` 下「本函数说 ``due_now_pending_delivery``」当且仅当「scheduler 说
    该条已到期」。区别只在于本函数把**尚未到期**的情况算成未来的具体时刻，而
    scheduler 只回答「现在要不要跑」。

    ``configured`` 允许调用方直接给「有没有正文」的判定（流式读取时正文只取 SQL 侧的
    存在性，不把正文带进内存）；为 ``None`` 时按 ``entry.text`` 判定。
    """

    if not entry.enabled:
        return None, "disabled"
    has_text = (
        bool(str(entry.text or "").strip()) if configured is None else bool(configured)
    )
    if not has_text:
        return None, "not_configured"

    current = now or now_shanghai_naive()
    if str(entry.schedule_type or "daily") == "interval":
        minutes = max(_MIN_INTERVAL_MINUTES, int(entry.interval_minutes or 0))
        anchor = entry.last_run_at or entry.created_at
        if anchor is None:
            # 没有落库的时钟锚点就不猜：`now + 间隔` 会是一句假话。
            return None, "no_persisted_clock"
        due_at = anchor + timedelta(minutes=minutes)
    else:
        parsed = parse_schedule_time(str(entry.schedule_time or ""))
        if parsed is None:
            return None, "invalid_schedule_time"
        due_at = current.replace(
            hour=parsed[0], minute=parsed[1], second=0, microsecond=0
        )
        if entry.last_run_at is not None and entry.last_run_at >= due_at:
            # 今天这一次已经跑过（或创建时被锚定过），下一次是明天。
            due_at += timedelta(days=1)

    if current >= due_at:
        return None, _REASON_DUE_NOW
    return due_at, _REASON_PLANNED


# --------------------------------------------------------------------- 小工具


def _stamp(value: datetime | None) -> str | None:
    """把库里的朴素上海时间转成带偏移的字符串；``None`` 原样返回。"""

    if value is None:
        return None
    return to_shanghai_datetime(value).isoformat()


def _failure(
    *,
    reason: str,
    at: datetime | None = None,
    count: int | None = None,
    backoff_until: datetime | None = None,
    pending_retry_at: datetime | None = None,
    kind: str | None = None,
    attempts_max: int | None = None,
    count_meaning: str | None = None,
    retry_state: str | None = None,
) -> dict:
    return {
        "at": _stamp(at),
        "count": count,
        #: ``count`` 的单位随任务不同而不同，必须显式标注，避免被读成「失败次数」。
        "count_meaning": count_meaning,
        "kind": kind,
        "backoff_until": _stamp(backoff_until),
        "pending_retry_at": _stamp(pending_retry_at),
        "attempts_max": attempts_max,
        #: 重试队列的当前状态（只有定时消息用）：已到期待处理 / 未来退避 / 有生效租约。
        #: 它**不是**「已经重试过」：任何取值都只说明「已安排、尚未完成」。
        "retry_state": retry_state,
        "reason": reason,
        # 这些台账都是「当前状态」，不是完整失败史 —— 显式声明，避免被读成历史。
        "history_complete": False,
    }


def _success(
    *, at: datetime | None = None, basis: str | None = None, reason: str | None = None
) -> dict:
    """``reason`` 只在「没有成功证据」时非空；有证据时它保持 ``None``。"""

    return {"at": _stamp(at), "basis": basis, "reason": reason}


def _next_run(
    *, reason: str, at: datetime | None = None
) -> dict:
    return {"at": _stamp(at), "reason": reason}


def _source(*, implemented: str, runner_task_name: str | None) -> dict:
    return {
        "implemented": implemented,
        "runner_task_name": runner_task_name,
        # 外部 cron 的登记情况不在程序可观测范围内：只展示「程序实现的来源」。
        "external_schedule_known": False,
        "external_schedule_reason": EXTERNAL_SCHEDULE_UNKNOWN,
    }


async def _policy_view(session: Any, *, task_id: str, group_id: int) -> Any:
    """读**按群持久策略**（B13b）。

    这是只读状态页的"来源"真源：内部 runner / 外部 CLI / 人工，都由它决定。
    读失败**向上抛**——状态页宁可失败，也不许把未知来源说成某个具体来源。
    """

    from bot.services import task_policy

    return await task_policy.load_policy(
        session, group_id=group_id, task_id=task_id
    )


def _policy_source(view: Any, *, internal: str, external: str, runner: str | None) -> dict:
    """把策略视图翻译成只读状态里的 ``source`` 块。

    ``implemented`` 只允许 ``in_process`` / ``external_cron_only`` 两个取值（既有
    契约）；来源判定的细节另放在 ``effective_source`` / ``policy_revision`` 等字段，
    不给外部宿主 cron 的登记情况编造事实（``external_schedule_known`` 恒为 False）。
    """

    source = (
        _source(implemented=internal, runner_task_name=runner)
        if view.source == SOURCE_INTERNAL
        else _source(implemented=external, runner_task_name=None)
    )
    source.update(
        {
            "effective_source": view.source,
            "source_default": view.source_default,
            "source_switchable": bool(view.source_switchable),
            "policy_revision": int(view.revision),
            "active_from": _stamp(view.active_from),
        }
    )
    return source


def _next_slot(*, now: datetime, slots: Iterable[int], active_from: datetime | None) -> datetime | None:
    """内部签到下一时段。

    用的是 runner 的同一个 :func:`slot_moment`（本地自然日 + 运行时段），不是
    ``now + 间隔``：算不出来就返回 ``None``（由调用方给 reason），绝不猜一个时刻。
    """

    today = now.date()
    moments = sorted(slot_moment(today, int(slot)) for slot in slots)
    moments += sorted(
        slot_moment(today + timedelta(days=1), int(slot)) for slot in slots
    )
    for moment in moments:
        if moment > now and (active_from is None or moment >= active_from):
            return moment
    return None


def _task(
    task_id: str,
    *,
    trigger: str,
    source: dict,
    next_run: dict,
    last_success: dict,
    last_failure: dict,
    details: dict,
    unknowns: Sequence[dict] = (),
    evidence: Sequence[dict] = (),
) -> dict:
    return {
        "task_id": task_id,
        "title": TASK_TITLES[task_id],
        "trigger": trigger,
        "source": source,
        "next_run": next_run,
        "last_success": last_success,
        "last_failure": last_failure,
        "details": details,
        "unknowns": list(unknowns),
        "evidence": list(evidence),
    }


def _unknown(field: str, reason: str) -> dict:
    return {"field": field, "reason": reason}


def _dedupe_unknowns(entries: Iterable[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for entry in entries:
        key = (entry["field"], entry["reason"])
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return result


def _week_key_from_ref(ref: str | None) -> str | None:
    """从 ``weekly-activity:<week>:<user>`` 取出 week；形状不符返回 ``None``。"""

    if not ref:
        return None
    parts = str(ref).split(":")
    if len(parts) < 3 or parts[0] != AWARD_REF_PREFIX:
        return None
    return parts[1] or None


# --------------------------------------------------------------- 六类任务聚合


async def _scheduled_messages_status(
    session: Any, group_id: int, observed_at: datetime
) -> dict:
    """全群台账事实 + 有界展示列表。

    旧实现把「最近 20 条」同时当成了事实来源：第 21 条配置的启用状态、投递证据
    与水位会整体消失（父代理独立验收 R3）。现在**事实部分全部走 SQL 聚合或逐行
    流式纯计算**，展示列表只限制 ``entries`` 的长度；occurrence 也不再整群读
    ORM 行（含 ``last_error``），只做 count / min / max 汇总。
    """

    entry_filters = (ScheduledMessage.group_id == group_id,)

    entry_count = int(
        await session.scalar(
            select(func.count()).select_from(ScheduledMessage).where(*entry_filters)
        )
        or 0
    )
    delivery_message_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ScheduledMessage)
            .where(*entry_filters, ScheduledMessage.last_message_id > 0)
        )
        or 0
    )
    schedule_watermark = await session.scalar(
        select(func.max(ScheduledMessage.last_run_at)).where(
            *entry_filters, ScheduledMessage.last_run_at.is_not(None)
        )
    )

    occurrence_filters = (
        ScheduledMessage.group_id == group_id,
    )
    occurrence_join = select(ScheduledMessageOccurrence).join(
        ScheduledMessage,
        ScheduledMessageOccurrence.scheduled_message_id == ScheduledMessage.id,
    )
    occurrence_attempt_rows = int(
        await session.scalar(
            occurrence_join.with_only_columns(
                func.count(ScheduledMessageOccurrence.id)
            ).where(*occurrence_filters)
        )
        or 0
    )
    retry_scheduled_filters = (
        *occurrence_filters,
        ScheduledMessageOccurrence.next_attempt_at.is_not(None),
    )
    #: 「没有生效租约」：租约为空或已过期。有生效租约说明另一处正在处理，不能当空闲重试。
    no_active_lease = or_(
        ScheduledMessageOccurrence.lease_until.is_(None),
        ScheduledMessageOccurrence.lease_until <= observed_at,
    )
    retry_pending_occurrence_count = int(
        await session.scalar(
            occurrence_join.with_only_columns(
                func.count(ScheduledMessageOccurrence.id)
            ).where(*retry_scheduled_filters)
        )
        or 0
    )
    #: 三态分别计数（父验收 R4）：已到期待处理 / 未来退避（都要求没有生效租约）/ 活动租约。
    retry_due_occurrence_count = int(
        await session.scalar(
            occurrence_join.with_only_columns(
                func.count(ScheduledMessageOccurrence.id)
            ).where(
                *retry_scheduled_filters,
                ScheduledMessageOccurrence.next_attempt_at <= observed_at,
                no_active_lease,
            )
        )
        or 0
    )
    retry_backoff_occurrence_count = int(
        await session.scalar(
            occurrence_join.with_only_columns(
                func.count(ScheduledMessageOccurrence.id)
            ).where(
                *retry_scheduled_filters,
                ScheduledMessageOccurrence.next_attempt_at > observed_at,
                no_active_lease,
            )
        )
        or 0
    )
    in_flight_occurrence_count = int(
        await session.scalar(
            occurrence_join.with_only_columns(
                func.count(ScheduledMessageOccurrence.id)
            ).where(
                *occurrence_filters,
                #: 只有「还没过期的租约」才算在途；过期租约是崩溃残留，不是正在处理。
                ScheduledMessageOccurrence.lease_until > observed_at,
            )
        )
        or 0
    )
    pending_retry_at = await session.scalar(
        select(func.min(ScheduledMessageOccurrence.next_attempt_at))
        .select_from(ScheduledMessageOccurrence)
        .join(
            ScheduledMessage,
            ScheduledMessageOccurrence.scheduled_message_id == ScheduledMessage.id,
        )
        .where(*retry_scheduled_filters)
    )
    attempts_max = await session.scalar(
        select(func.max(ScheduledMessageOccurrence.attempts))
        .select_from(ScheduledMessageOccurrence)
        .join(
            ScheduledMessage,
            ScheduledMessageOccurrence.scheduled_message_id == ScheduledMessage.id,
        )
        .where(*retry_scheduled_filters)
    )
    #: 单条配置「是否有已安排的重试」用相关 EXISTS 逐行判定，不再把整群 occurrence
    #: 的 ``scheduled_message_id`` 全量读进 Python 集合（父验收 R3 的有界性要求）。
    def _retry_exists(*extra: Any) -> Any:
        return (
            select(ScheduledMessageOccurrence.id)
            .where(
                ScheduledMessageOccurrence.scheduled_message_id
                == ScheduledMessage.id,
                *extra,
            )
            .exists()
        )

    retry_due_exists = _retry_exists(
        ScheduledMessageOccurrence.next_attempt_at.is_not(None),
        ScheduledMessageOccurrence.next_attempt_at <= observed_at,
        no_active_lease,
    )
    retry_backoff_exists = _retry_exists(
        ScheduledMessageOccurrence.next_attempt_at > observed_at,
        no_active_lease,
    )
    retry_leased_exists = _retry_exists(
        ScheduledMessageOccurrence.lease_until > observed_at
    )

    #: 逐行流式读取（明确批大小、用完即关），只为算「全群最早计划」与展示窗口内的条目。
    #: 正文列只取 SQL 侧的**存在性**（``length(trim(...)) > 0``），不把正文带进内存；
    #: 全量事实只累积标量（最早计划、各类计数）与固定的原因集合，不留整群对象。
    entry_documents: list[dict] = []
    next_run_unknown_reasons: list[str] = []
    retry_unknown_reasons: list[str] = []
    earliest_planned: datetime | None = None
    due_now_pending_count = 0
    invalid_schedule_time_count = 0
    schedule_unknown_count = 0
    not_configured_count = 0
    disabled_count = 0
    stream = await session.stream(
        select(
            ScheduledMessage.id,
            ScheduledMessage.enabled,
            ScheduledMessage.schedule_type,
            ScheduledMessage.schedule_time,
            ScheduledMessage.interval_minutes,
            ScheduledMessage.last_run_at,
            ScheduledMessage.created_at,
            ScheduledMessage.last_message_id,
            #: 只回答「这条配置有没有正文」，不取正文本身。
            (
                func.length(func.trim(func.coalesce(ScheduledMessage.text, ""))) > 0
            ).label("has_text"),
            retry_due_exists.label("retry_due"),
            retry_backoff_exists.label("retry_backoff"),
            retry_leased_exists.label("retry_leased"),
        )
        .where(*entry_filters)
        .order_by(ScheduledMessage.id)
        .execution_options(yield_per=_STREAM_BATCH),
    )
    try:
        async for entry in stream:
            planned_at, reason = scheduled_message_next_plan(
                entry, now=observed_at, configured=bool(entry.has_text)
            )
            if str(entry.schedule_type or "daily") == "interval":
                clock_missing = reason == "no_persisted_clock"
            else:
                clock_missing = False
            retry_due = bool(entry.retry_due)
            retry_backoff = bool(entry.retry_backoff)
            retry_leased = bool(entry.retry_leased)
            pending_retry = retry_due or retry_backoff or retry_leased
            #: 计划到期与重试队列是两个维度：**计数不依赖互斥的展示状态**，否则一条既到期
            #: 又有重试记录的配置会整条从 `due_now_pending_count` 里消失（父验收 R4-2）。
            is_due_now = (
                bool(entry.enabled)
                and bool(entry.has_text)
                and reason != "invalid_schedule_time"
                and reason == _REASON_DUE_NOW
            )
            if is_due_now:
                due_now_pending_count += 1
            if not entry.enabled:
                state = "disabled"
                disabled_count += 1
            elif not bool(entry.has_text):
                state = "not_configured"
                not_configured_count += 1
            elif reason == "invalid_schedule_time":
                state = "invalid_schedule_time"
                invalid_schedule_time_count += 1
            elif retry_leased:
                #: 有生效租约：另一处正在处理这条记录，不能报成「等你去重试」。
                state = "retry_in_flight"
            elif is_due_now:
                #: 当前就该跑（即使另有一条未来退避的重试记录）：到期优先于退避展示。
                state = "due_now_pending_delivery"
            elif retry_due:
                state = "retry_due"
            elif retry_backoff:
                state = "pending_retry"
            elif clock_missing:
                state = "schedule_unknown"
                schedule_unknown_count += 1
            else:
                state = "scheduled"
            if planned_at is not None and (
                earliest_planned is None or planned_at < earliest_planned
            ):
                earliest_planned = planned_at
            if len(entry_documents) < _ENTRY_LIMIT:
                entry_documents.append(
                    {
                        "id": int(entry.id),
                        "enabled": bool(entry.enabled),
                        "schedule_type": str(entry.schedule_type or "daily"),
                        "state": state,
                        "next_planned_at": _stamp(planned_at),
                        "next_run_reason": reason,
                        "has_delivery_message": int(entry.last_message_id or 0) > 0,
                        "pending_retry": pending_retry,
                    }
                )
            if (
                planned_at is None
                and state in {"schedule_unknown", "invalid_schedule_time"}
                and reason not in next_run_unknown_reasons
            ):
                next_run_unknown_reasons.append(reason)
            if pending_retry and (
                "retry_scheduled_backoff_not_completed" not in retry_unknown_reasons
            ):
                retry_unknown_reasons.append(
                    "retry_scheduled_backoff_not_completed"
                )
    finally:
        await stream.close()
    unknowns = [
        _unknown("tasks.scheduled_messages.next_run.at", reason)
        for reason in next_run_unknown_reasons
    ]
    unknowns.extend(
        _unknown("tasks.scheduled_messages.last_failure.pending_retry_at", reason)
        for reason in retry_unknown_reasons
    )

    last_delivery = delivery_message_count > 0
    if last_delivery:
        last_success = _success(
            reason=_REASON_DELIVERY_TIME_NOT_RECORDED,
            basis="scheduled_messages.last_message_id",
        )
    else:
        last_success = _success(reason=_REASON_NO_DELIVERY_EVIDENCE)

    #: 重试队列的当前状态（三态互斥展示，按「当前更相关」排序），它不是执行证据。
    if retry_due_occurrence_count:
        retry_state: str | None = "retry_due"
    elif in_flight_occurrence_count:
        retry_state = "retry_in_flight"
    elif retry_backoff_occurrence_count:
        retry_state = "retry_backoff"
    else:
        retry_state = None

    if retry_pending_occurrence_count:
        last_failure = _failure(
            reason="failure_time_not_recorded",
            count=retry_pending_occurrence_count,
            pending_retry_at=pending_retry_at,
            kind="delivery_attempt_failed",
            attempts_max=int(attempts_max) if attempts_max is not None else None,
            count_meaning=_COUNT_MEANING_SCHEDULED_RETRY,
            retry_state=retry_state,
        )
    else:
        last_failure = _failure(reason=_REASON_NOT_RECORDED)

    details = {
        "entry_count": entry_count,
        "entries_returned": len(entry_documents),
        "entries_truncated": entry_count > len(entry_documents),
        "entries": entry_documents,
        "recent_window_meaning": _RECENT_WINDOW_MEANING,
        "schedule_watermark_at": _stamp(schedule_watermark),
        "schedule_watermark_meaning": SCHEDULE_WATERMARK_MEANING,
        "last_delivery_message_present": last_delivery,
        "delivery_message_count": delivery_message_count,
        "occurrence_attempt_rows": occurrence_attempt_rows,
        "retry_pending_occurrence_count": retry_pending_occurrence_count,
        "retry_due_occurrence_count": retry_due_occurrence_count,
        "retry_backoff_occurrence_count": retry_backoff_occurrence_count,
        "in_flight_occurrence_count": in_flight_occurrence_count,
        "due_now_pending_count": due_now_pending_count,
        "disabled_count": disabled_count,
        "not_configured_count": not_configured_count,
        "invalid_schedule_time_count": invalid_schedule_time_count,
        "schedule_unknown_count": schedule_unknown_count,
        #: 未来计划（可能一边有到期待处理）；到期待处理时它绝不冒充「下一次执行」。
        "earliest_planned_at": _stamp(earliest_planned),
    }
    evidence = [
        {"table": "scheduled_messages", "rows": entry_count},
        {"table": "scheduled_message_occurrences", "rows": occurrence_attempt_rows},
    ]
    return _task(
        "scheduled_messages",
        trigger="wall_clock",
        source=_source(
            implemented="in_process", runner_task_name="scheduled-message-runner"
        ),
        next_run=_next_run_from_entries(
            planned_at=earliest_planned,
            has_entries=bool(entry_count),
            due_now_pending_count=due_now_pending_count,
            invalid_schedule_time_count=invalid_schedule_time_count,
            schedule_unknown_count=schedule_unknown_count,
            not_configured_count=not_configured_count,
        ),
        last_success=last_success,
        last_failure=last_failure,
        details=details,
        unknowns=unknowns,
        evidence=evidence,
    )


def _next_run_from_entries(
    *,
    planned_at: datetime | None,
    has_entries: bool,
    due_now_pending_count: int,
    invalid_schedule_time_count: int,
    schedule_unknown_count: int,
    not_configured_count: int,
) -> dict:
    """把**全群**定时消息压成一条「下一次执行」；算不出来就给 reason。

    R4：现在就该跑的条目（``due_now_pending_delivery``）优先于未来计划 —— 未来计划
    仍然通过 ``details.earliest_planned_at`` 独立可见，绝不冒充「全群下一次执行」。
    """

    if due_now_pending_count:
        return _next_run(reason=_REASON_DUE_NOW)
    if planned_at is not None:
        return _next_run(reason=_REASON_PLANNED, at=planned_at)
    if not has_entries:
        return _next_run(reason="not_configured")
    if invalid_schedule_time_count:
        return _next_run(reason="invalid_schedule_time")
    if schedule_unknown_count:
        return _next_run(reason="no_persisted_clock")
    if not_configured_count:
        return _next_run(reason="not_configured")
    return _next_run(reason="disabled")


async def _group_summary_status(session: Any, group_id: int) -> dict:
    summary = await session.get(GroupSummary, group_id)
    failure = await session.get(GroupSummaryFailureState, group_id)

    if summary is not None:
        last_success = _success(
            at=summary.generated_at, basis="group_summaries.generated_at"
        )
    else:
        last_success = _success(reason=_REASON_NOT_RECORDED)

    if failure is not None:
        last_failure = _failure(
            at=failure.updated_at,
            count=int(failure.failure_count or 0),
            backoff_until=failure.backoff_until,
            kind="summary_publish_failed",
            reason="current_backoff_state_not_full_history",
            count_meaning=_COUNT_MEANING_SUMMARY_FAILURES,
        )
        unknowns = [
            _unknown(
                "tasks.group_summary.last_failure",
                "current_backoff_state_not_full_history",
            )
        ]
    else:
        last_failure = _failure(reason=_REASON_NOT_RECORDED)
        unknowns = []

    details = {
        "summary_present": summary is not None,
        "summary_version": int(summary.version) if summary is not None else None,
        "covered_count": int(summary.covered_count) if summary is not None else None,
        "source_revision": (
            int(summary.source_revision) if summary is not None else None
        ),
        "source_stale": (
            int(summary.source_revision) == -1 if summary is not None else None
        ),
        "summary_body_included": False,
    }
    evidence = [
        {"table": "group_summaries", "rows": 1 if summary is not None else 0},
        {
            "table": "group_summary_failure_states",
            "rows": 1 if failure is not None else 0,
        },
    ]
    return _task(
        "group_summary",
        trigger="event_driven",
        source=_source(
            implemented="in_process", runner_task_name="group-summary-scheduler"
        ),
        next_run=_next_run(reason=_REASON_EVENT_DRIVEN),
        last_success=last_success,
        last_failure=last_failure,
        details=details,
        unknowns=_dedupe_unknowns(
            [_unknown("tasks.group_summary.next_run.at", _REASON_EVENT_DRIVEN), *unknowns]
        ),
        evidence=evidence,
    )


async def _checkin_reminder_status(
    session: Any, group_id: int, observed_at: datetime
) -> dict:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(CheckinReminderPost)
            .where(CheckinReminderPost.group_id == group_id)
        )
        or 0
    )
    delivered_count = int(
        await session.scalar(
            select(func.count())
            .select_from(CheckinReminderPost)
            .where(
                CheckinReminderPost.group_id == group_id,
                CheckinReminderPost.message_id > 0,
            )
        )
        or 0
    )
    #: 成功证据取自**全群台账**：早于展示窗口的成功也算成功（父代理独立验收 R3）。
    delivered_with_time_count = int(
        await session.scalar(
            select(func.count())
            .select_from(CheckinReminderPost)
            .where(
                CheckinReminderPost.group_id == group_id,
                CheckinReminderPost.delivered_at.is_not(None),
            )
        )
        or 0
    )
    latest_delivered_at = await session.scalar(
        select(func.max(CheckinReminderPost.delivered_at)).where(
            CheckinReminderPost.group_id == group_id,
            CheckinReminderPost.delivered_at.is_not(None),
        )
    )
    recent = (
        await session.scalars(
            select(CheckinReminderPost)
            .where(CheckinReminderPost.group_id == group_id)
            .order_by(CheckinReminderPost.created_at.desc(), CheckinReminderPost.id.desc())
            .limit(_POST_LIMIT)
        )
    ).all()

    delivered_missing_time_count = max(delivered_count - delivered_with_time_count, 0)
    unknowns: list[dict] = []
    if latest_delivered_at is not None:
        last_success = _success(
            at=latest_delivered_at, basis="checkin_reminder_posts.delivered_at"
        )
    elif delivered_count:
        # B-27 之前的台账：只承认「已投递」，完成时间未知。
        last_success = _success(
            reason=_REASON_DELIVERY_TIME_NOT_RECORDED,
            basis="checkin_reminder_posts.message_id",
        )
        unknowns.append(
            _unknown(
                "tasks.checkin_reminder.last_success.at",
                _REASON_DELIVERY_TIME_NOT_RECORDED,
            )
        )
    elif total:
        last_success = _success(reason=_REASON_PLACEHOLDER_ONLY)
    else:
        last_success = _success(reason=_REASON_NOT_RECORDED)

    details = {
        "total_count": total,
        "delivered_count": delivered_count,
        "placeholder_count": total - delivered_count,
        "delivered_with_time_count": delivered_with_time_count,
        "delivered_missing_time_count": delivered_missing_time_count,
        "latest_delivered_at": _stamp(latest_delivered_at),
        "recent_window_meaning": _RECENT_WINDOW_MEANING,
        "recent_posts_truncated": total > len(recent),
        "recent_posts": [
            {
                "slot_key": str(row.slot_key),
                "delivered_at": _stamp(row.delivered_at),
                "has_message_id": int(row.message_id or 0) > 0,
            }
            for row in recent
        ],
        "latest_slot_key": str(recent[0].slot_key) if recent else None,
        "slots": list(reminder_slots()),
    }
    # B13b：这个群归内部 runner 还是外部 cron 管，读的是持久策略（缺行＝默认
    # external，即现场 cron）。内部时下一时刻由 runner 同一个 slot_moment 算出来。
    view = await _policy_view(session, task_id="checkin_reminder", group_id=group_id)
    details.update(
        {
            "effective_source": view.source,
            "policy_revision": int(view.revision),
            "active_from": _stamp(view.active_from),
        }
    )
    if view.source == SOURCE_INTERNAL:
        planned = _next_slot(
            now=observed_at,
            slots=reminder_slots(),
            active_from=view.active_from,
        )
        if planned is None:
            next_run = _next_run(reason=_REASON_NO_SLOTS_CONFIGURED)
        else:
            next_run = _next_run(reason=_REASON_PLANNED, at=planned)
        trigger = "wall_clock"
        next_unknown = next_run["reason"] if planned is None else None
    else:
        trigger = "external_cron"
        next_run = _next_run(reason=EXTERNAL_SCHEDULE_UNKNOWN)
        next_unknown = EXTERNAL_SCHEDULE_UNKNOWN
    evidence = [{"table": "checkin_reminder_posts", "rows": total}]
    return _task(
        "checkin_reminder",
        trigger=trigger,
        source=_policy_source(
            view,
            internal="in_process",
            external="external_cron_only",
            runner="checkin_reminder_internal",
        ),
        next_run=next_run,
        last_success=last_success,
        last_failure=_failure(reason=_REASON_NOT_RECORDED),
        details=details,
        unknowns=_dedupe_unknowns(
            [
                *(
                    [_unknown("tasks.checkin_reminder.next_run.at", next_unknown)]
                    if next_unknown
                    else []
                ),
                *unknowns,
            ]
        ),
        evidence=evidence,
    )


async def _activity_award_status(session: Any, group_id: int) -> dict:
    rows = (
        await session.scalars(
            select(MemberPointAward)
            .where(
                MemberPointAward.group_id == group_id,
                MemberPointAward.reason == AWARD_REASON_WEEKLY_ACTIVITY,
                MemberPointAward.ref.like(f"{AWARD_REF_PREFIX}:%"),
            )
            .order_by(MemberPointAward.created_at.desc(), MemberPointAward.id.desc())
            .limit(_AWARD_SCAN_LIMIT)
        )
    ).all()

    total_points = sum(int(row.points or 0) for row in rows)
    latest = rows[0] if rows else None
    if latest is not None:
        last_success = _success(
            at=latest.created_at, basis="member_point_awards.created_at"
        )
    else:
        last_success = _success(reason=_REASON_NOT_RECORDED)

    details = {
        "award_count": len(rows),
        "total_points": total_points,
        "latest_week_key": _week_key_from_ref(latest.ref) if latest is not None else None,
        "latest_award_at": _stamp(latest.created_at) if latest is not None else None,
        "scan_truncated": len(rows) >= _AWARD_SCAN_LIMIT,
        "settlement_embedded_in": "weekly_report",
        "subject_identifiers_included": False,
        "settlement_trigger": "manual_or_weekly_report",
    }
    # B13b：活动结算**没有独立自动计划**（现网也没有独立 cron），只有人工在运营
    # 中心发起、或随周报披露一起结算；这里如实报"没有下一时刻"，不造时间。
    view = await _policy_view(session, task_id="activity_award", group_id=group_id)
    details.update(
        {
            "effective_source": view.source,
            "policy_revision": int(view.revision),
        }
    )
    evidence = [{"table": "member_point_awards", "rows": len(rows)}]
    return _task(
        "activity_award",
        trigger="event_driven",
        source=_policy_source(
            view, internal="in_process", external="external_cron_only", runner=None
        ),
        next_run=_next_run(reason=_REASON_MANUAL_ONLY),
        last_success=last_success,
        last_failure=_failure(reason=_REASON_NOT_RECORDED),
        details=details,
        unknowns=[
            _unknown("tasks.activity_award.next_run.at", _REASON_MANUAL_ONLY)
        ],
        evidence=evidence,
    )


async def _weekly_report_status(session: Any, group_id: int) -> dict:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(WeeklyReportPost)
            .where(WeeklyReportPost.target_id == group_id)
        )
        or 0
    )
    delivered_total = int(
        await session.scalar(
            select(func.count())
            .select_from(WeeklyReportPost)
            .where(
                WeeklyReportPost.target_id == group_id,
                WeeklyReportPost.message_id > 0,
            )
        )
        or 0
    )
    recent = (
        await session.scalars(
            select(WeeklyReportPost)
            .where(WeeklyReportPost.target_id == group_id)
            .order_by(WeeklyReportPost.week_key.desc(), WeeklyReportPost.id.desc())
            .limit(_POST_LIMIT)
        )
    ).all()

    #: 成功证据取自**全群台账**：早于展示窗口的已投递周也算成功（父代理独立验收 R3）。
    #: 该表没有 delivered_at，只有登记时刻，因此成功只到「已投递」为止。
    latest_delivered = (
        await session.execute(
            select(WeeklyReportPost.week_key, WeeklyReportPost.created_at)
            .where(
                WeeklyReportPost.target_id == group_id,
                WeeklyReportPost.message_id > 0,
            )
            .order_by(WeeklyReportPost.created_at.desc(), WeeklyReportPost.id.desc())
            .limit(1)
        )
    ).first()

    delivered = [row for row in recent if int(row.message_id or 0) > 0]
    placeholders = [row for row in recent if int(row.message_id or 0) <= 0]
    unknowns: list[dict] = []
    if latest_delivered is not None:
        last_success = _success(
            reason=_REASON_DELIVERY_TIME_NOT_RECORDED,
            basis="weekly_report_posts.message_id",
        )
        week_registered_at = latest_delivered.created_at
        latest_delivered_week_key = str(latest_delivered.week_key)
        unknowns.append(
            _unknown(
                "tasks.weekly_report.last_success.at",
                _REASON_DELIVERY_TIME_NOT_RECORDED,
            )
        )
    elif total:
        last_success = _success(reason=_REASON_PLACEHOLDER_ONLY)
        week_registered_at = None
        latest_delivered_week_key = None
    else:
        last_success = _success(reason=_REASON_NOT_RECORDED)
        week_registered_at = None
        latest_delivered_week_key = None

    details = {
        "total_count": total,
        "delivered_count": delivered_total,
        "placeholder_count": total - delivered_total,
        "recent_window_meaning": _RECENT_WINDOW_MEANING,
        "recent_rows_truncated": total > len(recent),
        #: 这两个列表只是展示窗口的内容；全群总数见 delivered_count / placeholder_count。
        "recent_delivered_week_keys": [str(row.week_key) for row in delivered],
        "recent_placeholder_week_keys": [str(row.week_key) for row in placeholders],
        "latest_delivered_week_key": latest_delivered_week_key,
        "latest_week_key": str(recent[0].week_key) if recent else None,
        "week_registered_at": _stamp(week_registered_at),
        "week_registered_at_meaning": WEEK_REGISTERED_AT_MEANING,
    }
    # B13b：周报是**人工任务 + 可选周期计划**，默认 off；现网没有周报 cron，所以
    # 这里的来源只可能是"程序内"（人工确认 / 内部计划 runner），绝不声称外部计划。
    # 下一时刻只来自实际落库的锚点（管理员预览确认时算出的严格未来时刻）。
    view = await _policy_view(session, task_id="weekly_report", group_id=group_id)
    plan_supported = bool(view.plan_supported)
    if plan_supported and view.plan_enabled and view.anchor_at is not None:
        trigger = "wall_clock"
        # R-B3：与 runner 共用同一个容差函数。锚点在窗口内 → 报落库的锚点（这一期
        # 到点，等 runner 下一个 tick）；已经超出窗口 → 这一期没跑上，runner 会跳过
        # 并把锚点推进到严格未来，所以这里报**下一个未来时刻**，绝不显示一个过去的
        # "下一时刻"。
        from bot.services import task_policy as _task_policy

        observed_moment = now_shanghai_naive()
        if (
            _task_policy.plan_fire_state(
                anchor_at=view.anchor_at, now=observed_moment
            )
            == "missed"
        ):
            planned_at = None
            if view.plan_weekday is not None and view.plan_minute is not None:
                planned_at = _task_policy.next_occurrence(
                    observed_moment,
                    int(view.plan_weekday),
                    int(view.plan_minute),
                )
            next_run = _next_run(reason=_REASON_PLAN_MISSED, at=planned_at)
            next_unknown = None
        else:
            next_run = _next_run(reason=_REASON_PLANNED, at=view.anchor_at)
            next_unknown = None
        runner = "weekly_report_internal_plan"
    elif plan_supported and view.plan_enabled:
        # 计划开着却没有锚点（理论上写不进来）：不猜时间。
        trigger = "wall_clock"
        next_run = _next_run(reason=_REASON_PLAN_DISABLED)
        next_unknown = _REASON_PLAN_DISABLED
        runner = "weekly_report_internal_plan"
    else:
        trigger = "event_driven"
        next_run = _next_run(reason=_REASON_PLAN_DISABLED)
        next_unknown = _REASON_PLAN_DISABLED
        runner = None
    details.update(
        {
            "effective_source": view.source,
            "policy_revision": int(view.revision),
            "plan_supported": plan_supported,
            "plan_enabled": bool(view.plan_enabled),
            "plan_weekday": view.plan_weekday,
            "plan_minute": view.plan_minute,
            "plan_anchor_at": _stamp(view.anchor_at),
            "plan_timezone": str(SHANGHAI_TZ),
        }
    )
    evidence = [{"table": "weekly_report_posts", "rows": total}]
    return _task(
        "weekly_report",
        trigger=trigger,
        source=_policy_source(
            view, internal="in_process", external="external_cron_only", runner=runner
        ),
        next_run=next_run,
        last_success=last_success,
        last_failure=_failure(reason=_REASON_NOT_RECORDED),
        details=details,
        unknowns=_dedupe_unknowns(
            [
                *(
                    [_unknown("tasks.weekly_report.next_run.at", next_unknown)]
                    if next_unknown
                    else []
                ),
                *unknowns,
            ]
        ),
        evidence=evidence,
    )


async def _shop_expire_status(
    session: Any, group_id: int, observed_at: datetime
) -> dict:
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(MemberEntitlement)
            .where(MemberEntitlement.group_id == group_id)
        )
        or 0
    )
    due_count = int(
        await session.scalar(
            select(func.count())
            .select_from(MemberEntitlement)
            .where(
                MemberEntitlement.group_id == group_id,
                MemberEntitlement.expires_at <= observed_at,
            )
        )
        or 0
    )
    details = {
        "active_count": total - due_count,
        "due_count": due_count,
        # 双来源：进程内 runner 与 CLI 都调用同一个到期函数；此处只报来源。
        "external_cli_also_exists": True,
        "ledger_note": "到期处理成功后会删除权益行，因此表内没有成功台账可查。",
    }
    # B13b：商店默认由进程内 runner 处理（300/900 节奏）；只有被切到 external 的
    # 群才由旧的 */10 入口处理，而那个入口在没有任何 external 群时是 no-op。
    view = await _policy_view(session, task_id="shop_expire", group_id=group_id)
    details.update(
        {
            "effective_source": view.source,
            "policy_revision": int(view.revision),
            "active_from": _stamp(view.active_from),
            "external_entry_role": (
                "no_op_when_all_internal" if view.source == SOURCE_INTERNAL else "active"
            ),
        }
    )
    evidence = [{"table": "member_entitlements", "rows": total}]
    return _task(
        "shop_expire",
        trigger="wall_clock",
        source=_policy_source(
            view,
            internal="in_process",
            external="external_cron_only",
            runner="shop-expiry-runner",
        ),
        next_run=_next_run(reason=_REASON_RUNTIME_NOT_OBSERVED),
        last_success=_success(reason=_REASON_NO_SUCCESS_LEDGER),
        last_failure=_failure(reason=_REASON_NOT_RECORDED),
        details=details,
        unknowns=[
            _unknown("tasks.shop_expire.next_run.at", _REASON_RUNTIME_NOT_OBSERVED)
        ],
        evidence=evidence,
    )


# ------------------------------------------------------------------ 对外入口


async def collect_operations_task_status(
    session_factory: Any, *, group_id: int, now: datetime | None = None
) -> dict:
    """聚合单个群的运营任务状态。**只读**；DB 故障必须向上抛出（不许静默给全绿）。"""

    observed_at = now or now_shanghai_naive()
    async with session_factory() as session:
        tasks = [
            await _scheduled_messages_status(session, group_id, observed_at),
            await _group_summary_status(session, group_id),
            await _checkin_reminder_status(session, group_id, observed_at),
            await _activity_award_status(session, group_id),
            await _weekly_report_status(session, group_id),
            await _shop_expire_status(session, group_id, observed_at),
        ]
    return {
        "generated_at": to_shanghai_datetime(observed_at).isoformat(),
        "timezone": str(SHANGHAI_TZ),
        "group_id": int(group_id),
        "tasks": tasks,
    }
