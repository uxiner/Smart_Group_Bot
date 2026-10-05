"""第 3 期：群聊 / 私聊 / 搜索三条链路**共用的同一道 token 闸门**。

为什么要单独一个模块：三条链路各自都在往 prompt 里塞东西——系统提示词 + 人设、
本轮消息、记忆召回、检索留档、历史。以前「装不下怎么办」分散在三处（群聊历史的
``assemble_group_history``、私聊历史的 ``assemble_private_history``、
``MemoryService._trim_by_token_budget``），谁裁谁、裁多少没有单一出处。这里给**一个**
入口 :func:`assemble_context_within_budget`，把五层装进 ``278528``（272K）的闸门内，
裁剪顺序固定为：

    最老的历史 → 最旧的搜索记录 → 记忆召回条数

**永远不裁**：系统提示词 + 人设、本轮消息（含当前这一条）。所以「模型这一轮到底看到
了什么」的最低保证是：人设与围栏在、当前这条消息在。

设计取舍：

* **纯函数**：只依赖 :func:`bot.utils.tokens.estimate_text_tokens`，不碰数据库、不碰
  全局状态，所以单测可以直接喂构造好的 layers 断言裁剪结果；并发/多群互不影响
  （没有共享可变状态）。
* **裁剪是「按条丢 + 单条超长截断」**：每一层都从**最旧**的一端开始整条丢；只剩最后
  一条还装不下时，截断它的正文（补一句「已截断」），而不是把这一层清空——与第 1/2 期
  历史装配的「最新那条一定留下」同口径。
* 预算按 ``CONTEXT_TOKEN_BUDGET_MIN/MAX`` 夹取（与 runtime_config 的字段约束一致）；
  token 换算复用 :func:`bot.utils.tokens.estimate_text_tokens`（CJK 1 token/字，故意高估）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from bot.services.model_limits import effective_context_window, loose_budget_tokens
from bot.utils.budget import (
    BUSINESS_CONTEXT_TOKENS_MAX,
    BUSINESS_CONTEXT_TOKENS_MIN,
)
from bot.utils.tokens import estimate_text_tokens

#: 统一闸门的默认预算：272K = 278528（全项目统一用这个精确数字）。
#: **2026-10-04 之后**：``auto`` 模式下这是"查不到模型元数据"时的保守降级值；
#: 拿到真实窗口时闸门按真实窗口给（见 :func:`context_token_budget`）。
CONTEXT_TOKEN_BUDGET = 278_528
#: 夹取范围。**上界取自 :mod:`bot.utils.budget` 这一个来源**（CTX-001）。
#:
#: 之前这里写着 2_000_000，而 ``runtime_config`` 的
#: ``context_budget_tokens`` 上界是 ``BUSINESS_CONTEXT_TOKENS_MAX`` = 16_000_000，
#: 两处对不上：注释说"与 runtime_config 一致"，实际不一致，于是同一个"上界"在
#: 文档、UI 与校验三处给出三个答案。现在只留一个来源——``bot.utils.budget``。
#: 这**不改变**当前可配的 auto/fixed 行为：``context_token_budget()`` 本来就只保
#: 下限、不按上界截断，所以上界取多少都不影响今天跑出来的结果。
CONTEXT_TOKEN_BUDGET_MIN = BUSINESS_CONTEXT_TOKENS_MIN
CONTEXT_TOKEN_BUDGET_MAX = BUSINESS_CONTEXT_TOKENS_MAX

#: 每条消息在预算里额外占的固定开销（角色、时间、发送者、分隔等），与历史装配同口径。
CONTEXT_MESSAGE_TOKEN_OVERHEAD = 12

#: 单条内容被截断时补的说明（截断必须留痕），文案与第 1/2 期历史装配一致。
CONTEXT_TRUNCATION_NOTE = "…（本条过长，已截断）"

#: 层的裁剪顺序（越靠前越先被裁）。这是本模块的**唯一**裁剪口径。
TRIM_PRIORITY: tuple[str, ...] = ("history", "search_records", "memory_recall")

#: 永不裁剪的层（顺序也是它们在最终 messages 里的顺序）。
FIXED_LAYERS: tuple[str, ...] = ("system", "current_turn")


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)
    return min(int(high), max(int(low), number))


def bounded_context_token_budget(value: Any) -> int:
    """统一闸门预算的夹取口径（与 runtime_config 的字段约束一致）。"""

    return _bounded_int(
        value,
        default=CONTEXT_TOKEN_BUDGET,
        low=CONTEXT_TOKEN_BUDGET_MIN,
        high=CONTEXT_TOKEN_BUDGET_MAX,
    )


def context_token_budget(settings: Any) -> int:
    """当前生效的统一闸门**业务总窗口**（含 272Ki 上限）。

    这一轮的业务预算覆盖 system/人设 + 工具定义 + 记忆召回 + 检索留档 + 历史 + 本轮消息
    + 工具结果 + 输出预留——**不是只限制历史**。默认建议值是 272Ki = 278528
    （``bot.utils.budget.BUSINESS_CONTEXT_WINDOW_TOKENS``），但**显式配置不被隐藏常量
    截断**：上界以 :mod:`bot.utils.budget` 为唯一来源。
    模型侧解析出来的真实窗口（1M/4M 都原样记录、原样出现在日志里）只用于"模型更小就
    跟着更小"：

    * ``auto`` + 可信元数据 → ``min(模型真实窗口, 272Ki)``；
    * ``auto`` + 查不到 → 兼容字段的保守降级值（同一个 min，未知 ≠ 无限）；
    * ``fixed`` → 配置值（同样叠 272Ki，只允许更小）。

    三条链路必须用同一个数字：群聊 / 私聊 / 搜索的资料块都往同一个业务窗口里塞，
    各读各的默认值正是要消掉的分叉。
    """

    # 显式配置的业务预算不被隐藏常量截断：只保下限。
    return loose_budget_tokens(
        effective_context_window(settings),
        default=CONTEXT_TOKEN_BUDGET,
        low=CONTEXT_TOKEN_BUDGET_MIN,
    )


def _message_tokens(message: Any) -> int:
    if not isinstance(message, Mapping):
        return 0
    return estimate_text_tokens(str(message.get("content") or "")) + (
        CONTEXT_MESSAGE_TOKEN_OVERHEAD
    )


def _messages_tokens(messages: Iterable[Any]) -> int:
    return sum(_message_tokens(item) for item in messages)


def _cut_text_to_tokens(text: str, limit_tokens: int) -> str:
    """把文本从尾部硬切到 token 上限内（保留开头），与历史装配同口径。"""

    if limit_tokens <= 0 or not text:
        return ""
    if estimate_text_tokens(text) <= limit_tokens:
        return text
    candidate = text[: min(len(text), limit_tokens * 3 + 3)]
    low, high = 0, len(candidate)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_text_tokens(candidate[:mid]) <= limit_tokens:
            low = mid
        else:
            high = mid - 1
    return candidate[:low]


def _truncate_message(message: Mapping[str, Any], limit_tokens: int) -> dict[str, Any]:
    """截断一条消息的 ``content``，并在放得下的情况下补一句「已截断」。"""

    body = str(message.get("content") or "")
    allowed = limit_tokens - CONTEXT_MESSAGE_TOKEN_OVERHEAD
    if allowed <= 0:
        return {**dict(message), "content": ""}
    note = CONTEXT_TRUNCATION_NOTE
    kept = allowed - estimate_text_tokens(note)
    if kept <= 0:
        return {**dict(message), "content": _cut_text_to_tokens(body, allowed)}
    return {**dict(message), "content": f"{_cut_text_to_tokens(body, kept)}{note}"}


@dataclass(frozen=True)
class ContextTrim:
    """一层被裁掉了什么（给日志与单测看）。"""

    layer: str
    dropped_messages: int = 0
    truncated_messages: int = 0
    dropped_tokens: int = 0


@dataclass(frozen=True)
class ContextAssembly:
    """统一闸门的装配结果。"""

    #: 裁剪后按固定顺序拼好的 messages（system → memory_recall → search_records →
    #: history → current_turn）。调用方要自定义顺序时读 ``layers``。
    messages: list[dict[str, Any]] = field(default_factory=list)
    #: 每层裁剪后的消息（含未被裁的层）
    layers: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: 裁剪后总 token（含固定开销）
    used_tokens: int = 0
    #: 生效预算
    budget_tokens: int = CONTEXT_TOKEN_BUDGET
    #: 固定层自己就超预算（本轮消息与系统块永不裁，只能如实上报）
    over_budget: bool = False
    #: 每一层实际发生的裁剪（没裁的层不出现在这里）
    trims: tuple[ContextTrim, ...] = ()


def assemble_context_within_budget(
    *,
    system: Iterable[Mapping[str, Any]] | None = None,
    current_turn: Iterable[Mapping[str, Any]] | None = None,
    memory_recall: Iterable[Mapping[str, Any]] | None = None,
    search_records: Iterable[Mapping[str, Any]] | None = None,
    history: Iterable[Mapping[str, Any]] | None = None,
    budget_tokens: Any = CONTEXT_TOKEN_BUDGET,
    reserve_tokens: Any = 0,
    trim_order: tuple[str, ...] = TRIM_PRIORITY,
) -> ContextAssembly:
    """把五层上下文装进 ``budget_tokens`` 的闸门内，按固定优先级裁剪。

    参数里的每一层都是 ``[{"role", "content"}, ...]``。``history`` 与
    ``search_records`` 需要按**时间正序**（最老的在前）传，裁剪从最老的一端开始。
    ``memory_recall`` 也按「越前面越先被裁」的顺序传（通常是召回索引的尾部在前）。

    * ``system``（系统提示词 + 人设）与 ``current_turn``（本轮消息，含当前这一条）
      **永远不裁**；两者加起来加 ``reserve_tokens`` 就超预算时，如实把
      ``over_budget=True`` 报出来（这一轮已经没得裁了）。
    * 其余按 ``trim_order`` 依次裁：整条丢（从最旧端）；一层只剩最后一条还装不下时，
      截断这一条的正文而不是清空它。
    * 返回 :class:`ContextAssembly`，``layers`` 是裁剪后的各层，``messages`` 是拼好的
      默认顺序（system → memory_recall → search_records → history → current_turn）。
    """

    # 调用方算好的预算直接采信（业务上限由 context_token_budget 决定，≤272Ki）：
    # 这里只保下限，不给装配入口再藏一道 2M 截断。
    budget = loose_budget_tokens(
        budget_tokens,
        default=CONTEXT_TOKEN_BUDGET,
        low=CONTEXT_TOKEN_BUDGET_MIN,
    )
    try:
        reserve = max(0, int(reserve_tokens))
    except (TypeError, ValueError):
        reserve = 0

    layer_inputs: dict[str, list[dict[str, Any]]] = {
        "system": [dict(item) for item in (system or []) if isinstance(item, Mapping)],
        "current_turn": [
            dict(item) for item in (current_turn or []) if isinstance(item, Mapping)
        ],
        "memory_recall": [
            dict(item) for item in (memory_recall or []) if isinstance(item, Mapping)
        ],
        "search_records": [
            dict(item) for item in (search_records or []) if isinstance(item, Mapping)
        ],
        "history": [dict(item) for item in (history or []) if isinstance(item, Mapping)],
    }
    # system / current_turn 在原顺序上必须保持调用方给的顺序（提示词块有先后语义）
    fixed = [*layer_inputs["system"], *layer_inputs["current_turn"]]
    fixed_tokens = _messages_tokens(fixed)
    allowance = budget - fixed_tokens - reserve

    kept: dict[str, list[dict[str, Any]]] = {
        "system": list(layer_inputs["system"]),
        "current_turn": list(layer_inputs["current_turn"]),
        "memory_recall": list(layer_inputs["memory_recall"]),
        "search_records": list(layer_inputs["search_records"]),
        "history": list(layer_inputs["history"]),
    }
    trims: list[ContextTrim] = []
    used = sum(_messages_tokens(value) for value in kept.values())

    for layer in trim_order:
        if used <= allowance:
            break
        items = kept.get(layer)
        if not items:
            continue
        dropped = 0
        truncated = 0
        dropped_tokens = 0
        while items and used > allowance:
            if len(items) == 1:
                # 只剩最新的一条：截断它，而不是把这一层清空（「最新那条不整条丢」）。
                # 已经没内容可截（只剩固定开销）时，丢掉它，避免死循环。
                remaining = allowance - (used - _message_tokens(items[0]))
                cut = _truncate_message(items[0], remaining)
                before = _message_tokens(items[0])
                after = _message_tokens(cut)
                if after < before:
                    items[0] = cut
                    used -= before - after
                    truncated = 1
                    if not str(cut.get("content") or "").strip():
                        # 截到什么都不剩：留着空消息只会给 prompt 添噪音，直接丢掉。
                        used -= after
                        items.pop()
                        dropped += 1
                        dropped_tokens += before
                        truncated = 0
                else:
                    dropped_tokens += before
                    used -= before
                    items.pop()
                    dropped += 1
                break
            tokens = _message_tokens(items[0])
            items.pop(0)
            dropped += 1
            dropped_tokens += tokens
            used -= tokens
        if dropped or truncated:
            trims.append(
                ContextTrim(
                    layer=layer,
                    dropped_messages=dropped,
                    truncated_messages=truncated,
                    dropped_tokens=dropped_tokens,
                )
            )

    over_budget = (used + reserve) > budget
    merged: list[dict[str, Any]] = [
        *kept["system"],
        *[item for item in kept["memory_recall"]],
        *[item for item in kept["search_records"]],
        *[item for item in kept["history"]],
        *kept["current_turn"],
    ]
    return ContextAssembly(
        messages=merged,
        layers=kept,
        used_tokens=used,
        budget_tokens=budget,
        over_budget=over_budget,
        trims=tuple(trims),
    )
