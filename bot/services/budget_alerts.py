"""预算告警闭环：阈值判定 → 持久 outbox → 后台发送 → 确认/重试。

设计约束（每条都因为反过来的做法会出事）：

* **发送绝不进预算短事务**。Telegram 是外部 IO，放在同事务里会让安全审核去等
  网络。预算路径只做一次极短的 INSERT（带唯一键），发送在后台消费者里。
* **日级幂等靠唯一键**，不靠内存 cooldown：``(群, 自然日, 额度类型, 阈值档)``。
  进程重启、并发触发、重复拒绝都只产生一条事件。
* **只有 send 成功才写确认**。返回 False、超时、抛异常都算未送达：留在表里
  可见、有界重试。ops_alert 的 ``alert_super_admin`` 返回 False 绝不算成功。
* **无收件人必须可观测**，不能标成已送达。
* **"发送成功但回写确认前崩溃"**这个窗口没有幂等 API 能证明唯一解：显式记成
  ``unknown`` 且**不再重发**（宁可少一条也不 spam），不谎称 exactly-once。
* 阈值判定**不改**任何用量、预算金额或价格；unknown 占用保持保守，不当 0。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import BudgetAlertEvent
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
#: 发送成功但没能回写确认（进程在两者之间死了）。不重发。
STATUS_UNKNOWN = "unknown"

THRESHOLD_WARN = "warn"
THRESHOLD_PAUSE = "pause"

LIMIT_ATTEMPT = "attempt"
LIMIT_TOKEN = "token"
LIMIT_MONEY = "money"
LIMIT_OPERATION = "operation"

#: 一次尝试失败后的退避（秒），有界：不会无限重试，也不会瞬间打爆上游。
RETRY_BACKOFF_SECONDS = (30.0, 120.0, 600.0)
MAX_ATTEMPTS = 5
LEASE_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ThresholdSnapshot:
    """某个额度当前的用量与上限（**None 上限 = 该额度没设，不参与判定**）。"""

    limit_kind: str
    used: int
    limit: int | None
    #: operation 额度时具体是哪个功能；其余额度类型为空串。
    feature: str = ""


def _operation_snapshots(budget_row: Any, day: Any) -> list["ThresholdSnapshot"]:
    """每个**功能**一份快照：tts 与 search 各自独立判定，绝不互相吞。

    ``limit_kind`` 一律是裸的 ``operation``，feature 走独立那一列 —— 拼接
    会受 String(16) 限制截断，长 feature 会撞成同一个身份。

    非 LLM 操作没有可靠账单字段，但配额本身是真实计数，所以按操作额度报各自
    档位，**不**折算成 token/money（那会把没有账单的东西伪报成有账单）。
    """

    limits = getattr(budget_row, "operation_limits", None)
    if not isinstance(limits, dict):
        return []
    used_map = getattr(day, "used_operations", None)
    if not isinstance(used_map, dict):
        return []
    out: list[ThresholdSnapshot] = []
    for name in limits:
        feature = str(name or "").strip()
        if not feature:
            continue
        try:
            limit = int(limits[name])
        except (TypeError, ValueError):
            continue
        if limit <= 0:
            continue
        out.append(ThresholdSnapshot(
            LIMIT_OPERATION, int(used_map.get(name, 0) or 0), limit, feature
        ))
    return out


def _reached(used: int, limit: int | None, ratio: float) -> bool:
    if limit is None or limit <= 0:
        return False
    return used >= limit * ratio


async def enqueue_threshold_events(
    session: AsyncSession,
    *,
    group_id: int,
    budget_date: str,
    day: Any,
    budget_row: Any,
) -> list[BudgetAlertEvent]:
    """在**预算自己的短事务**里登记越阈事件。

    这里只做一次 INSERT（唯一键冲突就忽略），不碰网络、不 sleep、不等待。
    因此安全审核路径上多出来的只是一次同库插入。
    """

    if budget_row is None or not bool(getattr(budget_row, "enabled", False)):
        return []
    warn_ratio = float(getattr(budget_row, "warn_ratio", 0.8) or 0.8)
    pause_ratio = float(getattr(budget_row, "pause_ratio", 1.0) or 1.0)
    snapshots = [
        ThresholdSnapshot(
            LIMIT_ATTEMPT,
            int(getattr(day, "used_attempts", 0) or 0),
            getattr(budget_row, "daily_attempt_limit", None),
        ),
        ThresholdSnapshot(
            LIMIT_TOKEN,
            int(getattr(day, "used_tokens", 0) or 0),
            getattr(budget_row, "daily_token_limit", None),
        ),
        ThresholdSnapshot(
            LIMIT_MONEY,
            int(getattr(day, "used_money_micro", 0) or 0),
            getattr(budget_row, "daily_money_micro_limit", None),
        ),
    ]
    snapshots.extend(_operation_snapshots(budget_row, day))
    created: list[BudgetAlertEvent] = []
    for snapshot in snapshots:
        for kind, ratio in ((THRESHOLD_WARN, warn_ratio), (THRESHOLD_PAUSE, pause_ratio)):
            if not _reached(snapshot.used, snapshot.limit, ratio):
                continue
            threshold = (
                int(snapshot.limit * ratio) if snapshot.limit is not None else None
            )
            row = await _insert_event(
                session,
                group_id=group_id,
                budget_date=budget_date,
                snapshot=snapshot,
                threshold_kind=kind,
                threshold_value=threshold,
                feature=snapshot.feature,
            )
            if row is not None:
                created.append(row)
    return created


async def _insert_event(
    session: AsyncSession,
    *,
    group_id: int,
    budget_date: str,
    snapshot: ThresholdSnapshot,
    threshold_kind: str,
    threshold_value: int | None,
    feature: str = "",
) -> BudgetAlertEvent | None:
    """幂等插入：唯一键冲突 = 今天这一档已经报过了，返回 None（不重复报）。"""

    summary = (
        f"group={group_id} limit={snapshot.limit_kind} used={snapshot.used} "
        f"limit_value={snapshot.limit} threshold={threshold_kind}"
    )
    stmt = (
        sqlite_insert(BudgetAlertEvent)
        .values(
            group_id=int(group_id),
            budget_date=str(budget_date),
            limit_kind=snapshot.limit_kind,
            feature=str(feature or ""),
            threshold_kind=threshold_kind,
            threshold_value=threshold_value,
            used_value=int(snapshot.used),
            status=STATUS_PENDING,
            next_attempt_at=now_shanghai_naive(),
            summary=summary[:300],
        )
        .on_conflict_do_nothing(
            index_elements=[
                "group_id", "budget_date", "limit_kind", "feature",
                "threshold_kind",
            ]
        )
    )
    result = await session.execute(stmt)
    # rowcount == 0 = 今天这一档已经报过了（幂等命中），不是错误。
    if int(getattr(result, "rowcount", 1) or 0) != 1:
        return None
    return BudgetAlertEvent(
        group_id=int(group_id),
        budget_date=str(budget_date),
        limit_kind=snapshot.limit_kind,
        feature=str(feature or ""),
        threshold_kind=threshold_kind,
        threshold_value=threshold_value,
        used_value=int(snapshot.used),
        status=STATUS_PENDING,
    )


class BudgetAlertConsumer:
    """后台消费者：取事件 → 拿租约 → 发送 → 确认/退避重试。

    发送在**写事务之外**；确认在另一个短事务里。外部发送被挂起时，独立连接
    仍能正常写库（护栏、审核、台账都不被它拖住）。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        sender: Callable[[str, dict[str, Any]], Any],
        recipient: str = "",
        owner: str = "",
        lease_seconds: float = LEASE_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._sender = sender
        # 收件人**显式**来自部署自身的 settings.super_admin_id。不设成空串 +
        # 靠测试去 patch 私有属性，那只是把接线错误藏起来：真实装配下永远发不出。
        self._recipient_id = str(recipient or "").strip()
        self._owner = owner or f"pid-{id(self):x}"
        self._lease_seconds = float(lease_seconds)
        self._max_attempts = int(max_attempts)
        self._clock = clock or time.monotonic
        self._task: asyncio.Task[Any] | None = None
        self._stopping = False
        self.stats = {"sent": 0, "failed": 0, "recovered": 0, "skipped_no_recipient": 0}

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, 5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    async def _run(self) -> None:
        while not self._stopping:
            try:
                processed = await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 消费者绝不退出
                log.warning("budget alert consumer iteration failed", exc_info=True)
                processed = 0
            if not processed:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def drain_once(self) -> int:
        """取一批可发事件并发出去；返回处理条数。"""

        claimed = await self._claim()
        for event, text, recipient in claimed:
            await self._deliver(event, text, recipient)
        return len(claimed)

    async def _claim(self) -> list[tuple[BudgetAlertEvent, str, str]]:
        now = now_shanghai_naive()
        async with self._session_factory() as session:
            async with session.begin():
                candidates = (
                    await session.scalars(
                        select(BudgetAlertEvent)
                        .where(
                            BudgetAlertEvent.status.in_(
                                (STATUS_PENDING, STATUS_FAILED)
                            ),
                            BudgetAlertEvent.attempts < self._max_attempts,
                            (
                                (BudgetAlertEvent.next_attempt_at.is_(None))
                                | (BudgetAlertEvent.next_attempt_at <= now)
                            ),
                        )
                        .order_by(BudgetAlertEvent.id.asc())
                        .limit(32)
                    )
                ).all()
                claimed: list[tuple[BudgetAlertEvent, str, str]] = []
                for event in candidates:
                    # 拿租约：崩溃/退出时租约会过期，事件自动回到可领取状态。
                    updated = await session.execute(
                        update(BudgetAlertEvent)
                        .where(
                            BudgetAlertEvent.id == int(event.id),
                            BudgetAlertEvent.status.in_(
                                (STATUS_PENDING, STATUS_FAILED)
                            ),
                        )
                        .values(
                            status=STATUS_SENDING,
                            lease_owner=self._owner,
                            lease_expires_at=now
                            + _seconds(self._lease_seconds),
                            attempts=BudgetAlertEvent.attempts + 1,
                            updated_at=now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if int(updated.rowcount or 0) != 1:
                        continue
                    claimed.append(
                        (event, _render(event), self._recipient())
                    )
                return claimed

    def _recipient(self) -> str:
        return self._recipient_id

    async def _deliver(
        self, event: BudgetAlertEvent, text: str, recipient: str
    ) -> None:
        if not recipient:
            # 无收件人：**不能**标成已送达。留在 failed 里可见、可在配置收件人
            # 之后重试。
            await self._finish(
                event, STATUS_FAILED, "no super_admin_id configured",
                retryable=True,
            )
            self.stats["skipped_no_recipient"] += 1
            return
        # 一旦开始真正发送，就把 dispatch_started 持久化。崩溃在这个点之后
        # 就**不能**自动重发：Telegram 没有幂等发送 API，重发可能给管理员第二条
        # 一样的消息，而"可能重复"与"一定送达"之间只能选一个可证的。
        if not await self._mark_dispatched(event):
            return
        try:
            accepted = await asyncio.wait_for(self._send(text, _event_dict(event)), 20.0)
        except asyncio.TimeoutError:
            await self._finish(
                event, STATUS_FAILED, "send timeout", retryable=True
            )
            self.stats["failed"] += 1
            return
        except Exception as exc:  # noqa: BLE001 - 发送失败不能拖垮消费者
            await self._finish(
                event, STATUS_FAILED, type(exc).__name__, retryable=True
            )
            self.stats["failed"] += 1
            return
        if accepted is not True:
            # 返回 False ≠ 成功（ops_alert 的约定：没有收件人 / 冷却 / 发送失败
            # 都返回 False）。把它当成功会让"没送达"永远消失。
            await self._finish(
                event, STATUS_FAILED, "sender returned False", retryable=True
            )
            self.stats["failed"] += 1
            return
        if await self._finish(
            event, STATUS_SENT, "", retryable=False, dispatch_started=True
        ):
            self.stats["sent"] += 1
        else:
            # 确认写不进去：行多半已被标 unknown/被别人接管，不能报成功。
            self.stats["lost_confirmation"] = self.stats.get("lost_confirmation", 0) + 1

    async def _send(self, text: str, event: dict[str, Any]) -> bool:
        result = self._sender(text, event)
        if asyncio.iscoroutine(result):
            result = await result
        return result is True

    async def _mark_dispatched(self, event: BudgetAlertEvent) -> bool:
        """带 owner 条件的 CAS：只有还持有租约的消费者能标记"已开始发送"。"""

        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(BudgetAlertEvent)
                    .where(
                        BudgetAlertEvent.id == int(event.id),
                        BudgetAlertEvent.lease_owner == self._owner,
                        BudgetAlertEvent.status == STATUS_SENDING,
                    )
                    .values(dispatch_started=True, updated_at=now_shanghai_naive())
                    .execution_options(synchronize_session=False)
                )
                if int(result.rowcount or 0) != 1:
                    # 租约已经不属于我了：这一条多半已被恢复并被新 owner 领走。
                    # 继续发就是**第二条一样的私聊** —— 必须在这里停下。
                    log.info(
                        "budget alert lease lost before dispatch | event=%s", event.id
                    )
                    return False
        return True

    async def _finish(
        self,
        event: BudgetAlertEvent,
        status: str,
        error: str,
        *,
        retryable: bool,
        dispatch_started: bool = False,
    ) -> bool:
        """写回终态。返回**是否真的写到了**这一行。

        rowcount==0（租约已丢给别人）不能算成功：把它标成 sent 会让 stats 说
        "已送达"而其实没有，掩盖一个正在进行中的重复投递。
        """

        now = now_shanghai_naive()
        values: dict[str, Any] = {
            "status": status,
            "last_error": str(error)[:200],
            "lease_owner": "",
            "lease_expires_at": None,
            "updated_at": now,
        }
        if status == STATUS_SENT:
            values["sent_at"] = now
            values["next_attempt_at"] = None
        elif dispatch_started and not retryable:
            values["status"] = STATUS_UNKNOWN
            values["next_attempt_at"] = None
        elif retryable and int(event.attempts or 0) < self._max_attempts:
            index = min(max(int(event.attempts or 1) - 1, 0), len(RETRY_BACKOFF_SECONDS) - 1)
            values["next_attempt_at"] = now + _seconds(RETRY_BACKOFF_SECONDS[index])
        else:
            values["next_attempt_at"] = None
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(BudgetAlertEvent)
                    .where(
                        BudgetAlertEvent.id == int(event.id),
                        BudgetAlertEvent.lease_owner == self._owner,
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def mark_uncertain_dispatches(self) -> int:
        """把"已开始发送但租约过期"的置为 unknown：**不自动重发**。"""

        now = now_shanghai_naive()
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(BudgetAlertEvent)
                    .where(
                        BudgetAlertEvent.status == STATUS_SENDING,
                        BudgetAlertEvent.dispatch_started.is_(True),
                        BudgetAlertEvent.lease_expires_at.is_not(None),
                        BudgetAlertEvent.lease_expires_at <= now,
                    )
                    .values(
                        status=STATUS_UNKNOWN,
                        lease_owner="",
                        lease_expires_at=None,
                        next_attempt_at=None,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
        return int(result.rowcount or 0)

    async def recover_expired_leases(self) -> int:
        """把租约过期的 ``sending`` 放回可领取 —— 崩溃恢复路径。

        不会把已确认的 ``sent`` 动，也不会重发 ``unknown``。
        """

        now = now_shanghai_naive()
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(BudgetAlertEvent)
                    .where(
                        BudgetAlertEvent.status == STATUS_SENDING,
                        # 只回收**还没开始发送**的：已经开始过的那条处于
                        # "可能已送达但确认不明"，自动重发就是 spam。
                        BudgetAlertEvent.dispatch_started.is_(False),
                        BudgetAlertEvent.lease_expires_at.is_not(None),
                        BudgetAlertEvent.lease_expires_at <= now,
                    )
                    .values(
                        status=STATUS_PENDING,
                        lease_owner="",
                        lease_expires_at=None,
                        next_attempt_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                recovered = int(result.rowcount or 0)
        if recovered:
            self.stats["recovered"] += recovered
        return recovered

    async def snapshot(self) -> dict[str, int]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(BudgetAlertEvent.status, func.count())
                    .group_by(BudgetAlertEvent.status)
                )
            ).all()
        return {str(status): int(count) for status, count in rows}


def _seconds(value: float):
    from datetime import timedelta

    return timedelta(seconds=float(value))


def _event_dict(event: BudgetAlertEvent) -> dict[str, Any]:
    """结构化事件载荷（发送方据此渲染，不再共用一个粗粒度 kind）。"""

    return {
        "group_id": int(event.group_id),
        "budget_date": str(event.budget_date),
        "limit_kind": str(event.limit_kind),
        "feature": str(event.feature or ""),
        "threshold_kind": str(event.threshold_kind),
        "threshold_value": (
            None if event.threshold_value is None else int(event.threshold_value)
        ),
        "used_value": int(event.used_value or 0),
        "event_id": int(event.id),
    }


def _render(event: BudgetAlertEvent) -> str:
    return (
        "⚠️ 预算护栏告警\n"
        f"群 {event.group_id}\n"
        f"日期 {event.budget_date}\n"
        f"额度 {event.limit_kind}"
        + (f"（{event.feature}）" if event.feature else "")
        + f" 已用 {event.used_value} / 阈值 {event.threshold_value}\n"
        f"档位 {event.threshold_kind}"
    )
