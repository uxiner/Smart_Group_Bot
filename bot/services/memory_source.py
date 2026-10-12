"""C3 受控原文展开：按 stable fact id 读取**当前群**里那条来源消息的原文。

设计红线（与 B12/C3 合同一致）：

* **不改库、不调模型、不外发**：本模块只有只读查询与纯字符串处理；
* **不允许回退到** ``UserFact.evidence_excerpt``：那是写入时截下的片段，不是可核
  验的现归档，拿它当"原文"会绕开当前可见性；
* **身份一致性**：归档行必须属于**同一个 group**，且 ``telegram_message_id`` 与
  事实的 ``source_message_id`` 相同；thread 只信这条归档行里现读出来的值；
* **三态可区分**：已确认删除 / 缺失不可用 / 无法验证；查询故障一律 fail-closed；
* **长度与脱敏**：最多 :data:`SOURCE_TEXT_MAX_CHARS` 个字符并给出 ``truncated``，
  脱敏复用写入闸门那套规则（:func:`redact_sensitive_excerpt`）；
* **纯文本**：返回值永远是字符串，调用方负责转义；HTML/指令/恶意 URL 不解析。

私聊（``scope='private'``）**没有**受支持的原文归档：直接返回不可用，不开新的私聊
读取面（本人入口因此不会因为这个功能获得额外的读权限）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from bot.db.models import GroupMessageArchive, MemoryOptout, UserFact
from bot.services.long_term_memory import (
    STATUS_ACTIVE,
    normalize_source_kind,
    redact_sensitive_excerpt,
)
from bot.utils.message_links import message_link, trusted_thread_id
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

#: 一次展开最多返回多少字符（Unicode 字符数，不是字节数）
SOURCE_TEXT_MAX_CHARS = 2000

#: 状态
SOURCE_OK = "ok"
SOURCE_DELETED = "deleted"
SOURCE_MISSING = "missing"
SOURCE_UNVERIFIABLE = "unverifiable"
SOURCE_NOT_VISIBLE = "not_visible"

#: 原因码（固定值，不含任何数据；给 API 与回归断言用）
SOURCE_REASON_GONE = "gone"
SOURCE_REASON_STATUS = "status"
SOURCE_REASON_EXPIRED = "expired"
SOURCE_REASON_OPTOUT = "optout"
SOURCE_REASON_UNSUPPORTED_SCOPE = "unsupported_scope"
SOURCE_REASON_NO_MESSAGE_ID = "no_message_id"
SOURCE_REASON_NO_ARCHIVE = "no_archive"
SOURCE_REASON_MODERATED = "moderated"
SOURCE_REASON_QUERY_FAILED = "query_failed"


@dataclass(frozen=True)
class FactSourceExpansion:
    """一次原文展开的结果。

    ``available=False`` 时 ``text``/``url`` 一定为空；``status`` 只取上面五个常量之一。
    ``corrected=True`` 表示这条事实正文是人工纠错过的——``text`` 仍是**原消息**
    原文，调用方必须把两者分开显示，不能把纠错正文当作来源原话。
    """

    status: str = SOURCE_MISSING
    available: bool = False
    reason: str = ""
    fact_id: int = 0
    revision: int = 0
    corrected: bool = False
    source_kind: str = ""
    message_id: int | None = None
    thread_id: int | None = None
    url: str = ""
    link_kind: str = ""
    text: str = ""
    truncated: bool = False


def _bounded_text(value: Any) -> tuple[str, bool]:
    """脱敏 + 截断：返回 ``(text, truncated)``。"""

    body = str(value or "")
    if not body:
        return "", False
    redacted = redact_sensitive_excerpt(body)
    if len(redacted) <= SOURCE_TEXT_MAX_CHARS:
        return redacted, False
    return redacted[:SOURCE_TEXT_MAX_CHARS], True


async def archive_threads(
    session: Any, *, group_id: Any, message_ids: Any
) -> dict[int, int | None] | None:
    """核验「本群现存归档」并取回可信 thread：``{message_id: thread or None}``。

    返回 ``None`` 表示**这一次核验失败**（未知，不是"缺失"）：调用方必须 fail-closed，
    绝不发出不可验证的回链。只查**同一 group** 且 ``telegram_message_id`` 相同的行，
    所以 ``message_id in result`` 就是「本群现归档里确实有这条消息」；查不到的 id 不会
    出现在结果里，**不能**拿写入时存下的 ``source_thread_id`` 或 ``evidence_excerpt``
    冒充现存证据。

    thread 只信这一行里 :func:`trusted_thread_id` 认可的正整数；0/负数/非整数一律当
    「没有可信 thread」，退回普通（非 Topic）链接形状，绝不猜、绝不编。
    """

    ids = {
        int(value)
        for value in (message_ids or ())
        if isinstance(value, int) and not isinstance(value, bool) and int(value) > 0
    }
    if not ids:
        return {}
    try:
        sid = int(group_id)
    except (TypeError, ValueError):
        return None
    try:
        rows = (
            await session.execute(
                select(
                    GroupMessageArchive.telegram_message_id,
                    GroupMessageArchive.message_thread_id,
                )
                .where(
                    GroupMessageArchive.group_id == sid,
                    GroupMessageArchive.telegram_message_id.in_(sorted(ids)),
                )
                .order_by(GroupMessageArchive.id)
            )
        ).all()
    except Exception as exc:
        # 只记固定元数据（群、条数、异常类型名），不输出归档正文或 SQL。
        log.warning(
            "memory source: 来源归档核验失败，本次不提供链接 | group_id=%s | count=%d | reason=%s",
            sid,
            len(ids),
            type(exc).__name__,
        )
        return None
    found: dict[int, int | None] = {}
    for row in rows:
        try:
            mid = int(row[0])
        except (TypeError, ValueError):
            continue
        if mid <= 0 or mid in found:
            continue
        found[mid] = trusted_thread_id(row[1])
    return found


async def expand_fact_source(
    session: Any,
    *,
    scope: str,
    scope_id: Any,
    fact_id: Any,
    subject_user_id: int | None = None,
    now: Any | None = None,
) -> FactSourceExpansion:
    """按 stable fact id 展开当前群来源原文（只读）。

    ``subject_user_id`` 是可选的**额外**约束：``None`` 表示调用方已经在更上层做过
    作用域授权（例如本群管理员），只按 ``scope``/``scope_id`` 定位；给了值则必须是
    该主体的事实（本人自助入口用）。无论哪种情况，跨群/私聊 id 都不会命中。
    """

    try:
        fid = int(fact_id)
        sid = int(scope_id)
    except (TypeError, ValueError):
        return FactSourceExpansion(status=SOURCE_MISSING, reason=SOURCE_REASON_GONE)
    normalized_scope = str(scope or "").strip().lower()
    if fid <= 0:
        return FactSourceExpansion(status=SOURCE_MISSING, reason=SOURCE_REASON_GONE)
    subject_filter = []
    if subject_user_id is not None:
        try:
            subject_filter.append(UserFact.subject_user_id == int(subject_user_id))
        except (TypeError, ValueError):
            return FactSourceExpansion(status=SOURCE_MISSING, reason=SOURCE_REASON_GONE)

    stamp = now or now_shanghai_naive()
    try:
        row = (
            await session.execute(
                select(
                    UserFact.id,
                    UserFact.scope,
                    UserFact.status,
                    UserFact.revision,
                    UserFact.source_kind,
                    UserFact.source_message_id,
                    UserFact.expires_at,
                    UserFact.corrected_by,
                    UserFact.subject_user_id,
                ).where(
                    UserFact.id == fid,
                    UserFact.scope == normalized_scope,
                    UserFact.scope_id == sid,
                    *subject_filter,
                )
            )
        ).first()
        if row is None:
            return FactSourceExpansion(status=SOURCE_MISSING, reason=SOURCE_REASON_GONE)
        revision = int(row[3] or 1)
        base = dict(
            fact_id=int(row[0]),
            revision=revision,
            corrected=row[7] is not None,
            source_kind=normalize_source_kind(row[4]),
        )
        status_value = str(row[2] or STATUS_ACTIVE)
        if status_value != STATUS_ACTIVE:
            return FactSourceExpansion(
                status=SOURCE_NOT_VISIBLE,
                reason=f"{SOURCE_REASON_STATUS}:{status_value}",
                **base,
            )
        expires_at = row[6]
        if expires_at is not None and expires_at <= stamp:
            return FactSourceExpansion(
                status=SOURCE_NOT_VISIBLE, reason=SOURCE_REASON_EXPIRED, **base
            )
        subject = int(row[8] or 0)
        if subject:
            opted_out = (
                await session.execute(
                    select(MemoryOptout.user_id).where(MemoryOptout.user_id == subject)
                )
            ).first()
            if opted_out is not None:
                return FactSourceExpansion(
                    status=SOURCE_NOT_VISIBLE, reason=SOURCE_REASON_OPTOUT, **base
                )
        if normalized_scope != "group":
            # 私聊没有受支持的原文归档：明确不可用，不开新读取面。
            return FactSourceExpansion(
                status=SOURCE_MISSING, reason=SOURCE_REASON_UNSUPPORTED_SCOPE, **base
            )
        try:
            message_id = int(row[5])
        except (TypeError, ValueError):
            message_id = 0
        if message_id <= 0:
            return FactSourceExpansion(
                status=SOURCE_MISSING, reason=SOURCE_REASON_NO_MESSAGE_ID, **base
            )
        archive = (
            await session.execute(
                select(
                    GroupMessageArchive.id,
                    GroupMessageArchive.raw_text,
                    GroupMessageArchive.content,
                    GroupMessageArchive.message_thread_id,
                ).where(
                    GroupMessageArchive.group_id == sid,
                    GroupMessageArchive.telegram_message_id == message_id,
                )
            )
        ).first()
        if archive is None:
            return FactSourceExpansion(
                status=SOURCE_MISSING,
                reason=SOURCE_REASON_NO_ARCHIVE,
                message_id=message_id,
                **base,
            )
        base["message_id"] = message_id
        # 真实删除证据：复用 B6 的 disposed 语义（只有真实"消息已被删除"处置算）。
        # 局部 import：memory.py 会 import 本包内其它模块，放在函数里避免任何
        # 未来的循环依赖；这里只借它的删除判据，不碰它的会话/写入路径。
        from bot.services.memory import disposed_message_ids

        disposed = await disposed_message_ids(
            session, group_id=sid, telegram_message_ids=[message_id]
        )
        thread_value = archive[3]
        thread_id = (
            thread_value
            if isinstance(thread_value, int)
            and not isinstance(thread_value, bool)
            and thread_value > 0
            else None
        )
        base["thread_id"] = thread_id
        if message_id in disposed:
            return FactSourceExpansion(
                status=SOURCE_DELETED, reason=SOURCE_REASON_MODERATED, **base
            )
        text, truncated = _bounded_text(archive[1] or archive[2])
        # thread 只信这条同群同 id 的归档行；General(1) 由 helper 退回普通形状。
        url, kind = message_link(
            SimpleNamespace(id=sid, username=""), message_id, thread_id
        )
        return FactSourceExpansion(
            status=SOURCE_OK,
            available=True,
            reason="",
            url=url,
            link_kind=kind,
            text=text,
            truncated=truncated,
            **base,
        )
    except Exception as exc:  # noqa: BLE001 - 查询故障必须 fail-closed
        log.warning(
            "memory source: 原文展开失败，按不可验证处理 | fact_id=%s | reason=%s",
            fid,
            type(exc).__name__,
        )
        return FactSourceExpansion(
            status=SOURCE_UNVERIFIABLE, reason=SOURCE_REASON_QUERY_FAILED
        )
