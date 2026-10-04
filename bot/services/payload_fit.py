"""最终可发送载荷的裁剪：保住本轮问题与核心系统块，按固定优先级裁旧内容。

为什么需要它（2026-10-04 生产事故）：装配链路已经按 token 预算装了一遍，但"装配时的
口径"和"最终请求闸门的口径"以前不是同一个（见 ``bot/services/model_limits.py`` 的
说明），于是最终闸门把一份装得下的载荷判成超限，**主模型和备用都没发 HTTP** 就被
skip。修好计量之后仍然需要最后一道兜底：万一载荷真的超了（长工具结果、超长召回、
网关窗口比声明的更小……），要**裁到能发**而不是整条跳过。

裁剪口径（与 ``bot/services/context_gate.py`` 的层语义一致）：

1. **优先裁旧历史** → 检索留档 → 记忆召回：每层都从**最旧的一端**整条丢；
2. **永不裁**：核心系统块（人设/围栏/输出协议……）与**本轮**消息；带
   ``tool_calls`` 的 assistant 消息与它的 ``tool`` 结果属于工具协议，**整对保留**，
   绝不制造"有 tool 结果没 assistant 声明"的孤儿消息；
3. 工具结果本身很长时，**截断正文**而不是丢消息（协议配对照样成立）；
4. 裁完还是超预算 → 如实报 ``over_budget=True``，由调用方**诚实失败**（不发请求），
   而不是把一条明知超出模型窗口的请求硬发出去，也不是假装成功。

标记方式：装配方在每条可裁消息上打 ``_ctx_layer``（见 :func:`tag_context_layer`）。
没有标记的消息一律视为 core（不可裁）——宁可诚实失败，也不猜着丢核心规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from bot.utils.tokens import cut_text_to_tokens, estimate_text_tokens

#: 层标记的键名。**故意用下划线开头**：它只在进程内传递，不会进模型请求体
#: （请求构造只挑 role/content/tool_calls/name/tool_call_id 这些字段）。
CTX_LAYER_KEY = "_ctx_layer"

LAYER_HISTORY = "history"
LAYER_SEARCH_RECORDS = "search_records"
LAYER_MEMORY_RECALL = "memory_recall"
#: 不可裁的层（核心系统块、本轮消息、工具协议）。
LAYER_CORE = "core"

#: 裁剪顺序：越靠前越先被裁。这是本模块的**唯一**口径。
TRIM_ORDER: tuple[str, ...] = (
    LAYER_HISTORY,
    LAYER_SEARCH_RECORDS,
    LAYER_MEMORY_RECALL,
)

#: 预算下限：调用方必须传一个正数；``<= 0`` 一律按 1 处理（等价于"只剩核心块"，
#: 由调用方按 ``over_budget`` 诚实失败，而不是静默把门禁关掉）。
MIN_FIT_BUDGET_TOKENS = 1


def tag_context_layer(message: Any, layer: str) -> Any:
    """给一条消息打层标记（原对象返回，方便链式调用）。"""

    if isinstance(message, dict):
        message[CTX_LAYER_KEY] = str(layer or LAYER_CORE)
    return message


def tag_context_layers(messages: Iterable[Any], layer: str) -> list[Any]:
    items = list(messages or [])
    for message in items:
        tag_context_layer(message, layer)
    return items


def message_context_layer(message: Any) -> str:
    if not isinstance(message, Mapping):
        return LAYER_CORE
    layer = str(message.get(CTX_LAYER_KEY) or "").strip()
    return layer if layer in TRIM_ORDER else LAYER_CORE


def context_layer_for_history_row(row: Any) -> str:
    """把一条装配期的历史行映射到最终载荷里的层。

    * ``memory_source='search_record'`` → 检索留档层；
    * ``memory_source='recalled_archive_index'`` → 记忆召回层；
    * 系统角色（头部说明/来源声明）→ 核心层（**永不裁**）；
    * 其它（真实对话历史）→ 历史层（最先被裁）。
    """

    if not isinstance(row, Mapping):
        return LAYER_CORE
    source = str(row.get("memory_source") or "").strip().lower()
    if source == "search_record":
        return LAYER_SEARCH_RECORDS
    if source == "recalled_archive_index":
        return LAYER_MEMORY_RECALL
    if str(row.get("role") or "").strip().lower() == "system":
        return LAYER_CORE
    return LAYER_HISTORY


def tag_rendered_history(
    source_rows: Iterable[Any],
    rendered_messages: Iterable[Any],
) -> list[Any]:
    """``sanitize_history_for_llm`` 的输出是 1:1 的，按输入行的来源打层标记。

    两条链路（群聊技能装配、私聊装配）都用同一个映射，裁剪优先级就不会各说各话。
    """

    rows = list(source_rows or [])
    rendered = list(rendered_messages or [])
    offset = max(0, len(rows) - len(rendered))
    for index, message in enumerate(rendered):
        tag_context_layer(message, context_layer_for_history_row(rows[offset + index]))
    return rendered


def strip_context_layer(message: Mapping[str, Any]) -> dict[str, Any]:
    """复制一条消息并去掉层标记（写进用户可见内容/持久化前用）。"""

    return {
        key: value for key, value in dict(message).items() if key != CTX_LAYER_KEY
    }


def strip_internal_message_keys(
    messages: Iterable[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """发请求前的最后一道清洁：去掉所有下划线开头的**进程内**标记。

    层标记只是我们自己用来决定"先裁谁"的，绝不能出现在发给 provider 的请求体里
    （未知字段会被严格的 OpenAI 兼容网关判成 400）。返回新字典，不改入参——每个
    fallback 都要拿原始载荷重新裁一次。
    """

    cleaned: list[dict[str, Any]] = []
    for message in messages or []:
        if not isinstance(message, Mapping):
            continue
        cleaned.append(
            {
                key: value
                for key, value in dict(message).items()
                if not str(key).startswith("_")
            }
        )
    return cleaned


@dataclass(frozen=True)
class FittedPayload:
    """裁剪结果（给日志与单测看）。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    budget_tokens: int = MIN_FIT_BUDGET_TOKENS
    #: 被整条丢掉的消息（按层累计）
    dropped_messages: int = 0
    dropped_tokens: int = 0
    #: 被截断正文的消息（工具结果）
    truncated_messages: int = 0
    truncated_tokens: int = 0
    #: 实际发生裁剪的层（按发生顺序）
    layers_dropped: tuple[str, ...] = ()
    #: 裁到极限还是超预算（核心块 + 本轮自己就装不下）→ 调用方诚实失败
    over_budget: bool = False
    #: 有没有发生任何裁剪
    changed: bool = False


