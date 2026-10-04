"""群聊历史按 token 预算装配（第 2 期；每轮业务预算 272Ki，最近 1000 条）。

为什么要单独一个模块（而不是塞进 ``bot/services/memory.py``）：

* 这里的核心是一个**纯函数** :func:`assemble_group_history`，只依赖
  ``bot.utils.tokens.estimate_text_tokens``，不碰 SQLAlchemy、向量召回、自动压缩与
  保留策略，可以像 ``private_chat.assemble_private_history`` 一样直接单测；
* ``memory.py`` 已经 4000+ 行，同时管热窗口、归档、召回、压缩与保留策略。本期的硬
  边界是「**只改怎么读，不改怎么存/怎么删**」——把装配口径独立出来，读预算与存/删
  策略就不会继续糊在同一个文件里；
* 「装配出来的历史 + 固定余量 ≤ 模型窗口」这道硬闸门需要一个**单一出处**：配置夹取、
  ``MemoryService`` 取数与单测断言都指向这里，不会出现三份各自为政的口径。

与第 1 期私聊的对应关系：语义对齐 ``private_chat.assemble_private_history``（从新到旧
累积、最新一条一定保留、单条超长截断并注明、空内容跳过、返回时间正序），token 换算
复用同一个 :func:`bot.utils.tokens.estimate_text_tokens`（CJK 1 token/字，故意高估）。
"""

from __future__ import annotations

from typing import Any

from bot.services.model_limits import (
    auto_window_for,
    business_total_window,
    loose_budget_tokens,
)
from bot.utils.tokens import estimate_text_tokens

#: 群聊历史装配的默认 token 预算：272K = 278528（全项目统一用这个精确数字）。
#: **2026-10-04 之后**：这只是``auto`` 模式查不到任何模型元数据时的保守降级值。
#: 拿到了真实窗口（网关自报 1,000,000）时，预算按 ``窗口 − 固定余量`` 给。
GROUP_HISTORY_TOKEN_BUDGET = 278_528
#: token 预算的夹取范围（与 ``runtime_config.BotBehaviorConfig`` 的 ge/le 一致）
GROUP_HISTORY_TOKEN_BUDGET_MIN = 1024
GROUP_HISTORY_TOKEN_BUDGET_MAX = 2_000_000

#: 给「固定部分」留的余量：系统提示词/人设 + 本轮消息 + 记忆召回块 + 回复预留。
#:
#: 默认 32768 的依据（同一套 ``estimate_text_tokens`` 口径实测，见 ``prompt/``）：
#
#   * 人设 + casual 任务提示 ~8.4K（persona.md 5.4K + casual.md 3.0K）；
#   * 技能工具提示 ~4.6K（skill_tools_v2.md，随本轮选中技能增减）；
#   * 回复模式块 ~0.7K、记忆来源规则块 ~0.25K、项目事实/身份块 <1K；
#   * 永久记忆 ≤40 条 × ≤200 字 ≈ 最多 ~8K；
#   * 召回索引 ≤1150 字符 ≈ ≤1.2K，本轮消息 ≤1600 字符 ≈ ≤1.6K；
#   * 回复预留 2048（max_output_tokens）。
#
# 最坏情况合计约 28K，取 32K 留一点瞬时波动空间。**这不是**「随便留一点」：装配出来的
# 历史 + 这个余量必须 ≤ 模型窗口（见 :func:`effective_group_history_budget`）。
GROUP_HISTORY_RESERVE_TOKENS = 32_768
#: 余量的夹取范围（与 ``runtime_config`` 的 ge/le 一致）
GROUP_HISTORY_RESERVE_TOKENS_MIN = 1024
GROUP_HISTORY_RESERVE_TOKENS_MAX = 1_000_000

#: 单次装配的**条数安全上限**。token 预算才是真正的闸门，但预算只在「内容本身够长」
#: 时才会先咬住——一个刷「+1」的群，272K 预算能装下两万多行，读库/组装/估算就成了
#: 每条回复的固定开销。单次最多装配最近 **1000** 行（用户 2026-10-04 最终口径）：
#: 归档读取按页从新到旧进行，累计到"条数或预算"先到即停，**不会**读出几万条再切片。
GROUP_HISTORY_MAX_MESSAGES = 1000
#: 每条历史消息在预算里额外占的固定开销（角色、时间、发送者、分隔等），与第 1 期
#: 私聊同口径（``private_chat._HISTORY_MESSAGE_TOKEN_OVERHEAD`` 也是 +12）。
GROUP_HISTORY_MESSAGE_TOKEN_OVERHEAD = 12

#: 参与判定（decision）那一步只需要「最近几条」的尾部：判定提示词把那批历史截到
#: 2200 字符、最多 ``decision_context_items``（≤20）条，最近消息窗口只取 6 条。
#: 这里给一个固定小预算：既不会因为重启后热窗口是空的而看不到历史，也不会让每次
#: 「要不要回复」的判定都去装配 240K 的对话。
DECISION_HISTORY_TOKEN_BUDGET = 8192
#: 参与判定单次读取的条数上限（尾部够用即可；预算通常先咬住）。
DECISION_HISTORY_MAX_MESSAGES = 80

