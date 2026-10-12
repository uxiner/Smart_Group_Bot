"""按群可选功能的预算护栏：预留 → 结算 → 退回。

护栏的形状（每一条都是因为反过来的做法会出人事故）：

* **默认关闭，且关闭时一次库都不读**。开启状态是进程内缓存（带 TTL），
  因为关着的时候每条消息都查一次库，等于把一个"没开的功能"变成每次回复的
  额外延迟与写压力。
* **预留发生在网络调用之前**，且每个 retry / fallback / 工具步**各自**预留。
  正确的最终用量留到 settle 补上，因为发送前只能知道上界。
* **预留用一条条件 UPDATE 完成**，不做 SELECT→判断→INSERT。20 条并发各自
  「读一下还剩多少」会让它们同时看到还剩 1，然后全部通过。
* **safety 永远绕过可选准入**。护栏存储故障、额度用尽、日期切换中的任何一
  种，都不许让审核停下来。
* **启用了护栏时的核验失败 = 拒绝**（fail closed），不偷偷放行。关闭状态下
  的核验失败不影响服务（那时本来就没有护栏）。
* **结果未知不退款**：已发出的 attempt 保留占用并标 unknown。把未知当免费，
  等于给每一次超时发一次免费额度。
* **发送前取消才退款**，而且退款是幂等的。

日窗口用 Asia/Shanghai 的自然日，和 ``llm_usage_daily`` / 签到台账同口径。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import GroupBudget, GroupBudgetDay, GroupBudgetReservation
from bot.services import pricing, usage_scope
from bot.services.strict_values import StrictValueError, strict_int
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

STATUS_RESERVED = "reserved"
#: 已被 CAS 抢占、正在改状态。它**必须**可被下一次 CAS 再抢占，否则进程在
#: 这段里崩掉会让这笔预留永远既结不清也退不回。
STATUS_SETTLING = "settling"
STATUS_SETTLED = "settled"
STATUS_RELEASED = "released"
STATUS_UNKNOWN = "unknown"

REASON_DISABLED = "budget_disabled"
REASON_NOT_CONFIGURED = "budget_not_configured"
REASON_SAFETY = "safety_bypasses_guard"
REASON_ATTEMPTS = "daily_attempt_limit"
REASON_TOKENS = "daily_token_limit"
REASON_MONEY = "daily_money_limit"
REASON_OPERATION = "operation_limit"
REASON_STORE_UNAVAILABLE = "budget_store_unavailable"
REASON_MONEY_UNPRICED = "money_cap_unpriced"
REASON_MONEY_CURRENCY = "money_cap_currency_mismatch"

#: 启用状态缓存 TTL。改预算后显式 :meth:`GroupBudgetService.invalidate`，
#: TTL 只是兜底（多进程部署时另一个进程改了预算）。
ENABLED_CACHE_TTL_SECONDS = 5.0
#: 「全局没有配过预算」这个结论的有效期它必须有界：另一个进程或另一个服务
#: 实例随时可能新增预算，而永久缓存 False 会让「启用预算」在它们面前永远无效。
_ANY_BUDGET_PROBE_TTL_SECONDS = 5.0
#: 预留行的租约：进程崩掉时由 reap 回收，不会永久泄漏额度。
RESERVATION_LEASE_SECONDS = 300.0


#: 进程级阈值登记钩子（由 ``bot.services.budget_alerts`` 在启动时装配）。
#: 刻意做成可空：没装配就完全不登记事件，既有预算行为一点不变。
_ALERT_ENQUEUER: Any = None


def configure_alert_enqueuer(enqueuer: Any | None) -> None:
    global _ALERT_ENQUEUER
    _ALERT_ENQUEUER = enqueuer


def shared_service_or_none() -> "GroupBudgetService | None":
    """已装配的共享服务；**没装配**时返回 None（而不是抛错）。

    "没装预算护栏"与"存储坏了"是两件事：前者这台部署压根没启用可选额度，不该
    把可选功能一起拒掉；后者才是 fail-closed。
    """

    return _SHARED_SERVICE


def alert_enqueuer() -> Any | None:
    """当前登记钩子。启动包装器用它记住"进入时的那个"，失败时还原回去。"""

    return _ALERT_ENQUEUER


class _DaySnapshot:
    """给阈值判定用的当日快照（字段与 ``GroupBudgetDay`` 对齐）。"""

    __slots__ = (
        "used_attempts", "used_tokens", "used_money_micro", "used_operations",
    )

    def __init__(self, *, used_attempts, used_tokens, used_money_micro,
                 used_operations):
        self.used_attempts = used_attempts
        self.used_tokens = used_tokens
        self.used_money_micro = used_money_micro
        self.used_operations = used_operations


class BudgetError(RuntimeError):
    """预算配置或状态异常。"""


class BudgetConflict(BudgetError):
    """并发配置冲突（expected revision 不匹配）。"""


def _snapshot_of(entry: pricing.PriceEntry | None) -> dict[str, Any] | None:
    """把一条单价冻成可持久化的快照（结算时只读它，不再查目录）。"""

    if entry is None:
        return None
    return {
        "provider_key": entry.provider_key,
        "model": entry.model,
        "currency": entry.currency,
        "input_per_million": (
            str(entry.input_per_million) if entry.input_per_million is not None else None
        ),
        "output_per_million": (
            str(entry.output_per_million)
            if entry.output_per_million is not None
            else None
        ),
        "cache_read_per_million": (
            str(entry.cache_read_per_million)
            if entry.cache_read_per_million is not None
            else None
        ),
        "cache_write_per_million": (
            str(entry.cache_write_per_million)
            if entry.cache_write_per_million is not None
            else None
        ),
    }


def _entry_from_snapshot(snapshot: Any) -> pricing.PriceEntry | None:
    if not isinstance(snapshot, dict):
        return None
    try:
        return pricing.PriceEntry(
            provider_key=snapshot.get("provider_key", ""),
            model=snapshot.get("model", ""),
            currency=snapshot.get("currency", ""),
            input_per_million=snapshot.get("input_per_million"),
            output_per_million=snapshot.get("output_per_million"),
            cache_read_per_million=snapshot.get("cache_read_per_million"),
            cache_write_per_million=snapshot.get("cache_write_per_million"),
        )
    except (pricing.PricingError, AttributeError, TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: int | None
    admitted: bool
    enforced: bool
    reason: str = ""
    reserved_money_micro: int = 0
    reserved_tokens: int = 0
    currency: str = ""
    price_version: int = 0
    #: False = 这次操作没有可靠账单字段（TTS/媒体/搜索等），UI 必须显示
    #: 「未计量」而不是「0 元」。
    metered: bool = True


@dataclass(frozen=True, slots=True)
class DayState:
    group_id: int
    budget_date: str
    used_attempts: int
    used_tokens: int
    used_money_micro: int
    unknown_attempts: int
    used_operations: dict[str, int] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "budget_date": self.budget_date,
            "used_attempts": self.used_attempts,
            "used_tokens": self.used_tokens,
            "used_money_micro": self.used_money_micro,
            "unknown_attempts": self.unknown_attempts,
            "used_operations": dict(self.used_operations),
        }


def _today() -> str:
    return now_shanghai_naive().date().isoformat()


def _int_or_none(value: Any, *, field: str = "limit") -> int | None:
    """额度解析：**坏值必须报错**，不得悄悄变成"未设置"。

    曾经畸形值（``'not-an-integer'``）被当 None，于是用户写错上限之后系统
    变成**无限额**，而界面上还显示"未启用" —— 一条拼错的配置直接撤掉了护栏。
    """

    if value is None or value == "":
        return None
    try:
        return strict_int(value, field=field, minimum=0)
    except StrictValueError as exc:
        raise BudgetError(f"{field} 必须是 0 或正整数。") from exc


class GroupBudgetService:
    """按群预算护栏的独立服务（自带会话工厂，不与群设置事务耦合）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        catalog_provider: Callable[[], pricing.PriceCatalog] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._catalog_provider = catalog_provider or (lambda: pricing.PriceCatalog(version=1))
        self._enabled_cache: dict[int, tuple[float, GroupBudget | None]] = {}
        #: 「本进程见过任何一条预算配置吗」。``None`` = 还没探过。
        #:
        #: 它让"全局所有群都没配预算"这件事只需要**一次**只读探测，之后关闭快
        #: 路径连 session 都不开；而存储故障发生时也能区分「已知没配」（放行）
        #: 与「不知道」（fail closed）。
        self._any_budget_seen: bool | None = None
        #: 进程级探测的**时间戳**。False 不是永久结论：另一个进程可能刚配了预算，
        #: 永久缓存 False 等于「启用预算在别的进程/服务实例上永远不生效」。
        #: TTL 让它最终会被重新探到，同时把"没预算"的成本压到常数级。
        self._any_budget_probed_at: float = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def configure(self, session_factory: Any) -> None:
        self._session_factory = session_factory
        self.invalidate()

    def set_catalog_provider(
        self, provider: Callable[[], pricing.PriceCatalog]
    ) -> None:
        self._catalog_provider = provider

    def invalidate(self, group_id: int | None = None) -> None:
        if group_id is None:
            self._enabled_cache.clear()
            self._any_budget_seen = None
            self._any_budget_probed_at = 0.0
        else:
            self._enabled_cache.pop(int(group_id), None)
            self._any_budget_seen = None
            self._any_budget_probed_at = 0.0

    async def _probe_any_budget(self) -> bool:
        async with self._session_factory() as session:
            # GroupBudget 的主键就是 group_id（没有自增 id 列）。
            row = await session.execute(
                select(GroupBudget.group_id).limit(1)
            )
            return row.first() is not None

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    async def get_budget(self, group_id: int) -> GroupBudget | None:
        async with self._session_factory() as session:
            return await session.get(GroupBudget, int(group_id))

    async def save_budget(
        self,
        group_id: int,
        *,
        expected_revision: int,
        enabled: bool,
        currency: str = "",
        daily_attempt_limit: Any = None,
        daily_token_limit: Any = None,
        daily_money_micro_limit: Any = None,
        operation_limits: dict[str, Any] | None = None,
        money_covers: list[tuple[str, str]] | None = None,
        updated_by: int = 0,
    ) -> GroupBudget:
        attempts = _int_or_none(daily_attempt_limit)
        tokens = _int_or_none(daily_token_limit)
        money = _int_or_none(daily_money_micro_limit)
        for name, value in (
            ("daily_attempt_limit", attempts),
            ("daily_token_limit", tokens),
            ("daily_money_micro_limit", money),
        ):
            if value is not None and value < 0:
                raise BudgetError(f"{name} 不能为负。")
        limits: dict[str, int] = {}
        for feature, value in (operation_limits or {}).items():
            name = str(feature or "").strip()
            if not name:
                continue
            count = _int_or_none(value)
            if count is None or count < 0:
                raise BudgetError("操作次数配额必须是非负整数。")
            limits[name] = count
        if money is not None and not str(currency or "").strip():
            raise BudgetError("设置金额上限时必须指定币种。")
        if money is not None:
            # 金额 hard cap 只有在**计价完整**时才允许存在：列出来的每个模型
            # 都必须有 input/output 单价，且与预算币种一致。否则报出来的
            # "金额上限"是算不出来的，等于给了一个永远不会被触达的假护栏。
            catalog = self._catalog_provider()
            if not catalog.entries:
                raise BudgetError("尚未配置任何单价，无法启用金额上限。")
            seen_currency = str(currency or "").strip().upper()
            for provider_key, model in money_covers or []:
                entry = catalog.find(provider_key=provider_key, model=model)
                if entry is None:
                    raise BudgetError(f"模型 {model} 没有配置单价，无法启用金额上限。")
                if not entry.complete_for(("input", "output")):
                    raise BudgetError(f"模型 {model} 的输入/输出单价不完整。")
                if entry.currency != seen_currency:
                    raise BudgetError(
                        f"模型 {model} 的计价币种 {entry.currency} 与预算币种不一致。"
                    )

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(GroupBudget, int(group_id))
                if row is None:
                    if int(expected_revision) != 0:
                        raise BudgetConflict("预算配置不存在。")
                    # 建行的并发由主键唯一约束兜：两个并发创建只有一个成功，
                    # 另一个在 flush 时撞唯一键 → BudgetConflict（见调用方捕获）。
                    row = GroupBudget(
                        group_id=int(group_id),
                        enabled=bool(enabled),
                        revision=1,
                        currency=str(currency or "").strip().upper()[:8],
                        daily_attempt_limit=attempts,
                        daily_token_limit=tokens,
                        daily_money_micro_limit=money,
                        operation_limits=limits,
                        updated_by=int(updated_by),
                    )
                    session.add(row)
                    try:
                        await session.flush()
                    except IntegrityError as exc:
                        raise BudgetConflict("预算配置已被其他会话创建。") from exc
                    await session.refresh(row)
                else:
                    # CAS 写在 SQL 的 WHERE 里：先读后比 revision 在并发下会让两个
                    # 会话都看到"当前是 N"，两次都成功，最后写进去的是随机那个。
                    updated = await session.execute(
                        update(GroupBudget)
                        .where(
                            GroupBudget.group_id == int(group_id),
                            GroupBudget.revision == int(expected_revision),
                        )
                        .values(
                            enabled=bool(enabled),
                            currency=str(currency or "").strip().upper()[:8],
                            daily_attempt_limit=attempts,
                            daily_token_limit=tokens,
                            daily_money_micro_limit=money,
                            operation_limits=limits,
                            revision=GroupBudget.revision + 1,
                            updated_by=int(updated_by),
                            updated_at=now_shanghai_naive(),
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if int(updated.rowcount or 0) != 1:
                        raise BudgetConflict("预算配置已被其他会话更新。")
                    await session.refresh(row)
        self.invalidate(int(group_id))
        return row

    async def _budget_for(self, group_id: int) -> GroupBudget | None:
        now = time.monotonic()
        cached = self._enabled_cache.get(int(group_id))
        if cached is not None and cached[0] > now:
            return cached[1]
        if (
            self._any_budget_seen is None
            or now - self._any_budget_probed_at > _ANY_BUDGET_PROBE_TTL_SECONDS
        ):
            self._any_budget_seen = await self._probe_any_budget()
            self._any_budget_probed_at = now
        if self._any_budget_seen is False:
            # 进程级探测已经确认"没有任何群配过预算"：这个群必然是未配置，
            # 不必再开一次 session 去确认同一个事实。
            self._enabled_cache[int(group_id)] = (now + ENABLED_CACHE_TTL_SECONDS, None)
            return None
        async with self._session_factory() as session:
            row = await session.get(GroupBudget, int(group_id))
        self._enabled_cache[int(group_id)] = (now + ENABLED_CACHE_TTL_SECONDS, row)
        return row

    # ------------------------------------------------------------------
    # 预留
    # ------------------------------------------------------------------
    async def reserve(
        self,
        *,
        group_id: int,
        purpose: str,
        stage: str = "",
        provider: str = "",
        model: str = "",
        prompt_tokens: Any = 0,
        max_output_tokens: Any = 0,
    ) -> Reservation:
        """为一次**即将发出**的可选调用预留额度。

        返回 ``admitted=False`` 时调用方**必须**在发请求前停手；不许"先发出去
        再说额度不够" —— 那正是本护栏要防的事。
        """

        if purpose == usage_scope.PURPOSE_SAFETY:
            # 安全用途只计量、不准入：护栏不能成为停掉审核的开关。
            return Reservation(
                reservation_id=None,
                admitted=True,
                enforced=False,
                reason=REASON_SAFETY,
            )
        try:
            budget_row = await self._budget_for(int(group_id))
        except Exception:
            # 存储故障：**已知**全局没有配过预算就放行（本来就没有护栏要守），
            # 否则 fail closed。区分这两者靠 ``_any_budget_seen``，否则一次
            # 数据库抖动会让所有群的可选功能全停。
            if self._any_budget_seen is False:
                log.info(
                    "budget store unavailable but no budget is configured | group=%s",
                    group_id,
                )
                return Reservation(
                    reservation_id=None,
                    admitted=True,
                    enforced=False,
                    reason=REASON_DISABLED,
                )
            log.warning("budget lookup unavailable | group=%s", group_id, exc_info=True)
            return Reservation(
                reservation_id=None,
                admitted=False,
                enforced=True,
                reason=REASON_STORE_UNAVAILABLE,
            )
        if budget_row is None or not bool(budget_row.enabled):
            # **off 快路径**：这里一次数据库写入都没有（只用了上面那次只读缓存）。
            return Reservation(
                reservation_id=None,
                admitted=True,
                enforced=False,
                reason=REASON_DISABLED,
            )

        catalog = self._catalog_provider()
        entry = catalog.find(provider_key=provider, model=model)
        snapshot = _snapshot_of(entry)
        if budget_row.daily_money_micro_limit is not None:
            # 金额上限必须**在预留时**就绑定到实际候选，而不是只在保存预算时
            # 检查一次 ``money_covers``：保存之后换模型、走到没配价的回退、
            # 或者 prompt_tokens 为空，都会让金额上界根本不存在。
            if entry is None:
                return Reservation(
                    reservation_id=None,
                    admitted=False,
                    enforced=True,
                    reason=REASON_MONEY_UNPRICED,
                )
            budget_currency = str(budget_row.currency or "").strip().upper()
            if budget_currency and entry.currency != budget_currency:
                return Reservation(
                    reservation_id=None,
                    admitted=False,
                    enforced=True,
                    reason=REASON_MONEY_CURRENCY,
                )
            estimate = pricing.conservative_upper_bound(
                entry, prompt_tokens=prompt_tokens, max_output_tokens=max_output_tokens
            )
            if estimate.micro is None:
                return Reservation(
                    reservation_id=None,
                    admitted=False,
                    enforced=True,
                    reason=REASON_MONEY_UNPRICED,
                )
        else:
            estimate = pricing.conservative_upper_bound(
                entry, prompt_tokens=prompt_tokens, max_output_tokens=max_output_tokens
            )
        reserved_money = int(estimate.micro or 0)
        # 在预留边界严格校验：坏值在这里就被拒，不会走到"夹成 0 后放行"。
        # 校验放在事务**之前**，坏输入不该占住写锁。
        try:
            reserved_prompt, prompt_unknown = _reserve_count_or_unknown(
                prompt_tokens, "prompt_tokens"
            )
            reserved_output, output_unknown = _reserve_count_or_unknown(
                max_output_tokens, "max_output_tokens"
            )
            bound_in_force = any(
                value is not None
                for value in (
                    budget_row.daily_token_limit,
                    budget_row.daily_money_micro_limit,
                )
            )
            if (prompt_unknown or output_unknown) and bound_in_force:
                # 明确设了 token / 金额上限，却给了"未知"的入参：报不出上界就
                # 不能声称上限守得住。**不**把它当成 0 —— 0 是一条合法的已知值，
                # 混用会让一次未知输入静默变成免费调用。
                raise BudgetError(
                    "已启用额度上限时不能使用未知的 token 上界。"
                )
        except BudgetError as exc:
            log.info(
                "budget reservation input rejected | group=%s stage=%s error=%s",
                group_id, stage, exc,
            )
            return Reservation(
                reservation_id=None,
                admitted=False,
                enforced=True,
                reason=REASON_STORE_UNAVAILABLE,
            )
        reserved_tokens = reserved_prompt + reserved_output
        today = _today()

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await _ensure_day_row(session, int(group_id), today)
                    if not await _bump_day(
                        session,
                        group_id=int(group_id),
                        budget_date=today,
                        attempts=1,
                        tokens=reserved_tokens,
                        money_micro=reserved_money,
                        attempt_limit=budget_row.daily_attempt_limit,
                        token_limit=budget_row.daily_token_limit,
                        money_limit=budget_row.daily_money_micro_limit,
                    ):
                        raise _Denied(await _day_state(session, int(group_id), today), budget_row)
                    # 阈值告警：只在**同一个短事务**里登记一条 outbox 事件。
                    # 没有网络调用、没有 sleep、没有等待；消费者在事务外发送。
                    if _ALERT_ENQUEUER is not None:
                        try:
                            await _ALERT_ENQUEUER(
                                session,
                                group_id=group_id,
                                budget_date=today,
                                day=await _day_state(session, group_id, today),
                                budget_row=budget_row,
                            )
                        except Exception:  # noqa: BLE001 - 告警绝不挡住额度判定
                            log.info(
                                "budget alert enqueue failed | group=%s", group_id,
                                exc_info=True,
                            )
                    row = GroupBudgetReservation(
                        group_id=int(group_id),
                        budget_date=today,
                        purpose=purpose,
                        stage=str(stage or "")[:32],
                        provider=str(provider or "")[:48],
                        model=str(model or "")[:128],
                        # 用**已校验**的值落库：此前这里再 int() 一次，坏值
                        # 到这一步已经变成 0，审计看不出当时传的是什么。
                        reserved_prompt_tokens=reserved_prompt,
                        reserved_output_tokens=reserved_output,
                        reserved_money_micro=reserved_money,
                        price_version=catalog.version,
                        price_snapshot=snapshot,
                        currency=str(estimate.currency or "")[:8],
                        status=STATUS_RESERVED,
                        created_at=now_shanghai_naive(),
                        lease_expires_at=now_shanghai_naive()
                        + timedelta(seconds=RESERVATION_LEASE_SECONDS),
                    )
                    session.add(row)
                    await session.flush()
                    reservation_id = int(row.reservation_id)
        except _Denied as denied:
            return denied.reservation()
        except BudgetError as exc:
            # 非法预留输入：护栏真的启用时明确拒绝（fail closed），
            # 而不是把它变成一次免费调用。
            log.info(
                "budget reservation input rejected | group=%s stage=%s error=%s",
                group_id, stage, exc,
            )
            return Reservation(
                reservation_id=None,
                admitted=False,
                enforced=True,
                reason=REASON_STORE_UNAVAILABLE,
            )
        except Exception:
            log.warning(
                "budget reservation failed | group=%s stage=%s", group_id, stage, exc_info=True
            )
            # 护栏已启用却核验不了：明确拒绝，不偷偷放行。
            return Reservation(
                reservation_id=None,
                admitted=False,
                enforced=True,
                reason=REASON_STORE_UNAVAILABLE,
            )

        return Reservation(
            reservation_id=reservation_id,
            admitted=True,
            enforced=True,
            reserved_money_micro=reserved_money,
            reserved_tokens=reserved_tokens,
            currency=str(estimate.currency or ""),
            price_version=catalog.version,
        )

    async def settle(
        self,
        reservation_id: int | None,
        *,
        usage: Any = None,
        unknown: bool = False,
    ) -> bool:
        """结算预留：把预留换成真实用量，或标 unknown。**精确一次**。

        幂等靠**条件 UPDATE 抢占状态**（``WHERE status='reserved'`` 且
        rowcount==1），不是先读后写：后者在并发下两个事务都读到 reserved，
        于是退两次 —— 额度凭空多出来。
        """

        if reservation_id is None:
            return False
        async with self._session_factory() as session:
            async with session.begin():
                # 允许把 **unknown** 行也抢占过来做对账：上游吞掉取消后迟到
                # 返回真实 usage 时，预算必须能从"保守占用"收敛成"已知"。
                # 只认 reserved 的话那次预留会永远停在 unknown —— 遥测补上了，
                # 预算却没有。
                # 先读出**抢占前**的状态，且用独立标量查询绕开 identity map：
                # CAS 用 synchronize_session=False，之后 session.get 返回的仍是
                # 那个"旧"对象，既判断不出原本是 unknown，也会让后面的 status
                # 赋值不被当成脏数据、SQL 压根不写。
                # 抢占与「先前状态」必须是**同一个原子状态转移**（不是"先读再写"）。
                #
                # 之前是「先 SELECT 再条件 UPDATE」，而那时还没拿到项目写锁：
                # 两个并发调用会同时读到 reserved，于是各自都以为自己是第一次
                # 转移 —— 两次 unknown 各记一次 day.unknown_attempts，
                # unknown→known 那条也永远减不回 unknown。
                #
                # 现在用**互斥的条件 UPDATE** 让 winner 由数据库判定：
                #   1) 从 reserved/settling 转过来 → rowcount=1 表示**这次**发生了
                #      转移，该记 unknown；
                #   2) 本来就是 unknown → 走第二条，只更新字段、不重复计数；
                #   3) settled/released → 不可抢占。
                # 整个过程没有"先读后写"，也不存在把读屏障放进持写锁事务的窗口。
                first_claim = await session.execute(
                    update(GroupBudgetReservation)
                    .where(
                        GroupBudgetReservation.reservation_id == int(reservation_id),
                        GroupBudgetReservation.status.in_(
                            (STATUS_RESERVED, STATUS_SETTLING)
                        ),
                    )
                    .values(status="settling", settled_at=now_shanghai_naive())
                    .execution_options(synchronize_session=False)
                )
                was_unknown = False
                if int(first_claim.rowcount or 0) != 1:
                    second_claim = await session.execute(
                        update(GroupBudgetReservation)
                        .where(
                            GroupBudgetReservation.reservation_id == int(reservation_id),
                            GroupBudgetReservation.status == STATUS_UNKNOWN,
                        )
                        .values(settled_at=now_shanghai_naive())
                        .execution_options(synchronize_session=False)
                    )
                    if int(second_claim.rowcount or 0) != 1:
                        await session.rollback()
                        return False
                    was_unknown = True
                row = await session.get(GroupBudgetReservation, int(reservation_id))
                if row is None:  # pragma: no cover - 抢占成功必然存在
                    return False
                # 注意：这里**不能**靠 ORM 的脏标记写最终状态。上面两条
                # 抢占都是 synchronize_session=False，identity map 里的对象没被
                # 标脏，改属性可能压根不产生 SQL（行就停在 settling）；而
                # session.expire(row) 又会在后续读属性时触发懒加载、在
                # greenlet 之外炸 MissingGreenlet。下面的最终状态一律用显式
                # Core UPDATE 写，不依赖 ORM 脏标记。

                tokens = _usage_tokens(usage)
                if unknown or not tokens["complete"]:
                    # usage 缺失/畸形：**不退款**。曾经 ``_num`` 把缺字段当 0，
                    # 于是 settle 一次就把整笔预留退光，而那次调用其实花钱了。
                    #
                    # 重复的 unknown 通知**不得**再累加一次：同一笔预留被上报两次
                    # 未知，不代表发生了两次未知的调用。
                    if not was_unknown:
                        await self._mark_unknown(session, row)
                    await session.execute(
                        update(GroupBudgetReservation)
                        .where(
                            GroupBudgetReservation.reservation_id == int(reservation_id)
                        )
                        .values(
                            status=STATUS_UNKNOWN,
                            actual_tokens=None,
                            actual_money_micro=None,
                            settled_at=now_shanghai_naive(),
                        )
                        .execution_options(synchronize_session=False)
                    )
                    return True

                actual_tokens = int(tokens["prompt"] + tokens["completion"])
                estimate = pricing.estimate(
                    _entry_from_snapshot(getattr(row, "price_snapshot", None)),
                    prompt_tokens=int(tokens["prompt"]),
                    output_tokens=int(tokens["completion"]),
                    cached_tokens=int(tokens["cached"]),
                    cache_write_tokens=int(tokens["cache_write"]),
                )
                if was_unknown:
                    # 之前记的 unknown 现在被真实用量顶掉，计数要同步减回去。
                    await session.execute(
                        update(GroupBudgetDay)
                        .where(
                            GroupBudgetDay.group_id == int(row.group_id),
                            GroupBudgetDay.budget_date == str(row.budget_date),
                            GroupBudgetDay.unknown_attempts > 0,
                        )
                        .values(
                            unknown_attempts=GroupBudgetDay.unknown_attempts - 1
                        )
                        .execution_options(synchronize_session=False)
                    )
                if estimate.known:
                    actual_money: int | None = int(estimate.micro or 0)
                else:
                    actual_money = None
                await session.execute(
                    update(GroupBudgetReservation)
                    .where(
                        GroupBudgetReservation.reservation_id == int(reservation_id)
                    )
                    .values(
                        status=STATUS_SETTLED,
                        actual_tokens=actual_tokens,
                        actual_money_micro=actual_money,
                        settled_at=now_shanghai_naive(),
                    )
                    .execution_options(synchronize_session=False)
                )
                await self._apply_settle_delta(
                    session,
                    row,
                    delta_tokens=actual_tokens - int(
                        row.reserved_prompt_tokens or 0
                    ) - int(row.reserved_output_tokens or 0),
                    delta_money=(
                        (actual_money - int(row.reserved_money_micro or 0))
                        if actual_money is not None
                        else 0
                    ),
                )
                # 结算后也要判阈值：真实用量只有 settle 才确定，reserve 里那份
                # 只是**预估上界**。只挂 reserve 会漏掉"预估很小、实际很大"这类
                # （父探针：预估 3 token、真实 852 token、limit 1000）。
                if _ALERT_ENQUEUER is not None:
                    try:
                        await _ALERT_ENQUEUER(
                            session,
                            group_id=int(row.group_id),
                            budget_date=str(row.budget_date),
                            day=await _day_state(
                                session, int(row.group_id), str(row.budget_date)
                            ),
                            # 同一事务里读配置：``get_budget`` 会开**新会话**，
                            # 在已经持写锁的事务中那样做等于把读挪到别的事务上，
                            # 既可能自锁也会让"按哪份配置判阈值"变得不确定。
                            budget_row=await session.get(
                                GroupBudget, int(row.group_id)
                            ),
                        )
                    except Exception:  # noqa: BLE001 - 告警绝不挡住结算
                        log.info(
                            "budget alert enqueue after settle failed | group=%s",
                            row.group_id,
                            exc_info=True,
                        )
                return True

    async def _mark_unknown(
        self, session: AsyncSession, row: GroupBudgetReservation
    ) -> None:
        await session.execute(
            update(GroupBudgetDay)
            .where(
                GroupBudgetDay.group_id == int(row.group_id),
                GroupBudgetDay.budget_date == str(row.budget_date),
            )
            .values(unknown_attempts=GroupBudgetDay.unknown_attempts + 1)
            .execution_options(synchronize_session=False)
        )

    async def _apply_settle_delta(
        self,
        session: AsyncSession,
        row: GroupBudgetReservation,
        *,
        delta_tokens: int,
        delta_money: int,
    ) -> None:
        # 用 CASE 夹到 0：真实用量比预留小（回退时估多了）会产生负增量，
        # 累计被压成负数会让额度凭空多出来。
        await session.execute(
            update(GroupBudgetDay)
            .where(
                GroupBudgetDay.group_id == int(row.group_id),
                GroupBudgetDay.budget_date == str(row.budget_date),
            )
            .values(
                used_tokens=_floor_zero(GroupBudgetDay.used_tokens + delta_tokens),
                used_money_micro=_floor_zero(
                    GroupBudgetDay.used_money_micro + delta_money
                ),
            )
            .execution_options(synchronize_session=False)
        )

    async def release(
        self, reservation_id: int | None,
        *,
        reason: str = "",
    ) -> bool:
        """退回预留（**发送前**取消 / 被上游拒绝）。幂等且精确一次。

        ``reason`` 非空表示"这次其实**已经发出**了、只是结果未知" —— 那是
        unknown 而不是释放：把已发出的调用当成没发，等于每次超时都送一次
        免费额度。
        """

        if reservation_id is None:
            return False
        async with self._session_factory() as session:
            async with session.begin():
                target = (
                    STATUS_UNKNOWN if reason else STATUS_RELEASED
                )
                claimed = await session.execute(
                    update(GroupBudgetReservation)
                    .where(
                        GroupBudgetReservation.reservation_id == int(reservation_id),
                        GroupBudgetReservation.status == STATUS_RESERVED,
                    )
                    .values(status=target, settled_at=now_shanghai_naive())
                    .execution_options(synchronize_session=False)
                )
                if int(claimed.rowcount or 0) != 1:
                    return False
                row = await session.get(GroupBudgetReservation, int(reservation_id))
                if row is None:  # pragma: no cover
                    return False
                if target == STATUS_UNKNOWN:
                    await self._mark_unknown(session, row)
                    return True
                await _bump_day(
                    session,
                    group_id=int(row.group_id),
                    budget_date=str(row.budget_date),
                    attempts=-1,
                    tokens=-(
                        int(row.reserved_prompt_tokens or 0)
                        + int(row.reserved_output_tokens or 0)
                    ),
                    money_micro=-int(row.reserved_money_micro or 0),
                    attempt_limit=None,
                    token_limit=None,
                    money_limit=None,
                    clamp=True,
                )
                return True

    async def reap_stale_reservations(self, group_id: int | None = None) -> int:
        """回收过期租约（进程崩掉留下的预留），不退还 attempt，只标 unknown。

        只标 unknown 而**不**退还 attempt：租约过期只说明我们不知道那次调用
        到底发没发出去，而"没发出去"是不可证的假设。
        """

        now = now_shanghai_naive()
        stmt = (
            select(GroupBudgetReservation)
            .where(
                GroupBudgetReservation.status == STATUS_RESERVED,
                GroupBudgetReservation.lease_expires_at.is_not(None),
                GroupBudgetReservation.lease_expires_at <= now,
            )
            .limit(256)
        )
        if group_id is not None:
            stmt = stmt.where(GroupBudgetReservation.group_id == int(group_id))
        async with self._session_factory() as session:
            async with session.begin():
                rows = list((await session.scalars(stmt)).all())
                for row in rows:
                    row.status = STATUS_UNKNOWN
                    row.settled_at = now
                    await session.execute(
                        update(GroupBudgetDay)
                        .where(
                            GroupBudgetDay.group_id == int(row.group_id),
                            GroupBudgetDay.budget_date == str(row.budget_date),
                        )
                        .values(
                            unknown_attempts=GroupBudgetDay.unknown_attempts + 1
                        )
                        .execution_options(synchronize_session=False)
                    )
        return len(rows)

    # ------------------------------------------------------------------
    # 操作次数配额（非 LLM）
    # ------------------------------------------------------------------
    async def consume_operation(self, group_id: int, *, feature: str) -> Reservation:
        """功能操作次数配额。TTS / 媒体 / 搜索没有可靠账单字段，走次数。"""

        name = str(feature or "").strip()
        try:
            budget_row = await self._budget_for(int(group_id))
        except Exception:
            return Reservation(None, False, True, REASON_STORE_UNAVAILABLE)
        if budget_row is None or not bool(budget_row.enabled):
            return Reservation(None, True, False, REASON_DISABLED)
        limit = (budget_row.operation_limits or {}).get(name)
        if limit is None:
            return Reservation(None, True, False, REASON_NOT_CONFIGURED)
        today = _today()
        async with self._session_factory() as session:
            async with session.begin():
                await _ensure_day_row(session, int(group_id), today)
                day = await session.get(GroupBudgetDay, (int(group_id), today))
                used = dict((day.used_operations or {}) if day else {})
                current = int(used.get(name, 0) or 0)
                if current >= int(limit):
                    return Reservation(
                        None, False, True, REASON_OPERATION
                    )
                used[name] = current + 1
                await session.execute(
                    update(GroupBudgetDay)
                    .where(
                        GroupBudgetDay.group_id == int(group_id),
                        GroupBudgetDay.budget_date == today,
                    )
                    .values(used_operations=used)
                    .execution_options(synchronize_session=False)
                )
                # 操作额度也要告警：与 reserve 同理，只在**同一个短事务**里登记
                # outbox 事件，没有网络调用、没有等待。
                if _ALERT_ENQUEUER is not None:
                    try:
                        await _ALERT_ENQUEUER(
                            session,
                            group_id=int(group_id),
                            budget_date=today,
                            # 上面那条 UPDATE 用了 synchronize_session=False，
                            # ``day`` 仍是**旧值** —— 直接交给 enqueuer 会让它按
                            # 少一次的用判阈值（第二个 feature 永远看不到自己
                            # 的那一次）。这里显式构造本次更新后的完整快照。
                            day=_DaySnapshot(
                                used_attempts=int(day.used_attempts or 0),
                                used_tokens=int(day.used_tokens or 0),
                                used_money_micro=int(day.used_money_micro or 0),
                                used_operations=dict(used),
                            ),
                            budget_row=budget_row,
                        )
                    except Exception:  # noqa: BLE001 - 告警绝不挡住额度判定
                        log.info(
                            "operation budget alert enqueue failed | group=%s %s",
                            group_id, name, exc_info=True,
                        )
        # 非 LLM 操作没有可靠账单字段：admitted 但 metered 明确为 False。
        return Reservation(
            reservation_id=None, admitted=True, enforced=True, metered=False
        )

    # ------------------------------------------------------------------
    # 读侧
    # ------------------------------------------------------------------
    async def day_state(self, group_id: int, budget_date: str | None = None) -> DayState:
        async with self._session_factory() as session:
            return await _day_state(session, int(group_id), budget_date or _today())

    async def snapshot(self, group_id: int) -> dict[str, Any]:
        """给 UI 用的状态：包括「当前到底在不在保护」。"""

        budget_row = await self.get_budget(group_id)
        state = await self.day_state(group_id)
        enabled = bool(budget_row.enabled) if budget_row else False
        limits = {
            "daily_attempt_limit": budget_row.daily_attempt_limit if budget_row else None,
            "daily_token_limit": budget_row.daily_token_limit if budget_row else None,
            "daily_money_micro_limit": (
                budget_row.daily_money_micro_limit if budget_row else None
            ),
            "operation_limits": dict(budget_row.operation_limits or {})
            if budget_row
            else {},
        }
        # 注意 ``0`` 是**有效**的零额度，不是"未设置"。曾经用
        # ``value not in (None, {}, 0)`` 判"有没有额度"，于是上限 0 的群显示成
        # 未启用 —— 而它其实什么都发不出去。
        any_limit = any(
            value is not None and value != {} for value in limits.values()
        )
        return {
            "group_id": int(group_id),
            "enabled": enabled,
            "protecting": bool(enabled and any_limit),
            "revision": int(budget_row.revision) if budget_row else 0,
            "currency": str(budget_row.currency or "") if budget_row else "",
            "warn_ratio": float(budget_row.warn_ratio) if budget_row else 0.8,
            "pause_ratio": float(budget_row.pause_ratio) if budget_row else 1.0,
            "limits": limits,
            "today": state.document(),
            "budget_date": state.budget_date,
        }


class _Denied(Exception):
    def __init__(self, state: DayState, budget_row: GroupBudget) -> None:
        super().__init__("budget denied")
        self._state = state
        self._budget = budget_row

    def reservation(self) -> Reservation:
        limit = self._budget.daily_attempt_limit
        if limit is not None and self._state.used_attempts >= int(limit):
            reason = REASON_ATTEMPTS
        elif (
            self._budget.daily_token_limit is not None
            and self._state.used_tokens >= int(self._budget.daily_token_limit)
        ):
            reason = REASON_TOKENS
        else:
            reason = REASON_MONEY
        return Reservation(
            reservation_id=None, admitted=False, enforced=True, reason=reason
        )


#: 进程内**唯一**的护栏服务。启动装配与 HTTP 接口必须共用同一个实例：
#: 曾经它们各自 new 一个，于是 HTTP 保存的预算既不会让启动装的 guard 失效，
#: 后者的"全局没有预算"又永久缓存着 —— 启用预算在真实链路上完全不生效。
_SHARED_SERVICE: GroupBudgetService | None = None


def shared_service(
    session_factory: Any = None,
    catalog_provider: Callable[[], pricing.PriceCatalog] | None = None,
) -> GroupBudgetService:
    """返回进程内共享的护栏服务（首次创建，之后复用并重绑工厂）。

    不传参数 = 只取已经装配好的那个实例。调用入口（如技能功能额度）必须走这里：
    自己 new 一份就与 HTTP 侧缓存各说各话，热更新 0→2→0 也不会立刻生效。
    """

    global _SHARED_SERVICE
    if session_factory is None and catalog_provider is None:
        if _SHARED_SERVICE is None:
            raise BudgetError("预算护栏尚未装配。")
        return _SHARED_SERVICE
    if _SHARED_SERVICE is None:
        _SHARED_SERVICE = GroupBudgetService(
            session_factory=session_factory,
            catalog_provider=catalog_provider,
        )
    else:
        _SHARED_SERVICE.configure(session_factory)
        _SHARED_SERVICE.set_catalog_provider(catalog_provider)
    return _SHARED_SERVICE


def reset_shared_service() -> None:
    """测试与"模拟重启"用：丢掉共享实例与它的缓存。"""

    global _SHARED_SERVICE
    _SHARED_SERVICE = None


def set_catalog_provider(self: Any, provider: Callable[[], pricing.PriceCatalog]) -> None:
    self._catalog_provider = provider


def _reserve_count(value: Any, field: str) -> int:
    """预留边界的 token 数：**只接受非负整数**，其它一律拒绝。

    父探针：``prompt_tokens=-200`` 仍 admitted=True、used_tokens=0 ——
    ``_safe_add`` 把负数夹成 0，等于「负输入 = 白送一次调用」。而真正该做的是
    在**预留入口**拒掉它。

    这里刻意**不**把非法值静默变成 0：0 是一条合法值（"空请求"），把坏值
    映射成它，就再也分不清「本来就没有 token」和「有人传了个坏值」。
    """

    if isinstance(value, bool):
        raise BudgetError(f"{field} 必须是 0 或正整数。")
    if isinstance(value, float):
        # **所有** float 都拒，包括 10.0 这种"看起来是整数"的：JSON 解析出来
        # 的 10.0 与 10 不该在护栏上被当成两件事，而 strict_int 早就统一了
        # 这个口径（``int(10.0)`` 会静默截断）。这里再放行一次等于把同一个
        # 合同在两层里各写一遍。
        raise BudgetError(f"{field} 必须是 0 或正整数（不接受浮点）。")
    if isinstance(value, int):
        if value < 0:
            raise BudgetError(f"{field} 不能为负。")
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise BudgetError(f"{field} 必须是 0 或正整数。")
        return int(text)
    raise BudgetError(f"{field} 必须是 0 或正整数。")


def _reserve_count_or_unknown(value: Any, field: str) -> tuple[int, bool]:
    """返回 (计数, 是否显式未知)。

    ``None`` 是**显式未知**，与「省略参数」不同：函数签名里省略会拿到默认的
    0（合法已知零），而显式传 None 是在说"我不知道这个上界"。两者混为一谈
    就会让 token-only 上限下的未知输入变成一次白送 —— 父探针实测
    ``prompt_tokens=None`` 仍 admitted、durable used_tokens=0。
    """

    if value is None:
        return 0, True
    return _reserve_count(value, field), False


def _usage_tokens(usage: Any) -> dict[str, Any]:
    """严格解析一次响应的 token 账。

    三态而不是二态：

    * **缺**（上游协议里该字段可选）→ 该段未知，**不**因此退掉已确认的用量；
    * **坏**（字段在、但类型/取值不合法：负数、bool、浮点、nan/inf、非数字串）
      → 整次结算标 unknown，预留**保留**。把坏值当成 0 等于白送一次调用；
    * **好** → 计入。

    ``complete=False`` 时调用方必须走 unknown 分支：缺任一必需字段或任一字段
    坏值都不允许退额度。
    """

    if usage is None:
        return {
            "complete": False, "bad": False,
            "prompt": 0, "completion": 0, "cached": 0, "cache_write": 0,
        }

    def pick(*names: str):
        """返回 (是否在, 值)；值在但不合法时值为 None。"""

        for name in names:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            if value is None:
                continue
            if isinstance(value, bool):
                return True, None
            if isinstance(value, float):
                if not math.isfinite(value) or value != int(value) or value < 0:
                    return True, None
                return True, int(value)
            if isinstance(value, int):
                return True, (value if value >= 0 else None)
            if isinstance(value, str):
                text = value.strip()
                if not text.isdigit():
                    return True, None
                return True, int(text)
            return True, None
        return False, None

    prompt_seen, prompt = pick("prompt_tokens", "input_tokens")
    completion_seen, completion = pick("completion_tokens", "output_tokens")
    cached_seen, cached = pick(
        "cached_tokens", "cache_read_input_tokens", "cache_read_tokens"
    )
    write_seen, write = pick(
        "cache_creation_input_tokens", "cache_write_tokens"
    )
    bad = any(
        seen and value is None
        for seen, value in (
            (prompt_seen, prompt),
            (completion_seen, completion),
            (cached_seen, cached),
            (write_seen, write),
        )
    )
    complete = (
        prompt_seen and completion_seen and prompt is not None
        and completion is not None and not bad
    )
    return {
        "complete": complete,
        "bad": bad,
        "prompt": prompt or 0,
        "completion": completion or 0,
        "cached": cached or 0,
        "cache_write": write or 0,
    }


def _num(usage: Any, *names: str) -> int:
    for name in names:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def _floor_zero(expression: Any):
    """``CASE WHEN <expr> < 0 THEN 0 ELSE <expr> END``（退款不许把累计压成负数）。"""

    from sqlalchemy import case

    return case((expression < 0, 0), else_=expression)


async def _ensure_day_row(
    session: AsyncSession, group_id: int, budget_date: str
) -> None:
    await session.execute(
        sqlite_insert(GroupBudgetDay)
        .values(group_id=int(group_id), budget_date=str(budget_date))
        .on_conflict_do_nothing(index_elements=["group_id", "budget_date"])
    )


async def _bump_day(
    session: AsyncSession,
    *,
    group_id: int,
    budget_date: str,
    attempts: int,
    tokens: int,
    money_micro: int,
    attempt_limit: int | None,
    token_limit: int | None,
    money_limit: int | None,
    clamp: bool = False,
) -> bool:
    """一次条件 UPDATE 完成预留判定。

    条件写在 UPDATE 的 WHERE 里而不是 Python 里，是为了让 SQLite 在写锁内
    原子求值 —— 否则「读一下当前值再判断」在并发下必然超发。
    """

    conditions = [GroupBudgetDay.group_id == int(group_id), GroupBudgetDay.budget_date == str(budget_date)]
    if attempt_limit is not None:
        conditions.append(
            GroupBudgetDay.used_attempts + attempts <= int(attempt_limit)
        )
    if token_limit is not None:
        conditions.append(GroupBudgetDay.used_tokens + tokens <= int(token_limit))
    if money_limit is not None:
        conditions.append(
            GroupBudgetDay.used_money_micro + money_micro <= int(money_limit)
        )
    values: dict[str, Any] = {
        "used_attempts": _clamp(GroupBudgetDay.used_attempts + attempts, clamp),
        "used_tokens": _clamp(GroupBudgetDay.used_tokens + tokens, clamp),
    }
    if money_micro:
        values["used_money_micro"] = _clamp(
            GroupBudgetDay.used_money_micro + money_micro, clamp  # type: ignore[attr-defined]
        )
    result = await session.execute(
        update(GroupBudgetDay).where(*conditions).values(**values)
    )
    return int(result.rowcount or 0) == 1


def _clamp(expression: Any, enabled: bool):
    return _floor_zero(expression) if enabled else expression


async def _day_state(
    session: AsyncSession, group_id: int, budget_date: str
) -> DayState:
    row = await session.get(GroupBudgetDay, (int(group_id), str(budget_date)))
    if row is None:
        return DayState(int(group_id), str(budget_date), 0, 0, 0, 0, {})
    return DayState(
        group_id=int(group_id),
        budget_date=str(budget_date),
        used_attempts=int(row.used_attempts or 0),
        used_tokens=int(row.used_tokens or 0),
        used_money_micro=int(row.used_money_micro or 0),
        unknown_attempts=int(row.unknown_attempts or 0),
        used_operations=dict(row.used_operations or {}),
    )


# ---------------------------------------------------------------------------
# LLM 调用点的接线
# ---------------------------------------------------------------------------
def llm_budget_guard(service: GroupBudgetService) -> dict[str, Any]:
    """给 ``LLMService.chat`` / ``complete_with_tools`` 的 ``**kwargs``。

    每次真实尝试（含重试与回退）各自预留；发送前被拒就不发。
    """

    def _guard(provider: str = "", model: str = "") -> Any:
        return CostGuard(service, provider_key=provider, model=model)

    return _guard


class UnavailableCostGuard:
    """装配失败时的护栏：**可选**功能拒绝，**安全**调用照常。

    之前这里直接把 guard 留成 None，等于"没有护栏" —— 数据库里明明有一条
    cap=0 的上限，却照样每次都真的发请求。装配失败是一种"不确定有没有在保护"
    的状态，不能当成"确定没保护"。
    """

    def __init__(self, error: str = "budget guard unavailable") -> None:
        self.error = error

    async def admit(self, **_: Any) -> Reservation:
        scope = usage_scope.current_usage_scope()
        purpose = usage_scope.effective_purpose(scope, _.get("stage", ""))
        if purpose == usage_scope.PURPOSE_SAFETY:
            return Reservation(
                reservation_id=None,
                admitted=True,
                enforced=False,
                reason=REASON_SAFETY,
            )
        return Reservation(
            reservation_id=None,
            admitted=False,
            enforced=True,
            reason=REASON_STORE_UNAVAILABLE,
        )

    async def settle(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def release(self, *_args: Any, **_kwargs: Any) -> bool:
        return False


class CostGuard:
    """LLMService 在每次**真正发出**之前问一次护栏。"""

    def __init__(
        self,
        service: GroupBudgetService,
        *,
        provider_key: str = "",
        model: str = "",
    ) -> None:
        self.service = service
        self.provider_key = provider_key
        self.model = model

    async def settle(
        self, reservation_id: int | None, *, usage: Any = None, unknown: bool = False
    ) -> None:
        await self.service.settle(reservation_id, usage=usage, unknown=unknown)

    async def release(self, reservation_id: int | None) -> bool:
        return await self.service.release(reservation_id)

    async def admit(
        self,
        *,
        stage: str,
        purpose: str | None = None,
        prompt_tokens: int = 0,
        max_output_tokens: int = 0,
        provider: str | None = None,
        model: str | None = None,
    ) -> Reservation:
        """每次真实尝试问一次护栏。

        ``provider`` / ``model`` 由调用方（LLMService）按**这一次**的实际候选
        传入；留才退回构造 guard 时给的固定身份。
        """

        scope = usage_scope.current_usage_scope()
        return await self.service.reserve(
            group_id=int(scope.group_id) if scope.group_id is not None else 0,
            purpose=purpose or usage_scope.effective_purpose(scope, stage),
            stage=stage,
            provider=provider or self.provider_key,
            model=model or self.model,
            prompt_tokens=prompt_tokens,
            max_output_tokens=max_output_tokens,
        )