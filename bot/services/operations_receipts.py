"""真实出站的**持久业务 receipt**（B13a 返工 R1）。

运营任务中心、常驻后台 runner 与 CLI 是**同一条业务链的三个入口**。只把结论写进
``operations_run_items`` 是不够的：进程死在"发送成功"与"记账成功"之间时，run 审计
要么还没写、要么写着 unknown，换一个入口重跑就会把同一条消息再发一遍。

所以这里维护一张只追加的小表：``(task_id, group_id, business_key)`` → 出站证据。
业务键是**稳定业务身份**（群 + 时段 / 条目 + occurrence / 周 / 权益 + 版本），
而不是 run id，也不是"内存里的这次循环"。

三条硬规则：

1. **先落保护再出站**：受保护的入口在真实出站**之前**先 ``claim_business_dispatch``
   抢一条 ``dispatching`` 行，并在**独立事务**里 commit；出站有结果后（成功或未知）
   再用 ``record_receipt`` 把同一行改成 ``sent``/``unknown``。
2. **未知禁止重发**：``sent``/``unknown`` 与出站前的 ``dispatching`` 一样阻断重发；
   只有能证明"明确没出站"的路径（发送前失败、Telegram 明确拒收）才允许
   ``release_business_dispatch`` 删掉保护。进程死在 send 与记账之间、或出站之后
   确认写入失败，保护行都还在库里。
3. **永不 downgrade**：``sent`` 不会被后来的 ``unknown`` 覆盖，也不会被
   ``dispatching`` 覆盖；``external_message_id`` 取已知的非零值。

本模块只负责这张表的读写，不做任何业务判断，也不打印。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import delete, literal, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import OperationsBusinessReceipt
from bot.services.operations_guard import PersistedGuard, guard_predicate
from bot.utils.timezone import now_shanghai_naive

#: 出站**之前**的保守保护态：本入口声明"这条业务键由我负责发送"，在真实出站结果
#: 落盘之前一直有效。进程死在 send 与记账之间、或出站之后确认写入失败时，这一行
#: 仍然在库里，所有入口（运营中心 / 常驻 runner / CLI / reaper / 对账）都看得见，
#: 因此不会再发第二条。
RECEIPT_STATE_DISPATCHING = "dispatching"
#: 拿到真实出站结果（``external_message_id`` 可能仍为 0）。
RECEIPT_STATE_SENT = "sent"
#: 出站结果未知（超时 / 断连 / 取消 / 出站后确认写入失败）。
RECEIPT_STATE_UNKNOWN = "unknown"
#: 任何一条 receipt（含出站前的 ``dispatching``）都阻断重发。
RECEIPT_STATES = (
    RECEIPT_STATE_DISPATCHING,
    RECEIPT_STATE_SENT,
    RECEIPT_STATE_UNKNOWN,
)
#: 出站**之后**才可能出现的状态：这些状态可以断言"已经有真实出站动作"。
OUTBOUND_RECEIPT_STATES = (RECEIPT_STATE_SENT, RECEIPT_STATE_UNKNOWN)

#: 六类任务里会产生真实出站动作的三类（加上 shop 的撤销/通知）。
TASK_CHECKIN_REMINDER = "checkin_reminder"
TASK_SCHEDULED_MESSAGE = "scheduled_message"
TASK_WEEKLY_REPORT = "weekly_report"
TASK_SHOP_EXPIRE = "shop_expire"
#: 摘要不发消息，但**发布一个新版本**是不可撤销的业务副作用（R3）：手动发布成功
#: 之后用 receipt 记下"这次 run 真的发布了 vN/source revision/覆盖水位"，对账只认
#: 这份本次 run 的证据，不再靠 GroupSummary.generated_at 的时间水位猜归属。
TASK_GROUP_SUMMARY = "group_summary"

_MAX_BUSINESS_KEY = 200
_MAX_DETAIL = 300


@dataclass(frozen=True, slots=True)
class BusinessReceipt:
    """一行出站证据（只读快照）。"""

    task_id: str
    group_id: int
    business_key: str
    state: str
    external_message_id: int
    detail: str
    created_at: datetime | None
    updated_at: datetime | None

    @property
    def outbound_committed(self) -> bool:
        """True = 这条业务键上已经有真实出站动作（``sent``/``unknown``）。"""

        return self.state in OUTBOUND_RECEIPT_STATES

    @property
    def protected(self) -> bool:
        """True = 这条业务键正被某个入口保护（含只写了出站前保护的 ``dispatching``）。"""

        return self.state in RECEIPT_STATES


def receipt_key(*parts: Any) -> str:
    """把业务键的各维度拼成稳定的定长字符串（不含随机成分）。"""

    return ":".join(str(part) for part in parts)[:_MAX_BUSINESS_KEY]


def _snapshot(row: OperationsBusinessReceipt) -> BusinessReceipt:
    return BusinessReceipt(
        task_id=str(row.task_id),
        group_id=int(row.group_id),
        business_key=str(row.business_key),
        state=str(row.state),
        external_message_id=int(row.external_message_id or 0),
        detail=str(row.detail or ""),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def load_receipt(
    session: AsyncSession,
    *,
    task_id: str,
    group_id: int,
    business_key: str,
) -> BusinessReceipt | None:
    """读一条出站证据；没有则返回 ``None``（**不是**"没发过"的证明）。"""

    row = await session.scalar(
        select(OperationsBusinessReceipt)
        .where(
            OperationsBusinessReceipt.task_id == str(task_id),
            OperationsBusinessReceipt.group_id == int(group_id),
            OperationsBusinessReceipt.business_key == str(business_key)[:_MAX_BUSINESS_KEY],
        )
        .limit(1)
    )
    return None if row is None else _snapshot(row)


async def has_receipt(
    session: AsyncSession,
    *,
    task_id: str,
    group_id: int,
    business_key: str,
) -> bool:
    """这条业务键是否已有出站证据（``sent`` 或 ``unknown`` 都算）。"""

    return await load_receipt(
        session,
        task_id=task_id,
        group_id=group_id,
        business_key=business_key,
    ) is not None


async def load_receipts(
    session: AsyncSession,
    *,
    task_id: str,
    group_id: int,
    business_keys: Iterable[str],
) -> dict[str, BusinessReceipt]:
    """批量读（一次查询），键为 ``business_key``。"""

    keys = [str(key)[:_MAX_BUSINESS_KEY] for key in business_keys]
    if not keys:
        return {}
    rows = (
        await session.scalars(
            select(OperationsBusinessReceipt).where(
                OperationsBusinessReceipt.task_id == str(task_id),
                OperationsBusinessReceipt.group_id == int(group_id),
                OperationsBusinessReceipt.business_key.in_(keys),
            )
        )
    ).all()
    return {str(row.business_key): _snapshot(row) for row in rows}


async def record_receipt(
    session: AsyncSession,
    *,
    task_id: str,
    group_id: int,
    business_key: str,
    state: str,
    external_message_id: int = 0,
    detail: str = "",
    now: datetime | None = None,
) -> BusinessReceipt:
    """写入/更新一条出站证据（**调用方负责 commit**，事务边界由业务决定）。

    更新是保守的：``sent`` 不会被 ``unknown`` 覆盖，``external_message_id`` 只在
    已知非零时更新。这样"先记 sent 后补 unknown"的乱序写入也不会丢失已确认的出站。
    """

    if state not in RECEIPT_STATES:
        raise ValueError(f"unknown receipt state: {state!r}")
    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    key = str(business_key)[:_MAX_BUSINESS_KEY]
    row = await session.scalar(
        select(OperationsBusinessReceipt)
        .where(
            OperationsBusinessReceipt.task_id == str(task_id),
            OperationsBusinessReceipt.group_id == int(group_id),
            OperationsBusinessReceipt.business_key == key,
        )
        .limit(1)
    )
    message_id = int(external_message_id or 0)
    if row is None:
        row = OperationsBusinessReceipt(
            task_id=str(task_id),
            group_id=int(group_id),
            business_key=key,
            state=str(state),
            external_message_id=message_id,
            detail=str(detail or "")[:_MAX_DETAIL],
            created_at=moment,
            updated_at=moment,
        )
        session.add(row)
    else:
        # 永不 downgrade：``sent`` 不被任何状态覆盖；``dispatching`` 只用于出站**前**
        # 抢占保护，绝不允许把已经写成 ``unknown`` 的行改回"还没发"。
        if row.state != RECEIPT_STATE_SENT and state != RECEIPT_STATE_DISPATCHING:
            row.state = str(state)
        if message_id and int(row.external_message_id or 0) == 0:
            row.external_message_id = message_id
        if detail:
            row.detail = str(detail)[:_MAX_DETAIL]
        row.updated_at = moment
    await session.flush()
    return _snapshot(row)


async def claim_business_dispatch(
    session: AsyncSession,
    *,
    task_id: str,
    group_id: int,
    business_key: str,
    detail: str = "",
    now: datetime | None = None,
    guard: "PersistedGuard | None" = None,
) -> BusinessReceipt | None:
    """真实出站**之前**抢占这条业务键的持久保护（返回 ``None`` = 没抢到）。

    这是"只写结果"收口不了的缺口：进程若死在 send 与 receipt 之间，或者出站之后
    的确认写入直接失败，靠事后补写就永远补不上。所以这里改成**先落保护再发**：

    * 用 ``INSERT ... ON CONFLICT DO NOTHING`` 抢占；唯一约束保证并发只有一个赢家，
      赢家之后才允许执行真实出站；
    * 调用方必须在真实出站**之前** commit 这一行（独立事务），此后任何入口
      （运营中心 / 常驻 runner / CLI / reaper）读到的都是"这条业务键已被保护"；
    * 出站有结果后用 :func:`record_receipt` 把**同一行**改成 ``sent``/``unknown``
      （因此一条业务键始终只有一行 receipt）；
    * 只有能**证明没有出站**的路径才允许 :func:`release_business_dispatch`。

    本函数不 commit，事务边界由调用方决定（与 :func:`record_receipt` 一致）。

    ``guard``（返工 v3 / R2）把"这一刻还算不算合法执行"（群授权、发起人当前权限、
    持久配置 revision、这个 run 是否已被持久取消）编译进**同一条 claim SQL**：
    ``INSERT ... SELECT ... WHERE <guard> ON CONFLICT DO NOTHING``。于是"抢到保护"
    就等于"在抢占这一刻仍然合法"——起点复核与真实出站之间那几次 ``await`` 里发生的
    撤权/改配置再也钻不进来。``guard is None`` 时完全保持既有行为（legacy CLI 与
    常驻 runner 没有运营中心 run 上下文，不替它们制造发起人）。
    """

    moment = now if isinstance(now, datetime) else now_shanghai_naive()
    key = str(business_key)[:_MAX_BUSINESS_KEY]
    detail_text = str(detail or "")[:_MAX_DETAIL]
    if guard is None:
        statement = (
            sqlite_insert(OperationsBusinessReceipt)
            .values(
                task_id=str(task_id),
                group_id=int(group_id),
                business_key=key,
                state=RECEIPT_STATE_DISPATCHING,
                external_message_id=0,
                detail=detail_text,
                created_at=moment,
                updated_at=moment,
            )
            .on_conflict_do_nothing(
                index_elements=["task_id", "group_id", "business_key"]
            )
            .returning(OperationsBusinessReceipt.id)
        )
    else:
        statement = (
            sqlite_insert(OperationsBusinessReceipt)
            .from_select(
                [
                    "task_id",
                    "group_id",
                    "business_key",
                    "state",
                    "external_message_id",
                    "detail",
                    "created_at",
                    "updated_at",
                ],
                select(
                    literal(str(task_id)),
                    literal(int(group_id)),
                    literal(key),
                    literal(RECEIPT_STATE_DISPATCHING),
                    literal(0),
                    literal(detail_text),
                    literal(moment),
                    literal(moment),
                ).where(guard_predicate(guard)),
            )
            .on_conflict_do_nothing(
                index_elements=["task_id", "group_id", "business_key"]
            )
            .returning(OperationsBusinessReceipt.id)
        )
    claimed = (await session.execute(statement)).scalar_one_or_none()
    if claimed is None:
        return None
    return BusinessReceipt(
        task_id=str(task_id),
        group_id=int(group_id),
        business_key=key,
        state=RECEIPT_STATE_DISPATCHING,
        external_message_id=0,
        detail=detail_text,
        created_at=moment,
        updated_at=moment,
    )


async def release_business_dispatch(
    session: AsyncSession,
    *,
    task_id: str,
    group_id: int,
    business_key: str,
) -> bool:
    """删掉**出站前**的保护行（返回是否真的删掉了一行）。

    只允许在能证明"明确没有出站"的路径上调用（发送前渲染/校验失败、Telegram 明确
    拒收）。``sent``/``unknown`` 永远不会被这个函数删掉——那等于把未知重新解锁成
    可以重发。本函数不 commit。
    """

    result = await session.execute(
        delete(OperationsBusinessReceipt).where(
            OperationsBusinessReceipt.task_id == str(task_id),
            OperationsBusinessReceipt.group_id == int(group_id),
            OperationsBusinessReceipt.business_key
            == str(business_key)[:_MAX_BUSINESS_KEY],
            OperationsBusinessReceipt.state == RECEIPT_STATE_DISPATCHING,
        )
    )
    return bool(getattr(result, "rowcount", 0))


async def clear_receipt(
    session: AsyncSession,
    *,
    task_id: str,
    group_id: int,
    business_key: str,
) -> None:
    """删除一条出站证据。

    只允许在**明确未出站**的恢复路径上调用（例如渲染失败、发送前参数错误）。任何
    "结果未知"的分支都不许走这里——那等于把未知重新解锁成可以重发。
    """

    await session.execute(
        delete(OperationsBusinessReceipt).where(
            OperationsBusinessReceipt.task_id == str(task_id),
            OperationsBusinessReceipt.group_id == int(group_id),
            OperationsBusinessReceipt.business_key
            == str(business_key)[:_MAX_BUSINESS_KEY],
        )
    )


#: 传输层错误：消息**可能已经送达**，只是回执丢了——永远不算"确定没发出去"。
_TRANSPORT_UNKNOWN_EXCEPTIONS = (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)

#: Telegram **明确拒收**这条消息的真实 aiogram 异常类型（服务端返回 4xx 错误响应，
#: aiogram 用响应里的 ``error_code``/``description`` 构造出这些类）。只有真正的
#: aiogram 对象才算证据：同名的自定义类、以及"文本里恰好带拒绝字样"的普通异常
#: 一律不算——它们无法证明消息没有出站。
_REFUSAL_EXCEPTION_TYPES: tuple[type[BaseException], ...] | None = None

#: 网络/服务端/传输未知类型。在 aiogram 3 里 ``TelegramNetworkError`` 与
#: ``TelegramServerError`` 也继承 ``TelegramAPIError``，所以必须先按类型把它们排除：
#: 代理/隧道/旧响应会把这类错误的文本弄得像"服务端拒收"（例如
#: ``TelegramNetworkError(message="proxy connection failed: chat not found")``），
#: 但那不是拒收凭证，只能保持"结果未知"。
_TRANSPORT_UNKNOWN_TYPES: tuple[type[BaseException], ...] | None = None


def _refusal_exception_types() -> tuple[type[BaseException], ...]:
    """真实 aiogram 明确拒收类型（懒加载并缓存，避免模块导入期硬依赖顺序）。"""

    global _REFUSAL_EXCEPTION_TYPES
    if _REFUSAL_EXCEPTION_TYPES is None:
        try:
            from aiogram.exceptions import (
                TelegramBadRequest,
                TelegramConflictError,
                TelegramForbiddenError,
                TelegramNotFound,
                TelegramRetryAfter,
                TelegramUnauthorizedError,
            )
        except Exception:  # pragma: no cover - aiogram 是硬依赖，防御性兜底
            _REFUSAL_EXCEPTION_TYPES = ()
        else:
            _REFUSAL_EXCEPTION_TYPES = (
                TelegramBadRequest,
                TelegramConflictError,
                TelegramForbiddenError,
                TelegramNotFound,
                TelegramRetryAfter,
                TelegramUnauthorizedError,
            )
    return _REFUSAL_EXCEPTION_TYPES


def _unknown_transport_exception_types() -> tuple[type[BaseException], ...]:
    """aiogram 里"结果未知"的网络/服务端错误类型（含 ``TelegramEntityTooLarge``）。"""

    global _TRANSPORT_UNKNOWN_TYPES
    if _TRANSPORT_UNKNOWN_TYPES is None:
        try:
            from aiogram.exceptions import TelegramNetworkError, TelegramServerError
        except Exception:  # pragma: no cover - aiogram 是硬依赖，防御性兜底
            _TRANSPORT_UNKNOWN_TYPES = ()
        else:
            _TRANSPORT_UNKNOWN_TYPES = (TelegramNetworkError, TelegramServerError)
    return _TRANSPORT_UNKNOWN_TYPES


def definitely_not_delivered(exc: BaseException) -> bool:
    """这次发送异常是否**能证明消息没有出站**（可以安全重试）。

    这是 :func:`ambiguous_send_failure` 的**反选**版本，供出站前已经落保护的路径
    使用。判据只有一条：抛出的是**真实的 aiogram 明确拒收类型**（服务端 4xx 错误
    响应：参数错、被踢、群不存在、限流……）。除此之外一律保持"可能已出站"：

    * 超时、断连、``OSError``/``ConnectionError`` 以及取消：结果未知；
    * 真实 aiogram 网络/服务端错误（``TelegramNetworkError``、
      ``TelegramServerError``，在 3.x 里它们**也继承** ``TelegramAPIError``）：
      结果未知——代理隧道会把嵌套的旧响应文本带进来，文本不能当证据；
    * 任何普通异常（``RuntimeError`` 等），即使文本里带 "chat not found"
      "blocked by the user" 这类字样：不是服务端拒收凭证；
    * 仅仅类名相同但并非 aiogram 构造的异常：不算。
    """

    if isinstance(exc, asyncio.CancelledError):
        # 取消不等于"服务端拒收"：必须原样向上传播，由调用方保留保护。
        return False
    if isinstance(exc, _TRANSPORT_UNKNOWN_EXCEPTIONS):
        return False
    unknown_types = _unknown_transport_exception_types()
    if unknown_types and isinstance(exc, unknown_types):
        return False
    refusal_types = _refusal_exception_types()
    if not refusal_types:
        return False
    return isinstance(exc, refusal_types)


def ambiguous_send_failure(exc: BaseException) -> bool:
    """这次发送异常是否意味着"可能已经出站"（只能标 unknown，不能当未发）。

    **注意**：这是既有 legacy 入口的窄判据（沿用 B13a-R1 的行为，只把超时算歧义），
    新的受保护路径必须用 :func:`definitely_not_delivered` 反选，不要用这个函数决定
    "可以重发"。超时与连接层错误（断连、连接被重置）下消息**可能已经送达**；这里
    把这两类都算歧义，其余异常仍按既有 at-least-once 策略释放占位并允许重试。
    """

    return isinstance(
        exc, (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)
    )


__all__ = [
    "BusinessReceipt",
    "OUTBOUND_RECEIPT_STATES",
    "RECEIPT_STATE_DISPATCHING",
    "RECEIPT_STATE_SENT",
    "RECEIPT_STATE_UNKNOWN",
    "RECEIPT_STATES",
    "TASK_CHECKIN_REMINDER",
    "TASK_GROUP_SUMMARY",
    "TASK_SCHEDULED_MESSAGE",
    "TASK_SHOP_EXPIRE",
    "TASK_WEEKLY_REPORT",
    "ambiguous_send_failure",
    "claim_business_dispatch",
    "clear_receipt",
    "definitely_not_delivered",
    "has_receipt",
    "load_receipt",
    "load_receipts",
    "receipt_key",
    "record_receipt",
    "release_business_dispatch",
]
