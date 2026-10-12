"""运营任务中心写侧安全内核（第三优先 B13a）。

这个模块只做**执行请求的记账、单次确认、取消与对账**，业务动作全部交给
``bot.services.operations_tasks`` 里的六类任务适配器。它存在的理由是让写侧的四件事
在代码里无法被混淆：

1. **预览只读。** ``preview`` 不写审计、不占位、不调用模型、不发消息、不动积分。
   预览令牌是带签名的**参数绑定**，不是"待执行工单"。
2. **确认 = 一次真实执行请求。** ``confirm`` 最多插入一行 ``accepted`` 并返回
   ``run_key``；它**不代表任务已经完成**，完成与否只看逐项结果与业务台账对账。
3. **单次令牌。** ``operations_runs.token_id`` 上的唯一索引 + HMAC 签名 + 有效期：
   同一个预览令牌无论被确认多少次、被谁确认，都只可能有一个 run；重复确认返回
   **同一个 run**，不会再执行一次业务动作。
4. **四态分离。** 已成功 / 在途 / 明确失败 / **未知外部结果**。未知绝不自动重发，
   管理员点重试也不覆盖；``OperationsRunItem.resendable`` 是写侧结论而不是 UI 建议。

守卫（权限、参数、业务水位、enabled、配置 revision）在**确认的实际写事务里**复核：
先 INSERT 拿到 SQLite 写锁，再在同一个事务里读守卫，然后才 commit —— 不是"先读
revision 再异步排队"那种看起来像 CAS 的假守卫。执行在 commit 之后才开始。

取消等真实 worker 收尾：``request_cancel`` 只置位取消标志并等待在跑的 asyncio
任务自然结束（有界等待），已发生的副作用不撤销、不退款、不假装没有。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import OperationsRun, OperationsRunItem
from bot.services.operations_task_status import TASK_IDS
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

RUN_STATUS_ACCEPTED = "accepted"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_PARTIAL = "partial"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_UNKNOWN = "unknown"
#: 手动触发时"确实没有可做的事"（已发过 / 没到点 / 明确 disabled / 退避中）。
#: 单独一个终态，避免把"什么都没做"包装成 succeeded。
RUN_STATUS_NO_WORK = "no_work"
#: 这些终态的 run 不需要（也说不清还能）再对账；``unknown``/``failed``/``partial``
#: 仍可对账，因为它们的逐项结论需要业务台账来收敛。
RECONCILED_RUN_STATUSES = (
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_NO_WORK,
)

TERMINAL_RUN_STATUSES = (
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED,
    # 未知也是终态：不再自动推进，只能由管理员显式重试（而且只有可安全重试的项）。
    RUN_STATUS_UNKNOWN,
    RUN_STATUS_NO_WORK,
)

ITEM_STATUS_SUCCEEDED = "succeeded"
ITEM_STATUS_SKIPPED = "skipped"
ITEM_STATUS_FAILED = "failed"
ITEM_STATUS_UNKNOWN = "unknown"
ITEM_STATUS_CANCELLED = "cancelled"

REASON_OK = ""
REASON_UNKNOWN_TASK = "unknown_task"
REASON_TOKEN_INVALID = "preview_token_invalid"
REASON_TOKEN_EXPIRED = "preview_token_expired"
REASON_ACTOR_MISMATCH = "preview_actor_mismatch"
REASON_GUARD_BLOCKED = "guard_blocked"
REASON_GROUP_MISMATCH = "group_mismatch"
REASON_TASK_MISMATCH = "task_mismatch"
REASON_NOT_FOUND = "not_found"
REASON_UNSUPPORTED_SCOPE = "unsupported_scope"
REASON_DISABLED = "disabled"
REASON_NO_WORK = "no_work"
REASON_BACKOFF = "backoff"
REASON_QUEUE_FULL = "queue_full"
REASON_RUNNER_UNAVAILABLE = "runner_unavailable"
REASON_NOT_DUE = "not_due"
REASON_ALREADY_DELIVERED = "already_delivered"
REASON_ALREADY_SUCCEEDED = "already_succeeded"
REASON_CANCEL_REQUESTED = "cancel_requested"
REASON_NOT_SAFELY_RESENDABLE = "unknown_not_safely_resendable"
REASON_EXTERNAL_UNKNOWN = "external_result_unknown"
REASON_BOOKKEEPING_FAILED = "sent_bookkeeping_failed"
REASON_GENERATION_FAILED = "generation_failed"
REASON_ADAPTER_CRASHED = "adapter_crashed"
REASON_RUN_NOT_FOUND = "run_not_found"
REASON_ALREADY_FINISHED = "run_already_finished"
REASON_NOTHING_TO_RETRY = "nothing_to_retry"
REASON_RETRY_CONFLICT = "retry_conflict"
#: B13b 返工（R-B2）：周期计划在**真实入队边界**上复核不通过（已被关闭/改期）。
#: 与"预览没有目标"（可以推进锚点）严格区分：这里既不建 run，也不许推进锚点。
REASON_PLAN_CHANGED = "plan_changed"
#: 入队结果**不确定**（提交可能成功但回执丢失、连唯一键都读不回来）。
REASON_ENQUEUE_UNCONFIRMED = "enqueue_unconfirmed"
#: B13b 返工 v2（R-B4）：稳定计划键命中的执行记录**已经开始过、但结果无法确认**
#: （状态 running 却没有本进程内的执行任务，通常是进程在跑的过程中挂了）。这种
#: 情况不自动重跑、也不补发，留给人工对账；锚点因此不推进（等 R-B3 的容差窗口
#: 过后按"错过窗口"推进，不会永远卡住）。
REASON_PLAN_RUN_UNRESOLVED = "plan_run_unresolved"

#: B13b 返工 v2（R-B4）：稳定计划键对账后，这一期唯一执行记录的处置方式。
#: ``started``：从未开始（accepted）→ 由产品消费者接管并启动**唯一**一次执行；
#: ``in_flight``：本进程里已经在跑 → 不重复建、不重复发；
#: ``settled``：已经终态 → 不重复建、不重复发；
#: ``unresolved``：已经开始但结果无法确认 → 不自动重跑、不补发（见上）。
PLAN_RECOVERY_STARTED = "started"
PLAN_RECOVERY_IN_FLIGHT = "in_flight"
PLAN_RECOVERY_SETTLED = "settled"
PLAN_RECOVERY_UNRESOLVED = "unresolved"

PLAN_RECOVERY_DETAIL = {
    PLAN_RECOVERY_STARTED: (
        "这一期已经有执行记录但从未开始（上一次入队提交成功、回执丢失）："
        "已按同一个稳定计划键接管并启动唯一一次执行"
    ),
    PLAN_RECOVERY_IN_FLIGHT: "这一期已经有执行记录且正在执行：不重复建、不重复发",
    PLAN_RECOVERY_SETTLED: "这一期已经有执行记录且已经结束：不重复建、不重复发",
    PLAN_RECOVERY_UNRESOLVED: (
        "这一期已经有执行记录，但它已经开始过、结果无法确认：不自动重跑、也不补发，"
        "请先核对这条执行记录再决定是否需要人工重试"
    ),
}

#: 取消等真实 worker 收尾的有界等待上限（秒）。
CANCEL_WAIT_SECONDS = 30.0

#: 一次预览最多绑定多少个目标快照（与适配器的 MAX_TARGETS 对齐，防止令牌过大）。
_MAX_TARGET_KEYS = 200

SOURCE_MANUAL = "manual"
SOURCE_RETRY = "retry"
#: B13b/C4：由按群周期计划（internal scheduler）自动入队的执行来源。
SOURCE_SCHEDULE = "schedule"


class OperationsError(Exception):
    """带 HTTP 语义的内核错误；``settings_api`` 只做一层映射。"""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details) if details else None


@dataclass(frozen=True, slots=True)
class PreviewTarget:
    """预览里的一条**待执行目标**（稳定键 + 人类可读说明）。"""

    item_key: str
    target: dict[str, Any]
    label: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TaskPreview:
    """适配器返回的只读预览结论。

    ``status``：``ok``（有可执行目标）/ ``no_work``（现在没有可做的，不是失败）
    / ``blocked``（明确不能执行，``reason`` 说明为什么）。
    """

    status: str
    reason: str = REASON_OK
    detail: str = ""
    targets: tuple[PreviewTarget, ...] = ()
    effects: tuple[str, ...] = ()
    cost: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ItemResult:
    """一个稳定业务键的真实结果。``resendable`` 见模块 docstring。"""

    item_key: str
    target: dict[str, Any]
    status: str
    reason: str = REASON_OK
    detail: str = ""
    external_message_id: int = 0
    resendable: bool = False


@dataclass(frozen=True, slots=True)
class TaskRunResult:
    """适配器一次执行的全部结果。"""

    status: str
    reason: str = REASON_OK
    detail: str = ""
    items: tuple[ItemResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """对账结论：只允许在"业务台账证明"的前提下把未知改成确定。"""

    status: str
    reason: str = REASON_OK
    detail: str = ""
    items: tuple[ItemResult, ...] = ()


@dataclass(frozen=True, slots=True)
class RunContext:
    """适配器执行时能看到的一切（只读 + 取消信号）。"""

    run_id: int
    run_key: str
    task_id: str
    group_id: int
    params: dict[str, Any]
    source: str
    retry_of_run_id: int | None
    bot: Any
    settings: Any
    session_factory: async_sessionmaker[AsyncSession]
    cancel_event: asyncio.Event
    #: 重试时：上一次 run 的逐项结果（item_key → ItemResult），适配器据此跳过
    #: 已成功项与"不可安全重发"的未知项。
    previous_items: Mapping[str, ItemResult] = field(default_factory=dict)
    #: 确认时预览选中的**目标快照**。非空时适配器只允许处理这些 item_key；
    #: 空元组表示"没有绑定信息"（旧数据），此时按适配器自己的当前集合处理。
    target_keys: tuple[str, ...] = ()
    #: 发起人快照（执行期重新核权限用；0/"" = 未知，按最保守方式处理）。
    actor_user_id: int = 0
    actor_role: str = ""
    #: 确认时的配置 revision（执行期重新从持久配置读一遍做比对）。
    guard_revision: int = 0
    #: 确认/入队时的**按群策略 revision**（B13b/C4）：来源或周期计划被改过之后，
    #: 真实不可逆准入的守卫谓词会因为版本不等而拒绝旧执行。
    policy_revision: int = 0

    def bound_keys(self) -> tuple[str, ...]:
        return tuple(self.target_keys)

    def is_bound(self, item_key: str) -> bool:
        """``item_key`` 是否在这次确认的目标快照里（无快照时保守返回 False）。"""

        return str(item_key) in self.target_keys

    def cancel_requested(self) -> bool:
        return bool(self.cancel_event.is_set())

    def previous(self, item_key: str) -> ItemResult | None:
        return self.previous_items.get(item_key)


class TaskAdapter(Protocol):
    """六类任务适配器的统一形状（全部由 ``operations_tasks`` 提供）。"""

    task_id: str
    title: str

    def normalize_params(self, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """严格类型校验（拒绝 bool/float 静默转 int），返回规范参数。"""

    async def preview(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        group_id: int,
        params: Mapping[str, Any],
        bot: Any = None,
    ) -> TaskPreview:
        """**只读**预览：不写审计、不占位、不发消息、不调模型、不动积分。"""

    async def recheck_guard(
        self,
        session: AsyncSession,
        *,
        group_id: int,
        params: Mapping[str, Any],
        guard_revision: int,
        manager: Any = None,
    ) -> str:
        """在确认的写事务里复核守卫；返回非空 reason 即拒绝确认。

        ``manager`` 是运行中的 ``RuntimeConfigManager``（可能为 None）：有它时
        适配器可以直接比较**当前**配置 revision，而不是只信预览时的那份证据。
        """

    async def run(self, context: RunContext) -> TaskRunResult:
        """真实执行；取消信号出现时不得再开始新的副作用。"""

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        run: OperationsRun,
        items: Sequence[OperationsRunItem],
    ) -> ReconcileResult | None:
        """重启/取消后按**业务台账**对账；返回 None 表示保持现状（未知仍是未知）。"""


_TASKS: dict[str, TaskAdapter] = {}
_ACTIVE_RUNS: dict[int, asyncio.Task[None]] = {}
_CANCEL_EVENTS: dict[int, asyncio.Event] = {}
_DONE_EVENTS: dict[int, asyncio.Event] = {}


def register_task(adapter: TaskAdapter) -> TaskAdapter:
    """注册/替换一个任务适配器（同 id 覆盖，方便测试注入）。"""

    _TASKS[str(adapter.task_id)] = adapter
    return adapter


def ensure_tasks_loaded() -> None:
    """延迟导入适配器模块，避免内核与适配器互相 import。"""

    if not _TASKS:
        from bot.services.operations_tasks import register_all_tasks

        register_all_tasks()


def get_task(task_id: object) -> TaskAdapter:
    ensure_tasks_loaded()
    key = str(task_id or "").strip()
    adapter = _TASKS.get(key)
    if adapter is None:
        raise OperationsError(
            404,
            REASON_UNKNOWN_TASK,
            "没有这个运营任务",
            details={"task_id": key, "allowed": list(TASK_IDS)},
        )
    return adapter


def registered_task_ids() -> tuple[str, ...]:
    ensure_tasks_loaded()
    return tuple(_TASKS)


# ---------------------------------------------------------------------------
# 预览令牌：签名 + 有效期 + 参数绑定
# ---------------------------------------------------------------------------


def preview_ttl_seconds(settings: Any) -> int:
    """预览令牌有效期（秒）：默认 120，允许 30..600（``admin_ops`` 段）。"""

    view = getattr(settings, "admin_ops", None)
    raw = getattr(view, "operations_preview_ttl_seconds", None)
    if isinstance(raw, bool):  # bool 是 int 的子类，显式挡掉
        return 120
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 120
    if value < 30 or value > 600:
        return 120
    return value


def _token_secret(settings: Any) -> bytes:
    raw = str(getattr(settings, "config_master_key", "") or "").strip()
    if not raw:
        raise OperationsError(
            503,
            "preview_unsupported",
            "服务端没有可用的签名密钥，无法签发预览令牌",
        )
    return raw.encode("utf-8")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class PreviewBinding:
    """令牌里被签名钉死的全部内容（确认时一律以它为准）。"""

    token_id: str
    task_id: str
    group_id: int
    actor_user_id: int
    actor_role: str
    guard_revision: int
    params: dict[str, Any]
    issued_at: datetime
    expires_at: datetime
    #: 预览时选中的**目标快照**（稳定 item_key）。确认后执行只允许处理这些键：
    #: 否则"确认 3 条 → 期间又到期 2 条"会静默多发 2 条预览里没展示过的动作。
    target_keys: tuple[str, ...] = ()
    #: B13b/C4：预览/确认那一刻的按群策略版本（来源切换 / 周期计划改动后，旧确认
    #: 在真实不可逆准入被守卫谓词挡住）。
    policy_revision: int = 0


def issue_preview_token(
    settings: Any,
    *,
    task_id: str,
    group_id: int,
    actor_user_id: int,
    actor_role: str,
    guard_revision: int,
    params: Mapping[str, Any],
    target_keys: Sequence[str] = (),
    policy_revision: int = 0,
    now: datetime | None = None,
) -> tuple[str, PreviewBinding]:
    """签发预览令牌：绑定操作者/群/任务/参数/守卫 revision/有效期/目标快照。"""

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    ttl = preview_ttl_seconds(settings)
    keys = tuple(
        str(key)[:200] for key in target_keys if str(key or "").strip()
    )[:_MAX_TARGET_KEYS]
    binding = PreviewBinding(
        token_id=secrets.token_hex(8),
        task_id=str(task_id),
        group_id=int(group_id),
        actor_user_id=int(actor_user_id),
        actor_role=str(actor_role or ""),
        guard_revision=int(guard_revision),
        params=dict(params),
        issued_at=moment,
        expires_at=moment + timedelta(seconds=ttl),
        target_keys=keys,
        policy_revision=int(policy_revision or 0),
    )
    body = {
        "v": 1,
        "id": binding.token_id,
        "task": binding.task_id,
        "group": binding.group_id,
        "actor": binding.actor_user_id,
        "role": binding.actor_role,
        "rev": binding.guard_revision,
        "prev": binding.policy_revision,
        "iat": binding.issued_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "exp": binding.expires_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "params": binding.params,
        "keys": list(binding.target_keys),
    }
    payload = base64.urlsafe_b64encode(_canonical(body).encode("utf-8")).decode("ascii")
    token = f"{payload}.{_sign(_token_secret(settings), payload)}"
    if len(token) > 4096:  # 参数异常巨大时直接拒绝，别把令牌当传参通道
        raise OperationsError(400, "params_too_large", "任务参数过大")
    return token, binding


def parse_preview_token(
    settings: Any,
    token: object,
    *,
    now: datetime | None = None,
) -> PreviewBinding:
    """校验签名与有效期，返回绑定内容（过期/篡改/格式错都拒绝）。"""

    raw = str(token or "").strip()
    if not raw or raw.count(".") != 1:
        raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌格式不正确")
    payload, _, signature = raw.partition(".")
    if not hmac.compare_digest(_sign(_token_secret(settings), payload), signature):
        raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌签名不正确")
    try:
        decoded = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, binascii.Error):
        raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌内容不可解析")
    if not isinstance(decoded, dict) or int(decoded.get("v") or 0) != 1:
        raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌版本不受支持")
    params = decoded.get("params")
    if not isinstance(params, dict):
        raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌缺少参数")
    raw_keys = decoded.get("keys") or []
    if not isinstance(raw_keys, list):
        raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌目标快照不可解析")
    target_keys = tuple(str(key)[:200] for key in raw_keys if str(key or "").strip())
    if len(target_keys) > _MAX_TARGET_KEYS:
        raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌目标快照过大")

    def _moment(key: str) -> datetime:
        try:
            return datetime.strptime(str(decoded.get(key) or ""), "%Y-%m-%dT%H:%M:%S")
        except (TypeError, ValueError):
            raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌时间不可解析")

    try:
        group_id = int(decoded.get("group") or 0)
        actor = int(decoded.get("actor") or 0)
        revision = int(decoded.get("rev") or 0)
        policy_revision = int(decoded.get("prev") or 0)
    except (TypeError, ValueError):
        raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌绑定字段不可解析")
    binding = PreviewBinding(
        token_id=str(decoded.get("id") or ""),
        task_id=str(decoded.get("task") or ""),
        group_id=group_id,
        actor_user_id=actor,
        actor_role=str(decoded.get("role") or ""),
        guard_revision=revision,
        params=dict(params),
        issued_at=_moment("iat"),
        expires_at=_moment("exp"),
        target_keys=target_keys,
        policy_revision=policy_revision,
    )
    if not binding.token_id or binding.group_id == 0 or binding.actor_user_id == 0:
        raise OperationsError(400, REASON_TOKEN_INVALID, "预览令牌缺少必要绑定")
    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    if moment > binding.expires_at:
        raise OperationsError(
            409,
            REASON_TOKEN_EXPIRED,
            "预览已过期，请重新预览后再确认",
            details={"expires_at": binding.expires_at.strftime("%Y-%m-%dT%H:%M:%S")},
        )
    return binding


# ---------------------------------------------------------------------------
# 只读预览
# ---------------------------------------------------------------------------


def _stamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S") if isinstance(value, datetime) else ""


async def _current_policy_revision(
    session_factory: async_sessionmaker[AsyncSession], *, group_id: int, task_id: str
) -> int:
    """读取**此刻**的按群策略版本（B13b/C4 守卫条件之一）。

    读失败按 0 处理（保守：确认时快照 0，而库里策略行版本 > 0 时守卫会在真实
    准入处拒绝，不会误放行）。
    """

    if int(group_id) == 0 or not str(task_id or "").strip():
        return 0
    from bot.services import task_policy

    try:
        async with session_factory() as session:
            return await task_policy.policy_revision(
                session, group_id=int(group_id), task_id=str(task_id)
            )
    except Exception:  # noqa: BLE001 - 读不到就是不匹配，绝不因此放行
        log.warning("operations: policy revision read failed | group=%s task=%s",
                    int(group_id), str(task_id))
        return 0


async def preview_task(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    manager: Any,
    *,
    task_id: object,
    group_id: int,
    actor_user_id: int,
    actor_role: str,
    params: Mapping[str, Any] | None = None,
    bot: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """只读预览。**绝不写任何表**（审计、水位、占位、积分都不动）。"""

    adapter = get_task(task_id)
    normalized = adapter.normalize_params(params)
    guard_revision = int(getattr(manager, "revision", 0) or 0)
    policy_revision = await _current_policy_revision(
        session_factory, group_id=int(group_id), task_id=adapter.task_id
    )
    preview = await adapter.preview(
        session_factory=session_factory,
        settings=settings,
        group_id=int(group_id),
        params=normalized,
        bot=bot,
    )
    token = ""
    expires_at = ""
    token_id = ""
    if preview.status == "ok" and preview.targets:
        token, binding = issue_preview_token(
            settings,
            task_id=adapter.task_id,
            group_id=int(group_id),
            actor_user_id=int(actor_user_id),
            actor_role=actor_role,
            guard_revision=guard_revision,
            params=normalized,
            target_keys=[item.item_key for item in preview.targets],
            policy_revision=policy_revision,
            now=now,
        )
        expires_at = binding.expires_at.strftime("%Y-%m-%dT%H:%M:%S")
        token_id = binding.token_id
    return {
        "task_id": adapter.task_id,
        "task_title": adapter.title,
        "group_id": int(group_id),
        "guard_revision": guard_revision,
        "policy_revision": policy_revision,
        "read_only": True,
        "status": preview.status,
        "reason": preview.reason,
        "detail": preview.detail,
        "effects": list(preview.effects),
        "cost": dict(preview.cost),
        "extra": dict(preview.extra),
        "targets": [
            {
                "item_key": item.item_key,
                "target": dict(item.target),
                "label": item.label,
                "detail": item.detail,
            }
            for item in preview.targets
        ],
        "token": token,
        "token_id": token_id,
        "expires_at": expires_at,
        "ttl_seconds": preview_ttl_seconds(settings),
        "confirmable": bool(token),
    }


# ---------------------------------------------------------------------------
# 单次确认（原子守卫）
# ---------------------------------------------------------------------------


def _run_payload(run: OperationsRun) -> dict[str, Any]:
    return {
        "run_key": run.run_key,
        "task_id": run.task_id,
        "group_id": int(run.group_id),
        "params": dict(run.params or {}),
        "status": run.status,
        "reason": run.reason,
        "detail": run.detail,
        "source": run.source,
        "retry_of_run_id": run.retry_of_run_id,
        "target_keys": [str(key) for key in (run.target_keys or [])],
        "cancel_requested": bool(run.cancel_requested),
        "items_total": int(run.items_total or 0),
        "items_succeeded": int(run.items_succeeded or 0),
        "items_failed": int(run.items_failed or 0),
        "items_unknown": int(run.items_unknown or 0),
        "guard_revision": int(run.guard_revision or 0),
        "policy_revision": int(getattr(run, "policy_revision", 0) or 0),
        "created_at": _stamp(run.created_at),
        "started_at": _stamp(run.started_at),
        "finished_at": _stamp(run.finished_at),
        "finished": run.status in TERMINAL_RUN_STATUSES,
    }


def _item_payload(item: OperationsRunItem) -> dict[str, Any]:
    return {
        "item_key": item.item_key,
        "target": dict(item.target or {}),
        "status": item.status,
        "reason": item.reason,
        "detail": item.detail,
        "external_message_id": int(item.external_message_id or 0),
        "resendable": bool(item.resendable),
        "attempts": int(item.attempts or 0),
        "updated_at": _stamp(item.updated_at),
    }


async def confirm_run(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    manager: Any,
    *,
    token: object,
    actor_user_id: int,
    actor_role: str = "",
    bot: Any = None,
    now: datetime | None = None,
    group_id: int | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """确认一次预览：原子插入 run（单次令牌）+ 写事务内复核守卫 → 开始执行。

    同一令牌的第二次确认返回**同一个 run**（``duplicate=True``），不会再执行。
    令牌绑定操作者/群/任务/参数/guard revision：换一个群或换一个任务的路径来
    确认都会被拒（``group_id``/``task_id`` 是调用方路径上的真实值）。
    """

    binding = parse_preview_token(settings, token, now=now)
    adapter = get_task(binding.task_id)
    if int(binding.actor_user_id) != int(actor_user_id):
        raise OperationsError(
            403, REASON_ACTOR_MISMATCH, "这个预览不是当前账号发起的，请重新预览"
        )
    if group_id is not None and int(binding.group_id) != int(group_id):
        raise OperationsError(
            403, REASON_GROUP_MISMATCH, "这个预览不是本群的，请在本群重新预览"
        )
    if task_id is not None and str(binding.task_id) != str(task_id):
        raise OperationsError(
            403, REASON_TASK_MISMATCH, "这个预览不是这个任务的，请重新预览"
        )
    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    run = OperationsRun(
        run_key=secrets.token_hex(16),
        token_id=binding.token_id,
        task_id=adapter.task_id,
        group_id=binding.group_id,
        actor_user_id=binding.actor_user_id,
        actor_role=str(actor_role or binding.actor_role or ""),
        params=dict(binding.params),
        guard_revision=int(binding.guard_revision),
        policy_revision=int(binding.policy_revision or 0),
        source=SOURCE_MANUAL,
        status=RUN_STATUS_ACCEPTED,
        created_at=moment,
        target_keys=list(binding.target_keys),
    )
    duplicate = False
    async with session_factory() as session:
        session.add(run)
        try:
            # 先落 INSERT：拿到 SQLite 写锁 + 触发 token_id 唯一约束。
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            if "token_id" not in str(getattr(exc, "orig", exc)):
                raise
            existing = await session.scalar(
                select(OperationsRun).where(OperationsRun.token_id == binding.token_id)
            )
            if existing is None:
                raise
            duplicate = True
            run = existing
        else:
            # 守卫复核在**同一个写事务**里（写锁已持有）。
            blocked = await adapter.recheck_guard(
                session,
                group_id=binding.group_id,
                params=binding.params,
                guard_revision=binding.guard_revision,
                manager=manager,
            )
            if blocked:
                await session.rollback()
                raise OperationsError(
                    409,
                    REASON_GUARD_BLOCKED,
                    "确认时复核未通过，本次没有执行任何动作",
                    details={"reason": str(blocked)},
                )
            await session.commit()
    if not duplicate:
        _spawn_execution(int(run.id), bot=bot)
    payload = _run_payload(run)
    payload["duplicate"] = duplicate
    payload["confirmed"] = True
    return payload


def _plan_recovery_state(run: Any) -> str:
    """稳定计划键命中的执行记录该被怎么处置（B13b 返工 v2 / R-B4）。

    ``accepted`` 是 :func:`_execute_run` 在真正跑适配器**之前**就持久写下的
    "从未开始"标记：提交成功但回执丢失时留下的就是这种行。它如果在本进程里没有
    活跃任务，就可以安全地被接管并启动唯一一次执行；``running`` 表示已经开始过，
    结果可能未知——只对账、不重跑。
    """

    run_id = int(run.id)
    active = _ACTIVE_RUNS.get(run_id)
    if active is not None and not active.done():
        return PLAN_RECOVERY_IN_FLIGHT
    status = str(run.status)
    if status == RUN_STATUS_ACCEPTED:
        return PLAN_RECOVERY_STARTED
    if status == RUN_STATUS_RUNNING:
        return PLAN_RECOVERY_UNRESOLVED
    return PLAN_RECOVERY_SETTLED


def _plan_recovery_payload(payload: dict[str, Any], recovery: str) -> dict[str, Any]:
    """把"对账结论"拼成调用方看得懂的载荷（scheduled/advance/detail 同口径）。"""

    payload["found"] = True
    payload["duplicate"] = True
    payload["scheduled"] = True
    payload["recovery"] = recovery
    payload["advance"] = recovery != PLAN_RECOVERY_UNRESOLVED
    payload["detail"] = PLAN_RECOVERY_DETAIL[recovery]
    if recovery == PLAN_RECOVERY_UNRESOLVED:
        payload["reason"] = REASON_PLAN_RUN_UNRESOLVED
    return payload


async def reconcile_scheduled_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    task_id: object,
    group_id: int,
    run_key: str,
    bot: Any = None,
) -> dict[str, Any]:
    """按**稳定计划键**对账"这一期到底有没有执行记录"（B13b 返工 v2 / R-B4）。

    父 v3 复现的漏口：提交真的成功、但回执在 ``commit`` 之后丢失时，唯一一条 run
    永远停在 ``accepted``；下一轮 tick 命中稳定键后却只把锚点推进、从不启动这条
    run，于是"回执上说这一期已经报了，实际上一个副作用都没发生"。

    这个函数不依赖预览（目标可能已经被上一轮真的消费掉了，预览会返回"无目标"），
    只看持久执行记录本身，并把结论翻成 ``scheduled``/``advance``/``recovery``：

    * ``found=False``：**没有任何执行记录** —— 明确未入队，不推进锚点、保留失败
      证据，下一轮用同一个键继续对账（绝不重复建、重复发）。
    * ``found=True``：处置方式见 :data:`PLAN_RECOVERY_DETAIL`；只有
      ``started``（从未开始）才会由这里启动唯一一次执行，且
      :func:`_spawn_execution` 本身幂等。
    """

    adapter = get_task(task_id)
    key = str(run_key)
    spawned: int | None = None
    async with session_factory() as session:
        existing = (
            await session.execute(
                select(OperationsRun).where(OperationsRun.run_key == key)
            )
        ).scalar_one_or_none()
        if existing is None:
            return {
                "found": False,
                "scheduled": False,
                "advance": False,
                "task_id": adapter.task_id,
                "group_id": int(group_id),
                "status": RUN_STATUS_UNKNOWN,
                "reason": REASON_ENQUEUE_UNCONFIRMED,
                "detail": (
                    "入队结果无法确认：还没有查到这一期的执行记录；不会重复建、"
                    "不会重复发，下一轮用同一个稳定计划键继续对账"
                ),
                "run_key": key,
            }
        payload = _run_payload(existing)
        recovery = _plan_recovery_state(existing)
        if recovery == PLAN_RECOVERY_STARTED:
            spawned = int(existing.id)
    payload = _plan_recovery_payload(payload, recovery)
    if spawned is not None:
        _spawn_execution(spawned, bot=bot)
    return payload


async def enqueue_scheduled_run(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    manager: Any,
    *,
    task_id: object,
    group_id: int,
    params: Mapping[str, Any] | None = None,
    bot: Any = None,
    now: datetime | None = None,
    plan: Any = None,
) -> dict[str, Any]:
    """给**按群周期计划**入队一次真实执行（内部 scheduler 专用入口）。

    与 :func:`confirm_run` 的区别：这里没有人工确认的令牌（触发者是计划本身），
    目标是**当场只读预览出来的那批**（不补历史、不多发），按群策略版本按此刻钉死。
    预览没有可执行目标时**不建 run**（返回 ``scheduled=False``），调用方据此推进
    自己的锚点，不会留下一堆空 run。

    B13b 返工（R-B2/R-B4）之后多了两件事，都由返回值里的 ``advance`` 字段告诉
    调用方"这一期的锚点能不能推进"：

    * ``plan`` 非空时（内部计划 runner 总会传），先在同一事务里跑
      :func:`bot.services.task_policy.admit_plan_run` —— 它是一条带
      ``revision/plan_enabled/anchor_at`` 谓词的**真实 DML**，在持有 SQLite writer
      锁的前提下确认"这次入队依据的计划身份仍然成立"。不成立就返回
      ``advance=False``、``reason=plan_changed``：既不建 run，也不许把锚点推进
      （计划已被管理员关闭/改期，那不是"本期的活干完了"）。
    * ``plan`` 非空时 run_key 由 :func:`bot.services.task_policy.plan_run_key` 按
      (任务, 群, 锚点) 稳定派生，插入用 ``ON CONFLICT DO NOTHING``：提交成功但
      ACK 丢失后的重试会命中既有行并被识别为 ``duplicate``，绝不会重复建 run、
      重复发分。
    * ``plan`` 非空时**先按稳定键对账**再跑预览（R-B4）：上一轮可能已经把这一期
      的活干完（预览于是"无目标"），也可能建了 run 但 ACK 丢失（唯一一条永远停在
      ``accepted``）。如果先看预览，"无目标"会让人以为这一期本来就没事，那条
      ``accepted`` 的 run 就永远没人启动——父 v3 就是这么复现的。对账结论由
      :func:`reconcile_scheduled_run` 给出，``recovery=started`` 时由这里接管并
      启动唯一一次执行（:func:`_spawn_execution` 幂等）。
    """

    adapter = get_task(task_id)
    normalized = adapter.normalize_params(params)
    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    guard_revision = int(getattr(manager, "revision", 0) or 0)
    from bot.services import task_policy

    if plan is not None:
        policy_revision = int(getattr(plan, "revision", 0) or 0)
        run_key = task_policy.plan_run_key(
            group_id=int(group_id),
            task_id=adapter.task_id,
            anchor_at=plan.anchor_at,
        )
        # R-B4：稳定键对账在预览**之前**。目标可能已经被上一轮真的消费掉（预览会
        # 说"无目标"），也可能建了 run 但 ACK 丢失（唯一一条永远停在 accepted）。
        # 先看预览就会把后者当成"这一期本来就没事"，那条 run 永远没人启动。
        reconciled = await reconcile_scheduled_run(
            session_factory,
            task_id=adapter.task_id,
            group_id=int(group_id),
            run_key=run_key,
            bot=bot,
        )
        if reconciled.get("found"):
            return reconciled
    else:
        policy_revision = await _current_policy_revision(
            session_factory, group_id=int(group_id), task_id=adapter.task_id
        )
        run_key = secrets.token_hex(16)
    preview = await adapter.preview(
        session_factory=session_factory,
        settings=settings,
        group_id=int(group_id),
        params=normalized,
        bot=bot,
    )
    if preview.status != "ok" or not preview.targets:
        return {
            "scheduled": False,
            "advance": True,
            "task_id": adapter.task_id,
            "group_id": int(group_id),
            "status": RUN_STATUS_NO_WORK,
            "reason": preview.reason,
            "detail": preview.detail,
            "run_key": "",
        }
    async with session_factory() as session:
        if plan is not None:
            admitted = await task_policy.admit_plan_run(
                session,
                group_id=int(group_id),
                task_id=adapter.task_id,
                expected_revision=int(getattr(plan, "revision", 0) or 0),
                expected_anchor_at=plan.anchor_at,
            )
            if not admitted:
                await session.rollback()
                return {
                    "scheduled": False,
                    "advance": False,
                    "task_id": adapter.task_id,
                    "group_id": int(group_id),
                    "status": RUN_STATUS_NO_WORK,
                    "reason": REASON_PLAN_CHANGED,
                    "detail": "计划在入队前已被关闭或改期，本次不建执行记录",
                    "run_key": "",
                }
        statement = (
            sqlite_insert(OperationsRun)
            .values(
                run_key=run_key,
                token_id=f"schedule:{run_key}",
                task_id=adapter.task_id,
                group_id=int(group_id),
                actor_user_id=0,
                actor_role="",
                params=dict(normalized),
                guard_revision=guard_revision,
                policy_revision=policy_revision,
                source=SOURCE_SCHEDULE,
                status=RUN_STATUS_ACCEPTED,
                created_at=moment,
                target_keys=[item.item_key for item in preview.targets],
            )
            .on_conflict_do_nothing(index_elements=["run_key"])
            .returning(OperationsRun.id)
        )
        run_id = (await session.execute(statement)).scalar_one_or_none()
        run: OperationsRun | None = None
        if run_id is not None:
            run = await session.get(OperationsRun, int(run_id))
            await session.commit()
        elif plan is None:
            await session.rollback()
            raise RuntimeError("operations: scheduled run key collision")
        else:
            # 稳定键已经存在：这是同一次计划触发（或提交成功但回执丢失后的重试），
            # 读回来按"这条 run 处于什么阶段"对账，绝不重复建 run。若它从未开始
            # （accepted 且本进程内没有活跃任务），就由这里接管并启动唯一一次执行
            # ——这正是父 v3 复现的"回执丢失后永远没人跑"的缺口。
            existing = (
                await session.execute(
                    select(OperationsRun).where(OperationsRun.run_key == run_key)
                )
            ).scalar_one_or_none()
            if existing is None:
                await session.rollback()
                return {
                    "scheduled": False,
                    "advance": False,
                    "task_id": adapter.task_id,
                    "group_id": int(group_id),
                    "status": RUN_STATUS_UNKNOWN,
                    "reason": REASON_ENQUEUE_UNCONFIRMED,
                    "detail": (
                        "入队结果无法确认：还没有查到这一期的执行记录；不会重复建、"
                        "不会重复发，下一轮用同一个稳定计划键继续对账"
                    ),
                    "run_key": run_key,
                }
            # 先物化载荷再回滚：rollback 会让 ORM 实例过期，之后再读属性会触发
            # 非 await 上下文的懒加载（MissingGreenlet）。
            payload = _run_payload(existing)
            recovery = _plan_recovery_state(existing)
            spawned = int(existing.id) if recovery == PLAN_RECOVERY_STARTED else None
            await session.rollback()
            payload = _plan_recovery_payload(payload, recovery)
            if spawned is not None:
                _spawn_execution(spawned, bot=bot)
            return payload
    if run is None:  # pragma: no cover - 刚插入的行不可能读不回来
        raise RuntimeError("operations: scheduled run vanished after insert")
    _spawn_execution(int(run.id), bot=bot)
    payload = _run_payload(run)
    payload["scheduled"] = True
    payload["advance"] = True
    return payload


def _spawn_execution(run_id: int, *, bot: Any) -> None:
    ensure_tasks_loaded()
    existing = _ACTIVE_RUNS.get(int(run_id))
    if existing is not None and not existing.done():
        # B13b 返工 v2（R-B4）：这个 run 在本进程里已经在跑了。稳定键对账可能被
        # 并发/重复调用（两个同锚点 tick、异常后的对账），这里必须幂等——否则
        # 同一个 run 会被执行两次，积分与出站都可能重复。
        return
    _CANCEL_EVENTS.setdefault(int(run_id), asyncio.Event())
    _DONE_EVENTS.setdefault(int(run_id), asyncio.Event())
    task = asyncio.get_running_loop().create_task(_execute_run(int(run_id), bot=bot))
    _ACTIVE_RUNS[int(run_id)] = task

    def _done(finished: asyncio.Task[None]) -> None:
        _ACTIVE_RUNS.pop(int(run_id), None)
        event = _DONE_EVENTS.get(int(run_id))
        if event is not None:
            event.set()
        if finished.cancelled():
            return
        error = finished.exception()
        if error is not None:
            log.error(
                "operations run executor crashed | run_id=%s | %s",
                run_id,
                type(error).__name__,
            )

    task.add_done_callback(_done)


async def _execute_run(run_id: int, *, bot: Any) -> None:
    """在后台把一次 run 跑完，并把逐项结果写进审计。"""

    session_factory, engine, owns_engine = await _execution_runtime()
    try:
        async with session_factory() as session:
            run = await session.get(OperationsRun, int(run_id))
            if run is None:
                return
            if run.status not in (RUN_STATUS_ACCEPTED, RUN_STATUS_RUNNING):
                return
            adapter = get_task(run.task_id)
            params = dict(run.params or {})
            group_id = int(run.group_id)
            retry_of = int(run.retry_of_run_id) if run.retry_of_run_id else None
            run_key = str(run.run_key)
            source = str(run.source)
            # 确认时的目标快照与发起人快照：执行期只允许复核并处理这些键。
            target_keys = tuple(str(key) for key in (run.target_keys or []))
            actor_user_id = int(run.actor_user_id or 0)
            actor_role = str(run.actor_role or "")
            guard_revision = int(run.guard_revision or 0)
            policy_revision = int(getattr(run, "policy_revision", 0) or 0)
            previous_items: dict[str, ItemResult] = {}
            if retry_of:
                rows = (
                    await session.scalars(
                        select(OperationsRunItem).where(
                            OperationsRunItem.run_id == retry_of
                        )
                    )
                ).all()
                previous_items = {
                    str(row.item_key): _item_result_of(row) for row in rows
                }
            run.status = RUN_STATUS_RUNNING
            run.started_at = now_shanghai_naive()
            cancel_event = _CANCEL_EVENTS.setdefault(int(run_id), asyncio.Event())
            if bool(run.cancel_requested):
                cancel_event.set()
            await session.commit()
        context = RunContext(
            run_id=int(run_id),
            run_key=run_key,
            task_id=str(adapter.task_id),
            group_id=group_id,
            params=params,
            source=source,
            retry_of_run_id=retry_of,
            bot=bot,
            settings=_execution_settings(),
            session_factory=session_factory,
            cancel_event=cancel_event,
            previous_items=previous_items,
            target_keys=target_keys,
            actor_user_id=actor_user_id,
            actor_role=actor_role,
            guard_revision=guard_revision,
            policy_revision=policy_revision,
        )
        try:
            result = await adapter.run(context)
        except asyncio.CancelledError:
            # 原始取消必须原样传播；run 保持"在途/未知"，由取消路径或对账收尾。
            raise
        except Exception as exc:  # 适配器崩了：不能假装没发生，也不能说"没副作用"
            log.error(
                "operations run adapter crashed | run=%s | task=%s | %s",
                run_key,
                adapter.task_id,
                type(exc).__name__,
            )
            result = TaskRunResult(
                status=RUN_STATUS_UNKNOWN,
                reason=REASON_ADAPTER_CRASHED,
                detail="执行过程中出现未预期错误，结果未知；请先核对业务台账再决定是否重试",
                items=(),
            )
        await _persist_result(int(run_id), result)
    finally:
        if owns_engine and engine is not None:
            await engine.dispose()


def _item_result_of(row: OperationsRunItem) -> ItemResult:
    return ItemResult(
        item_key=str(row.item_key),
        target=dict(row.target or {}),
        status=str(row.status),
        reason=str(row.reason or ""),
        detail=str(row.detail or ""),
        external_message_id=int(row.external_message_id or 0),
        resendable=bool(row.resendable),
    )


#: 执行期使用的 settings/session_factory（由进程启动或测试注入一次）。
_EXECUTION_SETTINGS: Any = None
_EXECUTION_SESSION_FACTORY: async_sessionmaker[AsyncSession] | None = None
_EXECUTION_ENGINE: Any = None


def bind_execution_runtime(
    *,
    settings: Any,
    session_factory: async_sessionmaker[AsyncSession],
    engine: Any = None,
) -> None:
    """把进程级执行运行时（settings/session_factory）绑给内核。

    由 ``settings_api`` 在注册路由时调用一次；测试直接调用它注入真实 ``init_db``
    的 ``session_factory``（**注入的 engine 不会被内核 dispose**）。没有绑定时执行期
    自己 ``Settings()`` + ``init_db`` 建一份（CLI 场景）。
    """

    global _EXECUTION_SETTINGS, _EXECUTION_SESSION_FACTORY, _EXECUTION_ENGINE
    _EXECUTION_SETTINGS = settings
    _EXECUTION_SESSION_FACTORY = session_factory
    _EXECUTION_ENGINE = engine


def reset_execution_runtime() -> None:
    """测试/重启清理：解绑执行运行时，并丢掉全部内存执行状态。

    ``_CANCEL_EVENTS`` / ``_DONE_EVENTS`` 是以数据库自增 id 为键的进程内缓存；
    同一个进程里换一个库（测试、或运维手动切换）时 id 会从 1 重新开始，留着旧
    event 会让新 run 一出生就被判定成"已请求取消"。所以 reset 必须把它们清空。
    """

    bind_execution_runtime(settings=None, session_factory=None, engine=None)
    _ACTIVE_RUNS.clear()
    _CANCEL_EVENTS.clear()
    _DONE_EVENTS.clear()


def _execution_settings() -> Any:
    if _EXECUTION_SETTINGS is not None:
        return _EXECUTION_SETTINGS
    from bot.config import Settings

    return Settings()


async def _execution_runtime() -> tuple[async_sessionmaker[AsyncSession], Any, bool]:
    """返回 (session_factory, engine, owns_engine)。"""

    if _EXECUTION_SESSION_FACTORY is not None:
        return _EXECUTION_SESSION_FACTORY, _EXECUTION_ENGINE, False
    from bot.config import Settings
    from bot.db.engine import init_db

    settings = Settings()
    engine, factory = await init_db(settings.database_url)
    return factory, engine, True


async def _persist_result(run_id: int, result: TaskRunResult) -> None:
    session_factory, engine, owns_engine = await _execution_runtime()
    try:
        async with session_factory() as session:
            run = await session.get(OperationsRun, int(run_id))
            if run is None:
                return
            rows = list(
                (
                    await session.scalars(
                        select(OperationsRunItem).where(
                            OperationsRunItem.run_id == int(run_id)
                        )
                    )
                ).all()
            )
            by_key = {str(row.item_key): row for row in rows}
            moment = now_shanghai_naive()
            for item in result.items:
                row = by_key.get(item.item_key)
                if row is None:
                    row = OperationsRunItem(
                        run_id=int(run_id),
                        item_key=item.item_key,
                        created_at=moment,
                    )
                    session.add(row)
                    rows.append(row)
                    by_key[item.item_key] = row
                row.target = dict(item.target)
                row.status = item.status
                row.reason = item.reason
                row.detail = item.detail
                row.external_message_id = int(item.external_message_id or 0)
                row.resendable = bool(item.resendable)
                row.attempts = int(row.attempts or 0) + 1
                row.updated_at = moment
            await session.flush()
            status = str(result.status)
            reason = str(result.reason or "")
            unknown = sum(1 for row in rows if row.status == ITEM_STATUS_UNKNOWN)
            if status == RUN_STATUS_SUCCEEDED and unknown:
                # 适配器说成功但存在未知项：以更保守的结论为准。
                status = RUN_STATUS_UNKNOWN
                reason = reason or REASON_EXTERNAL_UNKNOWN
            if bool(run.cancel_requested) and status in (
                RUN_STATUS_SUCCEEDED,
                RUN_STATUS_RUNNING,
            ):
                # R6：取消**不得**把已经真实完成的项抹成 cancelled，也**不得**在有项
                # 被取消/失败时对外宣称 succeeded。只有"每一项都真的完成"时才保留
                # succeeded（说明取消来得比工作收尾更晚，这是真实结果）。
                states = [row.status for row in rows]
                if ITEM_STATUS_UNKNOWN in states:
                    status = RUN_STATUS_UNKNOWN
                    reason = reason or REASON_EXTERNAL_UNKNOWN
                elif any(state != ITEM_STATUS_SUCCEEDED for state in states):
                    status = RUN_STATUS_PARTIAL
                    reason = reason or REASON_CANCEL_REQUESTED
                else:
                    status = RUN_STATUS_SUCCEEDED
            run.items_total = len(rows)
            run.items_succeeded = sum(
                1 for row in rows if row.status == ITEM_STATUS_SUCCEEDED
            )
            run.items_failed = sum(
                1 for row in rows if row.status == ITEM_STATUS_FAILED
            )
            run.items_unknown = unknown
            run.status = status
            run.reason = reason
            run.detail = str(result.detail or "")[:300]
            run.finished_at = moment
            await session.commit()
    finally:
        if owns_engine and engine is not None:
            await engine.dispose()


# ---------------------------------------------------------------------------
# 读取、审计列表、取消、重试、对账
# ---------------------------------------------------------------------------


async def read_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_key: object,
    group_id: int,
) -> dict[str, Any]:
    """读一次 run（含逐项结果）。群隔离靠 ``group_id`` 谓词，绝不跨群返回。"""

    key = str(run_key or "").strip()
    if not key:
        raise OperationsError(404, REASON_RUN_NOT_FOUND, "没有这次执行记录")
    async with session_factory() as session:
        run = await session.scalar(
            select(OperationsRun).where(
                OperationsRun.run_key == key,
                OperationsRun.group_id == int(group_id),
            )
        )
        if run is None:
            raise OperationsError(404, REASON_RUN_NOT_FOUND, "没有这次执行记录")
        items = (
            await session.scalars(
                select(OperationsRunItem)
                .where(OperationsRunItem.run_id == int(run.id))
                .order_by(OperationsRunItem.id)
            )
        ).all()
    payload = _run_payload(run)
    payload["items"] = [_item_payload(item) for item in items]
    return payload


async def list_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    group_id: int,
    limit: int = 20,
    cursor: int | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """本群审计列表（有界分页，按 id 倒序的 keyset 分页）。"""

    size = max(1, min(int(limit), 50))
    async with session_factory() as session:
        statement = select(OperationsRun).where(OperationsRun.group_id == int(group_id))
        if task_id:
            statement = statement.where(OperationsRun.task_id == str(task_id))
        if cursor:
            statement = statement.where(OperationsRun.id < int(cursor))
        rows = (
            await session.scalars(
                statement.order_by(OperationsRun.id.desc()).limit(size + 1)
            )
        ).all()
    visible = list(rows[:size])
    has_more = len(rows) > size
    next_cursor = int(visible[-1].id) if visible and has_more else None
    return {
        "group_id": int(group_id),
        "items": [_run_payload(row) for row in visible],
        "has_more": has_more,
        "next_cursor": next_cursor,
        "limit": size,
    }


async def request_cancel(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_key: object,
    group_id: int,
    wait_seconds: float = CANCEL_WAIT_SECONDS,
) -> dict[str, Any]:
    """请求取消：置位标志 + 等真实 worker 收尾（有界）。已发生的副作用不撤销。"""

    key = str(run_key or "").strip()
    async with session_factory() as session:
        run = await session.scalar(
            select(OperationsRun).where(
                OperationsRun.run_key == key,
                OperationsRun.group_id == int(group_id),
            )
        )
        if run is None:
            raise OperationsError(404, REASON_RUN_NOT_FOUND, "没有这次执行记录")
        if run.status in TERMINAL_RUN_STATUSES:
            payload = _run_payload(run)
            payload["cancel_applied"] = False
            payload["worker_finished"] = True
            payload["reason"] = payload["reason"] or REASON_ALREADY_FINISHED
            return payload
        run.cancel_requested = True
        run_id = int(run.id)
        await session.commit()
    event = _CANCEL_EVENTS.get(run_id)
    if event is not None:
        event.set()
    task = _ACTIVE_RUNS.get(run_id)
    waited = False
    if task is not None:
        try:
            async with asyncio.timeout(max(1.0, float(wait_seconds))):
                await asyncio.shield(task)
            waited = True
        except (TimeoutError, asyncio.TimeoutError):
            waited = False
        except asyncio.CancelledError:
            raise
        except Exception:
            waited = True
    payload = await read_run(session_factory, run_key=key, group_id=group_id)
    payload["cancel_applied"] = True
    payload["worker_finished"] = waited
    return payload


async def cancel_requested_persisted(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: int,
) -> bool:
    """读**持久**取消标记（``operations_runs.cancel_requested``）。

    取消边界收口（返工 v6）：取消有两个真源。``request_cancel`` 先 commit 持久标记、之后才
    set 内存事件（``_CANCEL_EVENTS``），所以"取消已经落库、事件还没置位"是一个真实且可
    稳定复现的窗口（父 v16 探针就插在这里）。适配器判断"守卫拒绝的原因是不是本 run
    已被取消"必须看这个真源，不能只信内存事件。

    只读、不写入、不产生副作用；``run_id`` 不合法或读失败一律按"未取消"返回 ``False``
    —— 保守地保持原有 ``guard_blocked`` 归类，绝不把撤权/配置漂移误报成取消。
    """

    identifier = int(run_id or 0)
    if identifier <= 0:
        return False
    try:
        async with session_factory() as session:
            flag = await session.scalar(
                select(OperationsRun.cancel_requested)
                .where(OperationsRun.id == identifier)
                .limit(1)
            )
    except Exception:
        return False
    return bool(flag)


async def retry_run(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Any,
    manager: Any,
    *,
    run_key: object,
    group_id: int,
    actor_user_id: int,
    actor_role: str = "",
    bot: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """安全重试：新建一个 run，跳过已成功项与不可安全重发的未知项。

    重试**不是** force send：任何未知外部结果都不会被重新发一次。
    """

    key = str(run_key or "").strip()
    async with session_factory() as session:
        previous = await session.scalar(
            select(OperationsRun).where(
                OperationsRun.run_key == key,
                OperationsRun.group_id == int(group_id),
            )
        )
        if previous is None:
            raise OperationsError(404, REASON_RUN_NOT_FOUND, "没有这次执行记录")
        if previous.status not in TERMINAL_RUN_STATUSES:
            raise OperationsError(
                409, "run_in_flight", "这次执行还没结束，先取消或等它跑完"
            )
        items = (
            await session.scalars(
                select(OperationsRunItem).where(
                    OperationsRunItem.run_id == int(previous.id)
                )
            )
        ).all()
        retryable = [
            str(row.item_key)
            for row in items
            if bool(row.resendable)
            and row.status
            in (ITEM_STATUS_FAILED, ITEM_STATUS_UNKNOWN, ITEM_STATUS_CANCELLED)
        ]
        if not retryable:
            raise OperationsError(
                409, REASON_NOTHING_TO_RETRY, "这次执行没有可以安全重试的项"
            )
        adapter = get_task(previous.task_id)
        moment = now if isinstance(now, datetime) else now_shanghai_naive()
        revision = int(getattr(manager, "revision", 0) or 0)
        # B13b/C4：重试是一次新的真实动作，按**此刻**的按群策略版本钉死；期间
        # 改过来源/周期计划就让守卫在真实不可逆准入处拒绝（不靠提前 SELECT）。
        from bot.services import task_policy as _task_policy

        policy_revision = await _task_policy.policy_revision(
            session,
            group_id=int(previous.group_id),
            task_id=str(previous.task_id),
        )
        run = OperationsRun(
            run_key=secrets.token_hex(16),
            # R1：重试的 token_id 必须是**确定性**的（= 源 run + 可重试项集合的
            # 摘要），否则双击/并发重试会各自生成一个随机 token_id，唯一的
            # ``token_id`` 约束什么都挡不住，同一批项被独立执行多次。
            token_id=hashlib.sha256(
                f"retry:{int(previous.id)}:{','.join(sorted(retryable))}".encode(
                    "utf-8"
                )
            ).hexdigest()[:32],
            task_id=str(previous.task_id),
            group_id=int(previous.group_id),
            actor_user_id=int(actor_user_id),
            actor_role=str(actor_role or ""),
            params=dict(previous.params or {}),
            guard_revision=revision,
            policy_revision=policy_revision,
            source=SOURCE_RETRY,
            retry_of_run_id=int(previous.id),
            status=RUN_STATUS_ACCEPTED,
            created_at=moment,
            # 目标快照继承"可重试项"：重试只处理上次失败/未知且允许重发的键，
            # 绝不因为"现在又有新到期项"而多发。
            target_keys=list(retryable),
        )
        session.add(run)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise OperationsError(409, REASON_RETRY_CONFLICT, "重试创建冲突，请重试一次")
        blocked = await adapter.recheck_guard(
            session,
            group_id=int(previous.group_id),
            params=dict(previous.params or {}),
            guard_revision=revision,
            manager=manager,
        )
        if blocked:
            await session.rollback()
            raise OperationsError(
                409,
                REASON_GUARD_BLOCKED,
                "重试时复核未通过，本次没有执行任何动作",
                details={"reason": str(blocked)},
            )
        await session.commit()
    _spawn_execution(int(run.id), bot=bot)
    payload = _run_payload(run)
    payload["retry_of_run_key"] = key
    payload["retryable_items"] = retryable
    return payload


async def reconcile_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_key: object,
    group_id: int,
) -> dict[str, Any]:
    """按业务台账对账一次 run：只把"台账已证明"的结论写回，未知仍是未知。"""

    key = str(run_key or "").strip()
    async with session_factory() as session:
        run = await session.scalar(
            select(OperationsRun).where(
                OperationsRun.run_key == key,
                OperationsRun.group_id == int(group_id),
            )
        )
        if run is None:
            raise OperationsError(404, REASON_RUN_NOT_FOUND, "没有这次执行记录")
        items = list(
            (
                await session.scalars(
                    select(OperationsRunItem)
                    .where(OperationsRunItem.run_id == int(run.id))
                    .order_by(OperationsRunItem.id)
                )
            ).all()
        )
        if run.status in RECONCILED_RUN_STATUSES:
            # 已经确定"做过什么"的 run（成功/取消/没做事）不需要再对账；
            # ``unknown`` / ``failed`` / ``partial`` 必须允许对账——否则重启后
            # 那批"未知外部结果"就永远只能停在未知，管理员也无从凭台账收敛。
            payload = _run_payload(run)
            payload["items"] = [_item_payload(item) for item in items]
            payload["reconciled"] = False
            payload["reason"] = payload["reason"] or REASON_ALREADY_FINISHED
            return payload
        adapter = get_task(run.task_id)
        verdict = await adapter.reconcile(session, run=run, items=items)
        if verdict is None:
            payload = _run_payload(run)
            payload["items"] = [_item_payload(item) for item in items]
            payload["reconciled"] = False
            return payload
        for item in verdict.items:
            existing = next(
                (row for row in items if str(row.item_key) == item.item_key), None
            )
            if existing is None:
                existing = OperationsRunItem(
                    run_id=int(run.id),
                    item_key=item.item_key,
                    created_at=now_shanghai_naive(),
                )
                session.add(existing)
                items.append(existing)
            if item.target:
                existing.target = dict(item.target)
            existing.status = item.status
            existing.reason = item.reason
            existing.detail = item.detail
            # R3：对账不得把**已知的出站证据**清零。0 的含义是"没有/未知"，不是
            # "确认没发出去"；把 non-zero 覆盖成 0 等于销毁唯一的外部证据。
            incoming_message_id = int(item.external_message_id or 0)
            if incoming_message_id or not int(existing.external_message_id or 0):
                existing.external_message_id = incoming_message_id
            existing.resendable = bool(item.resendable)
            existing.updated_at = now_shanghai_naive()
        run.status = str(verdict.status)
        run.reason = str(verdict.reason or "")
        run.detail = str(verdict.detail or "")[:300]
        if run.status in TERMINAL_RUN_STATUSES:
            run.finished_at = run.finished_at or now_shanghai_naive()
        await session.flush()
        run.items_succeeded = sum(
            1 for row in items if row.status == ITEM_STATUS_SUCCEEDED
        )
        run.items_failed = sum(1 for row in items if row.status == ITEM_STATUS_FAILED)
        run.items_unknown = sum(1 for row in items if row.status == ITEM_STATUS_UNKNOWN)
        run.items_total = len(items)
        await session.commit()
    payload = await read_run(session_factory, run_key=key, group_id=group_id)
    payload["reconciled"] = True
    return payload


async def await_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_key: object,
    group_id: int,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """等一次 run 落到终态（测试用；**事件驱动**，不靠 sleep 猜）。

    先读一次库：已经终态就直接返回；还在跑就等这个 run 的完成事件（没有事件，
    例如进程重启过，则直接返回当前库内状态，绝不假装它跑完了）。
    """

    key = str(run_key or "").strip()
    payload = await read_run(session_factory, run_key=key, group_id=group_id)
    if payload["finished"]:
        return payload
    run_id = await _run_id_of(session_factory, key)
    event = _DONE_EVENTS.get(int(run_id)) if run_id else None
    if event is None:
        return payload
    try:
        async with asyncio.timeout(max(0.1, float(timeout_seconds))):
            await asyncio.shield(event.wait())
    except (TimeoutError, asyncio.TimeoutError):
        pass
    return await read_run(session_factory, run_key=key, group_id=group_id)


async def _run_id_of(
    session_factory: async_sessionmaker[AsyncSession], run_key: str
) -> int | None:
    async with session_factory() as session:
        value = await session.scalar(
            select(OperationsRun.id).where(OperationsRun.run_key == run_key)
        )
    return int(value) if value is not None else None
