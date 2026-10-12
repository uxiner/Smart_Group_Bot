"""运营任务中心：六类任务的真实适配器（第三优先 B13a）。

本模块是写侧六类业务的**唯一适配层**，与 ``bot/services/operations_runs.py``
（记账、单次确认、取消、对账）分工明确：

* ``preview`` **只读**：不写审计、不占位、不发消息、不调模型、不动积分。所有
  "现在能不能做"的判断都从真实台账/真实服务读出来，不靠 UI 上的按钮。
* 真实动作全部调用**已有业务服务**，不在 API 侧复制发送链：
  ``ScheduledMessageService.deliver_entry_occurrence``、``send_checkin_slot``、
  ``deliver_group_report``、``GroupSummaryScheduler.run_manual``、
  ``settle_weekly_activity``、``expire_due_entitlements``。共享业务 claim
  （签到占位、周报占位、发奖 ``award_ref``、商店行 CAS、摘要 publish CAS）留在
  服务里；运营中心只是**发起者**，不另造一套锁。
* 四态分离：已成功 / 明确失败 / **未知外部结果**。任何"异常发生在出站之后"
  都可能已经产生副作用，一律 ``ITEM_STATUS_UNKNOWN`` + ``resendable=False``，
  管理员点重试也不会被重发（``retry_run`` 只重跑 ``resendable`` 项）。
* manual 范围按任务书收口，**绝不自动补发历史**：定时消息只处理当前 due
  occurrence、签到只处理当天已到时段、周报只处理当前 ISO 周台账键、活跃结算
  只结算上一个完整周、商店只处理本群当前到期项、摘要只按本群当前归档生成。
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import bot.services.group_summary as group_summary_module
from bot.db.models import (
    MemberEntitlement,
    MemberPointAward,
    OperationsRun,
    OperationsRunItem,
    ScheduledMessage,
)
from bot.services.activity import (
    AWARD_REASON_WEEKLY_ACTIVITY,
    award_ref,
    last_complete_week,
    render_activity_lines,
    settle_weekly_activity,
    week_key as activity_week_key,
)
from bot.services.checkin_reminder import (
    CHECKIN_OUTCOME_ALREADY_DELIVERED,
    CHECKIN_OUTCOME_MARK_FAILED,
    CHECKIN_OUTCOME_SEND_FAILED,
    CHECKIN_OUTCOME_SENT,
    backfill_durable_auto_delete,
    reminder_auto_delete_seconds,
    reminder_outbound_receipt,
    reminder_slot_state,
    reminder_slots,
    send_checkin_slot,
    slot_key,
)
from bot.services.group_summary import group_summary_config
from bot.services.operations_runs import (
    ITEM_STATUS_CANCELLED,
    ITEM_STATUS_FAILED,
    ITEM_STATUS_SKIPPED,
    ITEM_STATUS_SUCCEEDED,
    ITEM_STATUS_UNKNOWN,
    REASON_ADAPTER_CRASHED,
    REASON_ALREADY_DELIVERED,
    REASON_ALREADY_SUCCEEDED,
    REASON_BACKOFF,
    REASON_CANCEL_REQUESTED,
    REASON_DISABLED,
    REASON_EXTERNAL_UNKNOWN,
    REASON_GENERATION_FAILED,
    REASON_GUARD_BLOCKED,
    REASON_NOT_DUE,
    REASON_NOT_FOUND,
    REASON_NOT_SAFELY_RESENDABLE,
    REASON_NO_WORK,
    REASON_OK,
    REASON_QUEUE_FULL,
    REASON_RUNNER_UNAVAILABLE,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_NO_WORK,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_UNKNOWN,
    ItemResult,
    OperationsError,
    PreviewTarget,
    ReconcileResult,
    TaskPreview,
    TaskRunResult,
    cancel_requested_persisted,
    register_task,
)
from bot.services.operations_task_status import TASK_TITLES
from bot.services.operations_guard import PersistedGuard
from bot.services.operations_receipts import (
    RECEIPT_STATE_DISPATCHING,
    RECEIPT_STATE_SENT,
    RECEIPT_STATE_UNKNOWN,
    TASK_GROUP_SUMMARY,
    TASK_SCHEDULED_MESSAGE,
    load_receipt,
    load_receipts,
    record_receipt,
)
from bot.services.point_shop import (
    due_entitlements,
    expire_entitlement_target,
    expiry_notice_receipt,
)
from bot.services.quality_report import authorized_group_ids, render_group_quality
from bot.services.authz import is_group_admin_authorized
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.scheduled_messages import (
    DELIVER_RESULT_ALREADY_DELIVERED,
    DELIVER_RESULT_COMPLETION_UNCONFIRMED,
    DELIVER_RESULT_DISABLED,
    DELIVER_RESULT_ENTRY_MISSING,
    DELIVER_RESULT_GUARD_BLOCKED,
    DELIVER_RESULT_NOT_DUE,
    DELIVER_RESULT_OUTBOUND_UNKNOWN,
    DELIVER_RESULT_SEND_REJECTED,
    DELIVER_RESULT_SENT,
    OCCURRENCE_ALREADY_DELIVERED,
    OCCURRENCE_DUE,
    ScheduledMessageService,
    scheduled_message_manual_state,
    scheduled_message_occurrence_at,
    scheduled_occurrence_key,
)
from bot.services.weekly_report import (
    REPORT_OUTCOME_ALREADY_DELIVERED,
    REPORT_OUTCOME_DELIVERED,
    REPORT_OUTCOME_IN_FLIGHT,
    REPORT_OUTCOME_MARK_FAILED,
    REPORT_OUTCOME_RENDER_FAILED,
    REPORT_OUTCOME_SEND_FAILED,
    REPORT_SLOT_DELIVERED,
    REPORT_SLOT_PLACEHOLDER,
    deliver_group_report,
    report_outbound_receipt,
    report_slot_state,
)
from bot.utils.telegram import schedule_message_auto_delete_durable
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

REASON_INVALID_PARAMS = "invalid_params"
REASON_IN_FLIGHT = "in_flight"
REASON_NOT_READY = "not_ready"
REASON_RENDER_FAILED = "render_failed"
REASON_ADMISSION_TIMEOUT = "admission_timeout"
REASON_DEADLINE_EXCEEDED = "deadline_exceeded"
REASON_SOURCE_CHANGED = "source_changed"
REASON_INVALID_OUTPUT = "invalid_output"
REASON_DEFERRED_RETRY = "deferred_retry"
REASON_CHANGED_DURING_SCAN = "changed_during_scan"
REASON_NOT_ALLOWED_WEEK = "week_out_of_range"
REASON_NOT_SELECTED = "not_selected"

#: 一次确认最多处理多少条目标（防止把"手动"变成批量补发历史）。
MAX_TARGETS = 50
MAX_SCHEDULED_ENTRY_IDS = 20
MAX_SHOP_LIMIT = 200
MAX_REPORT_DAYS = 90

_WEEK_START_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

#: 未知外部结果、以及"配置/权限在预览之后变了"的统一文案。
UNKNOWN_DETAIL = "外部结果未知：无法证明没有产生副作用，禁止自动重发，请先核对业务台账"
SENT_BOOKKEEPING_DETAIL = (
    "已经出站但台账回填未确认：既不当作成功，也绝不当作没发出去；禁止自动重发"
)


def _invalid(message: str) -> OperationsError:
    return OperationsError(400, REASON_INVALID_PARAMS, message)


def _strict_int(
    value: object,
    *,
    name: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """严格整数：bool / float / 字符串一律拒绝，不做静默转换。"""

    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{name} 必须是整数")
    if value < minimum:
        raise _invalid(f"{name} 不能小于 {minimum}")
    if maximum is not None and value > maximum:
        raise _invalid(f"{name} 不能大于 {maximum}")
    return int(value)


def _optional_int(
    params: Mapping[str, Any] | None,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int | None:
    if not params or params.get(name) is None:
        return None
    return _strict_int(params.get(name), name=name, minimum=minimum, maximum=maximum)


def _run_status(items: Sequence[ItemResult]) -> tuple[str, str]:
    """按逐项结果收敛 run 状态；全 skipped 时给 ``no_work``，绝不包装成 succeeded。"""

    if not items:
        return RUN_STATUS_NO_WORK, REASON_NO_WORK
    unknown = [item for item in items if item.status == ITEM_STATUS_UNKNOWN]
    failed = [item for item in items if item.status == ITEM_STATUS_FAILED]
    succeeded = [item for item in items if item.status == ITEM_STATUS_SUCCEEDED]
    cancelled = [item for item in items if item.status == ITEM_STATUS_CANCELLED]
    if unknown:
        return RUN_STATUS_UNKNOWN, REASON_EXTERNAL_UNKNOWN
    if cancelled and not succeeded and not failed:
        return RUN_STATUS_CANCELLED, REASON_CANCEL_REQUESTED
    if failed and succeeded:
        return RUN_STATUS_PARTIAL, failed[0].reason or REASON_OK
    if failed:
        return RUN_STATUS_FAILED, failed[0].reason or REASON_OK
    if succeeded:
        return RUN_STATUS_SUCCEEDED, REASON_OK
    return RUN_STATUS_NO_WORK, items[0].reason or REASON_NO_WORK


async def _authorized_group_ids(session: AsyncSession) -> set[int]:
    return {int(value) for value in await authorized_group_ids(session)}


async def _actor_still_authorized(
    session: AsyncSession,
    settings: Any,
    *,
    group_id: int,
    user_id: int,
) -> bool:
    """发起人在**执行边界**是否仍是本群管理员。

    R2：预览时是管理员、确认后被撤权（或退群、或本地授权行被删）都不能继续在群里
    产生真实外发副作用。旧的 run 没有 actor 信息时返回 ``False``（无法复核就不放行），
    最高管理员按既有策略放行。
    """

    if int(user_id or 0) <= 0:
        return False
    if int(getattr(settings, "super_admin_id", 0) or 0) == int(user_id):
        return True
    return await is_group_admin_authorized(session, int(group_id), int(user_id))


def _parse_occurrence_stamp(stamp: str) -> datetime | None:
    """把 ``entry:{id}:{stamp}`` 里的 occurrence 时刻解析回 datetime（失败返回 None）。"""

    text = str(stamp or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


async def _runtime_manager(
    settings: Any, session_factory: async_sessionmaker[AsyncSession]
) -> RuntimeConfigManager:
    """构建并初始化运行时配置管理器（时段/阈值/奖励向量都来自运行时配置）。"""

    manager = RuntimeConfigManager(session_factory=session_factory, settings=settings)
    await manager.initialize()
    return manager


async def _bot_username(bot: Any) -> str:
    """取机器人 @username（签到提醒里的商店按钮要用）；取不到就不带按钮。"""

    if bot is None:
        return ""
    try:
        me = await bot.get_me()
    except Exception:  # 只是少一个按钮，不该让整次执行失败
        log.debug("operations: get_me failed | shop button omitted", exc_info=True)
        return ""
    return str(getattr(me, "username", "") or "").strip().lstrip("@")


def _cancelled_result(*, item_key: str, target: Mapping[str, Any]) -> ItemResult:
    """取消回执的唯一形状（v6：所有"未执行项"都走这里，口径一致）。"""

    return ItemResult(
        item_key=str(item_key),
        target=dict(target),
        status=ITEM_STATUS_CANCELLED,
        reason=REASON_CANCEL_REQUESTED,
        detail="已请求取消，本项未执行",
        resendable=True,
    )


def _cancelled_item(target: PreviewTarget) -> ItemResult:
    return _cancelled_result(item_key=target.item_key, target=target.target)


def _cancelled_report_result(
    *, item_key: str, target: Mapping[str, Any], award_note: str
) -> ItemResult:
    """周报这种**复合项**（结算 + 发送）的取消回执（v7）。

    周报在发送前就已经把积分结算**提交**掉了（R4 的 `session.commit()`），所以"取消"
    只可能取消掉后半截（发送）。此时回执必须带上真实的积分效果（新入账 N 分 / 此前
    已入账本次幂等跳过 / 没有达标成员）——笼统写"本项未执行"等于抹掉已经发生且不可
    回退的副作用，也会丢掉已经算好的金额说明。状态仍是取消、消息仍然没发出去；
    重新预览确认后可以再发周报，已入账的积分靠业务 ref 幂等，不会重复发放。
    """

    return ItemResult(
        item_key=str(item_key),
        target=dict(target),
        status=ITEM_STATUS_CANCELLED,
        reason=REASON_CANCEL_REQUESTED,
        detail=(
            f"已请求取消：本次没有发送周报，{award_note}；"
            "重试需要重新预览确认，已入账的积分不会重复发放"
        ),
        resendable=True,
    )


class _AdapterBase:
    """六类适配器的公共形状（严格参数校验 + 配置 revision 复核）。"""

    task_id: str = ""
    title: str = ""

    def normalize_params(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        return {}

    async def preview(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        group_id: int,
        params: Mapping[str, Any],
        bot: Any = None,
    ) -> TaskPreview:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def guard(
        self, session: AsyncSession, *, group_id: int, params: Mapping[str, Any]
    ) -> str:
        """确认事务里的业务守卫；返回非空 reason 即拒绝。"""

        return REASON_OK

    async def recheck_guard(
        self,
        session: AsyncSession,
        *,
        group_id: int,
        params: Mapping[str, Any],
        guard_revision: int,
        manager: Any = None,
    ) -> str:
        current = int(getattr(manager, "revision", 0) or 0)
        if guard_revision and current and current != int(guard_revision):
            # 预览之后配置被改过：参数可能已经不再合法，必须重新预览。
            return REASON_GUARD_BLOCKED
        return await self.guard(session, group_id=group_id, params=params)

    async def _authorized(
        self, session: AsyncSession, group_id: int
    ) -> bool:
        return int(group_id) in await _authorized_group_ids(session)

    async def _still_authorized(self, context: Any) -> bool:
        """在**新会话**里复核本群仍在授权列表（执行边界用）。"""

        async with context.session_factory() as session:
            return await self._authorized(session, int(context.group_id))

    async def _execution_guard(self, context: Any) -> str:
        """执行边界的统一复核（R2）：群授权 + 发起人**当前**权限 + 持久配置 revision。

        预览/确认只证明"当时"合法；真实外发动作发生在这里，所以必须用新开的会话重新
        读持久状态（授权行、管理员行、RuntimeConfigHistory 的当前 revision），不能复用
        manager 的内存快照，也不能假设参数在预览之后没变。

        B13b/C4：**计划触发的 run 没有人工发起人**（``source == "schedule"``，由内部
        scheduler 按群策略入队），发起人条件不适用，与
        :func:`bot.services.operations_guard.guard_predicate` 同一口径；其余条件
        （群授权、持久配置 revision、取消、按群策略 revision）照样复核。
        """

        group_id = int(context.group_id)
        actor = int(getattr(context, "actor_user_id", 0) or 0)
        scheduled = str(getattr(context, "source", "") or "") == "schedule"
        async with context.session_factory() as session:
            if not await self._authorized(session, group_id):
                return REASON_GUARD_BLOCKED
            if not scheduled and not await _actor_still_authorized(
                session, context.settings, group_id=group_id, user_id=actor
            ):
                return REASON_GUARD_BLOCKED
        manager = await _runtime_manager(context.settings, context.session_factory)
        expected = int(getattr(context, "guard_revision", 0) or 0)
        current = int(getattr(manager, "revision", 0) or 0)
        # 0 也是真实快照值（首次写入之前的 revision）。确认时的 0 与执行时的 1 必须
        # 判为不一致，否则「确认之后另一位管理员改了持久配置」可以在执行期逃过复核。
        if current != expected:
            return REASON_GUARD_BLOCKED
        return REASON_OK

    async def _cancel_pending(self, context: Any) -> bool:
        """取消是否已经生效（内存事件 **或** 持久标记，v6）。

        取消的真源是 ``operations_runs.cancel_requested``；内存事件只是 ``request_cancel``
        在**持久化成功之后**才置位的加速信号。因此"标记已落库、事件还没 set"的窗口里
        只看 ``context.cancel_requested()`` 会漏判：真实副作用写边界的守卫会按 SQL 谓词
        拒绝，适配器却会把这次拒绝报成"授权被撤/配置变更"（父 v16 探针复现的
        ``failed/guard_blocked``）。所有"开始新副作用之前"与"守卫拒绝之后"的判断都走
        这个助手，保证未执行项被如实归为 ``cancelled``/``cancel_requested``。

        读失败按"未取消"处理（保守），因此绝不会把撤权/配置漂移误归为取消。
        """

        if context.cancel_requested():
            return True
        return await cancel_requested_persisted(
            context.session_factory, run_id=int(getattr(context, "run_id", 0) or 0)
        )

    async def run(self, context: Any) -> TaskRunResult:  # pragma: no cover - 抽象
        raise NotImplementedError

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        run: OperationsRun,
        items: Sequence[OperationsRunItem],
    ) -> ReconcileResult | None:
        return None


# ---------------------------------------------------------------------------
# 定时消息
# ---------------------------------------------------------------------------


class ScheduledMessagesTask(_AdapterBase):
    task_id = "scheduled_messages"
    title = TASK_TITLES["scheduled_messages"]

    def normalize_params(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        raw = (params or {}).get("entry_ids")
        if raw is None:
            return {"entry_ids": []}
        if not isinstance(raw, (list, tuple)):
            raise _invalid("entry_ids 必须是整数列表")
        ordered: list[int] = []
        for value in raw:
            entry_id = _strict_int(value, name="entry_ids", minimum=1)
            if entry_id not in ordered:
                ordered.append(entry_id)
        if len(ordered) > MAX_SCHEDULED_ENTRY_IDS:
            raise _invalid(f"一次最多确认 {MAX_SCHEDULED_ENTRY_IDS} 条定时消息")
        return {"entry_ids": ordered}

    async def _entries(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        group_id: int,
        entry_ids: Sequence[int],
        now: datetime,
    ) -> tuple[list[tuple[ScheduledMessage, datetime]], dict[str, int]]:
        wanted = {int(value) for value in entry_ids}
        async with session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ScheduledMessage)
                        .where(ScheduledMessage.group_id == int(group_id))
                        .order_by(ScheduledMessage.id)
                    )
                ).all()
            )
            selected = [row for row in rows if not wanted or int(row.id) in wanted]
            skipped = {
                OCCURRENCE_ALREADY_DELIVERED: 0,
                "not_due": 0,
                "missing": 0,
                "outbound_evidence": 0,
            }
            known = {int(row.id) for row in rows}
            if wanted:
                skipped["missing"] = len([value for value in wanted if value not in known])
            candidates: list[tuple[ScheduledMessage, datetime]] = []
            for row in selected:
                state = scheduled_message_manual_state(row, now=now)
                if state == OCCURRENCE_DUE:
                    occurrence_at = scheduled_message_occurrence_at(row, now=now)
                    if occurrence_at is None:
                        skipped["not_due"] += 1
                    else:
                        candidates.append((row, occurrence_at))
                elif state == OCCURRENCE_ALREADY_DELIVERED:
                    skipped[OCCURRENCE_ALREADY_DELIVERED] += 1
                else:
                    skipped["not_due"] += 1
            # R1：出站证据是按"条目 + 这一时段"记的持久事实。有证据（sent/unknown）
            # 的时段**不是**可执行目标：预览就不再把它列进令牌，避免确认后靠别的
            # 入口重发。
            keys = [
                scheduled_occurrence_key(int(row.id), occurrence_at)
                for row, occurrence_at in candidates
            ]
            receipts = (
                await load_receipts(
                    session,
                    task_id=TASK_SCHEDULED_MESSAGE,
                    group_id=int(group_id),
                    business_keys=keys,
                )
                if keys
                else {}
            )
        due: list[tuple[ScheduledMessage, datetime]] = []
        for row, occurrence_at in candidates:
            key = scheduled_occurrence_key(int(row.id), occurrence_at)
            if key in receipts:
                skipped["outbound_evidence"] += 1
            else:
                due.append((row, occurrence_at))
        return due, skipped

    @staticmethod
    def _occurrence_stamp(occurrence_at: datetime) -> str:
        return occurrence_at.strftime("%Y-%m-%dT%H:%M:%S")

    def _target(
        self, row: ScheduledMessage, occurrence_at: datetime
    ) -> PreviewTarget:
        schedule = (
            f"每 {int(row.interval_minutes or 0)} 分钟"
            if str(row.schedule_type or "daily") == "interval"
            else f"每天 {row.schedule_time}"
        )
        stamp = self._occurrence_stamp(occurrence_at)
        return PreviewTarget(
            # R1：item_key 里带**准确的 occurrence**，而不是只有 entry id —— 否则
            # 同一行的不同时段会共用同一个业务身份，重试/对账都说不清是哪一次。
            item_key=f"entry:{int(row.id)}:{stamp}",
            target={
                "entry_id": int(row.id),
                "occurrence_at": stamp,
                "schedule_type": str(row.schedule_type or "daily"),
                "schedule_time": str(row.schedule_time or ""),
                "interval_minutes": int(row.interval_minutes or 0),
            },
            label=f"定时消息 #{int(row.id)}",
            detail=f"{schedule} · {stamp} · 只发当前这一时段",
        )

    async def preview(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        group_id: int,
        params: Mapping[str, Any],
        bot: Any = None,
    ) -> TaskPreview:
        now = now_shanghai_naive()
        entry_ids = [int(value) for value in params.get("entry_ids") or ()]
        async with session_factory() as session:
            if not await self._authorized(session, group_id):
                return TaskPreview(status="blocked", reason=REASON_GUARD_BLOCKED)
        due, skipped = await self._entries(
            session_factory, group_id=group_id, entry_ids=entry_ids, now=now
        )
        targets = tuple(
            self._target(row, occurrence_at) for row, occurrence_at in due
        )
        extra = {
            "already_delivered": skipped[OCCURRENCE_ALREADY_DELIVERED],
            "not_due": skipped["not_due"],
            "missing": skipped["missing"],
            "outbound_evidence": skipped["outbound_evidence"],
            "checked_at": now.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        effects = (
            "向本群发送这些定时消息（发送本身不产生 Telegram 货币费用，但仍是"
            "真实外发动作：置顶/自动删除等副作用会一并发生）",
        )
        cost = {
            "telegram_messages": len(targets),
            "telegram_monetary_cost": "none",
            "other_cost": "unknown",
        }
        if targets:
            return TaskPreview(
                status="ok",
                targets=targets,
                effects=effects,
                cost=cost,
                extra=extra,
                detail=f"当前有 {len(targets)} 条处于到期时段，只发这一时段，不补发历史",
            )
        if entry_ids and skipped["missing"] == len(entry_ids):
            return TaskPreview(
                status="blocked",
                reason=REASON_NOT_FOUND,
                detail="这些定时消息在本群不存在（或已被删除）",
                extra=extra,
            )
        return TaskPreview(
            status="no_work",
            reason=(
                REASON_ALREADY_DELIVERED
                if (
                    skipped[OCCURRENCE_ALREADY_DELIVERED]
                    or skipped["outbound_evidence"]
                )
                else REASON_NOT_DUE
            ),
            detail="当前没有处于到期时段的定时消息（没到点、这一时段已经发过，或已有出站证据），不会补发",
            effects=effects,
            cost=cost,
            extra=extra,
        )

    async def guard(
        self, session: AsyncSession, *, group_id: int, params: Mapping[str, Any]
    ) -> str:
        if not await self._authorized(session, group_id):
            return REASON_GUARD_BLOCKED
        entry_ids = [int(value) for value in params.get("entry_ids") or ()]
        if not entry_ids:
            return REASON_OK
        rows = (
            await session.execute(
                select(ScheduledMessage.id, ScheduledMessage.enabled).where(
                    ScheduledMessage.group_id == int(group_id),
                    ScheduledMessage.id.in_(entry_ids),
                )
            )
        ).all()
        enabled = {int(row[0]) for row in rows if bool(row[1])}
        if any(int(value) not in enabled for value in entry_ids):
            # 目标在预览之后被删除/禁用：此时执行会越界，直接拒绝确认。
            return REASON_GUARD_BLOCKED
        return REASON_OK

    async def run(self, context: Any) -> TaskRunResult:
        now = now_shanghai_naive()
        entry_ids = [int(value) for value in context.params.get("entry_ids") or ()]
        # R2：执行边界重新核群授权 / 发起人当前权限 / 持久配置 revision。
        blocked = await self._execution_guard(context)
        if blocked:
            return TaskRunResult(
                status=RUN_STATUS_FAILED,
                reason=blocked,
                detail="执行前复核未通过（授权被撤、发起人已不是管理员或配置已变更），未执行任何动作",
            )
        due, _skipped = await self._entries(
            context.session_factory,
            group_id=context.group_id,
            entry_ids=entry_ids,
            now=now,
        )
        service = ScheduledMessageService(
            bot=context.bot,
            settings=context.settings,
            session_factory=context.session_factory,
        )
        items: list[ItemResult] = []
        for row, occurrence_at in due:
            target = self._target(row, occurrence_at)
            if not context.is_bound(target.item_key):
                # R1/R2：确认时选中的是**准确的时段**；不在快照里的目标（这一行的
                # 另一个时段、或扫描期间新出现的时段）不属于这次 run，绝不能被
                # "顺手"处理掉。
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_NOT_SELECTED,
                        detail="不在本次确认选中的目标快照内，未处理",
                        resendable=False,
                    )
                )
                continue
            if await self._cancel_pending(context):
                items.append(_cancelled_item(target))
                continue
            previous = context.previous(target.item_key)
            if previous is not None and previous.status in (
                ITEM_STATUS_SUCCEEDED,
                ITEM_STATUS_UNKNOWN,
            ):
                # 重试时：上一次已经成功/结果未知的目标本轮不再执行（R1）。
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=previous.status,
                        reason=previous.reason or REASON_ALREADY_SUCCEEDED,
                        detail="上一次执行已确认发送或结果未知，本轮不重复执行",
                        external_message_id=int(previous.external_message_id or 0),
                        resendable=False,
                    )
                )
                continue
            try:
                code, message_id = await service.deliver_entry_occurrence(
                    int(row.id),
                    group_id=context.group_id,
                    now=now,
                    protect_outbound=True,
                    guard=PersistedGuard.from_context(context),
                )
            except Exception as exc:  # 发送链异常：可能已经出站
                log.warning(
                    "operations scheduled message failed | group=%s | entry=%s | %s",
                    context.group_id,
                    row.id,
                    type(exc).__name__,
                )
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_EXTERNAL_UNKNOWN,
                        detail=UNKNOWN_DETAIL,
                        resendable=False,
                    )
                )
                continue
            if code == DELIVER_RESULT_SENT:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SUCCEEDED,
                        detail="已发送（同一时段不会重复发送）",
                        external_message_id=int(message_id or 0),
                        resendable=False,
                    )
                )
            elif code == DELIVER_RESULT_ALREADY_DELIVERED:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_ALREADY_DELIVERED,
                        detail="这个时段已经发过，跳过",
                        resendable=False,
                    )
                )
            elif code == DELIVER_RESULT_NOT_DUE:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_NOT_DUE,
                        detail="确认时已不在到期时段，跳过",
                        resendable=False,
                    )
                )
            elif code == DELIVER_RESULT_DISABLED:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_DISABLED,
                        detail="确认时这条定时消息已被禁用，未发送",
                        resendable=False,
                    )
                )
            elif code == DELIVER_RESULT_ENTRY_MISSING:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_FAILED,
                        reason=REASON_NOT_FOUND,
                        detail="定时消息在本群已不存在，未发送",
                        resendable=False,
                    )
                )
            elif code == DELIVER_RESULT_COMPLETION_UNCONFIRMED:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_UNKNOWN,
                        reason="sent_bookkeeping_failed",
                        detail=SENT_BOOKKEEPING_DETAIL,
                        external_message_id=int(message_id or 0),
                        resendable=False,
                    )
                )
            elif code == DELIVER_RESULT_OUTBOUND_UNKNOWN:
                # R1：这个时段已经有"已出站但结果未知"的持久证据。绝不能当成
                # "没发出去"重发，也不能谎报失败。
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_NOT_SAFELY_RESENDABLE,
                        detail=SENT_BOOKKEEPING_DETAIL,
                        external_message_id=int(message_id or 0),
                        resendable=False,
                    )
                )
            elif code == DELIVER_RESULT_GUARD_BLOCKED:
                # R2（返工 v3）：写边界复核不通过——没有出站、没有副作用。
                if await self._cancel_pending(context):
                    # v6：写边界的守卫谓词里包含"本 run 已被取消"，取消落库后会以
                    # guard_blocked 返回；此时未执行项必须如实归类为取消，而不是报成
                    # 授权被撤/配置漂移（父 v16 探针复现的 failed/guard_blocked）。
                    items.append(_cancelled_item(target))
                    continue
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_FAILED,
                        reason=REASON_GUARD_BLOCKED,
                        detail=(
                            "出站前复核未通过（授权被撤、发起人已不是管理员、持久配置"
                            "已变更或本 run 已被取消）：未发送任何消息，请重新预览后确认"
                        ),
                        resendable=False,
                    )
                )
            elif code == DELIVER_RESULT_SEND_REJECTED:
                # 出站前就失败或被明确拒收：证明没有副作用，可安全重试。
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_FAILED,
                        reason=REASON_RENDER_FAILED,
                        detail="出站前失败，未发送任何消息，可安全重试",
                        resendable=True,
                    )
                )
            else:  # DELIVER_RESULT_SEND_FAILED 及其它
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_EXTERNAL_UNKNOWN,
                        detail=UNKNOWN_DETAIL,
                        resendable=False,
                    )
                )
        status, reason = _run_status(items)
        return TaskRunResult(status=status, reason=reason, items=tuple(items))

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        run: OperationsRun,
        items: Sequence[OperationsRunItem],
    ) -> ReconcileResult | None:
        """按**准确 occurrence 的持久出站证据**对账；没有证据就保持未知。

        R3：行被删除、水位推进、租约失效都**不能**证明"没发出去"或"确实发过"。
        只有 ``entry:{id}:{occurrence}`` 这条业务键上的 receipt 才能作证。
        """

        if not items:
            return None
        results: list[ItemResult] = []
        for item in items:
            if item.status not in (ITEM_STATUS_UNKNOWN, ITEM_STATUS_FAILED):
                continue
            target = dict(item.target or {})
            entry_id = int(target.get("entry_id") or 0)
            if entry_id <= 0:
                continue
            stamp = str(target.get("occurrence_at") or "")
            occurrence_at = _parse_occurrence_stamp(stamp)
            if occurrence_at is None:
                # 旧数据没有 occurrence 信息：无法把证据对到具体时段，保持未知。
                continue
            receipt = await load_receipt(
                session,
                task_id=TASK_SCHEDULED_MESSAGE,
                group_id=int(run.group_id),
                business_key=scheduled_occurrence_key(entry_id, occurrence_at),
            )
            if receipt is None:
                continue
            # 有出站证据 → 绝不重发、绝不改写成"没发出去"、也不清零消息 id。
            # 但**不**据此把这一项升级成 succeeded：出站证据只能证明"这一步真的做过"
            # （所以不可重发），证明不了本时段的完成/结算（R3：水位、行状态、占位都
            # 不能替代准确 receipt 的完成确认），所以保守保持 unknown。
            confirmed = receipt.state == RECEIPT_STATE_SENT
            results.append(
                ItemResult(
                    item_key=str(item.item_key),
                    target=target,
                    status=ITEM_STATUS_UNKNOWN,
                    reason=REASON_NOT_SAFELY_RESENDABLE,
                    detail=(
                        "已有出站证据（这一时段确实发送过），但本时段没有被完成台账确认；"
                        "保持未知且禁止自动重发"
                        if confirmed
                        else "出站证据表明结果仍然未知（可能已发送），禁止自动重发"
                    ),
                    external_message_id=int(receipt.external_message_id or 0),
                    resendable=False,
                )
            )
        if not results:
            return None
        return ReconcileResult(
            status=RUN_STATUS_PARTIAL, items=tuple(results), detail="按出站证据对账后更新"
        )


# ---------------------------------------------------------------------------
# 群摘要
# ---------------------------------------------------------------------------


def _summary_run_key(run_key: str) -> str:
    """摘要发布的**本次 run 证据键**（R3）。

    ``run_key`` 是确认时生成的唯一执行标识，所以"这一行 receipt 属于哪一次 run"
    是确定的；不再用 ``GroupSummary.generated_at`` 和 run.started_at 比时间来猜
    归属（别的发布者发布的新版本会比本次更晚，时间比较必然误判）。
    """

    return f"run:{str(run_key or '').strip()}"


class GroupSummaryTask(_AdapterBase):
    task_id = "group_summary"
    title = TASK_TITLES["group_summary"]

    def normalize_params(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        action = str((params or {}).get("action") or "generate").strip()
        if action not in ("generate", "retry"):
            raise _invalid("action 只允许 generate / retry")
        return {"action": action}

    @staticmethod
    def _scheduler() -> Any:
        # 模块属性访问：直接 from-import 会把进程启动时的 None 快照下来。
        return group_summary_module.GROUP_SUMMARY_SCHEDULER

    async def preview(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        group_id: int,
        params: Mapping[str, Any],
        bot: Any = None,
    ) -> TaskPreview:
        cfg = group_summary_config(settings)
        async with session_factory() as session:
            if not await self._authorized(session, group_id):
                return TaskPreview(status="blocked", reason=REASON_GUARD_BLOCKED)
        if not cfg.enabled:
            return TaskPreview(
                status="blocked",
                reason=REASON_DISABLED,
                detail="群摘要功能当前是关闭的（enabled=false），不会因为手动触发而打开",
            )
        scheduler = self._scheduler()
        if scheduler is None:
            return TaskPreview(
                status="blocked",
                reason=REASON_RUNNER_UNAVAILABLE,
                detail="摘要 worker 未初始化，无法执行（不会假装排队）",
            )
        if scheduler.group_is_running(int(group_id)):
            return TaskPreview(
                status="blocked",
                reason=REASON_QUEUE_FULL,
                detail="本群已经有一个摘要任务在跑，等它结束再试",
            )
        backoff_until = float(scheduler.group_backoff_until(int(group_id)) or 0.0)
        now = now_shanghai_naive()
        if backoff_until > now.timestamp():
            return TaskPreview(
                status="blocked",
                reason=REASON_BACKOFF,
                detail="本群摘要正处于失败退避窗口内，稍后再试",
                extra={"backoff_until": backoff_until},
            )
        pending = await scheduler.manual_pending_count(int(group_id), cfg)
        extra = {
            "pending": int(pending),
            "trigger_messages": int(cfg.trigger_messages),
            "active": int(scheduler.active_count()),
            "capacity": int(scheduler.capacity(cfg)),
        }
        if pending <= 0:
            return TaskPreview(
                status="no_work",
                reason=REASON_NO_WORK,
                detail="本群当前没有可摘要的新归档，未调用模型",
                extra=extra,
            )
        return TaskPreview(
            status="ok",
            targets=(
                PreviewTarget(
                    item_key=f"summary:{int(group_id)}",
                    target={"group_id": int(group_id), "pending": int(pending)},
                    label="生成本群摘要",
                    detail=f"待摘要归档 {int(pending)} 条（手动触发只放宽阈值，不绕预算与闸门）",
                ),
            ),
            effects=(
                "调用模型生成本群摘要并发布新版本（会消耗 token 与预算；发布带 source revision CAS）",
            ),
            cost={
                "llm_tokens": "unknown",
                "llm_monetary_cost": "unknown",
                "telegram_monetary_cost": "none",
                "points": "none",
                "note": "模型用量与金额在调用前无法预知，不编造估算",
            },
            extra=extra,
            detail="手动生成会真实调用模型并发布版本；排队成功不代表完成",
        )

    async def guard(
        self, session: AsyncSession, *, group_id: int, params: Mapping[str, Any]
    ) -> str:
        if not await self._authorized(session, group_id):
            return REASON_GUARD_BLOCKED
        cfg = group_summary_config(getattr(self, "_settings", None))
        scheduler = self._scheduler()
        if scheduler is None or not cfg.enabled:
            return REASON_GUARD_BLOCKED
        if scheduler.group_is_running(int(group_id)):
            return REASON_QUEUE_FULL
        backoff_until = float(scheduler.group_backoff_until(int(group_id)) or 0.0)
        if backoff_until > now_shanghai_naive().timestamp():
            return REASON_BACKOFF
        return REASON_OK

    async def run(self, context: Any) -> TaskRunResult:
        cfg = group_summary_config(context.settings)
        target = PreviewTarget(
            item_key=f"summary:{int(context.group_id)}",
            target={"group_id": int(context.group_id)},
            label="生成本群摘要",
        )
        # R2（返工 v2）：摘要以前只查群授权，撤权/换配置的发起人仍能推动发布。
        # 六类适配器统一在真实执行边界用同一份复核（群授权 + 发起人当前权限 +
        # 持久配置 revision），并且这一份复核要在**每次读写/发布之前**再做一遍。
        blocked = await self._execution_guard(context)
        if blocked:
            return TaskRunResult(
                status=RUN_STATUS_FAILED,
                reason=blocked,
                detail="执行前复核未通过（授权被撤、发起人已不是管理员或配置已变更），未执行任何动作",
            )
        async with context.session_factory() as session:
            if not await self._authorized(session, context.group_id):
                return TaskRunResult(
                    status=RUN_STATUS_FAILED,
                    reason=REASON_GUARD_BLOCKED,
                    detail="本群已不在授权列表，未执行任何动作",
                )
        scheduler = self._scheduler()
        if scheduler is None:
            return TaskRunResult(
                status=RUN_STATUS_FAILED,
                reason=REASON_RUNNER_UNAVAILABLE,
                detail="摘要 worker 未初始化",
                items=(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_FAILED,
                        reason=REASON_RUNNER_UNAVAILABLE,
                        resendable=False,
                    ),
                ),
            )
        if not cfg.enabled:
            return TaskRunResult(
                status=RUN_STATUS_NO_WORK,
                reason=REASON_DISABLED,
                items=(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_DISABLED,
                        resendable=False,
                    ),
                ),
            )
        if scheduler.group_is_running(int(context.group_id)):
            return TaskRunResult(
                status=RUN_STATUS_NO_WORK,
                reason=REASON_QUEUE_FULL,
                items=(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_QUEUE_FULL,
                        resendable=True,
                    ),
                ),
            )
        backoff_until = float(scheduler.group_backoff_until(int(context.group_id)) or 0.0)
        if backoff_until > now_shanghai_naive().timestamp():
            return TaskRunResult(
                status=RUN_STATUS_NO_WORK,
                reason=REASON_BACKOFF,
                items=(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_BACKOFF,
                        detail="退避窗口内不执行；窗口结束后可重新预览",
                        resendable=True,
                    ),
                ),
            )
        if await self._cancel_pending(context):
            return TaskRunResult(
                status=RUN_STATUS_NO_WORK,
                reason=REASON_CANCEL_REQUESTED,
                items=(_cancelled_item(target),),
            )
        try:
            code = await scheduler.run_manual(
                int(context.group_id),
                cfg,
                cancelled=context.cancel_requested,
                # R6：取消条件要进发布 CAS 的 SQL 谓词，不能只在发布前查一次。
                guard_run_key=str(getattr(context, "run_key", "") or ""),
                # R2（返工 v3）：授权/发起人/持久配置 revision 也要进同一句发布 CAS。
                # 生成要跨模型调用，只在 run 起点查一次等于把授权窗口留到任意长。
                guard=PersistedGuard.from_context(context),
            )
        except Exception as exc:
            log.warning(
                "operations group summary failed | group=%s | %s",
                context.group_id,
                type(exc).__name__,
            )
            return TaskRunResult(
                status=RUN_STATUS_UNKNOWN,
                reason=REASON_EXTERNAL_UNKNOWN,
                items=(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_EXTERNAL_UNKNOWN,
                        detail=UNKNOWN_DETAIL,
                        resendable=False,
                    ),
                ),
            )
        mapping = {
            "published": (ITEM_STATUS_SUCCEEDED, REASON_OK, ""),
            "no_messages": (ITEM_STATUS_SKIPPED, REASON_NO_WORK, "没有可摘要的归档"),
            "not_ready": (ITEM_STATUS_SKIPPED, REASON_NOT_READY, "归档数量不足，未生成"),
            "skipped": (ITEM_STATUS_SKIPPED, REASON_NO_WORK, "调度器跳过（本群已有任务在跑）"),
            "admission_timeout": (
                ITEM_STATUS_FAILED,
                REASON_ADMISSION_TIMEOUT,
                "等待并发许可超时，未开始生成",
            ),
            "deadline_exceeded": (
                ITEM_STATUS_UNKNOWN,
                REASON_DEADLINE_EXCEEDED,
                "生成超时：是否已经发布版本未知，禁止自动重发",
            ),
            "cancelled": (
                ITEM_STATUS_CANCELLED,
                REASON_CANCEL_REQUESTED,
                "取消已生效：发布 CAS 之前退出，没有发布新版本",
            ),
            "failure": (ITEM_STATUS_FAILED, REASON_GENERATION_FAILED, "生成失败"),
            "invalid_output": (ITEM_STATUS_FAILED, REASON_INVALID_OUTPUT, "模型输出不可用"),
            "source_changed": (
                ITEM_STATUS_FAILED,
                REASON_SOURCE_CHANGED,
                "归档在生成期间变化，发布被 CAS 拒绝",
            ),
            "stale": (ITEM_STATUS_FAILED, REASON_SOURCE_CHANGED, "结果已过期，未发布"),
            "guard_blocked": (
                ITEM_STATUS_FAILED,
                REASON_GUARD_BLOCKED,
                "发布前复核未通过（授权被撤、发起人已不是管理员、持久配置已变更"
                "或本 run 已被取消）：没有发布任何版本，请重新预览后确认",
            ),
        }
        status, reason, detail = mapping.get(
            str(code), (ITEM_STATUS_UNKNOWN, REASON_ADAPTER_CRASHED, "未知的调度器结果")
        )
        if str(code) == "guard_blocked" and await self._cancel_pending(context):
            # v6：发布 CAS 的守卫谓词里包含"本 run 已被取消"（NOT EXISTS
            # operations_runs.cancel_requested），所以取消落库后会以 guard_blocked 回来；
            # 此时未发布任何版本的真实原因就是取消，必须如实归类，而不是报成授权被撤。
            status, reason, detail = (
                ITEM_STATUS_CANCELLED,
                REASON_CANCEL_REQUESTED,
                "取消已生效：发布 CAS 之前退出，没有发布新版本",
            )
        resendable = status == ITEM_STATUS_FAILED and str(code) != "guard_blocked"
        if status == ITEM_STATUS_SUCCEEDED:
            detail = await self._record_publication(
                context.session_factory,
                group_id=int(context.group_id),
                run_key=str(context.run_key),
            )
        return TaskRunResult(
            status=_run_status(
                [
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=status,
                        reason=reason,
                        detail=detail,
                        resendable=resendable,
                    )
                ]
            )[0],
            reason=reason,
            items=(
                ItemResult(
                    item_key=target.item_key,
                    target=dict(target.target),
                    status=status,
                    reason=reason,
                    detail=detail,
                    resendable=resendable,
                ),
            ),
        )

    @staticmethod
    def _publication_detail(summary: Any) -> str:
        """把已发布版本写成人类可读的准确证据（读不到就说读不到）。"""

        if summary is None:
            return "生成结束但没有读到已发布版本（请对账后再决定是否重试）"
        generated = (
            summary.generated_at.strftime("%Y-%m-%d %H:%M:%S")
            if summary.generated_at is not None
            else "unknown"
        )
        return (
            f"已发布版本 v{summary.version} · 覆盖到归档 id {summary.covered_through_id}"
            f" · 生成于 {generated}"
        )

    @classmethod
    async def _record_publication(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        group_id: int,
        run_key: str,
    ) -> str:
        """发布 CAS 成功后，立刻写下**本次 run 自己的**发布证据（R3）。

        证据键含 ``run_key``，所以"这一行属于哪一次执行"是确定的：对账只认本次
        证据，绝不会拿别的发布者写入的 ``GroupSummary.generated_at`` 水位冒充本
        次成功。读不到已发布版本时不写证据（对账会保持未知），写证据失败也不把
        真实成功改判为失败（只在日志里留痕）。
        """

        detail = "生成结束但没有读到已发布版本（请对账后再决定是否重试）"
        try:
            summary = await group_summary_module.SqlGroupSummaryStore(
                session_factory
            ).load(int(group_id))
            detail = cls._publication_detail(summary)
            if summary is None:
                return detail
            evidence = (
                f"已发布版本 v{int(summary.version)} · source revision"
                f" {int(summary.source_revision)} · 覆盖到归档 id"
                f" {int(summary.covered_through_id)} · 生成于"
                f" {detail.split('生成于 ', 1)[-1]}"
            )
            async with session_factory() as session:
                await record_receipt(
                    session,
                    task_id=TASK_GROUP_SUMMARY,
                    group_id=int(group_id),
                    business_key=_summary_run_key(run_key),
                    state=RECEIPT_STATE_SENT,
                    detail=evidence,
                )
                await session.commit()
        except Exception:
            log.warning(
                "operations summary publication receipt failed | group=%s",
                group_id,
                exc_info=True,
            )
        return detail

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        run: OperationsRun,
        items: Sequence[OperationsRunItem],
    ) -> ReconcileResult | None:
        """只认**本次 run 自己的**发布证据（R3）。

        绝不再用 ``GroupSummary.generated_at`` 与 ``run.started_at`` 比水位：别的
        发布者发布的新版本一定比本次更晚，时间比较会把别人的成果算成本次成功。
        没有本次证据时一律保持未知（既不宣称成功，也不自动重发）。
        """

        receipt = await load_receipt(
            session,
            task_id=TASK_GROUP_SUMMARY,
            group_id=int(run.group_id),
            business_key=_summary_run_key(str(run.run_key)),
        )
        candidates: list[tuple[str, dict[str, Any]]] = []
        if items:
            for item in items:
                if item.status not in (ITEM_STATUS_UNKNOWN, ITEM_STATUS_FAILED):
                    continue
                candidates.append((str(item.item_key), dict(item.target or {})))
        else:
            # 重启重建：进程死在结果落盘之前时审计里可能一行 item 都没有。
            # 用确认时持久化的目标快照 + 本次 run 的发布证据重建保守逐项状态：
            # 有证据 → 成功；没证据 → 未知（绝不默认失败、绝不自动重发）。
            for bound_key in (
                str(key) for key in (run.target_keys or []) if str(key or "").strip()
            ):
                candidates.append((bound_key, {"group_id": int(run.group_id)}))
        if not candidates:
            return None
        results: list[ItemResult] = []
        for item_key, target in candidates:
            if receipt is None:
                results.append(
                    ItemResult(
                        item_key=item_key,
                        target=target,
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_NOT_SAFELY_RESENDABLE,
                        detail=(
                            "没有本次执行的发布证据：可能没有发布，也可能发布成功但证据丢失；"
                            "保持未知，禁止自动重发，请人工核对已发布版本后再决定"
                        ),
                        resendable=False,
                    )
                )
            else:
                results.append(
                    ItemResult(
                        item_key=item_key,
                        target=target,
                        status=ITEM_STATUS_SUCCEEDED,
                        detail=f"本次执行自己的发布证据：{receipt.detail}",
                        resendable=False,
                    )
                )
        return ReconcileResult(status=RUN_STATUS_PARTIAL, items=tuple(results))


# ---------------------------------------------------------------------------
# 签到提醒
# ---------------------------------------------------------------------------


class CheckinReminderTask(_AdapterBase):
    task_id = "checkin_reminder"
    title = TASK_TITLES["checkin_reminder"]

    def normalize_params(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        slot = _optional_int(params, "slot", minimum=0, maximum=23)
        return {"slot": slot}

    @staticmethod
    def _resolve_slot(slot: int | None, now: datetime) -> tuple[int | None, str]:
        """只允许「当天已经到点」且属于当前生效时段的 slot。"""

        slots = tuple(int(value) for value in reminder_slots())
        minutes = now.hour * 60 + now.minute
        reached = [value for value in slots if value * 60 <= minutes]
        if slot is None:
            if not reached:
                return None, REASON_NOT_DUE
            return max(reached), REASON_OK
        if slot not in slots:
            raise _invalid(
                "slot 不在当前生效的提醒时段里："
                + "/".join(str(value) for value in slots)
            )
        if slot not in reached:
            return None, REASON_NOT_DUE
        return int(slot), REASON_OK

    async def preview(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        group_id: int,
        params: Mapping[str, Any],
        bot: Any = None,
    ) -> TaskPreview:
        now = now_shanghai_naive()
        async with session_factory() as session:
            if not await self._authorized(session, group_id):
                return TaskPreview(status="blocked", reason=REASON_GUARD_BLOCKED)
        await _runtime_manager(settings, session_factory)
        slot, reason = self._resolve_slot(params.get("slot"), now)
        if slot is None:
            return TaskPreview(
                status="no_work",
                reason=reason,
                detail="今天还没有到任何提醒时段（或请求的时段尚未到点），不补发往日时段",
                extra={"slots": list(reminder_slots())},
            )
        key = slot_key(now.date(), slot)
        async with session_factory() as session:
            receipt = await reminder_outbound_receipt(
                session, group_id=int(group_id), key=key
            )
            state = await reminder_slot_state(session, group_id=int(group_id), key=key)
        if receipt is not None:
            # R1：这个时段已经有真实出站证据。占位可能因为回填失败而看起来是空的，
            # 但消息已经发出去了：预览绝不能给出可确认的目标。
            if receipt.state == RECEIPT_STATE_SENT:
                return TaskPreview(
                    status="no_work",
                    reason=REASON_ALREADY_DELIVERED,
                    detail=f"{key} 这个时段已有出站证据确认发送过",
                    extra={"slot_key": key, "slot": slot, "outbound_receipt": "sent"},
                )
            return TaskPreview(
                status="blocked",
                reason=REASON_IN_FLIGHT,
                detail=(
                    f"{key} 这个时段已有出站证据但结果未知（可能已经发出去了）："
                    "禁止重发，请人工核对群里的实际消息"
                ),
                extra={"slot_key": key, "slot": slot, "outbound_receipt": "unknown"},
            )
        if state == "delivered":
            return TaskPreview(
                status="no_work",
                reason=REASON_ALREADY_DELIVERED,
                detail=f"{key} 这个时段已经发过了",
                extra={"slot_key": key, "slot": slot},
            )
        if state == "placeholder":
            return TaskPreview(
                status="blocked",
                reason=REASON_IN_FLIGHT,
                detail=(
                    f"{key} 已有占位但没有投递记录：可能正在发送（结果未知）。"
                    "按既有策略由过期占位回收处理，这里不重复发送"
                ),
                extra={"slot_key": key, "slot": slot},
            )
        return TaskPreview(
            status="ok",
            targets=(
                PreviewTarget(
                    item_key=f"checkin:{key}",
                    target={"group_id": int(group_id), "slot": slot, "slot_key": key},
                    label=f"{slot}:00 签到提醒",
                    detail="只发今天这一个时段，一个时段只会发一次",
                ),
            ),
            effects=(
                "向本群发送签到提醒（含既有按钮），并按既有策略安排自动删除",
            ),
            cost={
                "telegram_messages": 1,
                "telegram_monetary_cost": "none",
                "llm_tokens": "none",
                "other_cost": "unknown",
            },
            extra={"slot_key": key, "slot": slot, "slots": list(reminder_slots())},
        )

    async def guard(
        self, session: AsyncSession, *, group_id: int, params: Mapping[str, Any]
    ) -> str:
        if not await self._authorized(session, group_id):
            return REASON_GUARD_BLOCKED
        slot, reason = self._resolve_slot(params.get("slot"), now_shanghai_naive())
        if slot is None:
            return reason
        key = slot_key(now_shanghai_naive().date(), slot)
        receipt = await reminder_outbound_receipt(
            session, group_id=int(group_id), key=key
        )
        if receipt is not None:
            return (
                REASON_ALREADY_DELIVERED
                if receipt.state == RECEIPT_STATE_SENT
                else REASON_IN_FLIGHT
            )
        state = await reminder_slot_state(session, group_id=int(group_id), key=key)
        if state == "delivered":
            return REASON_ALREADY_DELIVERED
        if state == "placeholder":
            return REASON_IN_FLIGHT
        return REASON_OK

    async def run(self, context: Any) -> TaskRunResult:
        now = now_shanghai_naive()
        slot = context.params.get("slot")
        resolved, reason = self._resolve_slot(
            None if slot is None else int(slot), now
        )
        if resolved is None:
            return TaskRunResult(
                status=RUN_STATUS_NO_WORK,
                reason=reason,
                items=(),
            )
        key = slot_key(now.date(), resolved)
        target = PreviewTarget(
            item_key=f"checkin:{key}",
            target={"group_id": int(context.group_id), "slot": resolved, "slot_key": key},
            label=f"{resolved}:00 签到提醒",
        )
        if not context.is_bound(target.item_key):
            # R2：确认时选中的目标快照里没有它（例如跨天/时段错位），不该被这次 run
            # 顺手发出去。
            skipped = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_SKIPPED,
                reason=REASON_NOT_SELECTED,
                detail="不在本次确认选中的目标快照内，未处理",
                resendable=False,
            )
            return TaskRunResult(
                status=RUN_STATUS_NO_WORK,
                reason=REASON_NOT_SELECTED,
                items=(skipped,),
            )
        previous = context.previous(target.item_key)
        if previous is not None and previous.status in (
            ITEM_STATUS_SUCCEEDED,
            ITEM_STATUS_UNKNOWN,
        ):
            # 重试时上一次已经成功/结果未知：本轮不得再发。
            repeated = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=previous.status,
                reason=previous.reason or REASON_ALREADY_SUCCEEDED,
                detail="上一次执行已确认发送或结果未知，本轮不重复执行",
                external_message_id=int(previous.external_message_id or 0),
                resendable=False,
            )
            return TaskRunResult(
                status=_run_status([repeated])[0],
                reason=repeated.reason,
                items=(repeated,),
            )
        blocked = await self._execution_guard(context)
        if blocked:
            # R2：确认之后群授权被撤 / 发起人不再是管理员 / 配置被改：不发送任何消息。
            return TaskRunResult(
                status=RUN_STATUS_FAILED,
                reason=blocked,
                detail="执行前复核未通过（授权被撤、发起人已不是管理员或配置已变更），未发送任何消息",
            )
        if await self._cancel_pending(context):
            return TaskRunResult(
                status=RUN_STATUS_CANCELLED,
                reason=REASON_CANCEL_REQUESTED,
                items=(_cancelled_item(target),),
            )
        bot_username = await _bot_username(context.bot)
        async with context.session_factory() as session:
            try:
                outcome = await send_checkin_slot(
                    session,
                    context.bot,
                    group_id=int(context.group_id),
                    slot=int(resolved),
                    auto_delete_seconds=reminder_auto_delete_seconds(),
                    queue_auto_delete=schedule_message_auto_delete_durable,
                    backfill_auto_delete=backfill_durable_auto_delete,
                    session_factory=context.session_factory,
                    bot_username=bot_username,
                    protect_outbound=True,
                    guard=PersistedGuard.from_context(context),
                )
            except Exception as exc:
                log.warning(
                    "operations checkin reminder failed | group=%s | %s",
                    context.group_id,
                    type(exc).__name__,
                )
                return TaskRunResult(
                    status=RUN_STATUS_UNKNOWN,
                    reason=REASON_EXTERNAL_UNKNOWN,
                    items=(
                        ItemResult(
                            item_key=target.item_key,
                            target=dict(target.target),
                            status=ITEM_STATUS_UNKNOWN,
                            reason=REASON_EXTERNAL_UNKNOWN,
                            detail=UNKNOWN_DETAIL,
                            resendable=False,
                        ),
                    ),
                )
        if outcome.status == CHECKIN_OUTCOME_SENT:
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_SUCCEEDED,
                detail=(
                    f"{key} 已发送 · 今日已签到 {outcome.checked_in} 人"
                    f" · 自动删除 {outcome.auto_delete}"
                ),
                external_message_id=int(outcome.message_id or 0),
                resendable=False,
            )
        elif (
            outcome.status == CHECKIN_OUTCOME_ALREADY_DELIVERED
            and outcome.reason
            in ("outbound_receipt_unknown", "outbound_dispatch_protected")
        ):
            # 已经有"已出站/正在出站但结果未知"的证据：既不能说已发送，也绝不能重发。
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_UNKNOWN,
                reason=REASON_NOT_SAFELY_RESENDABLE,
                detail=SENT_BOOKKEEPING_DETAIL,
                external_message_id=int(outcome.message_id or 0),
                resendable=False,
            )
        elif outcome.status == CHECKIN_OUTCOME_ALREADY_DELIVERED:
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_SKIPPED,
                reason=REASON_ALREADY_DELIVERED,
                detail=(
                    "已有出站证据确认这个时段发送过，跳过"
                    if outcome.reason == "outbound_receipt_confirmed"
                    else "这个时段已经发过，跳过"
                ),
                external_message_id=int(outcome.message_id or 0),
                resendable=False,
            )
        elif outcome.status == CHECKIN_OUTCOME_MARK_FAILED:
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_UNKNOWN,
                reason="sent_bookkeeping_failed",
                detail=SENT_BOOKKEEPING_DETAIL,
                external_message_id=int(outcome.message_id or 0),
                resendable=False,
            )
        elif outcome.status == CHECKIN_OUTCOME_SEND_FAILED:
            if outcome.reason == "guard_blocked" and await self._cancel_pending(context):
                # v6：已取消的 run 会在写边界被守卫拒绝（谓词含 cancel_requested）；
                # 未执行项如实归为取消，而不是报成授权被撤/配置漂移。
                item = _cancelled_item(target)
            elif outcome.reason == "guard_blocked":
                # R2（返工 v3）：写边界复核没通过——没有发送，也没有新的副作用。
                item = ItemResult(
                    item_key=target.item_key,
                    target=dict(target.target),
                    status=ITEM_STATUS_FAILED,
                    reason=REASON_GUARD_BLOCKED,
                    detail=(
                        "出站前复核未通过（授权被撤、发起人已不是管理员、持久配置已变更"
                        "或本 run 已被取消）：未发送任何消息，请重新预览后确认"
                    ),
                    resendable=False,
                )
            elif outcome.outbound_unknown:
                item = ItemResult(
                    item_key=target.item_key,
                    target=dict(target.target),
                    status=ITEM_STATUS_UNKNOWN,
                    reason=REASON_EXTERNAL_UNKNOWN,
                    detail=UNKNOWN_DETAIL,
                    resendable=False,
                )
            else:
                # 出站前就失败了（名单/渲染/保护写入）：明确没有发出任何消息，
                # 既不需要保护也不需要占位，如实标成可安全重试。
                item = ItemResult(
                    item_key=target.item_key,
                    target=dict(target.target),
                    status=ITEM_STATUS_FAILED,
                    reason=str(outcome.reason or REASON_RENDER_FAILED),
                    detail="出站前失败，未发送任何消息，可安全重试",
                    resendable=True,
                )
        else:
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_UNKNOWN,
                reason=REASON_ADAPTER_CRASHED,
                detail="未知的投递结果",
                resendable=False,
            )
        status, run_reason = _run_status([item])
        return TaskRunResult(status=status, reason=run_reason, items=(item,))

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        run: OperationsRun,
        items: Sequence[OperationsRunItem],
    ) -> ReconcileResult | None:
        """只认 ``(群, slot_key)`` 上的持久出站证据；没有证据就保持未知。

        R3：占位不存在**不能**证明"没发出去"（可能发完就回填失败 / 被回收），所以
        不再把它改写成 failed + resendable。
        """

        results: list[ItemResult] = []
        for item in items:
            if item.status not in (ITEM_STATUS_UNKNOWN, ITEM_STATUS_FAILED):
                continue
            key = str((item.target or {}).get("slot_key") or "")
            if not key:
                continue
            receipt = await reminder_outbound_receipt(
                session, group_id=int(run.group_id), key=key
            )
            state = await reminder_slot_state(
                session, group_id=int(run.group_id), key=key
            )
            if receipt is not None:
                # 只有"出站证据说发过"**且**"台账也确认回填"才是完整成功；两者
                # 不一致（发送成功但回填失败）时保持未知，绝不声称已成功。
                confirmed = receipt.state == RECEIPT_STATE_SENT and state == "delivered"
                if confirmed:
                    detail = "出站证据与台账一致：这个时段已发送"
                elif receipt.state == RECEIPT_STATE_SENT:
                    detail = "出站证据显示已发送，但台账没有回填（可能回填失败），保持未知"
                else:
                    detail = "出站证据表明结果仍然未知，禁止自动重发"
                results.append(
                    ItemResult(
                        item_key=str(item.item_key),
                        target=dict(item.target or {}),
                        status=(
                            ITEM_STATUS_SUCCEEDED if confirmed else ITEM_STATUS_UNKNOWN
                        ),
                        reason="" if confirmed else REASON_NOT_SAFELY_RESENDABLE,
                        detail=detail,
                        external_message_id=int(receipt.external_message_id or 0),
                        resendable=False,
                    )
                )
                continue
            # 旧数据兜底：占位本身带 message_id/delivered_at 也算"台账证明已投递"
            # （这是既有的正确语义，保留）。其余情况保持未知，不做任何推断。
            if state == "delivered":
                results.append(
                    ItemResult(
                        item_key=str(item.item_key),
                        target=dict(item.target or {}),
                        status=ITEM_STATUS_SUCCEEDED,
                        detail=f"{key} 台账显示已投递",
                        resendable=False,
                    )
                )
        if not results:
            return None
        return ReconcileResult(status=RUN_STATUS_PARTIAL, items=tuple(results))


# ---------------------------------------------------------------------------
# 周活跃结算
# ---------------------------------------------------------------------------


def _activity_item_key(week: str, user_id: int, points: int) -> str:
    """活跃结算的 item 身份 = 周 + 用户 + **预览当时的积分方案**。

    R2（返工 v2）：确认之后名次/点数可能变（同样的人、不同的分）。把点数写进
    item_key，让"点方案变了"在持久目标快照里表现为另一个目标，而不是悄悄按新点数
    发放；旧格式的键仍然包含 ``:{week}:``，不改变任何既有解析。
    """

    return f"activity:{week}:{int(user_id)}:{int(points)}"


def _activity_rejected_items(
    bound: set[str],
    planned: Mapping[str, Any],
    week: str,
    *,
    reason: str,
    detail: str,
    cancelled: bool = False,
) -> tuple[ItemResult, ...]:
    """写边界拒绝时的逐项目回执（只报告真实目标，不编造已发放的行）。

    ``cancelled=True`` 时（v6：拒绝的真实原因是本 run 已被取消）逐项按取消回执返回，
    点数仍然是 0、也不会建立任何补发权限。
    """

    keys = sorted(bound) or [f"activity:{week}"]
    items: list[ItemResult] = []
    for key in keys:
        entry = planned.get(key)
        target: dict[str, Any] = {"week": week}
        if entry is not None:
            target = {
                "week": week,
                "user_id": int(entry.user_id),
                "points": int(entry.points),
            }
        if cancelled:
            items.append(_cancelled_result(item_key=str(key), target=target))
            continue
        items.append(
            ItemResult(
                item_key=str(key),
                target=target,
                status=ITEM_STATUS_FAILED,
                reason=reason,
                detail=detail,
                resendable=False,
            )
        )
    return tuple(items)


class ActivityAwardTask(_AdapterBase):
    task_id = "activity_award"
    title = TASK_TITLES["activity_award"]

    def normalize_params(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        raw = (params or {}).get("week_start")
        if raw is None:
            return {"week_start": ""}
        if not isinstance(raw, str) or not _WEEK_START_RE.match(raw.strip()):
            raise _invalid("week_start 必须是 YYYY-MM-DD")
        return {"week_start": raw.strip()}

    @staticmethod
    def _week_start(params: Mapping[str, Any], now: datetime) -> date:
        """只允许上一个完整周（service 的周定义回源 ``last_complete_week``）。"""

        expected, _end = last_complete_week(now)
        raw = str(params.get("week_start") or "").strip()
        if not raw:
            return expected
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise _invalid("week_start 必须是 YYYY-MM-DD") from exc
        if parsed != expected:
            raise _invalid(
                "只能结算上一个完整周（" + expected.isoformat() + "），不补发历史周"
            )
        return parsed

    async def _plan(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        settings: Any,
        group_id: int,
        week_start: date,
    ) -> Any:
        await _runtime_manager(settings, session_factory)
        async with session_factory() as session:
            result = await settle_weekly_activity(
                session, group_id=int(group_id), week_start=week_start, award=False
            )
            # award=False 不写任何东西，回滚只是让"预览不写库"这件事在代码上显式。
            await session.rollback()
        return result

    async def preview(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        group_id: int,
        params: Mapping[str, Any],
        bot: Any = None,
    ) -> TaskPreview:
        now = now_shanghai_naive()
        try:
            start = self._week_start(params, now)
        except OperationsError as exc:
            return TaskPreview(status="blocked", reason=REASON_NOT_ALLOWED_WEEK, detail=exc.message)
        async with session_factory() as session:
            if not await self._authorized(session, group_id):
                return TaskPreview(status="blocked", reason=REASON_GUARD_BLOCKED)
        result = await self._plan(
            session_factory,
            settings=settings,
            group_id=group_id,
            week_start=start,
        )
        week = activity_week_key(start)
        paid_refs = await self._awarded_refs(session_factory, group_id, result)
        targets: list[PreviewTarget] = []
        already = 0
        for entry in result.entries:
            if not entry.points and not entry.awarded:
                continue
            if award_ref(week, entry.user_id) in paid_refs:
                already += 1
                continue
            targets.append(
                PreviewTarget(
                    item_key=_activity_item_key(week, entry.user_id, entry.points),
                    target={
                        "week": week,
                        "user_id": int(entry.user_id),
                        "points": int(entry.points),
                    },
                    label=f"第 {entry.rank} 名 · {entry.points} 分",
                    detail=f"活跃 {entry.active_days} 天 · 发言 {entry.messages} 条",
                )
            )
        extra = {
            "week": week,
            "week_start": start.isoformat(),
            "participants": int(result.participants),
            "qualified": int(result.qualified),
            "total_points": int(result.total_points),
            "already_awarded": already,
        }
        cost = {
            "points": int(sum(int(item.target.get("points") or 0) for item in targets)),
            "telegram_monetary_cost": "none",
            "llm_tokens": "none",
        }
        if targets:
            return TaskPreview(
                status="ok",
                targets=tuple(targets),
                effects=("给本群达标成员写入积分奖励流水（同一周同一人只发一次）",),
                cost=cost,
                extra=extra,
                detail=f"上一个完整周 {week}，只结算这一个周",
            )
        return TaskPreview(
            status="no_work",
            reason=REASON_ALREADY_DELIVERED if already else REASON_NO_WORK,
            detail=(
                f"{week} 已经没有待发放的奖励"
                if already
                else f"{week} 没有达标成员，不需要结算"
            ),
            effects=("给本群达标成员写入积分奖励流水",),
            cost=cost,
            extra=extra,
        )

    @staticmethod
    async def _awarded_amounts(
        session_factory: async_sessionmaker[AsyncSession],
        group_id: int,
        result: Any,
    ) -> dict[str, int]:
        """按精确 ref 从**独立会话**读回台账里这一周的入账流水（ref → 分值）。

        R4b：核对提交结果时必须"ref + 点数"一起看，只数行数无法分辨"入账了但分值
        不是预览时那一版"，也无法支撑正文里的积分口径。
        """

        user_ids = [int(entry.user_id) for entry in result.entries]
        if not user_ids:
            return {}
        week = str(getattr(result, "week", "") or "")
        refs = [award_ref(week, user_id) for user_id in user_ids]
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(MemberPointAward.ref, MemberPointAward.points).where(
                        MemberPointAward.group_id == int(group_id),
                        MemberPointAward.reason == AWARD_REASON_WEEKLY_ACTIVITY,
                        MemberPointAward.ref.in_(refs),
                    )
                )
            ).all()
        return {
            str(ref): int(points or 0) for ref, points in rows if str(ref or "").strip()
        }

    @staticmethod
    async def _awarded_refs(
        session_factory: async_sessionmaker[AsyncSession],
        group_id: int,
        result: Any,
    ) -> set[str]:
        return set(
            await ActivityAwardTask._awarded_amounts(session_factory, group_id, result)
        )

    async def guard(
        self, session: AsyncSession, *, group_id: int, params: Mapping[str, Any]
    ) -> str:
        if not await self._authorized(session, group_id):
            return REASON_GUARD_BLOCKED
        try:
            self._week_start(params, now_shanghai_naive())
        except OperationsError:
            return REASON_GUARD_BLOCKED
        return REASON_OK

    async def run(self, context: Any) -> TaskRunResult:
        now = now_shanghai_naive()
        try:
            start = self._week_start(context.params, now)
        except OperationsError as exc:
            return TaskRunResult(
                status=RUN_STATUS_FAILED, reason=REASON_NOT_ALLOWED_WEEK, detail=exc.message
            )
        week = activity_week_key(start)
        # R2（返工 v2）：执行边界统一复核（群授权 + 发起人当前权限 + 持久配置
        # revision）。撤权/换配置之后绝不再产生积分副作用。
        blocked = await self._execution_guard(context)
        if blocked:
            return TaskRunResult(
                status=RUN_STATUS_FAILED,
                reason=blocked,
                detail="执行前复核未通过（授权被撤、发起人已不是管理员或配置已变更），未执行任何动作",
            )
        async with context.session_factory() as session:
            if not await self._authorized(session, context.group_id):
                return TaskRunResult(
                    status=RUN_STATUS_FAILED,
                    reason=REASON_GUARD_BLOCKED,
                    detail="本群已不在授权列表，未执行任何动作",
                )
        if await self._cancel_pending(context):
            target = PreviewTarget(
                item_key=f"activity:{week}",
                target={"week": week},
                label=f"{week} 周活跃结算",
            )
            return TaskRunResult(
                status=RUN_STATUS_CANCELLED,
                reason=REASON_CANCEL_REQUESTED,
                items=(_cancelled_item(target),),
            )
        # 先按只读口径算出"这一周谁达标"，并记下执行之前就已经入账的 ref：
        # 只有这样才分得清"本次真的发了分"和"别人已经发过了"（后者如实报
        # 跳过，绝不把幂等重复当成功）。
        plan = await self._plan(
            context.session_factory,
            settings=context.settings,
            group_id=int(context.group_id),
            week_start=start,
        )
        # R2（返工 v2）：确认时选中的**目标快照**才是允许发放的集合。快照里的
        # 目标身份带着"当时的积分方案"，所以确认之后名次/点数变了就是另一个目标：
        # 已经确认的目标消失 → 整次拒绝并重新预览；确认之后才达标的**新增**成员
        # 只报告未选中，绝不顺带发放、绝不悄悄按新点数发。
        bound = {str(key) for key in context.bound_keys() if str(key or "").strip()}
        planned = {
            _activity_item_key(week, entry.user_id, entry.points): entry
            for entry in plan.entries
            if entry.points or entry.awarded
        }
        if bound:
            vanished = sorted(key for key in bound if key not in planned)
            if vanished:
                return TaskRunResult(
                    status=RUN_STATUS_FAILED,
                    reason=REASON_SOURCE_CHANGED,
                    detail="确认的目标已变化（名次或积分方案与预览时不一致），本次没有发放任何积分，请重新预览",
                    items=tuple(
                        ItemResult(
                            item_key=key,
                            target={"week": week},
                            status=ITEM_STATUS_FAILED,
                            reason=REASON_SOURCE_CHANGED,
                            detail="确认的目标在执行前发生变化，未发放，可安全重试（需重新预览）",
                            resendable=True,
                        )
                        for key in vanished
                    ),
                )
        only_refs = (
            {award_ref(week, planned[key].user_id) for key in bound}
            if bound
            else None
        )
        # R2（返工 v3）：把**确认时的点数方案**带进真实写边界。``bound`` 的目标身份
        # 里带着确认那一刻的点数，这里按 ref 取回来交给结算服务；执行时重新排名若
        # 名次或点数与确认方案不一致（例如确认后又有同分成员出现），写边界会原子拒绝
        # 并且一个字都不写——既不按新点数补发，也不把"没按方案发"报成"没操作"。
        expected_points = (
            {
                award_ref(week, planned[key].user_id): int(planned[key].points)
                for key in bound
            }
            if bound
            else None
        )
        guard = PersistedGuard.from_context(context)
        before = await self._awarded_refs(
            context.session_factory, int(context.group_id), plan
        )
        submitting = False
        try:
            async with context.session_factory() as session:
                result = await settle_weekly_activity(
                    session,
                    group_id=int(context.group_id),
                    week_start=start,
                    award=True,
                    only_refs=only_refs,
                    guard=guard,
                    expected_points=expected_points,
                )
                if result.award_rejected:
                    await session.rollback()
                    rejected = str(result.award_rejected)
                    cancelled = rejected == "guard_blocked" and await self._cancel_pending(
                        context
                    )
                    if cancelled:
                        # v6：积分写边界的守卫谓词里包含"本 run 已被取消"，取消落库后
                        # 会以 guard_blocked 拒绝；此时未执行项如实归为取消，绝不把
                        # "已取消"报成"授权被撤/配置漂移"。
                        status = RUN_STATUS_CANCELLED
                        reason = REASON_CANCEL_REQUESTED
                        detail = "取消已生效：积分写边界之前退出，本次没有发放任何积分"
                    elif rejected == "guard_blocked":
                        status = RUN_STATUS_FAILED
                        reason = REASON_GUARD_BLOCKED
                        detail = (
                            "写边界复核未通过（授权被撤、发起人已不是管理员、持久配置"
                            "已变更或本 run 已被取消）：本次没有发放任何积分，请重新预览"
                        )
                    else:
                        status = RUN_STATUS_FAILED
                        reason = REASON_SOURCE_CHANGED
                        detail = (
                            "确认之后这一周的名次或积分方案发生了变化：本次没有发放任何"
                            "积分（也不会按新方案补发），请重新预览后确认"
                        )
                    return TaskRunResult(
                        status=status,
                        reason=reason,
                        detail=detail,
                        items=_activity_rejected_items(
                            bound,
                            planned,
                            week,
                            reason=reason,
                            detail=detail,
                            cancelled=cancelled,
                        ),
                    )
                submitting = True
                await session.commit()
            submitting = False
        except Exception as exc:
            log.warning(
                "operations activity award failed | group=%s | %s",
                context.group_id,
                type(exc).__name__,
            )
            if submitting:
                # R4b：提交调用失败**不等于**事务回滚——回执可能在真正落库之后丢
                # 失。必须用独立会话按精确 ref 复核，绝不谎称"没有写入任何奖励"，
                # 否则对账会把已经入账的积分当成没发。
                paid_after = await self._awarded_amounts(
                    context.session_factory, int(context.group_id), plan
                )
                if paid_after:
                    return TaskRunResult(
                        status=RUN_STATUS_UNKNOWN,
                        reason="commit_unconfirmed",
                        detail=(
                            f"{week} 的结算提交结果未确认（提交回执丢失）：台账已经能读到"
                            f"本周 {len(paid_after)} 条入账（合计 "
                            f"{sum(paid_after.values())} 分），按“已入账但未确认”处理，"
                            "既不重复发放也不断言未发放"
                        ),
                        items=(
                            ItemResult(
                                item_key=f"activity:{week}",
                                target={"week": week},
                                status=ITEM_STATUS_UNKNOWN,
                                reason="commit_unconfirmed",
                                detail="提交结果未知，台账已有入账证据",
                                resendable=False,
                            ),
                        ),
                    )
            # 事务确实回滚：这一周没有任何入账，业务幂等（award_ref）保证可以安全重来。
            # 逐项失败仍按**确认时的目标快照**记账：这样"重试"只会重放当时确认过的
            # 目标，不会因为执行之后又有人达标而把新目标顺带发出去。
            rolled_back = (
                tuple(
                    ItemResult(
                        item_key=key,
                        target={"week": week},
                        status=ITEM_STATUS_FAILED,
                        reason="transaction_rolled_back",
                        detail="事务回滚，没有产生副作用；重试只重放本次确认选中的目标",
                        resendable=True,
                    )
                    for key in sorted(bound)
                )
                if bound
                else (
                    ItemResult(
                        item_key=f"activity:{week}",
                        target={"week": week},
                        status=ITEM_STATUS_FAILED,
                        reason="transaction_rolled_back",
                        detail="事务回滚，没有产生副作用",
                        resendable=True,
                    ),
                )
            )
            return TaskRunResult(
                status=RUN_STATUS_FAILED,
                reason="transaction_rolled_back",
                detail="结算事务已回滚，本周没有写入任何奖励；可以安全重试",
                items=rolled_back,
            )
        paid_refs = await self._awarded_refs(
            context.session_factory, int(context.group_id), result
        )
        items: list[ItemResult] = []
        for entry in result.entries:
            if not entry.points and not entry.awarded:
                continue
            item_key = _activity_item_key(week, entry.user_id, entry.points)
            target = {"week": week, "user_id": int(entry.user_id), "points": int(entry.points)}
            if bound and item_key not in bound:
                items.append(
                    ItemResult(
                        item_key=item_key,
                        target=target,
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_NOT_SELECTED,
                        detail="本次确认没有选中这个目标（确认之后才达标的成员不会被顺带发放）；如需发放请重新预览",
                        resendable=False,
                    )
                )
            elif award_ref(week, entry.user_id) in before:
                items.append(
                    ItemResult(
                        item_key=item_key,
                        target=target,
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_ALREADY_DELIVERED,
                        detail="这一周的奖励在本次执行之前就已经入账（幂等，没有重复发放）",
                        resendable=False,
                    )
                )
            elif award_ref(week, entry.user_id) in paid_refs:
                items.append(
                    ItemResult(
                        item_key=item_key,
                        target=target,
                        status=ITEM_STATUS_SUCCEEDED,
                        detail=f"第 {entry.rank} 名 · {entry.points} 分已入账",
                        resendable=False,
                    )
                )
            else:
                items.append(
                    ItemResult(
                        item_key=item_key,
                        target=target,
                        status=ITEM_STATUS_FAILED,
                        reason="award_not_recorded",
                        detail="结算后没有读到这条奖励流水，可安全重试",
                        resendable=True,
                    )
                )
        if not items:
            skipped = ItemResult(
                item_key=f"activity:{week}",
                target={"week": week},
                status=ITEM_STATUS_SKIPPED,
                reason=REASON_NO_WORK,
                detail="本周没有达标成员",
                resendable=False,
            )
            return TaskRunResult(
                status=RUN_STATUS_NO_WORK, reason=REASON_NO_WORK, items=(skipped,)
            )
        status, reason = _run_status(items)
        return TaskRunResult(status=status, reason=reason, items=tuple(items))

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        run: OperationsRun,
        items: Sequence[OperationsRunItem],
    ) -> ReconcileResult | None:
        results: list[ItemResult] = []
        for item in items:
            if item.status not in (ITEM_STATUS_UNKNOWN, ITEM_STATUS_FAILED):
                continue
            ref = str((item.target or {}).get("ref") or "")
            target = dict(item.target or {})
            week = str(target.get("week") or "")
            user_id = int(target.get("user_id") or 0)
            if not ref and week and user_id:
                ref = award_ref(week, user_id)
            if not ref:
                continue
            found = await session.scalar(
                select(MemberPointAward.id).where(
                    MemberPointAward.group_id == int(run.group_id),
                    MemberPointAward.ref == ref,
                )
            )
            if found:
                results.append(
                    ItemResult(
                        item_key=str(item.item_key),
                        target=target,
                        status=ITEM_STATUS_SUCCEEDED,
                        detail="奖励流水已存在（台账证明已入账）",
                        resendable=False,
                    )
                )
        if not results:
            return None
        return ReconcileResult(status=RUN_STATUS_PARTIAL, items=tuple(results))


# ---------------------------------------------------------------------------
# 周报
# ---------------------------------------------------------------------------


def _weekly_scheme_digest(plan: Any) -> str:
    """把"这一周的成员 + 点数方案"压成短摘要，参与目标键与确认快照。

    R2（返工 v3）：周报正文里会顺带结算上一完整周的活跃积分，只绑 ``weekly:{week}``
    字符串挡不住"确认之后这一周又多出一个达标成员"这类漂移。把方案摘要写进
    ``item_key`` 之后，"确认的是哪一套方案"就落在持久目标快照里：执行时重新算出
    的方案若不同就是**另一个目标**，不会再被静默执行。

    没有人达标（方案为空）时返回空串，item_key 保持历史形态 ``weekly:{week}``，
    既不影响既有两个入口的台账键，也不改变没有任何积分副作用的日常路径。
    """

    members = sorted(
        f"{int(entry.user_id)}:{int(entry.points)}"
        for entry in (getattr(plan, "entries", None) or ())
        if int(getattr(entry, "points", 0) or 0) > 0
    )
    if not members:
        return ""
    return hashlib.sha256("|".join(members).encode("utf-8")).hexdigest()[:12]


def _weekly_unconfirmed_refs(
    *, week: str, entries: Any, confirmed_refs: set[str] | None
) -> list[str]:
    """结算结果里"确认方案之外"的达标 ref（返工 v5）。

    ``confirmed_refs`` 为 ``None`` 表示这次执行拿不到可用的确认方案（方案重现失败的
    legacy 路径），维持老口径、不做新增判定。返回非空列表就说明真实方案已经不是确认
    的那一套（确认之后又有成员达标），周报正文的积分口径会与确认内容对不上，调用方
    必须整体拒绝。空方案（确认时无人达标）同样适用：那时白名单为空，之后任何新达标
    的成员都属于"确认方案之外"。
    """

    if confirmed_refs is None:
        return []
    found: list[str] = []
    for entry in entries or ():
        if int(getattr(entry, "points", 0) or 0) <= 0:
            continue
        ref = award_ref(week, entry.user_id)
        if ref not in confirmed_refs:
            found.append(ref)
    return sorted(found)


class WeeklyReportTask(_AdapterBase):
    task_id = "weekly_report"
    title = TASK_TITLES["weekly_report"]

    def normalize_params(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        days = _optional_int(params, "days", minimum=1, maximum=MAX_REPORT_DAYS)
        return {"days": 7 if days is None else days}

    @staticmethod
    def _week_identity(now: datetime) -> tuple[str, str, str]:
        """手动周报的业务周：**上一个完整自然周**（不是"今天所在的这一周"）。

        R4：预览令牌、占位键、积分结算与报告正文必须锚在同一个真实统计边界上，
        否则同一周可以换一个新键再发一次。
        """

        start, end = last_complete_week(now)
        return activity_week_key(start), start.isoformat(), end.isoformat()

    async def preview(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        group_id: int,
        params: Mapping[str, Any],
        bot: Any = None,
    ) -> TaskPreview:
        now = now_shanghai_naive()
        week, week_start, week_end = self._week_identity(now)
        days = int(params.get("days") or 7)
        async with session_factory() as session:
            if not await self._authorized(session, group_id):
                return TaskPreview(status="blocked", reason=REASON_GUARD_BLOCKED)
            receipt = await report_outbound_receipt(
                session, target_id=int(group_id), week_key=week
            )
            state = await report_slot_state(
                session, target_id=int(group_id), week_key=week
            )
            # 积分范围用只算不发的 dry-run 得到，和真正执行时口径一致。
            try:
                plan = await settle_weekly_activity(
                    session,
                    group_id=int(group_id),
                    now=now,
                    week_start=date.fromisoformat(week_start),
                    award=False,
                )
            except Exception:
                log.exception(
                    "operations weekly report plan failed | group=%s", group_id
                )
                plan = None
        extra = {
            "week_key": week,
            "week_start": week_start,
            "week_end": week_end,
            "days": days,
            "quality_window": f"{week_start}~{week_end}（上一完整自然周，精确边界）",
            "award_week": week,
            "award_scope": "上一完整自然周的活跃积分",
            "award_points": 0 if plan is None else int(plan.total_points),
            "award_qualified": 0 if plan is None else int(plan.qualified),
            "award_participants": 0 if plan is None else int(plan.participants),
            "award_pending_points": (
                0
                if plan is None
                else int(
                    sum(
                        entry.points
                        for entry in plan.entries
                        if entry.points > 0 and not entry.awarded
                    )
                )
            ),
            "award_idempotent": True,
            # R2（返工 v3）：把这次披露的**成员与点数方案**明确写出来，并把方案摘要
            # 放进目标键；确认的是这一套方案，执行时方案变了就是另一个目标。
            "award_scheme": _weekly_scheme_digest(plan),
            "award_members": (
                []
                if plan is None
                else [
                    {"user_id": int(entry.user_id), "points": int(entry.points)}
                    for entry in plan.entries
                    if int(entry.points) > 0
                ]
            ),
        }
        effects = (
            f"向本群发送上一完整周（{week_start}~{week_end}）周报：正文由既有渲染链从"
            "真实归档统计生成，只发本群；同时结算该周活跃积分（幂等，"
            f"本周待发 {extra['award_pending_points']} 分，同一周不会重复发放）",
            "消息质量与活跃段落都按上一完整自然周的精确起止统计（不是滚动窗口）",
        )
        cost = {
            "telegram_messages": 1,
            "telegram_monetary_cost": "none",
            "llm_tokens": "none",
            "note": "周报正文是统计口径；模型用量与金额未知，不编造估算",
        }
        if receipt is not None:
            confirmed = receipt.state == RECEIPT_STATE_SENT
            return TaskPreview(
                status="no_work" if confirmed else "blocked",
                reason=(
                    REASON_ALREADY_DELIVERED if confirmed else REASON_IN_FLIGHT
                ),
                detail=(
                    f"{week} 这一周的周报已有出站证据（已发送）"
                    if confirmed
                    else f"{week} 这一周的周报出站结果未知，禁止自动重发"
                ),
                effects=effects,
                cost=cost,
                extra=extra,
            )
        if state == REPORT_SLOT_DELIVERED:
            return TaskPreview(
                status="no_work",
                reason=REASON_ALREADY_DELIVERED,
                detail=f"{week} 这一周的周报已经发过了",
                effects=effects,
                cost=cost,
                extra=extra,
            )
        if state == REPORT_SLOT_PLACEHOLDER:
            return TaskPreview(
                status="blocked",
                reason=REASON_IN_FLIGHT,
                detail=(
                    f"{week} 已有占位但没有投递记录：可能正在发送（结果未知），"
                    "不重复发送"
                ),
                effects=effects,
                cost=cost,
                extra=extra,
            )
        return TaskPreview(
            status="ok",
            targets=(
                PreviewTarget(
                    item_key=(
                        f"weekly:{week}"
                        if not extra["award_scheme"]
                        else f"weekly:{week}:{extra['award_scheme']}"
                    ),
                    target={
                        "week_key": week,
                        "week_start": week_start,
                        "week_end": week_end,
                        "days": days,
                        "award_scheme": extra["award_scheme"],
                    },
                    label="本群周报（上一完整周）",
                    detail=(
                        f"台账键 {week}（{week_start}~{week_end}），一个周只会发一次；"
                        f"积分按同一周结算，待发 {extra['award_pending_points']} 分，"
                        f"成员与点数方案 {extra['award_scheme'] or '（本周无人达标）'}"
                    ),
                ),
            ),
            effects=effects,
            cost=cost,
            extra=extra,
        )

    async def guard(
        self, session: AsyncSession, *, group_id: int, params: Mapping[str, Any]
    ) -> str:
        if not await self._authorized(session, group_id):
            return REASON_GUARD_BLOCKED
        week, _, _ = self._week_identity(now_shanghai_naive())
        receipt = await report_outbound_receipt(
            session, target_id=int(group_id), week_key=week
        )
        if receipt is not None:
            return (
                REASON_ALREADY_DELIVERED
                if receipt.state == RECEIPT_STATE_SENT
                else REASON_IN_FLIGHT
            )
        state = await report_slot_state(
            session, target_id=int(group_id), week_key=week
        )
        if state == REPORT_SLOT_DELIVERED:
            return REASON_ALREADY_DELIVERED
        if state == REPORT_SLOT_PLACEHOLDER:
            return REASON_IN_FLIGHT
        return REASON_OK

    @staticmethod
    async def _weekly_plan(context: Any, *, now: datetime, week_start: str) -> Any:
        """只算不发地重现这一周的积分方案（与预览同一口径，独立会话且不写入）。

        R2（返工 v3）：确认时披露的成员/点数方案是周报副作用的一部分，执行时必须
        拿同一套方案去写；这里重现方案用于比对（写边界还会再核一次）。
        """

        try:
            async with context.session_factory() as session:
                plan = await settle_weekly_activity(
                    session,
                    group_id=int(context.group_id),
                    now=now,
                    week_start=date.fromisoformat(week_start),
                    award=False,
                )
                await session.rollback()
            return plan
        except Exception:
            log.warning(
                "operations weekly report plan reprobe failed | group=%s",
                context.group_id,
            )
            return None

    async def run(self, context: Any) -> TaskRunResult:
        now = now_shanghai_naive()
        week, week_start, week_end = self._week_identity(now)
        days = int(context.params.get("days") or 7)
        # R2（返工 v3）：周报正文会顺带结算这一周的活跃积分，所以"确认的是哪一套
        # 成员与点数方案"必须和"这一次真的写哪一套"对得上。先重现方案，把摘要编进
        # item_key：确认快照里存的就是这一套方案，执行时方案变了就是另一个目标。
        plan = await self._weekly_plan(context, now=now, week_start=week_start)
        expected_points = {
            award_ref(week, entry.user_id): int(entry.points)
            for entry in (getattr(plan, "entries", None) or ())
            if int(entry.points or 0) > 0
        }
        scheme = _weekly_scheme_digest(plan)
        # R2（返工 v5）：确认方案不只是"拿来做比对"的，它还定义**允许发放的 ref 集合**。
        # 只带 expected_points 时逐项比对只覆盖确认过的成员，确认之后**新达标**的人不在
        # 比对范围内，会被顺带发分，正文里"一共发出 N 分"也会写成含新人的金额（父 v14
        # 第四例）。这里把同一套确认方案同时用作真实写边界上的白名单（only_refs），并在
        # 结算结果里检查有没有确认方案之外的新达标成员：有就整体拒绝、rollback 成"一个
        # 字都没写"，既不按新成员补发、也不发一份金额对不上的周报。``plan`` 重现失败
        # （None）时拿不到确认方案，保持 legacy 口径不做新增判定。
        confirmed_refs = set(expected_points) if plan is not None else None
        target = PreviewTarget(
            item_key=f"weekly:{week}" if not scheme else f"weekly:{week}:{scheme}",
            target={
                "week_key": week,
                "week_start": week_start,
                "week_end": week_end,
                "days": days,
                "award_scheme": scheme,
            },
            label="本群周报（上一完整周）",
        )
        if not context.is_bound(target.item_key):
            if any(
                str(key).startswith(f"weekly:{week}") for key in context.bound_keys()
            ):
                drifted = ItemResult(
                    item_key=target.item_key,
                    target=dict(target.target),
                    status=ITEM_STATUS_FAILED,
                    reason=REASON_SOURCE_CHANGED,
                    detail=(
                        "确认之后这一周的活跃名次或积分方案发生了变化（与确认时披露的"
                        "成员/点数不一致）：本次没有发放任何积分、也没有发送周报，"
                        "请重新预览后确认"
                    ),
                    resendable=False,
                )
                return TaskRunResult(
                    status=RUN_STATUS_FAILED,
                    reason=REASON_SOURCE_CHANGED,
                    items=(drifted,),
                )
            skipped = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_SKIPPED,
                reason=REASON_NOT_SELECTED,
                detail="不在本次确认选中的目标快照内，未处理",
                resendable=False,
            )
            return TaskRunResult(
                status=RUN_STATUS_NO_WORK, reason=REASON_NOT_SELECTED, items=(skipped,)
            )
        previous = context.previous(target.item_key)
        if previous is not None and previous.status in (
            ITEM_STATUS_SUCCEEDED,
            ITEM_STATUS_UNKNOWN,
        ):
            repeated = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=previous.status,
                reason=previous.reason or REASON_ALREADY_SUCCEEDED,
                detail="上一次执行已确认发送或结果未知，本轮不重复执行",
                external_message_id=int(previous.external_message_id or 0),
                resendable=False,
            )
            return TaskRunResult(
                status=_run_status([repeated])[0],
                reason=repeated.reason,
                items=(repeated,),
            )
        blocked = await self._execution_guard(context)
        if blocked:
            return TaskRunResult(
                status=RUN_STATUS_FAILED,
                reason=blocked,
                detail="执行前复核未通过（授权被撤、发起人已不是管理员或配置已变更），未发送任何消息",
            )
        if await self._cancel_pending(context):
            return TaskRunResult(
                status=RUN_STATUS_CANCELLED,
                reason=REASON_CANCEL_REQUESTED,
                items=(_cancelled_item(target),),
            )
        awarded_points = 0
        settled = None
        submitting = False
        try:
            async with context.session_factory() as session:
                # R4：先结算上一完整周的活跃积分（写入同事务、**渲染成功后才提交**），
                # 这样正文里的"积分已入账"是真的，渲染失败时又不会留下积分副作用。
                result = await settle_weekly_activity(
                    session,
                    group_id=int(context.group_id),
                    now=now,
                    week_start=date.fromisoformat(week_start),
                    award=True,
                    only_refs=confirmed_refs,
                    guard=PersistedGuard.from_context(context),
                    expected_points=expected_points,
                )
                if result.award_rejected:
                    # R2（返工 v3）：写边界自己复核不通过或方案漂移——一个字都没写，
                    # 也不发周报；如实按失败回执，不留"已入账"的假象。
                    await session.rollback()
                    rejected = str(result.award_rejected)
                    cancelled = rejected == "guard_blocked" and await self._cancel_pending(
                        context
                    )
                    if cancelled:
                        # v6：写边界的守卫谓词里包含"本 run 已被取消"；未发放、未发送的
                        # 真实原因就是取消，如实归类而不是报成授权被撤/配置漂移。
                        status = RUN_STATUS_CANCELLED
                        reason = REASON_CANCEL_REQUESTED
                        detail = "取消已生效：积分写边界之前退出，本次没有发放任何积分、也没有发送周报"
                    elif rejected == "guard_blocked":
                        status = RUN_STATUS_FAILED
                        reason = REASON_GUARD_BLOCKED
                        detail = (
                            "写边界复核未通过（授权被撤、发起人已不是管理员、持久配置"
                            "已变更或本 run 已被取消）：本次没有发放任何积分、也没有发送"
                            "周报，请重新预览"
                        )
                    else:
                        status = RUN_STATUS_FAILED
                        reason = REASON_SOURCE_CHANGED
                        detail = (
                            "确认之后这一周的活跃名次或积分方案发生了变化（与确认时披露"
                            "的成员/点数不一致）：本次没有发放任何积分、也没有发送周报"
                            "（不会按新方案补发），请重新预览后确认"
                        )
                    if cancelled:
                        item = _cancelled_result(
                            item_key=target.item_key, target=target.target
                        )
                    else:
                        item = ItemResult(
                            item_key=target.item_key,
                            target=dict(target.target),
                            status=ITEM_STATUS_FAILED,
                            reason=reason,
                            detail=detail,
                            resendable=False,
                        )
                    return TaskRunResult(
                        status=status,
                        reason=reason,
                        items=(item,),
                    )
                newcomers = _weekly_unconfirmed_refs(
                    week=week,
                    entries=getattr(result, "entries", None),
                    confirmed_refs=confirmed_refs,
                )
                if newcomers:
                    # R2（返工 v5）：确认过的目标没有漂移，但真实方案已经不是确认的那一套
                    # （确认之后又有成员达标）。虽然写边界只放行了确认过的 ref，这里仍然
                    # 整体拒绝：正文的"一共发出 N 分"与按项结果都必须与真实已发金额一致，
                    # 不能拿旧方案的名义发一份口径对不上的周报。一个字都没写，可以安全重来。
                    await session.rollback()
                    detail = (
                        "确认之后这一周又有新的成员达标（与确认时披露的成员不一致）："
                        "本次没有发放任何积分、也没有发送周报（不会按新成员补发），"
                        "请重新预览后确认"
                    )
                    return TaskRunResult(
                        status=RUN_STATUS_FAILED,
                        reason=REASON_SOURCE_CHANGED,
                        items=(
                            ItemResult(
                                item_key=target.item_key,
                                target=dict(target.target),
                                status=ITEM_STATUS_FAILED,
                                reason=REASON_SOURCE_CHANGED,
                                detail=detail,
                                resendable=False,
                            ),
                        ),
                    )
                settled = result
                awarded_points = int(result.awarded_points or 0)
                lines = render_activity_lines(result)
                text = await render_group_quality(
                    session,
                    group_id=int(context.group_id),
                    days=days,
                    activity_lines=lines,
                    # R4：主统计用**上一完整业务周**的精确边界，不再拿滚动窗口冒充。
                    week_start=date.fromisoformat(week_start),
                    week_end=date.fromisoformat(week_end),
                )
                if context.cancel_requested():
                    # 还没提交：取消时不留积分副作用、也没有出站。
                    await session.rollback()
                    return TaskRunResult(
                        status=RUN_STATUS_CANCELLED,
                        reason=REASON_CANCEL_REQUESTED,
                        items=(_cancelled_item(target),),
                    )
                submitting = True
                await session.commit()
            submitting = False
        except Exception as exc:
            log.warning(
                "operations weekly report render failed | group=%s | %s",
                context.group_id,
                type(exc).__name__,
            )
            if submitting:
                # R4b：提交回执丢失。积分可能已经真实入账——不能再说"没有发放积分"，
                # 也不能发送周报（正文里的积分口径无法确认），按未知收口。
                paid_after = (
                    await ActivityAwardTask._awarded_amounts(
                        context.session_factory, int(context.group_id), settled
                    )
                    if settled is not None
                    else {}
                )
                if paid_after:
                    return TaskRunResult(
                        status=RUN_STATUS_UNKNOWN,
                        reason="commit_unconfirmed",
                        items=(
                            ItemResult(
                                item_key=target.item_key,
                                target=dict(target.target),
                                status=ITEM_STATUS_UNKNOWN,
                                reason="commit_unconfirmed",
                                detail=(
                                    "结算提交结果未确认（提交回执丢失）：台账已经能读到"
                                    "本周积分入账，因此既不断言未发放、也不发送周报，"
                                    "禁止自动重发"
                                ),
                                resendable=False,
                            ),
                        ),
                    )
            return TaskRunResult(
                status=RUN_STATUS_FAILED,
                reason=REASON_RENDER_FAILED,
                items=(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_FAILED,
                        reason=REASON_RENDER_FAILED,
                        detail="正文生成失败，未发送、未占位、也没有发放积分，可安全重试",
                        resendable=True,
                    ),
                ),
            )
        if awarded_points > 0:
            award_note = f"上周活跃积分已入账 {awarded_points} 分"
        elif int(getattr(settled, "total_points", 0) or 0) > 0:
            award_note = "上周活跃积分此前已入账，本次幂等跳过"
        else:
            award_note = "上周没有达到奖励标准的成员，本次没有发放积分"
        outcome = await deliver_group_report(
            context.session_factory,
            context.bot,
            target_id=int(context.group_id),
            week_key=week,
            text=text,
            guard=PersistedGuard.from_context(context),
        )
        if outcome.status == REPORT_OUTCOME_DELIVERED:
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_SUCCEEDED,
                detail=f"{week} 周报已发送（{award_note}）",
                external_message_id=int(outcome.message_id or 0),
                resendable=False,
            )
        elif outcome.status in (
            REPORT_OUTCOME_ALREADY_DELIVERED,
        ):
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_SKIPPED,
                reason=REASON_ALREADY_DELIVERED,
                detail=f"这一周的周报已经有出站证据，跳过（{award_note}）",
                external_message_id=int(outcome.message_id or 0),
                resendable=False,
            )
        elif outcome.status == REPORT_OUTCOME_IN_FLIGHT:
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_UNKNOWN,
                reason=REASON_IN_FLIGHT,
                detail=(
                    "这一周的周报出站结果未知：可能正在发送或已经发出，禁止自动重发"
                    f"（{award_note}）"
                ),
                resendable=False,
            )
        elif outcome.status == REPORT_OUTCOME_RENDER_FAILED:
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_FAILED,
                reason=REASON_RENDER_FAILED,
                detail=f"正文生成失败，未发送（{award_note}）",
                resendable=True,
            )
        elif outcome.status == REPORT_OUTCOME_MARK_FAILED:
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_UNKNOWN,
                reason="sent_bookkeeping_failed",
                detail=f"{SENT_BOOKKEEPING_DETAIL}（{award_note}）",
                external_message_id=int(outcome.message_id or 0),
                resendable=False,
            )
        elif outcome.status == REPORT_OUTCOME_SEND_FAILED:
            if outcome.reason == "guard_blocked" and await self._cancel_pending(context):
                # v6：出站守卫谓词含"本 run 已被取消"，取消落库后未发送的周报必须
                # 如实归为取消，而不是报成授权被撤/配置漂移。
                # v7：积分在发送前就已经提交，取消只取消了"发送"这一半，回执必须
                # 保留真实 award_note，不能拿通用"本项未执行"抹掉已入账的副作用。
                item = _cancelled_report_result(
                    item_key=target.item_key,
                    target=target.target,
                    award_note=award_note,
                )
            elif outcome.reason == "guard_blocked":
                # R2（返工 v3）：写边界复核没通过——没有发送，也没有新的副作用。
                item = ItemResult(
                    item_key=target.item_key,
                    target=dict(target.target),
                    status=ITEM_STATUS_FAILED,
                    reason=REASON_GUARD_BLOCKED,
                    detail=(
                        "出站前复核未通过（授权被撤、发起人已不是管理员、持久配置已变更"
                        f"或本 run 已被取消）：未发送任何消息，请重新预览后确认（{award_note}）"
                    ),
                    resendable=False,
                )
            elif outcome.reason in ("send_rejected", "dispatch_claim_failed"):
                # R1 返工 v2：Telegram 明确拒收或出站保护写不进去 —— 两种情况都
                # **证明没有出站**（保护写不进去时按协议直接不发），可以安全重试。
                item = ItemResult(
                    item_key=target.item_key,
                    target=dict(target.target),
                    status=ITEM_STATUS_FAILED,
                    reason=outcome.reason,
                    detail=f"出站前失败，未发送任何消息，可安全重试（{award_note}）",
                    resendable=True,
                )
            elif outcome.reason == "send_timeout_unknown":
                item = ItemResult(
                    item_key=target.item_key,
                    target=dict(target.target),
                    status=ITEM_STATUS_UNKNOWN,
                    reason=REASON_NOT_SAFELY_RESENDABLE,
                    detail=f"{UNKNOWN_DETAIL}（{award_note}）",
                    resendable=False,
                )
            else:
                item = ItemResult(
                    item_key=target.item_key,
                    target=dict(target.target),
                    status=ITEM_STATUS_UNKNOWN,
                    reason=REASON_EXTERNAL_UNKNOWN,
                    detail=(
                        "投递结果未知，未确认是否发出；"
                        f"积分已经入账，不会重复发放（{award_note}）"
                    ),
                    resendable=False,
                )
        else:
            item = ItemResult(
                item_key=target.item_key,
                target=dict(target.target),
                status=ITEM_STATUS_UNKNOWN,
                reason=REASON_ADAPTER_CRASHED,
                detail=f"未知的投递结果（{award_note}）",
                resendable=False,
            )
        status, run_reason = _run_status([item])
        return TaskRunResult(status=status, reason=run_reason, items=(item,))

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        run: OperationsRun,
        items: Sequence[OperationsRunItem],
    ) -> ReconcileResult | None:
        """只认 ``(target, week_key)`` 上的持久出站证据；没有证据就保持未知。

        R3：占位不存在**不能**证明"没发出去"（周报占位可能因为发送成功但回填失败
        而被清掉），所以不再把它改写成 failed + resendable。
        """

        results: list[ItemResult] = []
        for item in items:
            if item.status not in (ITEM_STATUS_UNKNOWN, ITEM_STATUS_FAILED):
                continue
            key = str((item.target or {}).get("week_key") or "")
            if not key:
                continue
            receipt = await report_outbound_receipt(
                session, target_id=int(run.group_id), week_key=key
            )
            if receipt is not None:
                confirmed = receipt.state == RECEIPT_STATE_SENT
                results.append(
                    ItemResult(
                        item_key=str(item.item_key),
                        target=dict(item.target or {}),
                        status=(
                            ITEM_STATUS_SUCCEEDED if confirmed else ITEM_STATUS_UNKNOWN
                        ),
                        reason="" if confirmed else REASON_NOT_SAFELY_RESENDABLE,
                        detail=(
                            "出站证据确认这一周的周报已发送"
                            if confirmed
                            else "出站证据表明结果仍然未知，禁止自动重发"
                        ),
                        external_message_id=int(receipt.external_message_id or 0),
                        resendable=False,
                    )
                )
                continue
            state = await report_slot_state(
                session, target_id=int(run.group_id), week_key=key
            )
            if state == REPORT_SLOT_DELIVERED:
                results.append(
                    ItemResult(
                        item_key=str(item.item_key),
                        target=dict(item.target or {}),
                        status=ITEM_STATUS_SUCCEEDED,
                        detail="周报台账显示已投递",
                        resendable=False,
                    )
                )
        if not results:
            return None
        return ReconcileResult(status=RUN_STATUS_PARTIAL, items=tuple(results))


# ---------------------------------------------------------------------------
# 商店到期处理
# ---------------------------------------------------------------------------


def _entitlement_stamp(expires_at: Any) -> str:
    return expires_at.strftime("%Y-%m-%d %H:%M:%S") if isinstance(
        expires_at, datetime
    ) else ""


def _parse_entitlement_stamp(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _entitlement_key(
    group_id: int, user_id: int, kind: str, expires_at: Any = None
) -> str:
    """权益项稳定业务键（R5：必须含**权益版本**，即到期时刻）。

    同一 (群, 用户, 类型) 续期后是新的一版：只有把版本写进键里，"预览选中的
    那一版"和"执行时实际处理的那一版"才可能对得上，也才不会把新出现的目标
    算进旧 run 的重试范围。
    """

    stamp = _entitlement_stamp(expires_at)
    base = f"entitlement:{int(group_id)}:{int(user_id)}:{str(kind)}"
    return f"{base}@{stamp}" if stamp else base


class ShopExpireTask(_AdapterBase):
    task_id = "shop_expire"
    title = TASK_TITLES["shop_expire"]

    def normalize_params(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        limit = _optional_int(params, "limit", minimum=1, maximum=MAX_SHOP_LIMIT)
        return {"limit": limit}

    async def _due(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        group_id: int,
        limit: int | None,
    ) -> list[Any]:
        async with session_factory() as session:
            return list(
                await due_entitlements(session, group_id=int(group_id), limit=limit)
            )

    async def preview(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        group_id: int,
        params: Mapping[str, Any],
        bot: Any = None,
    ) -> TaskPreview:
        async with session_factory() as session:
            if not await self._authorized(session, group_id):
                return TaskPreview(status="blocked", reason=REASON_GUARD_BLOCKED)
        rows = await self._due(
            session_factory, group_id=group_id, limit=params.get("limit")
        )
        # 到期提醒的持久出站证据：这一版权益已经通知过（sent）或结果未知
        # （unknown）时，本次一律不重复通知，预览里必须如实标出来。
        evidence: dict[str, str] = {}
        async with session_factory() as session:
            for row in rows:
                receipt = await expiry_notice_receipt(
                    session,
                    group_id=int(group_id),
                    user_id=int(row.user_id),
                    kind=str(row.kind),
                    expires_at=row.expires_at,
                )
                if receipt is not None:
                    evidence[
                        _entitlement_key(group_id, row.user_id, row.kind, row.expires_at)
                    ] = str(receipt.state)

        def _detail(row: Any) -> str:
            base = f"到期于 {row.expires_at:%Y-%m-%d %H:%M}"
            state = evidence.get(
                _entitlement_key(group_id, row.user_id, row.kind, row.expires_at)
            )
            if state == RECEIPT_STATE_SENT:
                return base + "；已有出站证据（已发送），本次不会重复通知"
            if state == RECEIPT_STATE_UNKNOWN:
                return base + "；上次到期提醒结果未知，本次不会自动重发通知"
            if state == RECEIPT_STATE_DISPATCHING:
                return base + "；已有出站保护（可能正在发送或结果未知），本次不会自动重发通知"
            return base

        targets = tuple(
            PreviewTarget(
                item_key=_entitlement_key(
                    group_id, row.user_id, row.kind, row.expires_at
                ),
                target={
                    "group_id": int(group_id),
                    "user_id": int(row.user_id),
                    "kind": str(row.kind),
                    "expires_at": _entitlement_stamp(row.expires_at),
                },
                label=f"{row.kind} · 用户 {int(row.user_id)}",
                detail=_detail(row),
            )
            for row in rows
        )
        effects = (
            "撤销到期的头衔/置顶（撤下动作本身幂等），并按「群 + 用户 + 类型 + 到期时刻」"
            "上的持久出站证据决定是否发送到期提醒：证据为「已发送」或「未知」时一律不重发；"
            "「撤下 + 通知 + 删行」整体不是 exactly-once，失败或未知时保留台账并如实报未知",
        )
        cost = {
            "telegram_messages": len(targets),
            "telegram_monetary_cost": "none",
            "points": "none",
            "other_cost": "unknown",
        }
        extra = {"notice_evidence": evidence, "bound_targets": len(targets)}
        if targets:
            return TaskPreview(
                status="ok",
                targets=targets,
                effects=effects,
                cost=cost,
                extra=extra,
                detail=f"本群当前有 {len(targets)} 项到期，只处理本群、只处理当前到期项",
            )
        return TaskPreview(
            status="no_work",
            reason=REASON_NO_WORK,
            detail="本群当前没有到期的权益项",
            effects=effects,
            cost=cost,
            extra=extra,
        )

    async def guard(
        self, session: AsyncSession, *, group_id: int, params: Mapping[str, Any]
    ) -> str:
        if not await self._authorized(session, group_id):
            return REASON_GUARD_BLOCKED
        return REASON_OK

    async def run(self, context: Any) -> TaskRunResult:
        rows = await self._due(
            context.session_factory,
            group_id=int(context.group_id),
            limit=context.params.get("limit"),
        )
        moment = now_shanghai_naive()
        items: list[ItemResult] = []
        for row in rows:
            target = PreviewTarget(
                item_key=_entitlement_key(
                    context.group_id, row.user_id, row.kind, row.expires_at
                ),
                target={
                    "group_id": int(context.group_id),
                    "user_id": int(row.user_id),
                    "kind": str(row.kind),
                    "expires_at": _entitlement_stamp(row.expires_at),
                },
                label=f"{row.kind} · 用户 {int(row.user_id)}",
            )
            # R1/R5：只处理本次确认里绑定过的**准确权益版本**，新出现的到期行
            # 不属于本次确认，不能被 partial retry 顺带处理。
            if not context.is_bound(target.item_key):
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_NOT_SELECTED,
                        detail="不在本次确认选中的目标里，未处理",
                        resendable=False,
                    )
                )
                continue
            previous = context.previous(target.item_key)
            if previous is not None and previous.status in (
                ITEM_STATUS_SUCCEEDED,
                ITEM_STATUS_UNKNOWN,
            ):
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=previous.status,
                        reason=previous.reason,
                        detail="上一次已经在同一条权益版本上有结论，本轮不重复处理",
                        external_message_id=int(previous.external_message_id or 0),
                        resendable=False,
                    )
                )
                continue
            if await self._cancel_pending(context):
                items.append(_cancelled_item(target))
                continue
            # R2：真实业务 write 边界重新复核群授权、发起人权限与配置 revision。
            guard = await self._execution_guard(context)
            if guard:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_FAILED,
                        reason=guard,
                        detail=(
                            "执行前复核未通过（授权被撤、发起人已不是管理员或配置已变更），"
                            "未做任何撤下与通知"
                        ),
                        resendable=False,
                    )
                )
                break
            try:
                async with context.session_factory() as session:
                    outcome = await expire_entitlement_target(
                        session,
                        bot=context.bot,
                        group_id=int(context.group_id),
                        user_id=int(row.user_id),
                        kind=str(row.kind),
                        expires_at=row.expires_at,
                        notify=True,
                        now=moment,
                        session_factory=context.session_factory,
                        guard=PersistedGuard.from_context(context),
                    )
            except Exception as exc:
                # 撤下动作与通知都可能已经发出：不能断言"没副作用"。
                log.warning(
                    "operations shop expire failed | group=%s | %s",
                    context.group_id,
                    type(exc).__name__,
                )
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_EXTERNAL_UNKNOWN,
                        detail=UNKNOWN_DETAIL,
                        resendable=False,
                    )
                )
                continue
            if outcome is None:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SKIPPED,
                        reason=REASON_CHANGED_DURING_SCAN,
                        detail="这一版权益在扫描期间被续期或改动，本次没有处理",
                        resendable=False,
                    )
                )
                continue
            if outcome.guard_blocked:
                # R2（返工 v3）：写边界复核不通过——没有撤销权益、没有删/推行、
                # 没有发出到期提醒，也**没有**占用出站保护。
                if await self._cancel_pending(context):
                    # v6：守卫谓词含"本 run 已被取消"；取消落库后未处理项如实归为取消。
                    items.append(_cancelled_item(target))
                    continue
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_FAILED,
                        reason=REASON_GUARD_BLOCKED,
                        detail=outcome.detail,
                        resendable=False,
                    )
                )
                continue
            if outcome.ok and outcome.notice_unknown:
                # 撤下成功、但到期提醒结果未知（超时/断连）：不能按"可以安全重试"
                # 处理，也不能声称通知已发或未发。
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_NOT_SAFELY_RESENDABLE,
                        detail=f"{outcome.action} · {outcome.detail}",
                        resendable=False,
                    )
                )
                continue
            if outcome.ok:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_SUCCEEDED,
                        detail=(
                            f"{outcome.action} · 通知"
                            + ("已发送" if outcome.notified else "未发送")
                        ),
                        resendable=False,
                    )
                )
            else:
                items.append(
                    ItemResult(
                        item_key=target.item_key,
                        target=dict(target.target),
                        status=ITEM_STATUS_FAILED,
                        reason=REASON_DEFERRED_RETRY,
                        detail=(
                            f"{outcome.detail}（行保留，可安全重试；若已有到期提醒"
                            "出站证据，重试不会再发一次通知）"
                        ),
                        resendable=True,
                    )
                )
        status, reason = _run_status(items)
        return TaskRunResult(status=status, reason=reason, items=tuple(items))

    def _target_of(self, item: OperationsRunItem) -> tuple[dict[str, Any], int, str, Any]:
        target = dict(item.target or {})
        user_id = int(target.get("user_id") or 0)
        kind = str(target.get("kind") or "")
        expires_at = _parse_entitlement_stamp(target.get("expires_at"))
        return target, user_id, kind, expires_at

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        run: OperationsRun,
        items: Sequence[OperationsRunItem],
    ) -> ReconcileResult | None:
        """只认这一版权益的持久出站证据（R3/R5）。

        "权益行还在" / "权益行没了" 都不是可靠证据：撤下成功但通知失败、通知发出
        但落库失败、用户续期后行被改写，都会让行状态与真实副作用不一致。缺证据
        一律保持未知，绝不解锁重发通知。
        """

        results: list[ItemResult] = []
        for item in items:
            if item.status not in (ITEM_STATUS_UNKNOWN, ITEM_STATUS_FAILED):
                continue
            target, user_id, kind, expires_at = self._target_of(item)
            if user_id <= 0 or not kind or expires_at is None:
                continue
            receipt = await expiry_notice_receipt(
                session,
                group_id=int(run.group_id),
                user_id=user_id,
                kind=kind,
                expires_at=expires_at,
            )
            if receipt is None:
                results.append(
                    ItemResult(
                        item_key=str(item.item_key),
                        target=target,
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_NOT_SAFELY_RESENDABLE,
                        detail=(
                            "缺少这一版权益的持久出站证据，无法确认撤下与通知的真实"
                            "结果，保持未知"
                        ),
                        resendable=False,
                    )
                )
                continue
            row_id = await session.scalar(
                select(MemberEntitlement.id).where(
                    MemberEntitlement.group_id == int(run.group_id),
                    MemberEntitlement.user_id == user_id,
                    MemberEntitlement.kind == kind,
                    MemberEntitlement.expires_at == expires_at,
                )
            )
            if receipt.state == RECEIPT_STATE_SENT and row_id is None:
                results.append(
                    ItemResult(
                        item_key=str(item.item_key),
                        target=target,
                        status=ITEM_STATUS_SUCCEEDED,
                        detail="出站证据确认到期提醒已发送，且这一版权益行已不存在",
                        external_message_id=int(receipt.external_message_id or 0),
                        resendable=False,
                    )
                )
            elif receipt.state == RECEIPT_STATE_SENT:
                results.append(
                    ItemResult(
                        item_key=str(item.item_key),
                        target=target,
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_NOT_SAFELY_RESENDABLE,
                        detail=(
                            "到期提醒已发送（不会重发），但权益行仍在，撤下动作是否"
                            "完成无法确认"
                        ),
                        external_message_id=int(receipt.external_message_id or 0),
                        resendable=False,
                    )
                )
            else:
                results.append(
                    ItemResult(
                        item_key=str(item.item_key),
                        target=target,
                        status=ITEM_STATUS_UNKNOWN,
                        reason=REASON_NOT_SAFELY_RESENDABLE,
                        detail="出站证据表明到期提醒结果未知，禁止自动重发",
                        resendable=False,
                    )
                )
        if not results:
            return None
        return ReconcileResult(status=RUN_STATUS_PARTIAL, items=tuple(results))


_ADAPTERS: tuple[_AdapterBase, ...] = (
    ScheduledMessagesTask(),
    GroupSummaryTask(),
    CheckinReminderTask(),
    ActivityAwardTask(),
    WeeklyReportTask(),
    ShopExpireTask(),
)


def register_all_tasks() -> None:
    """把六类适配器注册进写侧内核（幂等，``ensure_tasks_loaded`` 调用）。"""

    for adapter in _ADAPTERS:
        register_task(adapter)


__all__ = [
    "ActivityAwardTask",
    "CheckinReminderTask",
    "GroupSummaryTask",
    "ScheduledMessagesTask",
    "ShopExpireTask",
    "WeeklyReportTask",
    "register_all_tasks",
]
