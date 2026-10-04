"""最终载荷裁剪（``bot/services/payload_fit.py``）的回归测试。

锁的是 2026-10-04 事故之后的"最后一公里"：装配装得下、最终请求也**必须**发得出去，
装不下时按固定优先级裁，而**不是**整条 skip。优先级与统一闸门一致：

    最老的历史 → 检索留档 → 记忆召回

**永不裁**：核心系统块、本轮消息；工具协议（assistant ``tool_calls`` + ``tool`` 结果）
整对保留，长长的工具结果只截断正文。
"""

from __future__ import annotations

import unittest

from bot.services.payload_fit import (
    CTX_LAYER_KEY,
    LAYER_HISTORY,
    LAYER_MEMORY_RECALL,
    LAYER_SEARCH_RECORDS,
    context_layer_for_history_row,
    fit_messages_to_input_budget,
    message_context_layer,
    strip_context_layer,
    strip_internal_message_keys,
    tag_context_layer,
    tag_context_layers,
    tag_rendered_history,
)
from bot.services.model_limits import (
    conservative_message_tokens,
    estimate_tools_tokens,
)


def _msg(role: str, content: str, layer: str | None = None) -> dict:
    message = {"role": role, "content": content}
    if layer is not None:
        message[CTX_LAYER_KEY] = layer
    return message


def _fit(messages: list[dict], budget: int, tools: list[dict] | None = None):
    return fit_messages_to_input_budget(
        messages,
        message_tokens=conservative_message_tokens,
        tools_tokens=estimate_tools_tokens(tools),
        budget_tokens=budget,
    )


def _contents(messages: list[dict]) -> list[str]:
    return [str(item.get("content") or "") for item in messages]


class NoTrimTests(unittest.TestCase):
    def test_payload_within_budget_is_returned_untouched(self) -> None:
        messages = [
            _msg("system", "人设"),
            _msg("user", "旧历史", LAYER_HISTORY),
            _msg("user", "现在这条"),
        ]

        fitted = _fit(messages, budget=100_000)

        self.assertFalse(fitted.changed)
        self.assertFalse(fitted.over_budget)
        self.assertEqual(_contents(fitted.messages), ["人设", "旧历史", "现在这条"])
        self.assertEqual(fitted.dropped_messages, 0)

    def test_input_messages_are_never_mutated(self) -> None:
        """每个 fallback 都会重新裁一次，绝不能把上一次的裁剪累积到原载荷上。"""

        messages = [
            _msg("system", "人设"),
            _msg("user", "历史" * 500, LAYER_HISTORY),
            _msg("user", "现在这条"),
        ]
        before = [dict(item) for item in messages]

        first = _fit(messages, budget=1_200)
        second = _fit(messages, budget=1_200)

        self.assertEqual(messages, before)
        self.assertEqual(
            _contents(first.messages),
            _contents(second.messages),
        )