def fit_messages_to_input_budget(
    messages: Iterable[Mapping[str, Any]] | None,
    *,
    message_tokens: Callable[[Mapping[str, Any]], int],
    tools_tokens: int = 0,
    budget_tokens: int,
) -> FittedPayload:
    """把载荷裁进 ``budget_tokens``（**不修改入参**，返回新的消息列表）。

    ``message_tokens`` 必须是**可加的**单条保守上界（``tools_tokens`` + 每条之和 =
    整个载荷的上界），调用方用 ``model_limits.conservative_message_tokens`` 保持与装配
    链路同口径。``budget_tokens`` 应当已经扣掉本轮输出预留（只扣一次）。
    """

    try:
        budget = max(MIN_FIT_BUDGET_TOKENS, int(budget_tokens))
    except (TypeError, ValueError):
        budget = MIN_FIT_BUDGET_TOKENS
    try:
        tools_cost = max(0, int(tools_tokens or 0))
    except (TypeError, ValueError):
        tools_cost = 0

    work: list[dict[str, Any]] = [
        dict(message) for message in (messages or []) if isinstance(message, Mapping)
    ]
    costs = [max(0, int(message_tokens(message) or 0)) for message in work]
    total = tools_cost + sum(costs)

    if total <= budget:
        return FittedPayload(
            messages=work,
            prompt_tokens=total,
            budget_tokens=budget,
        )

    dropped: set[int] = set()
    layers_dropped: list[str] = []
    dropped_tokens = 0
    for layer in TRIM_ORDER:
        if total <= budget:
            break
        touched = False
        for index, message in enumerate(work):
            if total <= budget:
                break
            if index in dropped or message_context_layer(message) != layer:
                continue
            dropped.add(index)
            total -= costs[index]
            dropped_tokens += costs[index]
            touched = True
        if touched:
            layers_dropped.append(layer)

    # 工具结果太长：截断正文，保住 assistant/tool 协议配对。
    truncated_messages = 0
    truncated_tokens = 0
    if total > budget:
        for index, message in enumerate(work):
            if total <= budget:
                break
            if index in dropped:
                continue
            if str(message.get("role") or "") != "tool":
                continue
            overflow = total - budget
            target = costs[index] - overflow
            body = str(message.get("content") or "")
            # ``costs[index]`` 是"整条消息"的上界（含角色/分隔/tool_call_id 开销）；
            # 正文能占的额度要减掉这部分固定开销。
            overhead = costs[index] - estimate_text_tokens(body)
            content_budget = target - max(0, overhead)
            if content_budget <= 0:
                continue
            new_content = cut_text_to_tokens(body, content_budget)
            if new_content == body:
                continue
            message["content"] = new_content
            new_cost = max(0, int(message_tokens(message) or 0))
            if new_cost >= costs[index]:
                continue
            total -= costs[index] - new_cost
            truncated_tokens += costs[index] - new_cost
            costs[index] = new_cost
            truncated_messages += 1

    kept = [
        message for index, message in enumerate(work) if index not in dropped
    ]
    return FittedPayload(
        messages=kept,
        prompt_tokens=total,
        budget_tokens=budget,
        dropped_messages=len(dropped),
        dropped_tokens=dropped_tokens,
        truncated_messages=truncated_messages,
        truncated_tokens=truncated_tokens,
        layers_dropped=tuple(layers_dropped),
        over_budget=total > budget,
        changed=True,
    )
