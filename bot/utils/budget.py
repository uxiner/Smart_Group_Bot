"""业务预算的常量与显式校验（配置层与运行层共用，`bot.config` 的叶子依赖）。

为什么单独一个模块：业务预算既是**配置**（``bot.context_budget_tokens`` 等，运行时可读写、
pydantic 要校验）也是**运行时口径**（装配 / 最终闸门 / 群历史条数）。把常量与校验放在
叶子模块里，``bot.config`` 与 ``bot.services.*`` 都能引用而不产生环形依赖。

口径（2026-10-04 用户授权）：

* 每轮**业务总窗口**默认 272Ki = 278528，覆盖 system/人设 + 工具定义 + 记忆召回 +
  检索留档 + 历史 + 本轮消息 + 工具结果 + **输出预留**；
* 其中**输出/工具预留**默认 32Ki = 32768（默认输入上限因此是 245760）；
* 群历史单次读取安全条数默认 1000。

这三个值都是**运行时可配置**的：代码常量只作默认值；显式配置不会被隐藏常量截断
（文档建议业务总预算不超过 272Ki）。0/负数一律非法——**不允许用 0 关闭门禁**。
"""

from __future__ import annotations

from typing import Any

#: 业务总窗口默认值：272Ki。
BUSINESS_CONTEXT_WINDOW_TOKENS = 272 * 1024
#: 输出/工具预留默认值：32Ki。
BUSINESS_OUTPUT_RESERVE_TOKENS = 32 * 1024
#: 默认业务输入上限（配置化之后由上面两项相减得到）。
BUSINESS_INPUT_BUDGET_TOKENS = (
    BUSINESS_CONTEXT_WINDOW_TOKENS - BUSINESS_OUTPUT_RESERVE_TOKENS
)
#: 业务总预算的合法性边界。上界故意放宽：运维显式配大是合法的，只是不推荐；
#: 真正的天花板仍然是模型自己的窗口（取小）。
BUSINESS_CONTEXT_TOKENS_MIN = 1024
BUSINESS_CONTEXT_TOKENS_MAX = 16_000_000
#: 输出/工具预留的合法性边界：必须 > 0 且严格小于业务总预算。
CONTEXT_RESERVE_TOKENS_MIN = 1024
CONTEXT_RESERVE_TOKENS_MAX = 8_000_000
#: 群历史单次读取条数的默认值与合法性边界。
DEFAULT_GROUP_HISTORY_MAX_MESSAGES = 1000
GROUP_HISTORY_MAX_MESSAGES_MIN = 1
GROUP_HISTORY_MAX_MESSAGES_MAX = 20_000
#: 非法配置（预留 ≥ 总预算）时给输入至少留这么多 token，而不是把门禁关掉。
MIN_INPUT_ALLOWANCE_TOKENS = 1024


def bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    """把配置值夹到 ``[low, high]``；拿不到/不是数字就用默认值。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)
    return min(int(high), max(int(low), number))


def validate_business_budget(total_tokens: Any, reserve_tokens: Any) -> str:
    """业务预算的显式校验：返回错误说明，``""`` = 合法。

    0/负数、超过边界、以及 ``预留 ≥ 总预算`` 都算非法。
    """

    try:
        total = int(total_tokens)
    except (TypeError, ValueError):
        return "业务总预算必须是整数"
    try:
        reserve = int(reserve_tokens)
    except (TypeError, ValueError):
        return "输出/工具预留必须是整数"
    if total < BUSINESS_CONTEXT_TOKENS_MIN or total > BUSINESS_CONTEXT_TOKENS_MAX:
        return (
            f"业务总预算必须在 {BUSINESS_CONTEXT_TOKENS_MIN}.."
            f"{BUSINESS_CONTEXT_TOKENS_MAX} 之间（0 不能关闭门禁）"
        )
    if reserve < CONTEXT_RESERVE_TOKENS_MIN or reserve > CONTEXT_RESERVE_TOKENS_MAX:
        return (
            f"输出/工具预留必须在 {CONTEXT_RESERVE_TOKENS_MIN}.."
            f"{CONTEXT_RESERVE_TOKENS_MAX} 之间"
        )
    if reserve >= total:
        return "输出/工具预留必须小于业务总预算（否则没有输入空间）"
    return ""


def validate_group_history_max_messages(value: Any) -> str:
    """群历史条数上限的显式校验：返回错误说明，``""`` = 合法。"""

    try:
        count = int(value)
    except (TypeError, ValueError):
        return "群历史条数上限必须是整数"
    if count < GROUP_HISTORY_MAX_MESSAGES_MIN or count > GROUP_HISTORY_MAX_MESSAGES_MAX:
        return (
            f"群历史条数上限必须在 {GROUP_HISTORY_MAX_MESSAGES_MIN}.."
            f"{GROUP_HISTORY_MAX_MESSAGES_MAX} 之间"
        )
    return ""
