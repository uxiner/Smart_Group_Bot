"""F-038：decision 阶段的"再问一次"必须收敛、必须记账。

旧实现只在日志里区分空响应/不可解析，然后**无条件**再打一次完整判定：
上游在阶段 deadline 上稳定超时时，每条群消息都多花一次调用（成本 ×2、延迟叠加），
而这一次重试在成本看板上没有任何痕迹。

现在的口径：
* 解析成功 → 绝不第二次调用；
* 模型回了内容但不可解析（真正的解析失败）→ **保留**一次重试（为了拿到有效判定，
  符合"精度优先"），并记进 ``llm_metrics`` 的 ``parse_errors``（``/cost`` 可见）；
* 空响应（阶段 deadline 用尽 / 上游一声不响）→ 不重试；
* 重试仍不可用 → 按"不发言/不处置"处理，且一定有 WARNING 日志（绝不静默）。
"""

from __future__ import annotations

import unittest

from bot.services import llm_metrics
from bot.services.decision import DecisionService


class _ScriptedDecisionLLM:
    """按脚本依次回答的假决策模型；``calls`` 用来数调用次数。"""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses) or [""]
        self.calls = 0

    async def decision(self, system: str, prompt: str) -> str:
        index = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[index]


def _decision_parse_errors() -> int:
    """成本看板里 stage=decision 的解析失败计数。"""

    total = 0
    for (_day, stage), counters in llm_metrics.snapshot().items():
        if stage == "decision":
            total += int(counters.get("parse_errors", 0) or 0)
    return total


class DecisionRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        llm_metrics.reset()

    def tearDown(self) -> None:
        llm_metrics.reset()

    async def test_a_valid_verdict_is_never_asked_twice(self) -> None:
        llm = _ScriptedDecisionLLM("casual")

        result = await DecisionService(llm).decide("随便聊聊")

        self.assertEqual(result, "casual")
        self.assertEqual(llm.calls, 1, "解析成功就不该有第二次调用")
        self.assertEqual(_decision_parse_errors(), 0)

    async def test_unparsable_output_is_retried_once_and_counted(self) -> None:
        llm = _ScriptedDecisionLLM("I think the user is just chatting", "casual")

        result = await DecisionService(llm).decide("随便聊聊")

        self.assertEqual(result, "casual", "重试拿到有效判定就该采用")
        self.assertEqual(llm.calls, 2, "解析失败允许再问一次")
        self.assertEqual(_decision_parse_errors(), 1, "这次重试必须计入成本看板")

    async def test_invalid_output_is_retried_only_once(self) -> None:
        llm = _ScriptedDecisionLLM("nope", "still nope", "casual")

        result = await DecisionService(llm).decide("随便聊聊")

        self.assertEqual(result, "skip", "拿不到有效判定就不发言（不猜着处置）")
        self.assertEqual(llm.calls, 2, "重试上限是 1 次，不能越重试越多")
        self.assertEqual(_decision_parse_errors(), 1)

    async def test_empty_output_is_not_retried(self) -> None:
        """阶段 deadline 用尽 → 空响应：不再多打一次完整判定。"""

        llm = _ScriptedDecisionLLM("", "")

        with self.assertLogs("bot.services.decision", level="WARNING") as captured:
            result = await DecisionService(llm).decide("随便聊聊")

        self.assertEqual(result, "skip")
        self.assertEqual(llm.calls, 1, "空响应/超时不重试，避免成本与延迟翻倍")
        self.assertTrue(
            any("no retry" in line for line in captured.output),
            captured.output,
        )

    async def test_double_failure_is_loud_not_silent(self) -> None:
        llm = _ScriptedDecisionLLM("???", "???")

        with self.assertLogs("bot.services.decision", level="WARNING") as captured:
            result = await DecisionService(llm).decide("随便聊聊")

        self.assertEqual(result, "skip")
        self.assertTrue(
            any("treat as skip" in line for line in captured.output),
            captured.output,
        )

    async def test_a_mention_still_gets_a_reply_when_the_verdict_is_unusable(self) -> None:
        """"不发言"只针对主动插话；被 @ 时仍按既有路径回一条（不处罚任何人）。"""

        llm = _ScriptedDecisionLLM("", "")

        result = await DecisionService(llm).decide("在吗", is_mentioned=True)

        self.assertEqual(result, "casual")
        self.assertEqual(llm.calls, 1)


if __name__ == "__main__":
    unittest.main()