class TrimPriorityTests(unittest.TestCase):
    def test_oldest_history_is_dropped_before_search_records_and_recall(self) -> None:
        history = [_msg("user", f"历史{index}-" + "正文" * 200, LAYER_HISTORY) for index in range(4)]
        search = [_msg("system", "留档" + "x" * 600, LAYER_SEARCH_RECORDS)]
        recall = [_msg("system", "召回" + "y" * 600, LAYER_MEMORY_RECALL)]
        messages = [_msg("system", "人设"), *history, *search, *recall, _msg("user", "现在这条")]

        fitted = _fit(messages, budget=2_000)

        kept = _contents(fitted.messages)
        self.assertEqual(kept[0], "人设")
        self.assertEqual(kept[-1], "现在这条")
        self.assertTrue(fitted.layers_dropped)
        self.assertEqual(fitted.layers_dropped[0], LAYER_HISTORY)
        # 历史里保留的必然是最新的那几条（从最旧的一端丢）。
        kept_history = [item for item in kept if item.startswith("历史")]
        self.assertEqual(kept_history, [item for item in kept_history if item])
        if kept_history:
            self.assertTrue(kept_history[-1].startswith("历史3-"))

    def test_core_system_blocks_and_current_turn_survive_every_trim(self) -> None:
        messages = [
            _msg("system", "核心人设"),
            _msg("user", "历史" * 800, LAYER_HISTORY),
            _msg("system", "输出协议"),
            _msg("user", "现在这条"),
        ]

        fitted = _fit(messages, budget=400)

        kept = _contents(fitted.messages)
        self.assertIn("核心人设", kept)
        self.assertIn("现在这条", kept)
        self.assertNotIn("历史" * 800, kept)

    def test_recall_is_only_trimmed_after_search_records(self) -> None:
        messages = [
            _msg("system", "人设"),
            _msg("system", "留档" + "x" * 900, LAYER_SEARCH_RECORDS),
            _msg("system", "召回" + "y" * 900, LAYER_MEMORY_RECALL),
            _msg("user", "现在这条"),
        ]
        base = _fit(messages, budget=10_000)
        self.assertEqual(base.layers_dropped, ())

        # 只够放下其中一层时：留档先走，召回后走。
        tight = _fit(messages, budget=200)
        self.assertEqual(tight.layers_dropped, (LAYER_SEARCH_RECORDS, LAYER_MEMORY_RECALL))
        kept = _contents(tight.messages)
        self.assertNotIn("留档" + "x" * 900, kept)
        self.assertNotIn("召回" + "y" * 900, kept)
        self.assertIn("现在这条", kept)


class ToolProtocolTests(unittest.TestCase):
    def _loop_messages(self) -> list[dict]:
        return [
            _msg("system", "人设"),
            _msg("user", "历史" * 600, LAYER_HISTORY),
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "websearch", "arguments": '{"query":"天气"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "websearch",
                "content": "结果" * 4_000,
            },
            _msg("user", "现在这条"),
        ]

    def test_tool_pair_is_never_orphaned(self) -> None:
        fitted = _fit(self._loop_messages(), budget=1_500)

        roles = [item["role"] for item in fitted.messages]
        if "tool" in roles:
            # 有 tool 结果就必须还有那条 assistant 声明（协议不能断）。
            assistant_with_calls = [
                item
                for item in fitted.messages
                if item.get("role") == "assistant" and item.get("tool_calls")
            ]
            self.assertTrue(assistant_with_calls)
            self.assertIn("assistant", roles)
        self.assertIn("现在这条", _contents(fitted.messages))

    def test_long_tool_result_is_truncated_not_dropped(self) -> None:
        fitted = _fit(self._loop_messages(), budget=3_000)

        tool_messages = [item for item in fitted.messages if item.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], "call-1")
        self.assertEqual(fitted.truncated_messages, 1)
        self.assertLess(len(tool_messages[0]["content"]), len("结果" * 4_000))
        self.assertFalse(fitted.over_budget)

    def test_core_only_payload_over_budget_is_reported_honestly(self) -> None:
        """固定层（核心系统块 + 本轮）自己就装不下 → 如实报 over_budget，不发请求。"""

        messages = [
            _msg("system", "人设" * 2_000),
            _msg("user", "现在这条"),
        ]

        fitted = _fit(messages, budget=200)

        self.assertTrue(fitted.over_budget)
        self.assertEqual(_contents(fitted.messages), _contents(messages))


