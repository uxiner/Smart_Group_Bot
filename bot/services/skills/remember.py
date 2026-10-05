"""第 4 期：``remember`` 工具——模型**主动**写一条长期记忆。

这是长期记忆的第二条写入路径（第一条是后台被动提炼，见
``bot.services.long_term_memory.run_long_term_memory_extraction``）。两条路都走同一套
:func:`bot.services.long_term_memory.record_fact`：同一份去重指纹、同一套冲突替代、
同一道敏感信息闸门。

硬约束（第 4 期 C 项）：

* **主语只能是发消息者本人**：群里 ``subject_user_id`` 只能是 ``sender_user_id``，
  私聊里只能是对方自己。工具**没有** target 参数，所以模型连表达「记别人」的入口
  都没有——不是靠提示词求它别这么干。
* 「一次调用写一条」，每条独立提交；受 ``memory_tool_daily_cap``（每作用域每天）
  限制，超限就跳过并只记日志。
* 群作用域额外受 :data:`bot.services.long_term_memory.TOOL_SUBJECT_DAILY_CAP`
  （每个成员每天）限制（B-34）：整群额度不能被单个普通成员吃光。
* 返回给模型的只有一句**简短确认**（「已记住」），绝不回显整条事实——回显等于把
  用户隐私再塞回上下文，还可能被当成「机器人复述过」的证据。
"""

from __future__ import annotations

import logging
from typing import Any

from bot.services import long_term_memory as ltm
from bot.services.long_term_memory import (
    CATEGORIES,
    CATEGORY_OTHER,
    SCOPE_GROUP,
    SCOPE_PRIVATE,
    SOURCE_TOOL,
    contains_sensitive_fact,
    count_tool_facts_today,
    memory_facts_enabled,
    memory_tool_daily_cap,
    memory_tool_enabled,
    normalize_category,
    normalize_fact_text,
    record_fact,
)
from bot.services.skills.base import SkillContext, SkillRunResult

log = logging.getLogger(__name__)

#: 事实文本的保存上限（与 ``user_facts.fact_text`` 同口径）
FACT_INPUT_MAX_CHARS = 200
#: 出处片段的保存上限
EVIDENCE_MAX_CHARS = 200

_CONFIRMED_SUMMARY = "已记住"
_SKIPPED_SUMMARY = "这条先不记了"
_DISABLED_SUMMARY = "现在不方便记新的东西"
_CAP_REACHED_SUMMARY = "今天记得够多了，这条先不记"