#: 单条超长消息被截断时补的说明（截断必须留痕，不能让人以为对方只说了这半句）。
#: 文案与第 1 期私聊保持一致，两个通道对同一件事说同一句话。
GROUP_HISTORY_TRUNCATION_NOTE = "…（本条过长，已截断）"


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    """把配置值夹到 ``[low, high]``；拿不到/不是数字就用默认值。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)
    return min(int(high), max(int(low), number))


def bounded_group_history_token_budget(value: Any) -> int:
    """群聊历史 token 预算的夹取口径（与 runtime_config 的字段约束一致）。"""

    return _bounded_int(
        value,
        default=GROUP_HISTORY_TOKEN_BUDGET,
        low=GROUP_HISTORY_TOKEN_BUDGET_MIN,
        high=GROUP_HISTORY_TOKEN_BUDGET_MAX,
    )


def bounded_group_history_reserve_tokens(value: Any) -> int:
    """固定部分余量的夹取口径（与 runtime_config 的字段约束一致）。"""

    return _bounded_int(
        value,
        default=GROUP_HISTORY_RESERVE_TOKENS,
        low=GROUP_HISTORY_RESERVE_TOKENS_MIN,
        high=GROUP_HISTORY_RESERVE_TOKENS_MAX,
    )


def _bot_setting(settings: Any, name: str, default: Any) -> Any:
    """从 ``settings.bot`` 读一个字段；缺项/``None`` 都退回默认值。"""

    bot = getattr(settings, "bot", None)
    value = getattr(bot, name, None) if bot is not None else None
    return default if value is None else value


def group_history_token_budget(settings: Any) -> int:
    """当前生效的群聊历史 token 预算。

    ``auto``（默认）且拿到了真实模型窗口时：按 ``min(模型真实窗口, 272Ki) − 固定余量``
    装配——模型是 1M/4M 也不会填满（业务预算 272Ki），模型比 272Ki 小就跟着更小。
    只有拿不到任何可信窗口时才退回兼容字段的保守值（保持迁移前的深度口径）。
    ``fixed`` 仍读配置值。
    """

    reserve = group_history_reserve_tokens(settings)
    window = auto_window_for(settings)
    if window is not None:
        business = business_total_window(window)
        return effective_group_history_budget(
            configured_budget=business,
            reserve_tokens=reserve,
            model_window_tokens=business,
            clamp_budget=False,
        )
    return bounded_group_history_token_budget(
        _bot_setting(
            settings,
            "group_history_token_budget",
            GROUP_HISTORY_TOKEN_BUDGET,
        )
    )


def group_history_reserve_tokens(settings: Any) -> int:
    """当前生效的群聊历史余量（默认 32768）。"""

    return bounded_group_history_reserve_tokens(
        _bot_setting(
            settings,
            "group_history_reserve_tokens",
            GROUP_HISTORY_RESERVE_TOKENS,
        )
    )


def _loose_int(value: Any, *, default: int = GROUP_HISTORY_TOKEN_BUDGET) -> int:
    """宽容取整：拿不到数字就用默认值（不夹上限，只有调用方决定区间）。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def effective_group_history_budget(
    *,
    configured_budget: Any = GROUP_HISTORY_TOKEN_BUDGET,
    reserve_tokens: Any = GROUP_HISTORY_RESERVE_TOKENS,
    model_window_tokens: Any,
    clamp_budget: bool = True,
) -> int:
    """这道硬闸门：**装配出来的历史 + 余量 ≤ 模型窗口**。

    取 ``min(配置预算, 模型窗口 - 余量)``，并对下限做保护：

    * 窗口本身小于 2048 时先把余量压到 ``窗口 - 1024``，保证历史至少还有 1024；
    * 结果再夹到 ``[1024, 配置预算]``，所以「历史 + 余量 ≤ 窗口」恒成立。

    传入 ``model_window_tokens`` 的是**模型输入窗口**（``MemoryService.max_context``：
    配置的 ``max_context_tokens`` 与网关自报窗口的较小值）。

    ``clamp_budget=False``（``auto`` + 真实元数据时）：``configured_budget`` 是
    ``min(模型真实窗口, 272Ki 业务预算)``，不再套 ``CONTEXT_WINDOW_MAX`` 那道只约束
    兼容配置字段的夹取。硬闸门本身（历史 + 余量 ≤ 窗口）一点不放松。
    """

    if clamp_budget:
        budget = bounded_group_history_token_budget(configured_budget)
    else:
        budget = max(GROUP_HISTORY_TOKEN_BUDGET_MIN, _loose_int(configured_budget))
    try:
        window = int(model_window_tokens or 0)
    except (TypeError, ValueError):
        window = 0
    window = max(1024, window)
    try:
        reserve = int(reserve_tokens or 0)
    except (TypeError, ValueError):
        reserve = GROUP_HISTORY_RESERVE_TOKENS
    reserve = max(0, min(reserve, max(0, window - 1024)))
    return max(1024, min(budget, window - reserve))


