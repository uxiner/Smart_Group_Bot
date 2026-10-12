"""Scheduled group Markdown templates with optional buttons and pinning.

Rows live in the scheduled_messages table and are managed from the Mini App.
A lightweight background loop (same shape as the profile patrol) wakes every
check interval, sends whatever is due, and optionally pins the sent message
(a "定时置顶"). Daily entries compare last_run_at against today's scheduled
instant so restarts never double-fire; interval entries fire when
last_run_at (or created_at for the first run) is older than the interval.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.db.models import ScheduledMessage, ScheduledMessageOccurrence
from bot.services.background_health import record_background_failure
from bot.services.message_templates import (
    build_template_keyboard,
    render_markdown_html,
    render_plain_template,
    send_template_with_fallback,
)
from bot.services.authz import list_authorized_groups
from bot.services.operations_guard import PersistedGuard, verify_guard
from bot.services.operations_receipts import (
    BusinessReceipt,
    RECEIPT_STATE_DISPATCHING,
    RECEIPT_STATE_SENT,
    RECEIPT_STATE_UNKNOWN,
    TASK_SCHEDULED_MESSAGE,
    ambiguous_send_failure,
    claim_business_dispatch,
    definitely_not_delivered,
    load_receipt,
    receipt_key,
    record_receipt,
    release_business_dispatch,
)
from bot.services.patrol import parse_schedule_time
from bot.utils.telegram import (
    configured_auto_delete_seconds,
    schedule_message_auto_delete_durable,
    sanitize_outgoing_text,
)
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

_SCHEDULED_PASS_DEADLINE_SECONDS = 300.0

SCHEDULE_TYPES = ("daily", "interval")
_CHECK_INTERVAL_SECONDS = 30.0
_MIN_INTERVAL_MINUTES = 5
_CLAIM_LEASE_SECONDS = 180.0
_RETRY_BASE_SECONDS = 30.0
_RETRY_MAX_SECONDS = 1800.0
_PERSISTENT_FAILURE_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class _ClaimedScheduledOccurrence:
    occurrence_id: int
    occurrence_at: datetime
    lease_until: datetime
    attempts: int
    entry: ScheduledMessage


def scheduled_message_due(
    entry: ScheduledMessage,
    *,
    now: datetime | None = None,
) -> bool:
    """True when the entry should fire at `now` (Asia/Shanghai naive)."""
    return scheduled_message_occurrence_at(entry, now=now) is not None


def scheduled_message_occurrence_at(
    entry: ScheduledMessage,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Return the stable identity of the due occurrence, if any."""

    if not entry.enabled or not str(entry.text or "").strip():
        return None
    current = now if now is not None else now_shanghai_naive()
    if str(entry.schedule_type or "daily") == "interval":
        minutes = max(_MIN_INTERVAL_MINUTES, int(entry.interval_minutes or 0))
        anchor = entry.last_run_at or entry.created_at
        if anchor is None:
            return current
        due_at = anchor + timedelta(minutes=minutes)
        return due_at if current >= due_at else None
    parsed = parse_schedule_time(str(entry.schedule_time or ""))
    if parsed is None:
        return None
    due_at = current.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    if current < due_at:
        return None
    if entry.last_run_at is not None and entry.last_run_at >= due_at:
        return None
    return due_at


#: 运营任务中心手动触发时，对"当前这个时段"的三态判定。
OCCURRENCE_DUE = "due"
OCCURRENCE_NOT_DUE = "not_due"
OCCURRENCE_ALREADY_DELIVERED = "already_delivered"


def scheduled_message_manual_state(
    entry: ScheduledMessage,
    *,
    now: datetime | None = None,
) -> str:
    """手动确认前的三态判定：``due`` / ``not_due`` / ``already_delivered``。

    ``scheduled_message_occurrence_at`` 只能回答"现在该不该发"，而手动触发还需要
    把「这个时段已经发过了」与「还没到点」分开：daily 用
    ``last_run_at >= 当天计划时刻`` 判定已发；interval 没有"时段"概念，到点即
    ``due``、没到点即 ``not_due``。两者都不会把未来时段或往日时段补发出去。
    """

    if not entry.enabled or not str(entry.text or "").strip():
        return OCCURRENCE_NOT_DUE
    current = now if now is not None else now_shanghai_naive()
    if str(entry.schedule_type or "daily") == "interval":
        return (
            OCCURRENCE_DUE
            if scheduled_message_occurrence_at(entry, now=current) is not None
            else OCCURRENCE_NOT_DUE
        )
    parsed = parse_schedule_time(str(entry.schedule_time or ""))
    if parsed is None:
        return OCCURRENCE_NOT_DUE
    due_at = current.replace(
        hour=parsed[0], minute=parsed[1], second=0, microsecond=0
    )
    if current < due_at:
        return OCCURRENCE_NOT_DUE
    if entry.last_run_at is not None and entry.last_run_at >= due_at:
        return OCCURRENCE_ALREADY_DELIVERED
    return OCCURRENCE_DUE