class RememberSkill:
    """模型主动写长期记忆的工具（群聊与私聊都只有「本人」这一个主语）。"""

    name = "remember"
    description = (
        "记住关于**当前说话人本人**的一条稳定事实（身份、稳定偏好、人际关系、"
        "禁忌、长期目标、技能）。只记跨天以后还有用、且以后对话用得上的；"
        "一次性的安排、当下的情绪、公共常识、积分/头衔这类能直接查到的数据不要记；"
        "口令/密码/证件号/银行卡/手机号/精确住址一律不记，别人的隐私也不记。"
        "每条调用只写一条。"
    )
    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": (
                    "一句话事实（≤ 200 字），主语只能是当前说话人本人；"
                    "例如「他在日本读研，爱聊显卡」。"
                ),
            },
            "category": {
                "type": "string",
                "enum": list(CATEGORIES),
                "description": (
                    "类别：identity（身份）/ preference（偏好）/ relationship（关系）/ "
                    "event（有期限的事件）/ taboo（禁忌）/ skill（技能）/ other。"
                ),
            },
            "confidence": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "把握程度 0-100，默认 70。",
            },
        },
        "required": ["fact"],
        "additionalProperties": False,
    }

    def __init__(self, settings: Any | None = None) -> None:
        self.settings = settings

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        if self.settings is not None and (
            not memory_facts_enabled(self.settings)
            or not memory_tool_enabled(self.settings)
        ):
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=_DISABLED_SUMMARY,
                payload={"skipped": True, "reason": "disabled"},
            )

        fact = normalize_fact_text(arguments.get("fact"))
        if not fact:
            return SkillRunResult(ok=True, skill=self.name, summary=_SKIPPED_SUMMARY)

        chat_id = int(context.chat_id or 0)
        sender_id = int(context.sender_user_id or 0)
        if chat_id == 0 or sender_id == 0:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=_SKIPPED_SUMMARY,
                payload={"skipped": True, "reason": "missing_chat_context"},
            )
        # 群聊（chat_id < 0）= 本群的公共事实归属本群；私聊 = 这个用户自己的作用域。
        scope = SCOPE_GROUP if chat_id < 0 else SCOPE_PRIVATE
        scope_id = chat_id if chat_id < 0 else sender_id
        # 主语只能是发消息者本人：没有参数可以让模型指定别人。
        subject_user_id = sender_id

        evidence = " ".join(str(context.current_user_text or "").split())[
            :EVIDENCE_MAX_CHARS
        ].strip() or fact
        if contains_sensitive_fact(fact) or contains_sensitive_fact(evidence):
            log.info(
                "long-term memory: remember 工具拒收疑似敏感信息 | scope=%s | "
                "scope_id=%s | subject=%s",
                scope,
                scope_id,
                subject_user_id,
            )
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=_SKIPPED_SUMMARY,
                payload={"skipped": True, "reason": "sensitive"},
            )

        category = normalize_category(arguments.get("category") or CATEGORY_OTHER)
        try:
            confidence = int(arguments.get("confidence", 70))
        except (TypeError, ValueError):
            confidence = 70
        confidence = min(100, max(0, confidence))
        message_id = getattr(context.message, "message_id", None)

        async def _write(session: Any) -> int:
            cap = (
                memory_tool_daily_cap(self.settings)
                if self.settings is not None
                else ltm.TOOL_DAILY_CAP
            )
            # B-34：群作用域额外加一道 per-subject 闸门。``cap`` 是**整群**额度，
            # 没有这一道时任何普通成员都能独自吃光它。私聊作用域「一个人一个额度」，
            # 口径保持原样（不叠加第二道闸门）。
            if scope == SCOPE_GROUP:
                used_by_subject = await count_tool_facts_today(
                    session,
                    scope=scope,
                    scope_id=scope_id,
                    subject_user_id=subject_user_id,
                )
                if used_by_subject >= ltm.TOOL_SUBJECT_DAILY_CAP:
                    log.info(
                        "long-term memory: remember 工具已达**本人**每日上限 | scope=%s | "
                        "scope_id=%s | subject=%s | cap=%d",
                        scope,
                        scope_id,
                        subject_user_id,
                        ltm.TOOL_SUBJECT_DAILY_CAP,
                    )
                    return -1
            if cap > 0:
                used = await count_tool_facts_today(
                    session, scope=scope, scope_id=scope_id
                )
                if used >= cap:
                    log.info(
                        "long-term memory: remember 工具已达每日上限 | scope=%s | "
                        "scope_id=%s | cap=%d",
                        scope,
                        scope_id,
                        cap,
                    )
                    return -1
            return await record_fact(
                session,
                scope=scope,
                scope_id=scope_id,
                subject_user_id=subject_user_id,
                fact_text=fact,
                category=category,
                confidence=confidence,
                source_kind=SOURCE_TOOL,
                source_message_id=(
                    int(message_id) if message_id is not None else None
                ),
                evidence_excerpt=evidence,
                event_ttl_days=(
                    ltm.memory_event_ttl_days(self.settings)
                    if self.settings is not None
                    else ltm.EVENT_TTL_DAYS
                ),
            )

        try:
            if context.session_factory is not None:
                # 自己的短会话：写记忆失败时的回滚不该把这一轮别人的写入一起回滚。
                async with context.session_factory() as own_session:
                    written = await _write(own_session)
            elif context.session is not None:
                written = await _write(context.session)
            else:
                written = 0
        except Exception as exc:  # pragma: no cover - record_fact 自己已经吞了异常
            log.warning("long-term memory: remember 工具写入失败 | error=%s", exc)
            written = 0

        if written == -1:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=_CAP_REACHED_SUMMARY,
                payload={"skipped": True, "reason": "daily_cap"},
            )
        if not written:
            return SkillRunResult(
                ok=True,
                skill=self.name,
                summary=_SKIPPED_SUMMARY,
                payload={"skipped": True, "reason": "not_stored"},
            )
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary=_CONFIRMED_SUMMARY,
            payload={"fact_id": int(written)},
        )
