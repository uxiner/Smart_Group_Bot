"""按群作用域的资源行删除口径（B8）。

Mini App 里有四个资源删除端点（群规、关键词回复、定时消息、永久备忘）原本都是
同一形状：`session.get()` 预读 → `session.delete(row)` → `commit()` → 返回
`{"deleted": true}`。这个形状有一个共同的空洞 ——

* 预读拿到行之后，另一个独立 writer 先把这一行删掉并提交；本连接随后的 DML
  匹配 0 行，但 `commit()` 依然正常返回，端点照样回「本次删除成功」。
* ORM 的 `session.delete()` 是「按对象删」，它既不给调用者一个可信的 rowcount，
  也不把群边界放进同一条 DML 条件里。

这里给出一个**通用**实现：一条 `DELETE ... WHERE group_id = ? AND id = ?` 的原子
语句 + 严格 rowcount（只认 0/1）+ 真实 commit。群边界与稳定 ID 在同一条 DML
条件里，因此「跨群」不会变成一次误删。终态、原因码、有界归还与取消语义复用
B6/B7 已经验收的实现（`bot.services.memory` 的三个 helper），不另写一套。

数据真值确认与资源归还始终分开上报：commit 正常返回且 rowcount 可信就是确认
结果，随后的 close 故障不推翻它；全部恢复手段都失败时如实上报
`resource_released=False` 并留一个固定故障信号。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable
from types import SimpleNamespace
from copy import deepcopy

from sqlalchemy import delete, update

from bot.services.memory import (
    _pm_release_session,
    _pm_single_row_change,
    _pm_strict_int,
)

log = logging.getLogger(__name__)

#: 与 B7 逐字一致的终态取值，前端四类资源共用同一套判据。
GROUP_ROW_DELETED = "deleted"
GROUP_ROW_NO_CHANGE = "no_change"
#: 初始本群预读就没找到（与他群同形，不区分、不泄漏存在性）。
GROUP_ROW_NOT_FOUND = "not_found"
#: 写没被确认：commit 没正常返回，或 DML 结果不可信。**不是**「没生效」。
GROUP_ROW_UNCONFIRMED = "unconfirmed"

#: 固定原因码：只进服务端日志，不带上原始异常文本、SQL 或正文。
GROUP_ROW_REASON_LOOKUP_FAILED = "lookup_failed"
GROUP_ROW_REASON_EXECUTE_FAILED = "execute_failed"
GROUP_ROW_REASON_COMMIT_FAILED = "commit_failed"
GROUP_ROW_REASON_ROWCOUNT_UNKNOWN = "rowcount_unknown"
GROUP_ROW_REASON_CLEANUP_UNRELEASED = "cleanup_unreleased"


@dataclass(frozen=True, slots=True)
class GroupScopedDeleteOutcome:
    """一条按群作用域删除的结果。`changed_rows` 只在 rowcount 严格可信时才有值。"""

    status: str
    changed_rows: int | None = None
    detail: str = ""
    #: 数据真值与资源归还分开上报：全部恢复手段都失败时为 False（并留固定日志），
    #: 但这**不推翻**已经确认的持久化结果。
    resource_released: bool = True

    @property
    def confirmed_deleted(self) -> bool:
        return self.status == GROUP_ROW_DELETED

    @property
    def confirmed_no_change(self) -> bool:
        return self.status == GROUP_ROW_NO_CHANGE

    @property
    def not_found(self) -> bool:
        return self.status == GROUP_ROW_NOT_FOUND

    @property
    def unconfirmed(self) -> bool:
        return self.status == GROUP_ROW_UNCONFIRMED


def _same_group(row: Any, group_id: int) -> bool:
    """群边界与稳定 ID 一起判：跨群行一律当作「不存在」，不泄漏它属于谁。"""

    row_group = _pm_strict_int(getattr(row, "group_id", None))
    return row_group is not None and row_group == group_id


def _log_unreleased(operation: str) -> None:
    # 所有恢复手段都失败时只留一个固定的失败信号：不假装已经成功归还，
    # 也不在这里强行 release 全局锁。
    log.warning(
        "group scoped write cleanup unreleased | operation=%s | reason=%s",
        operation,
        GROUP_ROW_REASON_CLEANUP_UNRELEASED,
    )


async def delete_group_scoped_row(
    session_factory: Any,
    *,
    model: Any,
    group_id: int,
    row_id: int,
    operation: str,
) -> GroupScopedDeleteOutcome:
    """按稳定 ID 原子删除本群的一行，并给出可证明的三态结果。

    正常路径：1 次本群预读 SELECT（用来把「初始就不存在/别的群」判成 404）+
    1 次 `DELETE ... WHERE group_id AND id` + 1 次真实 commit。rowcount 严格
    取 0/1；读不到、负数、非 0/1 一律当**未知**，不伪造 0。
    """

    session = session_factory()
    original_cancel: asyncio.CancelledError | None = None
    phase = GROUP_ROW_REASON_LOOKUP_FAILED
    changed: int | None = None
    committed = False
    not_found = False
    try:
        try:
            row = await session.get(model, row_id)
            if row is None or not _same_group(row, group_id):
                not_found = True
            else:
                phase = GROUP_ROW_REASON_EXECUTE_FAILED
                result = await session.execute(
                    delete(model).where(
                        model.group_id == group_id,
                        model.id == row_id,
                    )
                )
                changed = _pm_single_row_change(result)
                phase = GROUP_ROW_REASON_COMMIT_FAILED
                await session.commit()
                committed = True
        except asyncio.CancelledError as exc:
            original_cancel = exc
        except Exception:  # noqa: BLE001 - 具体类型只进固定原因码，不进响应
            changed = None
    finally:
        released = await _pm_release_session(
            session,
            rollback_first=not committed,
            suppress_cancel=original_cancel is not None,
        )
        if not released:
            _log_unreleased(operation)

    if original_cancel is not None:
        raise original_cancel
    if not_found:
        return GroupScopedDeleteOutcome(
            status=GROUP_ROW_NOT_FOUND, resource_released=released
        )
    if not committed:
        return GroupScopedDeleteOutcome(
            status=GROUP_ROW_UNCONFIRMED, detail=phase, resource_released=released
        )
    if changed is None:
        return GroupScopedDeleteOutcome(
            status=GROUP_ROW_UNCONFIRMED,
            detail=GROUP_ROW_REASON_ROWCOUNT_UNKNOWN,
            resource_released=released,
        )
    if changed == 1:
        return GroupScopedDeleteOutcome(
            status=GROUP_ROW_DELETED, changed_rows=1, resource_released=released
        )
    return GroupScopedDeleteOutcome(
        status=GROUP_ROW_NO_CHANGE, changed_rows=0, resource_released=released
    )


@dataclass(frozen=True, slots=True)
class GroupScopedUpdateOutcome:
    status: str
    document: dict[str, Any] | None = None
    detail: str = ""
    resource_released: bool = True


async def update_group_scoped_row(
    session_factory: Any, *, model: Any, group_id: int, row_id: int,
    prepare: Callable[[Any], None], document_factory: Callable[[Any], dict[str, Any]],
) -> GroupScopedUpdateOutcome:
    """B10: matched group+id UPDATE, pre-ack materialization, shared cleanup.

    Preserve baseline last-writer-wins (no implicit CAS). Validation changes
    a detached scalar copy only. Only changed columns are written, except a
    same-value request still issues a matched UPDATE to detect disappearing rows.
    """
    session = session_factory()
    original_cancel = None
    committed = False
    phase = "lookup_failed"
    status = GROUP_ROW_UNCONFIRMED
    document = None
    try:
        try:
            row = await session.get(model, row_id)
            if row is None or not _same_group(row, group_id):
                status = GROUP_ROW_NOT_FOUND
            else:
                before = {c.key: deepcopy(getattr(row, c.key)) for c in model.__table__.columns}
                candidate = SimpleNamespace(**deepcopy(before))
                phase = "validate"
                prepare(candidate)
                values = {k: v for k, v in vars(candidate).items()
                          if k not in {"id", "group_id"} and v != before[k]}
                phase = "execute_failed"
                result = await session.execute(
                    update(model).where(model.group_id == group_id, model.id == row_id)
                    .values(**(values or {"id": row_id}))
                    .execution_options(synchronize_session=False)
                )
                matched = _pm_single_row_change(result)
                if matched == 0:
                    status = "unavailable"
                    phase = "row_missing"
                elif matched != 1:
                    phase = GROUP_ROW_REASON_ROWCOUNT_UNKNOWN
                else:
                    phase = "flush_failed"
                    await session.flush()
                    phase = "refresh_failed"
                    await session.refresh(row)
                    document = deepcopy(document_factory(row))
                    phase = GROUP_ROW_REASON_COMMIT_FAILED
                    await session.commit()
                    committed = True
                    status = "updated"
        except asyncio.CancelledError as exc:
            original_cancel = exc
        except Exception:
            if phase == "validate":
                raise  # preserve existing validation codes, never write invalid data
            document = None
    finally:
        released = await _pm_release_session(
            session, rollback_first=not committed,
            suppress_cancel=original_cancel is not None,
        )
        if not released:
            _log_unreleased("update")
            if original_cancel is None:
                # Fail closed even after commit: never a normal successful return.
                raise RuntimeError("group_resource_cleanup_unreleased")
    if original_cancel is not None:
        raise original_cancel
    return GroupScopedUpdateOutcome(status, document, phase, released)