#: 出站证据里的安全文案（不含异常原文/内容）。
SCHEDULED_SENT_DETAIL = "scheduled message delivered"
#: 出站**之前**写入的保护（B13a 返工 v2 R1）：值是保护，不是"已发送"的证明。
SCHEDULED_DISPATCH_DETAIL = "scheduled message outbound dispatch claim"


#: 一条定时消息的**稳定 occurrence 业务键**（群内 + 条目 + occurrence 时刻）。
#: 出站证据挂在这个键上：重启、换入口、换进程都认得同一次 occurrence。
def scheduled_occurrence_key(entry_id: int, occurrence_at: datetime) -> str:
    """``<entry_id>:<occurrence_at>``（秒精度，稳定、无随机成分）。"""

    stamp = (
        occurrence_at.strftime("%Y-%m-%dT%H:%M:%S")
        if isinstance(occurrence_at, datetime)
        else str(occurrence_at)
    )
    return receipt_key(int(entry_id), stamp)


#: 手动投递单条定时消息的结果（供运营任务中心映射到 item 状态）。
DELIVER_RESULT_SENT = "sent"
DELIVER_RESULT_ALREADY_DELIVERED = "already_delivered"
DELIVER_RESULT_NOT_DUE = "not_due"
DELIVER_RESULT_ENTRY_MISSING = "entry_missing"
DELIVER_RESULT_DISABLED = "disabled"
DELIVER_RESULT_SEND_FAILED = "send_failed"
DELIVER_RESULT_COMPLETION_UNCONFIRMED = "completion_unconfirmed"
#: 这个 occurrence 已经有过真实出站动作，但结果**未知**（超时/回包丢失）。
#: 绝不重发；调用方必须如实回报 unknown，而不是"已投递"或"没发出去"。
DELIVER_RESULT_OUTBOUND_UNKNOWN = "outbound_unknown"
#: 能证明**没有出站**的发送失败（渲染/参数/Telegram 明确 4xx 拒收）：
#: 可以安全重试，不必留保护，也不算未知。
DELIVER_RESULT_SEND_REJECTED = "send_rejected"
#: 出站前的写边界复核没通过（授权被撤 / 发起人已不是管理员 / 持久配置已变更 /
#: 本 run 已被取消）：一个字都没发，也没有任何副作用，必须重新预览后确认。
DELIVER_RESULT_GUARD_BLOCKED = "guard_blocked"


