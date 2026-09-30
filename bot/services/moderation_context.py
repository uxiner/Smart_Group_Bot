"""给审核模型补上"这句话是在什么对话里说的"。

孤立地看一条消息，群里的日常话题词（存储、套餐、邀请、白名单、加我、丢包…）很容易被读成引流；
把前面几句群内对话一起送审，模型才能区分"群友在聊天"和"有人在推销"。

数据源是归档表 `group_message_archive`（只读）。这里的所有异常都必须吞掉：
上下文取不到不该让审核本身失败，最多是退回"没有上下文"的旧行为。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import GroupMessageArchive

# 送审上下文的上限：行数少一点、每行短一点，避免为了准确度把 token 成本推高
MAX_CONTEXT_LINES = 8
MAX_LINE_CHARS = 110
MAX_REPLY_CHARS = 140
MAX_BLOCK_CHARS = 900

_WHITESPACE = re.compile(r"\s+")


def _clean(value: object, limit: int) -> str:
    text = _WHITESPACE.sub(" ", str(value or "")).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _label(row: GroupMessageArchive) -> str:
    name = _clean(
        row.sender_display_name or row.sender_username or row.sender_first_name, 24
    )
    if not name:
        name = "成员"
    return name


def _body(row: GroupMessageArchive) -> str:
    for candidate in (row.derived_text, row.content, row.raw_text):
        text = _clean(candidate, MAX_LINE_CHARS)
        if text:
            return text
    kind = _clean(row.message_type, 12) or "非文本"
    return f"[{kind}]"


def render_context_block(
    lines: Sequence[str], reply_to: str | None = None
) -> str:
    """把上下文行渲染成提示词里的那一段（空上下文也有明确说明）。"""

    parts: list[str] = []
    if reply_to:
        parts.append(f"[被回复的消息] {_clean(reply_to, MAX_REPLY_CHARS)}")
    parts.extend(str(line) for line in lines if str(line).strip())
    if not parts:
        return "(本群此刻没有可用的上下文；请按消息本身判断)"
    block = "\n".join(parts)
    if len(block) > MAX_BLOCK_CHARS:
        block = block[-MAX_BLOCK_CHARS:]
        block = "…" + block.lstrip("…")
    return block


def _anchor_candidates(anchor_text: str) -> list[str]:
    """归档里的正文不含 ``[reply_to_user] …`` 这类附注，比对时要先剥掉。"""

    raw = str(anchor_text or "").strip()
    if not raw:
        return []
    head = raw.split("\n[")[0].strip()
    out = [head] if head else []
    if raw not in out:
        out.append(raw)
    return [item for item in out if item]


async def _anchor_row_id(
    session: AsyncSession, group_id: int, anchor_text: str
) -> int | None:
    """在归档里找到"被复核的那条消息"自己，好取它**之前**的对话。"""

    for candidate in _anchor_candidates(anchor_text):
        try:
            result = await session.execute(
                select(GroupMessageArchive.id)
                .where(
                    GroupMessageArchive.group_id == int(group_id),
                    (GroupMessageArchive.content == candidate)
                    | (GroupMessageArchive.derived_text == candidate)
                    | (GroupMessageArchive.raw_text == candidate),
                )
                .order_by(GroupMessageArchive.id.desc())
                .limit(1)
            )
            row_id = result.scalar_one_or_none()
        except Exception:  # pragma: no cover
            row_id = None
        if row_id is not None:
            return int(row_id)
    return None


async def build_moderation_context(
    session: AsyncSession,
    *,
    group_id: int,
    exclude_message_id: int | None = None,
    exclude_text: str | None = None,
    anchor_text: str | None = None,
    before: datetime | None = None,
    limit: int = MAX_CONTEXT_LINES,
    reply_to: str | None = None,
) -> tuple[list[str], str]:
    """返回 (上下文行, 已渲染的提示词片段)。

    ``anchor_text`` 用于"复核一条历史消息"的场景（申诉、/report）：先在归档里定位那条
    消息本身，然后取它前面的对话，时区换算全都不需要。

    只读归档表；任何异常都返回空上下文而不是抛出。
    """

    rows: list[GroupMessageArchive] = []
    anchor_id: int | None = None
    if anchor_text:
        anchor_id = await _anchor_row_id(session, group_id, anchor_text)
    try:
        stmt = (
            select(GroupMessageArchive)
            .where(GroupMessageArchive.group_id == int(group_id))
            .order_by(GroupMessageArchive.id.desc())
            .limit(max(1, int(limit)) * 2 + 2)
        )
        if anchor_id is not None:
            stmt = stmt.where(GroupMessageArchive.id < anchor_id)
        if before is not None:
            stmt = stmt.where(GroupMessageArchive.sent_at <= before)
        result = await session.execute(stmt)
        candidates = list(result.scalars().all())
    except Exception:  # pragma: no cover - 读归档失败不值得影响审核
        candidates = []

    excluded_text = _clean(exclude_text, MAX_LINE_CHARS) if exclude_text else ""
    for row in candidates:
        if exclude_message_id is not None and row.telegram_message_id is not None:
            if int(row.telegram_message_id) == int(exclude_message_id):
                continue
        if excluded_text and _body(row) == excluded_text:
            continue
        rows.append(row)
        if len(rows) >= max(1, int(limit)):
            break

    rows.reverse()  # 归档里是倒序取出的，这里恢复成时间正序
    lines = [f"{_label(row)}: {_body(row)}" for row in rows]
    return lines, render_context_block(lines, reply_to=reply_to)