class LayerTaggingTests(unittest.TestCase):
    def test_history_rows_map_to_the_same_layers_as_the_gate(self) -> None:
        self.assertEqual(
            context_layer_for_history_row({"role": "user"}), LAYER_HISTORY
        )
        self.assertEqual(
            context_layer_for_history_row(
                {"role": "user", "memory_source": "search_record"}
            ),
            LAYER_SEARCH_RECORDS,
        )
        self.assertEqual(
            context_layer_for_history_row(
                {"role": "user", "memory_source": "recalled_archive_index"}
            ),
            LAYER_MEMORY_RECALL,
        )
        # 资料块的头部说明是 system 角色：永不裁。
        self.assertEqual(
            context_layer_for_history_row({"role": "system"}), "core"
        )

    def test_tag_rendered_history_aligns_rows_and_rendered_messages(self) -> None:
        rows = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b", "memory_source": "search_record"},
            {"role": "user", "content": "c", "memory_source": "recalled_archive_index"},
        ]
        rendered = [
            {"role": "user", "content": "rendered-a"},
            {"role": "user", "content": "rendered-b"},
            {"role": "user", "content": "rendered-c"},
        ]

        tag_rendered_history(rows, rendered)

        self.assertEqual(message_context_layer(rendered[0]), LAYER_HISTORY)
        self.assertEqual(message_context_layer(rendered[1]), LAYER_SEARCH_RECORDS)
        self.assertEqual(message_context_layer(rendered[2]), LAYER_MEMORY_RECALL)

    def test_untagged_messages_are_core(self) -> None:
        self.assertEqual(message_context_layer({"role": "user"}), "core")
        self.assertEqual(message_context_layer(None), "core")
        self.assertEqual(message_context_layer({"role": "user", CTX_LAYER_KEY: "junk"}), "core")

    def test_layer_marker_does_not_leak_into_a_copied_message(self) -> None:
        message = tag_context_layer({"role": "user", "content": "x"}, LAYER_HISTORY)
        self.assertEqual(message_context_layer(message), LAYER_HISTORY)

        clean = strip_context_layer(message)

        self.assertNotIn(CTX_LAYER_KEY, clean)
        self.assertEqual(clean["content"], "x")

    def test_tag_context_layers_returns_the_same_items(self) -> None:
        items = [{"role": "user", "content": "x"}]

        result = tag_context_layers(items, LAYER_MEMORY_RECALL)

        self.assertIs(result[0], items[0])
        self.assertEqual(message_context_layer(items[0]), LAYER_MEMORY_RECALL)

    def test_internal_keys_are_stripped_without_mutating_the_source(self) -> None:
        messages = [
            {"role": "user", "content": "x", CTX_LAYER_KEY: LAYER_HISTORY},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "function": {"name": "t", "arguments": "{}"}}],
            },
        ]

        cleaned = strip_internal_message_keys(messages)

        self.assertNotIn(CTX_LAYER_KEY, cleaned[0])
        self.assertEqual(cleaned[0]["content"], "x")
        self.assertEqual(cleaned[1]["tool_calls"], messages[1]["tool_calls"])
        # 原载荷必须保持带标记的状态：每个 fallback 都要拿它重新裁一次。
        self.assertEqual(message_context_layer(messages[0]), LAYER_HISTORY)


class NoInjectionTests(unittest.TestCase):
    def test_trimming_only_removes_or_truncates_never_injects(self) -> None:
        """裁剪只能"少给"，绝不能凭空加消息。

        这条对隐私红线同样重要：裁剪不可能把另一条链路（私聊）的内容搬进群聊载荷，
        也不可能把已执行工具的结果复制成第二份。
        """

        messages = [
            _msg("system", "人设"),
            _msg("user", "历史" * 400, LAYER_HISTORY),
            _msg("system", "协议"),
            _msg("user", "现在这条"),
        ]
        originals = {(item["role"], str(item["content"])) for item in messages}

        for budget in (50_000, 5_000, 1_000, 100, 1):
            fitted = _fit(messages, budget=budget)
            self.assertLessEqual(len(fitted.messages), len(messages))
            for item in fitted.messages:
                role = item["role"]
                content = str(item["content"])
                self.assertTrue(
                    (role, content) in originals
                    # 允许工具结果的"截断版"：本用例里没有工具消息，所以必须完全一致。
                    or any(
                        original_role == role and original_content.startswith(content)
                        for original_role, original_content in originals
                    ),
                    f"unexpected message injected: {role}/{content[:40]!r}",
                )


if __name__ == "__main__":
    unittest.main()