class ScheduledMessageService:
    """Background loop delivering due scheduled messages per authorized group."""

    def __init__(
        self,
        *,
        bot: Bot,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        check_interval_seconds: float = _CHECK_INTERVAL_SECONDS,
        claim_lease_seconds: float = _CLAIM_LEASE_SECONDS,
        retry_base_seconds: float = _RETRY_BASE_SECONDS,
        retry_max_seconds: float = _RETRY_MAX_SECONDS,
        persistent_failure_attempts: int = _PERSISTENT_FAILURE_ATTEMPTS,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.session_factory = session_factory
        self.check_interval_seconds = check_interval_seconds
        self.claim_lease_seconds = max(1.0, float(claim_lease_seconds))
        self.retry_base_seconds = max(0.01, float(retry_base_seconds))
        self.retry_max_seconds = max(
            self.retry_base_seconds,
            float(retry_max_seconds),
        )
        self.persistent_failure_attempts = max(
            1,
            int(persistent_failure_attempts),
        )
        self._run_lock = asyncio.Lock()

    async def run_forever(self) -> None:
        log.info("scheduled message service started")
        consecutive_failures = 0
        while True:
            try:
                async with asyncio.timeout(_SCHEDULED_PASS_DEADLINE_SECONDS):
                    await self.run_once(raise_on_total_failure=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("scheduled message pass failed")
                consecutive_failures = record_background_failure(
                    service="scheduled message service",
                    previous_failures=consecutive_failures,
                    error=exc,
                )
            else:
                consecutive_failures = 0
            await asyncio.sleep(max(5.0, float(self.check_interval_seconds)))

    async def run_once(
        self,
        *,
        now: datetime | None = None,
        raise_on_total_failure: bool = False,
    ) -> int:
        """Send every due entry once; returns the number delivered."""
        async with self._run_lock:
            return await self._run_once_locked(
                now=now,
                raise_on_total_failure=raise_on_total_failure,
            )

    async def _run_once_locked(
        self,
        *,
        now: datetime | None = None,
        raise_on_total_failure: bool = False,
    ) -> int:
        current = now if now is not None else now_shanghai_naive()
        async with self.session_factory() as session:
            authorized = {
                int(row.group_id) for row in await list_authorized_groups(session)
            }
            rows = (
                await session.scalars(
                    select(ScheduledMessage)
                    .where(ScheduledMessage.enabled.is_(True))
                    .order_by(ScheduledMessage.id)
                )
            ).all()
        due_occurrences = [
            (int(row.id), occurrence_at)
            for row in rows
            if int(row.group_id) in authorized
            and (
                occurrence_at := scheduled_message_occurrence_at(
                    row,
                    now=current,
                )
            )
            is not None
        ]
        await self._persist_due_occurrences(due_occurrences)

        delivered = 0
        failures = 0
        attempted = 0
        while True:
            claimed = await self._claim_next_occurrence(
                authorized_groups=authorized,
                now=current,
            )
            if claimed is None:
                break
            attempted += 1
            existing_receipt = await self.outbound_receipt(
                claimed.entry, claimed.occurrence_at
            )
            if existing_receipt is not None:
                # R1：这次 occurrence 已经有过真实出站动作（``sent``/``unknown``），
                # 或者存在出站保护（``dispatching``，可能正在发、也可能结果未知）。
                # 无论是上一轮进程死在"发送成功 → 完成记账"之间留下的，还是手动入口
                # 先发过，这里都只补业务台账，**绝不重发**。
                if await self._complete_occurrence(claimed, completed_at=current):
                    delivered += 1
                    log.warning(
                        "scheduled message occurrence already outbound; "
                        "completion only | group=%s entry=%s occurrence=%s",
                        claimed.entry.group_id,
                        claimed.entry.id,
                        claimed.occurrence_at,
                    )
                else:
                    failures += 1
                    log.error(
                        "scheduled message was already sent but durable "
                        "occurrence could not be completed | group=%s entry=%s "
                        "occurrence=%s",
                        claimed.entry.group_id,
                        claimed.entry.id,
                        claimed.occurrence_at,
                    )
                continue
            outcome, message_id = await self._send_occurrence(
                claimed.entry, claimed.occurrence_at
            )
            if outcome == "protected":
                # 这条业务键上已经有别人写下的出站保护（并发/上轮残留）：只补台账。
                if await self._complete_occurrence(claimed, completed_at=current):
                    delivered += 1
                else:
                    failures += 1
                continue
            if outcome != "sent":
                failures += 1
                await self._retry_occurrence(
                    claimed,
                    error=(
                        "Telegram scheduled message delivery result unknown"
                        if outcome == "unknown"
                        else "Telegram scheduled message delivery failed"
                    ),
                    now=current,
                )
                continue
            if not await self._complete_occurrence(
                claimed,
                completed_at=current,
                outbound=(RECEIPT_STATE_SENT, message_id, SCHEDULED_SENT_DETAIL),
            ):
                # 业务台账没提交成功：**立刻**用独立事务补写出站证据，否则租约
                # 过期后这个 occurrence 会被再次 claim 并重发一条。
                await self._record_outbound_safely(
                    claimed.entry,
                    claimed.occurrence_at,
                    state=RECEIPT_STATE_SENT,
                    external_message_id=message_id,
                    detail=SCHEDULED_SENT_DETAIL,
                )
                failures += 1
                log.error(
                    "scheduled message was sent but durable occurrence could "
                    "not be completed | group=%s entry=%s occurrence=%s",
                    claimed.entry.group_id,
                    claimed.entry.id,
                    claimed.occurrence_at,
                )
                continue
            delivered += 1
        if raise_on_total_failure:
            if attempted and failures and delivered == 0:
                raise RuntimeError(
                    "scheduled message delivery failed for all "
                    f"{attempted} claimed entries"
                )
            persistent_failures = await self._persistent_failure_count(
                authorized_groups=authorized,
            )
            if persistent_failures:
                # Backoff passes with no Telegram attempt must keep reporting a
                # poison occurrence. Otherwise run_forever would reset its
                # consecutive-failure counter during every quiet window and a
                # permanently broken destination would never reach supervisor.
                raise RuntimeError(
                    f"{persistent_failures} scheduled message occurrence(s) "
                    "remain persistently failed"
                )
        return delivered

    async def _persistent_failure_count(
        self,
        *,
        authorized_groups: set[int],
    ) -> int:
        if not authorized_groups:
            return 0
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(ScheduledMessageOccurrence.id)
                .join(
                    ScheduledMessage,
                    ScheduledMessage.id
                    == ScheduledMessageOccurrence.scheduled_message_id,
                )
                .where(
                    ScheduledMessage.enabled.is_(True),
                    ScheduledMessage.group_id.in_(authorized_groups),
                    ScheduledMessageOccurrence.attempts
                    >= self.persistent_failure_attempts,
                    ScheduledMessageOccurrence.last_error != "",
                )
            )
            return len(rows.all())

    async def _persist_due_occurrences(
        self,
        occurrences: list[tuple[int, datetime]],
    ) -> None:
        if not occurrences:
            return
        async with self.session_factory() as session:
            dialect = getattr(getattr(session, "bind", None), "dialect", None)
            for entry_id, occurrence_at in occurrences:
                if getattr(dialect, "name", "") == "sqlite":
                    await session.execute(
                        sqlite_insert(ScheduledMessageOccurrence)
                        .values(
                            scheduled_message_id=entry_id,
                            occurrence_at=occurrence_at,
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                ScheduledMessageOccurrence.scheduled_message_id,
                                ScheduledMessageOccurrence.occurrence_at,
                            ]
                        )
                    )
                else:
                    exists = await session.scalar(
                        select(ScheduledMessageOccurrence.id).where(
                            ScheduledMessageOccurrence.scheduled_message_id
                            == entry_id,
                            ScheduledMessageOccurrence.occurrence_at
                            == occurrence_at,
                        )
                    )
                    if exists is None:
                        session.add(
                            ScheduledMessageOccurrence(
                                scheduled_message_id=entry_id,
                                occurrence_at=occurrence_at,
                            )
                        )
            await session.commit()

    @staticmethod
    def _available_occurrence_filters(now: datetime) -> tuple[Any, Any]:
        return (
            or_(
                ScheduledMessageOccurrence.lease_until.is_(None),
                ScheduledMessageOccurrence.lease_until <= now,
            ),
            or_(
                ScheduledMessageOccurrence.next_attempt_at.is_(None),
                ScheduledMessageOccurrence.next_attempt_at <= now,
            ),
        )

    async def _claim_next_occurrence(
        self,
        *,
        authorized_groups: set[int],
        now: datetime,
        entry_id: int | None = None,
    ) -> _ClaimedScheduledOccurrence | None:
        if not authorized_groups:
            return None
        entry_filter: tuple[Any, ...] = ()
        if entry_id is not None:
            entry_filter = (
                ScheduledMessageOccurrence.scheduled_message_id == int(entry_id),
            )
        available = self._available_occurrence_filters(now)
        lease_until = now + timedelta(seconds=self.claim_lease_seconds)
        for _attempt in range(8):
            async with self.session_factory() as session:
                candidate_id = await session.scalar(
                    select(ScheduledMessageOccurrence.id)
                    .join(
                        ScheduledMessage,
                        ScheduledMessage.id
                        == ScheduledMessageOccurrence.scheduled_message_id,
                    )
                    .where(
                        ScheduledMessage.enabled.is_(True),
                        ScheduledMessage.group_id.in_(authorized_groups),
                        *entry_filter,
                        *available,
                    )
                    .order_by(
                        ScheduledMessageOccurrence.occurrence_at,
                        ScheduledMessageOccurrence.id,
                    )
                    .limit(1)
                )
                if candidate_id is None:
                    return None
                result = await session.execute(
                    update(ScheduledMessageOccurrence)
                    .where(
                        ScheduledMessageOccurrence.id == int(candidate_id),
                        *entry_filter,
                        *available,
                    )
                    .values(
                        attempts=ScheduledMessageOccurrence.attempts + 1,
                        lease_until=lease_until,
                        next_attempt_at=None,
                        last_error="",
                        updated_at=now,
                    )
                    .returning(
                        ScheduledMessageOccurrence.id,
                        ScheduledMessageOccurrence.scheduled_message_id,
                        ScheduledMessageOccurrence.occurrence_at,
                        ScheduledMessageOccurrence.attempts,
                    )
                )
                claimed = result.first()
                if claimed is None:
                    await session.rollback()
                    continue
                entry = await session.get(ScheduledMessage, int(claimed[1]))
                if entry is None or not bool(entry.enabled):
                    await session.execute(
                        delete(ScheduledMessageOccurrence).where(
                            ScheduledMessageOccurrence.id == int(claimed[0])
                        )
                    )
                    await session.commit()
                    continue
                await session.commit()
                return _ClaimedScheduledOccurrence(
                    occurrence_id=int(claimed[0]),
                    occurrence_at=claimed[2],
                    lease_until=lease_until,
                    attempts=int(claimed[3]),
                    entry=entry,
                )
        return None

    async def _retry_occurrence(
        self,
        claimed: _ClaimedScheduledOccurrence,
        *,
        error: str,
        now: datetime,
    ) -> None:
        delay = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, claimed.attempts - 1)),
        )
        async with self.session_factory() as session:
            await session.execute(
                update(ScheduledMessageOccurrence)
                .where(
                    ScheduledMessageOccurrence.id == claimed.occurrence_id,
                    ScheduledMessageOccurrence.lease_until == claimed.lease_until,
                )
                .values(
                    lease_until=None,
                    next_attempt_at=now + timedelta(seconds=delay),
                    last_error=str(error or "")[:1000],
                    updated_at=now,
                )
            )
            await session.commit()
        log.warning(
            "scheduled message retry scheduled | entry=%s occurrence=%s "
            "attempt=%s delay=%.1fs",
            claimed.entry.id,
            claimed.occurrence_at,
            claimed.attempts,
            delay,
        )

    async def _complete_occurrence(
        self,
        claimed: _ClaimedScheduledOccurrence,
        *,
        completed_at: datetime,
        outbound: tuple[str, int, str] | None = None,
    ) -> bool:
        """Advance last_run and remove the owned occurrence in one commit.

        ``outbound`` 给出时，**同一个事务**里也写入这次出站证据：成功路径因此只有
        一次 commit，而"业务台账提交失败"时由调用方立刻用独立事务补写证据——
        证据绝不依赖业务台账是否提交成功。
        """

        try:
            async with self.session_factory() as session:
                # Consume ownership first, in the same write transaction that
                # advances last_run_at.  A preliminary SELECT is insufficient:
                # its lease can expire and be claimed by another worker before
                # this transaction reaches DELETE.
                deleted = await session.execute(
                    delete(ScheduledMessageOccurrence).where(
                        ScheduledMessageOccurrence.id == claimed.occurrence_id,
                        ScheduledMessageOccurrence.lease_until
                        == claimed.lease_until,
                    )
                )
                if int(deleted.rowcount or 0) != 1:
                    await session.rollback()
                    return False
                row = await session.get(ScheduledMessage, int(claimed.entry.id))
                if row is None:
                    await session.commit()
                    return False
                row.last_run_at = completed_at
                if outbound is not None:
                    state, message_id, detail = outbound
                    await record_receipt(
                        session,
                        task_id=TASK_SCHEDULED_MESSAGE,
                        group_id=int(claimed.entry.group_id),
                        business_key=scheduled_occurrence_key(
                            int(claimed.entry.id), claimed.occurrence_at
                        ),
                        state=state,
                        external_message_id=message_id,
                        detail=detail,
                    )
                await session.commit()
                claimed.entry.last_run_at = completed_at
                return True
        except Exception:
            log.exception(
                "scheduled message occurrence completion failed | entry=%s",
                claimed.entry.id,
            )
            return False

    async def outbound_receipt(
        self,
        entry: ScheduledMessage,
        occurrence_at: datetime,
    ) -> BusinessReceipt | None:
        """这次 occurrence 上的出站证据（``None`` = 没有证据）。

        ``sent`` 与 ``unknown`` 都表示"已经可能出站"，调用方一律不得重发。
        """

        async with self.session_factory() as session:
            return await load_receipt(
                session,
                task_id=TASK_SCHEDULED_MESSAGE,
                group_id=int(entry.group_id),
                business_key=scheduled_occurrence_key(int(entry.id), occurrence_at),
            )

    async def _record_outbound(
        self,
        entry: ScheduledMessage,
        occurrence_at: datetime,
        *,
        state: str,
        external_message_id: int = 0,
        detail: str = "",
    ) -> None:
        """把"真实出站"写进**独立事务**并 commit。

        业务台账（``last_run_at`` / occurrence 行）稍后提交失败也不会把这条证据
        一起回滚掉——这正是"重启后不能重发"的依据。
        """

        async with self.session_factory() as session:
            await record_receipt(
                session,
                task_id=TASK_SCHEDULED_MESSAGE,
                group_id=int(entry.group_id),
                business_key=scheduled_occurrence_key(int(entry.id), occurrence_at),
                state=state,
                external_message_id=external_message_id,
                detail=detail,
            )
            await session.commit()

    async def _record_outbound_safely(
        self,
        entry: ScheduledMessage,
        occurrence_at: datetime,
        *,
        state: str,
        external_message_id: int = 0,
        detail: str = "",
    ) -> bool:
        """尽力补写一条出站证据（失败只记日志，绝不把异常抛回调用方）。"""

        try:
            await self._record_outbound(
                entry,
                occurrence_at,
                state=state,
                external_message_id=external_message_id,
                detail=detail,
            )
            return True
        except Exception:
            log.exception(
                "scheduled message outbound receipt record failed | group=%s "
                "entry=%s occurrence=%s",
                int(entry.group_id),
                entry.id,
                occurrence_at,
            )
            return False

    async def _claim_dispatch(
        self,
        entry: ScheduledMessage,
        occurrence_at: datetime,
        *,
        guard: "PersistedGuard | None" = None,
    ) -> str:
        """出站**之前**在独立事务里抢下这条业务键的保护（B13a 返工 v2 R1）。

        返回 ``"claimed"``（本入口拿到保护，可以发送）、``"protected"``（这条业务
        键上已有保护，本入口绝不发送）或 ``"blocked"``（R2 返工 v3：带运营中心
        run 上下文的入口，持久复核不通过——授权被撤、发起人已不是管理员、持久
        配置已变更或本 run 已被取消；查询出错按保守协议也视为 ``"blocked"``）。

        保护写不进去就返回非 ``"claimed"``：调用方**绝不允许**在没有保护的情况下
        发送。这条写入用 ``claim_business_dispatch`` 而不是 ``record_receipt``，因为
        "发送后确认失败"的故障注入恰好打在后者的名字上——保护必须在任何
        发送后记账失败、进程断电或取消的情况下依然留在库里。
        """

        async with self.session_factory() as session:
            claim = await claim_business_dispatch(
                session,
                task_id=TASK_SCHEDULED_MESSAGE,
                group_id=int(entry.group_id),
                business_key=scheduled_occurrence_key(int(entry.id), occurrence_at),
                detail=SCHEDULED_DISPATCH_DETAIL,
                guard=guard,
            )
            await session.commit()
            if claim is not None:
                return "claimed"
        if guard is None:
            return "protected"
        # 抢不到要么是别人持有保护，要么是复核不通过；如实区分（复核查询自己
        # 出错时同样不发，按保守协议报"复核不通过"）。
        try:
            async with self.session_factory() as session:
                allowed = await verify_guard(session, guard)
        except Exception:
            log.exception(
                "scheduled message dispatch guard verify failed | group=%s entry=%s",
                int(entry.group_id),
                entry.id,
            )
            return "blocked"
        # 复核通过 → 是别人握着保护（不是复核不通过），仍然绝不发送。
        return "protected" if allowed else "blocked"

    async def _release_dispatch(
        self,
        entry: ScheduledMessage,
        occurrence_at: datetime,
    ) -> None:
        """只有**能证明没有出站**时才撤销保护（``sent``/``unknown`` 永不删）。"""

        async with self.session_factory() as session:
            await release_business_dispatch(
                session,
                task_id=TASK_SCHEDULED_MESSAGE,
                group_id=int(entry.group_id),
                business_key=scheduled_occurrence_key(int(entry.id), occurrence_at),
            )
            await session.commit()

    async def _send_occurrence(
        self,
        entry: ScheduledMessage,
        occurrence_at: datetime,
        *,
        protect_outbound: bool = False,
        guard: "PersistedGuard | None" = None,
    ) -> tuple[str, int]:
        """真实发送这一次 occurrence，并如实区分四种结局。

        返回 ``("sent"|"unknown"|"failed"|"protected"|"blocked", message_id)``：

        * ``sent``——拿到真实出站结果（出站证据由调用方在完成事务里一起提交，
          失败时立刻补写，见 :meth:`_complete_occurrence` 的调用点）；
        * ``unknown``——结果无法证明（超时/断连/认不出来的异常，**可能已经送达**）：
          立刻落一条 ``unknown`` 证据，调用方不得把它当"没发出去"；
        * ``failed``——**能证明没出站**（渲染/参数/明确 4xx 拒收），按既有策略允许重试；
        * ``protected``——这条业务键上已经有（或刚被别人抢到）出站保护：
          本次**不发送**，调用方只能补台账；
        * ``blocked``——R2（返工 v3）运营中心手动入口的持久写边界复核不通过：
          本次**不发送**，也没有任何副作用，只能重新预览后确认。

        ``protect_outbound=True`` 是运营中心/手动入口的语义（B13a 返工 v2 R1）：
        先落保护再出站，认不出来的异常一律按"可能已出站"处理。后台常驻 runner
        保持既有 at-least-once 语义（``False``），但它**认识**别人写下的保护。
        ``guard`` 只在带真实中心 run 上下文的手动入口传（自动路径恒为 ``None``）。
        """

        if protect_outbound:
            dispatch = await self._claim_dispatch(
                entry, occurrence_at, guard=guard
            )
            if dispatch == "blocked":
                return "blocked", 0
            if dispatch != "claimed":
                return "protected", 0
        try:
            sent = await self._send_now(entry)
        except asyncio.CancelledError:
            # 取消点正好落在发送中：保护必须保留（消息可能已经出站）。
            raise
        except Exception as exc:
            if definitely_not_delivered(exc):
                # Telegram 明确拒收（4xx）：能证明没有出站，撤销保护允许重试。
                if protect_outbound:
                    await self._release_dispatch(entry, occurrence_at)
                log.warning(
                    "scheduled message send rejected | group=%s entry=%s | %s",
                    int(entry.group_id),
                    entry.id,
                    exc,
                )
                return "failed", 0
            if protect_outbound or ambiguous_send_failure(exc):
                await self._record_outbound_safely(
                    entry,
                    occurrence_at,
                    state=RECEIPT_STATE_UNKNOWN,
                    detail="scheduled send result unknown",
                )
                return "unknown", 0
            log.exception(
                "scheduled message send failed | group=%s entry=%s",
                int(entry.group_id),
                entry.id,
            )
            return "failed", 0
        if sent is None or sent is False:
            # 发送前就退出了（例如正文被清空）：没有出站，撤销保护允许重试。
            if protect_outbound:
                await self._release_dispatch(entry, occurrence_at)
            return "failed", 0
        message_id = int(getattr(sent, "message_id", 0) or 0)
        log.info(
            "scheduled message sent | group=%s entry=%s pinned=%s",
            int(entry.group_id),
            entry.id,
            bool(entry.pin_message),
        )
        return "sent", message_id

    async def deliver_message(self, entry: ScheduledMessage) -> Any | None:
        """真实发送单条定时消息（含自动删除排队与置顶）。

        运营任务中心与后台循环共用这一条链：发送成功返回 Telegram 消息对象，
        发送失败返回 ``None``（日志已记录）。调用方据此决定"明确未出站"还是
        "未知"，不在 API 侧复制一套发送逻辑。

        B13a 返工 R1：需要区分"超时（可能已出站）"的调用方用
        :meth:`_send_with_receipt`（同一个 :meth:`_send_now`，但异常不被吞掉，
        且发送成功后立刻落一条持久出站证据）。
        """

        try:
            return await self._send_now(entry)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "scheduled message send failed | group=%s entry=%s",
                int(entry.group_id),
                entry.id,
            )
            return None

    async def _send_now(self, entry: ScheduledMessage) -> Any:
        """真实发送（异常**原样抛出**，由调用方决定记 failed 还是 unknown）。"""

        group_id = int(entry.group_id)
        source_text = sanitize_outgoing_text(str(entry.text or "").strip())
        if not source_text:
            return False
        payload = render_markdown_html(source_text)
        plain_fallback = render_plain_template(source_text)
        keyboard = build_template_keyboard(getattr(entry, "buttons", None))
        raw_disable_link_preview = getattr(entry, "disable_link_preview", None)
        disable_link_preview = (
            True
            if raw_disable_link_preview is None
            else bool(raw_disable_link_preview)
        )
        sent = await send_template_with_fallback(
            lambda text, **kwargs: self.bot.send_message(
                chat_id=group_id,
                text=text,
                **kwargs,
            ),
            formatted_text=payload,
            plain_text=plain_fallback,
            reply_markup=keyboard,
            disable_link_preview=disable_link_preview,
        )
        if entry.auto_delete:
            await schedule_message_auto_delete_durable(
                sent,
                configured_auto_delete_seconds(self.settings, "scheduled"),
            )
        if entry.pin_message:
            await self._pin(entry, sent.message_id)
        return sent

    async def deliver_entry_occurrence(
        self,
        entry_id: int,
        *,
        group_id: int | None = None,
        now: datetime | None = None,
        protect_outbound: bool = False,
        guard: "PersistedGuard | None" = None,
    ) -> tuple[str, int]:
        """手动投递一条定时消息的**当前** due occurrence。

        返回 ``(结果码, 真实消息 id)``；只有 ``sent`` 才有非 0 的消息 id。

        与后台循环共用 ``_persist_due_occurrences`` / ``_claim_next_occurrence``
        / ``deliver_message`` / ``_complete_occurrence``：同一套租约与
        ``last_run_at`` 水位，不另造一套锁。只处理"现在"这一个时段：已经发过
        的时段返回 ``already_delivered``，没到点返回 ``not_due``，都不会补发
        旧时段或未来时段。
        """

        current = now if now is not None else now_shanghai_naive()
        async with self.session_factory() as session:
            entry = await session.get(ScheduledMessage, int(entry_id))
            authorized = {
                int(row.group_id) for row in await list_authorized_groups(session)
            }
        if entry is None:
            return DELIVER_RESULT_ENTRY_MISSING, 0
        if group_id is not None and int(entry.group_id) != int(group_id):
            # 跨群请求按"不存在"处理，不泄露其它群有没有这条定时消息。
            return DELIVER_RESULT_ENTRY_MISSING, 0
        if int(entry.group_id) not in authorized:
            return DELIVER_RESULT_ENTRY_MISSING, 0
        if not bool(entry.enabled):
            return DELIVER_RESULT_DISABLED, 0
        state = scheduled_message_manual_state(entry, now=current)
        if state == OCCURRENCE_ALREADY_DELIVERED:
            return DELIVER_RESULT_ALREADY_DELIVERED, 0
        if state != OCCURRENCE_DUE:
            return DELIVER_RESULT_NOT_DUE, 0
        occurrence_at = scheduled_message_occurrence_at(entry, now=current)
        if occurrence_at is None:
            return DELIVER_RESULT_NOT_DUE, 0
        if not str(entry.text or "").strip():
            return DELIVER_RESULT_NOT_DUE, 0
        await self._persist_due_occurrences([(int(entry.id), occurrence_at)])
        claimed = await self._claim_next_occurrence(
            authorized_groups=authorized,
            now=current,
            entry_id=int(entry.id),
        )
        if claimed is None:
            # 另一个真实写入者刚刚抢走了这个时段。
            return DELIVER_RESULT_ALREADY_DELIVERED, 0
        existing_receipt = await self.outbound_receipt(
            claimed.entry, claimed.occurrence_at
        )
        if existing_receipt is not None:
            # R1：这次 occurrence 已经有出站证据（``sent``/``unknown``/``dispatching``
            # 出站保护）：手动入口也只看同一份证据，只补业务台账，绝不重发。只有
            # 已确认的 ``sent`` 才能说"已投递"，其余（含出站保护）如实报结果未知。
            existing_message_id = int(existing_receipt.external_message_id or 0)
            unknown_outbound = existing_receipt.state in (
                RECEIPT_STATE_UNKNOWN,
                RECEIPT_STATE_DISPATCHING,
            )
            if await self._complete_occurrence(claimed, completed_at=current):
                if unknown_outbound:
                    return DELIVER_RESULT_OUTBOUND_UNKNOWN, existing_message_id
                return DELIVER_RESULT_ALREADY_DELIVERED, existing_message_id
            return DELIVER_RESULT_COMPLETION_UNCONFIRMED, existing_message_id
        outcome, message_id = await self._send_occurrence(
            claimed.entry,
            claimed.occurrence_at,
            protect_outbound=protect_outbound,
            guard=guard,
        )
        if outcome == "blocked":
            # R2（返工 v3）：写边界复核不通过——没发送、没留保护；把这个时段交回
            # 租约（下次尝试仍要重新预览确认），并如实回报给运营任务中心。
            await self._retry_occurrence(
                claimed,
                error="operations guard blocked before outbound dispatch",
                now=current,
            )
            return DELIVER_RESULT_GUARD_BLOCKED, 0
        if outcome == "protected":
            # 出站保护已存在或刚被别人抢到：不发送，也不声称成功。
            if await self._complete_occurrence(claimed, completed_at=current):
                return DELIVER_RESULT_OUTBOUND_UNKNOWN, 0
            return DELIVER_RESULT_COMPLETION_UNCONFIRMED, 0
        if outcome == "unknown":
            await self._retry_occurrence(
                claimed,
                error="Telegram scheduled message delivery result unknown",
                now=current,
            )
            return DELIVER_RESULT_SEND_FAILED, 0
        if outcome != "sent":
            await self._retry_occurrence(
                claimed,
                error="Telegram scheduled message delivery failed",
                now=current,
            )
            if protect_outbound and outcome == "failed":
                # 出站前就失败/被明确拒收：没有副作用，如实标成可安全重试。
                return DELIVER_RESULT_SEND_REJECTED, 0
            return DELIVER_RESULT_SEND_FAILED, 0
        if not await self._complete_occurrence(
            claimed,
            completed_at=current,
            outbound=(RECEIPT_STATE_SENT, message_id, SCHEDULED_SENT_DETAIL),
        ):
            # 消息已经出站：绝不能声称"没发出去"，也不能自动重发；同时补一条独立
            # 事务的出站证据，避免这个 occurrence 被后续任何入口再次 claim 后重发。
            await self._record_outbound_safely(
                claimed.entry,
                claimed.occurrence_at,
                state=RECEIPT_STATE_SENT,
                external_message_id=message_id,
                detail=SCHEDULED_SENT_DETAIL,
            )
            return DELIVER_RESULT_COMPLETION_UNCONFIRMED, message_id
        return DELIVER_RESULT_SENT, message_id

    async def _pin(self, entry: ScheduledMessage, message_id: int) -> None:
        group_id = int(entry.group_id)
        previous_id = int(entry.last_message_id or 0)
        if entry.unpin_previous and previous_id:
            try:
                await self.bot.unpin_chat_message(
                    chat_id=group_id, message_id=previous_id
                )
            except Exception:
                log.debug(
                    "scheduled message unpin skipped | group=%s message=%s",
                    group_id,
                    previous_id,
                )
        try:
            await self.bot.pin_chat_message(
                chat_id=group_id,
                message_id=message_id,
                disable_notification=True,
            )
        except Exception:
            log.warning(
                "scheduled message pin failed | group=%s entry=%s",
                group_id,
                entry.id,
            )
            return
        try:
            async with self.session_factory() as session:
                row = await session.get(ScheduledMessage, int(entry.id))
                if row is not None:
                    row.last_message_id = int(message_id)
                    await session.commit()
        except Exception:
            # Losing the unpin bookmark must not abort the remaining due
            # entries in this pass; their last_run_at is already committed.
            log.exception(
                "scheduled message pin bookkeeping failed | group=%s entry=%s",
                group_id,
                entry.id,
            )