def _cut_text_to_tokens(text: str, limit_tokens: int) -> str:
    """把文本从尾部硬切到 token 上限内（保留开头）。

    与第 1 期私聊 ``private_chat._cut_text_to_tokens`` 同口径。两个模块各自持有一份
    20 行的纯截断工具是有意的取舍：群聊这边**不 import 私聊模块**（两条通道的依赖图
    保持互不相干），真正必须单一出处的是 token 换算本身
    （:func:`bot.utils.tokens.estimate_text_tokens`），它已经被复用。
    """

    if limit_tokens <= 0 or not text:
        return ""
    if estimate_text_tokens(text) <= limit_tokens:
        return text
    # 非 CJK 字符约 3 字符/token，所以 limit 个 token 最多只需要 3 * limit 个字符。
    candidate = text[: min(len(text), limit_tokens * 3 + 3)]
    low, high = 0, len(candidate)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_text_tokens(candidate[:mid]) <= limit_tokens:
            low = mid
        else:
            high = mid - 1
    return candidate[:low]


def _truncate_history_content(text: str, limit_tokens: int) -> str:
    """截断一条超长历史消息，并在放得下的情况下补一句「已截断」。"""

    body = str(text or "")
    if limit_tokens <= 0:
        return ""
    if estimate_text_tokens(body) <= limit_tokens:
        return body
    note = GROUP_HISTORY_TRUNCATION_NOTE
    kept = limit_tokens - estimate_text_tokens(note)
    if kept <= 0:
        # 预算连说明都放不下：先保住正文，别为了注释把内容全挤掉。
        return _cut_text_to_tokens(body, limit_tokens)
    return f"{_cut_text_to_tokens(body, kept)}{note}"


def _history_role(value: Any) -> str:
    """库里的 role 只允许 user / assistant；脏值一律当 user（不可信的一侧）。"""

    return "assistant" if str(value or "").strip().lower() == "assistant" else "user"


def assemble_group_history(
    rows: list[dict[str, Any]] | None,
    *,
    budget_tokens: int = GROUP_HISTORY_TOKEN_BUDGET,
    max_messages: int = GROUP_HISTORY_MAX_MESSAGES,
) -> list[dict[str, Any]]:
    """按 token 预算**从新到旧**累积装配群聊历史，装不下就停。

    与第 1 期私聊 ``assemble_private_history`` 同样的三条硬规则：

    1. **预算优先给最近的内容**：倒着累积，一旦装不下就停——宁可少给早期上下文，
       也不能让最新的一条被挤掉。
    2. **单条超长不整条丢**：某条消息自己就超过整个预算（或它正好是最新的一条）时，
       截到剩余预算并补一句「已截断」，而不是把这条直接扔掉。
    3. **条数安全上限**：最多只看最近 ``max_messages`` 条（理由见
       ``GROUP_HISTORY_MAX_MESSAGES``）。

    与私聊版本的唯一差别：群聊历史条目除了 ``role``/``content`` 还带
    ``created_at``/``sender_name``/``message_type``/``message_id`` 等元数据（提示词要
    按发送者与时间渲染），所以这里**保留整条 dict**（浅拷贝）而只截断 ``content``，
    而不是像私聊那样只返回 ``{"role", "content"}``。

    输入 ``rows`` 需要按时间正序（最老的在前）；返回同样是时间正序。

    ``budget_tokens`` 会先按 ``GROUP_HISTORY_TOKEN_BUDGET_MIN/MAX`` 夹取（与第 1 期
    私聊同一口径），所以小于 1024 的预算按 1024 处理。
    """

    items = [item for item in (rows or []) if isinstance(item, dict)]
    if not items:
        return []
    # 调用方算好的预算直接采信（真实窗口可能是 3M/4M）：只保下限，不套 2M。
    budget = loose_budget_tokens(
        budget_tokens,
        default=GROUP_HISTORY_TOKEN_BUDGET,
        low=GROUP_HISTORY_TOKEN_BUDGET_MIN,
    )
    keep = max(1, int(max_messages))
    items = items[-keep:]

    selected: list[dict[str, Any]] = []
    used_tokens = 0
    for item in reversed(items):  # 从新到旧
        content = str(item.get("content") or "")
        if not content.strip():
            continue
        tokens = estimate_text_tokens(content) + GROUP_HISTORY_MESSAGE_TOKEN_OVERHEAD
        if used_tokens + tokens <= budget:
            selected.append(
                {**item, "role": _history_role(item.get("role")), "content": content}
            )
            used_tokens += tokens
            continue
        remaining = budget - used_tokens - GROUP_HISTORY_MESSAGE_TOKEN_OVERHEAD
        oversized = estimate_text_tokens(content) > budget
        if remaining > 0 and (not selected or oversized):
            # 最近的这条一定留下；单条超过总预算的也只能截断留下。
            cut = _truncate_history_content(content, remaining)
            if cut.strip():
                selected.append(
                    {**item, "role": _history_role(item.get("role")), "content": cut}
                )
        break
    selected.reverse()
    return selected
