"""Mini App 群永久备忘写操作的有界结果口径（B7）。

`async with session_factory() as session:` 表达不了「这一次写到底有没有生效」：

* commit 正常返回**之前**抛错或被取消时，调用者拿不到「已落库」的证据 ——
  事后再 SELECT 一次、或者看一眼 rowcount，都不能替这次 commit 作证。这种
  情况只能保守地说**未确认**，既不能说「失败、没生效」，也不能凭猜测补一次
  删除或补一次 commit。
* commit 正常返回**之后**的 close 失败属于**资源归还**问题：不能颠倒已经确认
  的持久化结果，也不能把本该 200 的确认改写成 5xx。数据真值确认与资源归还是
  两件事，各自如实上报。

这里只加两个 API 专用 helper，并复用 `bot.services.memory` 里 B6 已经验收的
严格 rowcount 与有界归还实现；不改 B6 的 DTO 状态集合、`on_memory_delete` 与
`delete_permanent_memory*` 的既有行为，也不碰 B6 的测试预期。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm.exc import StaleDataError

from bot.db.models import GroupPermanentMemory
from bot.services.group_scoped_writes import (
    GROUP_ROW_DELETED,
    GROUP_ROW_NO_CHANGE,
    GROUP_ROW_NOT_FOUND,
    GROUP_ROW_UNCONFIRMED,
    delete_group_scoped_row,
)
from bot.services.memory import (
    _pm_release_session,
    _pm_strict_int,
)

log = logging.getLogger(__name__)

#: 终态（对调用者可证明）。四个同型端点共用同一套取值，这里刻意别名到
#: `bot.services.group_scoped_writes`，避免两份字符串常量各自漂移。
PM_WRITE_DELETED = GROUP_ROW_DELETED
PM_WRITE_NO_CHANGE = GROUP_ROW_NO_CHANGE
PM_WRITE_UPDATED = "updated"
#: 入口状态：初始本群预读就没找到（与他群同形，不区分、不泄漏）。
PM_WRITE_NOT_FOUND = GROUP_ROW_NOT_FOUND
#: 预读之后这一行从库里消失了：不是服务器故障，是「已不可用」。
PM_WRITE_UNAVAILABLE = "unavailable"
#: 写没被确认：commit 没正常返回，或 DML 结果不可信。**不是**「没生效」。
PM_WRITE_UNCONFIRMED = GROUP_ROW_UNCONFIRMED

#: 固定原因码。它们只会进服务端日志，不带上原始异常文本、SQL 或正文。
PM_WRITE_REASON_LOOKUP_FAILED = "lookup_failed"
PM_WRITE_REASON_EXECUTE_FAILED = "execute_failed"
PM_WRITE_REASON_FLUSH_FAILED = "flush_failed"
PM_WRITE_REASON_REFRESH_FAILED = "refresh_failed"
PM_WRITE_REASON_COMMIT_FAILED = "commit_failed"
PM_WRITE_REASON_ROWCOUNT_UNKNOWN = "rowcount_unknown"
PM_WRITE_REASON_ROW_MISSING = "row_missing"
PM_WRITE_REASON_CLEANUP_UNRELEASED = "cleanup_unreleased"

#: 用户可见文案。与前端约定：503 说「未确认」，409 说「已不可用」。
PM_WRITE_UNCONFIRMED_MESSAGE = (
    "未能确认这次永久记忆操作是否已生效。请重新查看列表后再决定是否重试。"
)
PM_WRITE_UNAVAILABLE_MESSAGE = "这条永久记忆已不可用，未确认本次修改。请重新查看列表。"


@dataclass(frozen=True, slots=True)
class PermanentMemoryRowDeleteOutcome:
    """一条永久备忘的删除结果。`changed_rows` 只在 rowcount 严格可信时才有值。"""

    status: str
    changed_rows: int | None = None
    detail: str = ""
    #: 数据真值与资源归还分开上报：全部恢复手段都失败时为 False（并留固定日志），
    #: 但这**不推翻**已经确认的持久化结果。
    resource_released: bool = True

    @property
    def confirmed_deleted(self) -> bool:
        return self.status == PM_WRITE_DELETED

    @property
    def confirmed_no_change(self) -> bool:
        return self.status == PM_WRITE_NO_CHANGE


@dataclass(frozen=True, slots=True)
class PermanentMemoryRowUpdateOutcome:
    """一条永久备忘的修改结果。`document` 是真实写入后的标量快照。"""

    status: str
    document: dict[str, Any] | None = None
    detail: str = ""
    #: 同 `PermanentMemoryRowDeleteOutcome.resource_released`。
    resource_released: bool = True

    @property
    def confirmed_updated(self) -> bool:
        return self.status == PM_WRITE_UPDATED

    @property
    def unavailable(self) -> bool:
        return self.status == PM_WRITE_UNAVAILABLE


def _same_group(row: Any, group_id: int) -> bool:
    """群边界与稳定 ID 一起判：跨群行一律当作「不存在」，不泄漏它属于谁。"""

    row_group = _pm_strict_int(getattr(row, "group_id", None))
    return row_group is not None and row_group == group_id


def _log_unreleased(operation: str) -> None:
    # 所有恢复手段都失败时只留一个固定的失败信号：不假装已经成功归还，
    # 也不在这里强行 release 全局锁。
    log.warning(
        "permanent memory write cleanup unreleased | operation=%s | reason=%s",
        operation,
        PM_WRITE_REASON_CLEANUP_UNRELEASED,
    )


async def _row_confirmed_absent(session: Any, memory_id: int) -> bool:
    """有界的只读探测：这一行是不是真的不在了。探测本身失败就当「不知道」。"""

    try:
        await session.rollback()
        return await session.get(GroupPermanentMemory, memory_id) is None
    except Exception:  # noqa: BLE001 - 探测失败只能是「不知道」，不能是 409
        return False


async def delete_permanent_memory_row(
    session_factory: Any,
    *,
    group_id: int,
    memory_id: int,
) -> PermanentMemoryRowDeleteOutcome:
    """按稳定 ID 原子删除本群的一条永久备忘，并给出可证明的三态结果。

    B8 起实现已泛化为 `bot.services.group_scoped_writes.delete_group_scoped_row`
    （同一套严格 rowcount / 有界归还 / 取消语义，四个同型端点共用）；本函数只是
    B7 已验收的入口，行为与返回类型逐字不变。

    正常路径：1 次本群预读 SELECT（用来把「初始就不存在/别的群」判成 404）+
    1 次 `DELETE ... WHERE group_id AND id` + 1 次真实 commit。rowcount 严格
    取 0/1；读不到、负数、非 0/1 一律当**未知**，不伪造 0。
    """

    outcome = await delete_group_scoped_row(
        session_factory,
        model=GroupPermanentMemory,
        group_id=group_id,
        row_id=memory_id,
        operation="delete_permanent_memory",
    )
    # 四个端点共用同一套终态字符串；这里只做类型归属，把 B7 的返回类型原样交回。
    return PermanentMemoryRowDeleteOutcome(
        status=outcome.status,
        changed_rows=outcome.changed_rows,
        detail=outcome.detail,
        resource_released=outcome.resource_released,
    )


async def update_permanent_memory_row(
    session_factory: Any,
    *,
    group_id: int,
    memory_id: int,
    content: str,
    document_factory: Callable[[Any], dict[str, Any]],
) -> PermanentMemoryRowUpdateOutcome:
    """改一条本群永久备忘的正文，并在 ack 之前取好真实写入后的标量文档。

    `updated_at` 带 `onupdate=func.now()`，flush 之后这个属性会被标成过期；
    在异步会话里顺手读它就是 `MissingGreenlet`。所以这里先真 flush，再显式
    `await session.refresh(row)` 拿回**数据库真正写入的**值，然后把五个标量
    字段物化成普通 dict —— commit 正常返回之后不再碰任何可能过期的 ORM 字段。
    同内容保存不产生 UPDATE（既有语义），仍然返回一份正确的文档，且不改
    `created_by` / `created_at`。
    """

    session = session_factory()
    original_cancel: asyncio.CancelledError | None = None
    phase = PM_WRITE_REASON_LOOKUP_FAILED
    document: dict[str, Any] | None = None
    committed = False
    unavailable = False
    not_found = False
    try:
        try:
            row = await session.get(GroupPermanentMemory, memory_id)
            if row is None or not _same_group(row, group_id):
                not_found = True
            else:
                row.content = content
                phase = PM_WRITE_REASON_FLUSH_FAILED
                await session.flush()
                phase = PM_WRITE_REASON_REFRESH_FAILED
                await session.refresh(row)
                document = document_factory(row)
                phase = PM_WRITE_REASON_COMMIT_FAILED
                await session.commit()
                committed = True
        except asyncio.CancelledError as exc:
            original_cancel = exc
        except (StaleDataError, InvalidRequestError):
            # 预读之后这行没了：要么 UPDATE matched 0（flush/commit 抛
            # StaleDataError），要么根本没有 UPDATE 可发（同内容 + 行已删，
            # refresh 找不到实例）。确认它真的不在库里就是「已不可用」，否则
            # 一律退回未确认 —— 不能把「我们没搞懂」说成 409。
            unavailable = await _row_confirmed_absent(session, memory_id)
            if unavailable:
                phase = PM_WRITE_REASON_ROW_MISSING
        except Exception:  # noqa: BLE001
            document = None
    finally:
        released = await _pm_release_session(
            session,
            rollback_first=not committed,
            suppress_cancel=original_cancel is not None,
        )
        if not released:
            _log_unreleased("update")

    if original_cancel is not None:
        raise original_cancel
    if not_found:
        return PermanentMemoryRowUpdateOutcome(
            status=PM_WRITE_NOT_FOUND, resource_released=released
        )
    if unavailable:
        return PermanentMemoryRowUpdateOutcome(
            status=PM_WRITE_UNAVAILABLE,
            detail=PM_WRITE_REASON_ROW_MISSING,
            resource_released=released,
        )
    if not committed or document is None:
        return PermanentMemoryRowUpdateOutcome(
            status=PM_WRITE_UNCONFIRMED, detail=phase, resource_released=released
        )
    return PermanentMemoryRowUpdateOutcome(
        status=PM_WRITE_UPDATED, document=document, resource_released=released
    )
