"""第 3 期 D 项：统一预算闸门（``bot/services/context_gate.py``）的回归测试。

这道闸门要在一处回答「三条链路往同一个 272K 窗口里塞东西，装不下先裁谁」。用例锁定
的都是口径本身，不是「函数能跑」：

- 装得下 → 一层都不裁，顺序不变；
- 装不下 → **最老的历史 → 最旧的搜索记录 → 记忆召回条数**，一条一条从最旧端丢；
- **系统提示词/人设与本轮消息永远不裁**（真装不下就如实报 ``over_budget``）；
- 一层只剩最后一条还装不下 → 截断并注明，而不是把这一层清空；
- 预算夹取、配置读取的宽容口径；
- 纯函数：两次调用互不影响，输入不被改写（并发/多群不会互相串）。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from bot.services.context_gate import (
    CONTEXT_MESSAGE_TOKEN_OVERHEAD,
    CONTEXT_TOKEN_BUDGET,
    CONTEXT_TOKEN_BUDGET_MAX,
    CONTEXT_TOKEN_BUDGET_MIN,
    CONTEXT_TRUNCATION_NOTE,
    assemble_context_within_budget,
    bounded_context_token_budget,
    context_token_budget,
)
from bot.utils.tokens import estimate_text_tokens

#: 非 CJK 字符约 3 字符/token（与 estimate_text_tokens 同口径），300 字 ≈ 100 token
_BODY = "a" * 300


def _msg(prefix: str, index: int) -> dict[str, str]:
    return {"role": "system", "content": f"{prefix}{index}-{_BODY}"}


def _tokens(message: dict[str, str]) -> int:
    return estimate_text_tokens(message["content"]) + CONTEXT_MESSAGE_TOKEN_OVERHEAD


def _contents(messages: list[dict]) -> list[str]:
    return [str(item.get("content") or "") for item in messages]


class BudgetClampTests(unittest.TestCase):
    def test_budget_is_clamped_to_the_same_range_as_runtime_config(self) -> None:
        """CTX-001：上界的**唯一来源**是 :mod:`bot.utils.budget`。

        之前这里写死 2_000_000，而 ``runtime_config`` 的 ``context_budget_tokens``
        上界是 ``BUSINESS_CONTEXT_TOKENS_MAX`` = 16_000_000，注释还声称"与
        runtime_config 一致"——实际不一致，于是同一个上界在文档、UI、校验三处给出三个
        答案。现在两边必须相等；这条断言就是防止它再分叉。
        """

        from bot.utils.budget import (
            BUSINESS_CONTEXT_TOKENS_MAX,
            BUSINESS_CONTEXT_TOKENS_MIN,
        )
        from bot.services.runtime_config import BotBehaviorConfig

        self.assertEqual(CONTEXT_TOKEN_BUDGET_MAX, BUSINESS_CONTEXT_TOKENS_MAX)
        self.assertEqual(CONTEXT_TOKEN_BUDGET_MIN, BUSINESS_CONTEXT_TOKENS_MIN)
        schema_bound = [
            getattr(meta, "le", None)
            for meta in BotBehaviorConfig.model_fields["context_budget_tokens"].metadata
        ]
        self.assertIn(BUSINESS_CONTEXT_TOKENS_MAX, schema_bound)
        self.assertEqual(bounded_context_token_budget(0), BUSINESS_CONTEXT_TOKENS_MIN)
        self.assertEqual(bounded_context_token_budget(-5), BUSINESS_CONTEXT_TOKENS_MIN)
        self.assertEqual(
            bounded_context_token_budget(10**9), BUSINESS_CONTEXT_TOKENS_MAX
        )
        self.assertEqual(bounded_context_token_budget("nonsense"), CONTEXT_TOKEN_BUDGET)
        self.assertEqual(bounded_context_token_budget(None), CONTEXT_TOKEN_BUDGET)
        self.assertEqual(bounded_context_token_budget(4096), 4096)
        # 显式配大是合法的（运维的选择），只是不推荐——这里钉住"不按上界截断"。
        self.assertEqual(bounded_context_token_budget(4_000_000), 4_000_000)

    def test_one_entry_reads_the_live_setting(self) -> None:
        """三条链路必须读同一个数字：这里读 ``bot.max_context_tokens``。"""

        self.assertEqual(
            context_token_budget(
                SimpleNamespace(bot=SimpleNamespace(max_context_tokens=278528))
            ),
            278528,
        )
        self.assertEqual(
            context_token_budget(
                SimpleNamespace(bot=SimpleNamespace(max_context_tokens=4096))
            ),
            4096,
        )
        # 配置缺项/删掉 settings 都不能炸，一律退回 272K 默认值
        self.assertEqual(
            context_token_budget(SimpleNamespace(bot=SimpleNamespace())),
            CONTEXT_TOKEN_BUDGET,
        )
        self.assertEqual(
            context_token_budget(SimpleNamespace()), CONTEXT_TOKEN_BUDGET
        )


class NoTrimTests(unittest.TestCase):
    def test_everything_fits_and_order_is_preserved(self) -> None:
        assembly = assemble_context_within_budget(
            system=[{"role": "system", "content": "人设"}],
            current_turn=[{"role": "user", "content": "现在这条"}],
            memory_recall=[_msg("m", 1)],
            search_records=[_msg("s", 1)],
            history=[_msg("h", 1), _msg("h", 2)],
            budget_tokens=278_528,
        )
        self.assertFalse(assembly.over_budget)
        self.assertEqual(assembly.trims, ())
        self.assertEqual(
            {name: len(items) for name, items in assembly.layers.items()},
            {
                "system": 1,
                "current_turn": 1,
                "memory_recall": 1,
                "search_records": 1,
                "history": 2,
            },
        )
        self.assertEqual(
            [item["content"].split("-")[0] for item in assembly.messages],
            ["人设", "m1", "s1", "h1", "h2", "现在这条"],
        )

    def test_empty_layers_are_returned_as_empty_lists(self) -> None:
        assembly = assemble_context_within_budget(
            system=[{"role": "system", "content": "p"}],
            current_turn=[{"role": "user", "content": "now"}],
            budget_tokens=278_528,
        )
        self.assertEqual(assembly.layers["history"], [])
        self.assertEqual(assembly.layers["search_records"], [])
        self.assertEqual(assembly.layers["memory_recall"], [])
        self.assertEqual(len(assembly.messages), 2)


class TrimPriorityTests(unittest.TestCase):
    def test_oldest_history_is_dropped_before_anything_else(self) -> None:
        assembly = assemble_context_within_budget(
            system=[{"role": "system", "content": "p"}],
            current_turn=[{"role": "user", "content": "now"}],
            history=[_msg("h", 1), _msg("h", 2), _msg("h", 3), _msg("h", 4)],
            search_records=[_msg("s", 1), _msg("s", 2), _msg("s", 3)],
            memory_recall=[_msg("m", 1), _msg("m", 2)],
            budget_tokens=1024,
        )
        # 固定层 28 token，允许 996；历史 4×112 超一点点 → 只丢最老的那一条
        self.assertEqual(len(assembly.layers["history"]), 3)
        self.assertEqual(len(assembly.layers["search_records"]), 3)
        self.assertEqual(len(assembly.layers["memory_recall"]), 2)
        self.assertEqual(
            [(trim.layer, trim.dropped_messages, trim.truncated_messages) for trim in assembly.trims],
            [("history", 1, 0)],
        )
        self.assertEqual(
            assembly.layers["history"][0]["content"].split("-")[0], "h2"
        )
        self.assertLessEqual(assembly.used_tokens, 1024)

    def test_priority_is_history_then_search_then_recall(self) -> None:
        assembly = assemble_context_within_budget(
            system=[{"role": "system", "content": "p"}],
            current_turn=[{"role": "user", "content": "now"}],
            history=[_msg("h", 1), _msg("h", 2), _msg("h", 3), _msg("h", 4)],
            search_records=[_msg("s", 1), _msg("s", 2), _msg("s", 3)],
            memory_recall=[_msg("m", 1), _msg("m", 2)],
            budget_tokens=1024,
            reserve_tokens=900,
        )
        trimmed_layers = [trim.layer for trim in assembly.trims]
        self.assertEqual(trimmed_layers, ["history", "search_records", "memory_recall"])
        # 历史先被清空，再轮到检索留档，最后才是召回
        self.assertEqual(assembly.layers["history"], [])
        self.assertEqual(assembly.layers["search_records"], [])
        self.assertLessEqual(len(assembly.layers["memory_recall"]), 1)
        self.assertTrue(assembly.layers["system"])
        self.assertTrue(assembly.layers["current_turn"])
        # 裁剪不能留下空消息（空 system 块只会给 prompt 添噪音）
        self.assertFalse(
            [item for item in assembly.messages if not str(item["content"]).strip()]
        )

    def test_recall_is_trimmed_from_the_front(self) -> None:
        assembly = assemble_context_within_budget(
            system=[{"role": "system", "content": "p"}],
            current_turn=[{"role": "user", "content": "now"}],
            memory_recall=[_msg("m", 1), _msg("m", 2), _msg("m", 3)],
            budget_tokens=1024,
            reserve_tokens=700,
        )
        kept = [item["content"].split("-")[0] for item in assembly.layers["memory_recall"]]
        self.assertEqual(kept, ["m2", "m3"], "记忆召回按条裁，先裁最旧的")


class NeverTrimTests(unittest.TestCase):
    def test_system_and_current_turn_survive_an_oversized_fixed_part(self) -> None:
        persona = "P" * 4000  # 约 1333 token，本身就超过 1024 的预算
        current = {"role": "user", "content": "current-turn-sentinel"}
        assembly = assemble_context_within_budget(
            system=[{"role": "system", "content": persona}],
            current_turn=[current],
            history=[_msg("h", 1)],
            search_records=[_msg("s", 1)],
            memory_recall=[_msg("m", 1)],
            budget_tokens=1024,
        )
        self.assertEqual(_contents(assembly.messages)[0], persona)
        self.assertEqual(assembly.messages[-1], current)
        self.assertEqual(assembly.layers["history"], [])
        self.assertEqual(assembly.layers["search_records"], [])
        self.assertEqual(assembly.layers["memory_recall"], [])
        self.assertTrue(assembly.over_budget, "固定层自己超预算必须如实上报")

    def test_reserve_is_counted_against_the_budget(self) -> None:
        assembly = assemble_context_within_budget(
            system=[{"role": "system", "content": "p"}],
            current_turn=[{"role": "user", "content": "now"}],
            history=[_msg("h", 1)],
            budget_tokens=1024,
            reserve_tokens=1000,
        )
        self.assertEqual(assembly.layers["history"], [])
        self.assertTrue(assembly.over_budget)


class TruncationTests(unittest.TestCase):
    def test_a_single_oversized_message_is_truncated_not_dropped(self) -> None:
        huge = {"role": "user", "content": "z" * 3000}
        assembly = assemble_context_within_budget(
            system=[{"role": "system", "content": "p"}],
            current_turn=[{"role": "user", "content": "now"}],
            history=[huge],
            budget_tokens=1024,
        )
        kept = assembly.layers["history"]
        self.assertEqual(len(kept), 1, "最新那条历史不能整条丢")
        self.assertTrue(str(kept[0]["content"]).endswith(CONTEXT_TRUNCATION_NOTE))
        self.assertLessEqual(_tokens(kept[0]), 1024 - 28, "截断后必须装进剩余预算")
        self.assertEqual(
            [(trim.layer, trim.truncated_messages) for trim in assembly.trims],
            [("history", 1)],
        )


class PurityTests(unittest.TestCase):
    def test_calls_do_not_share_state_and_inputs_are_not_mutated(self) -> None:
        history = [_msg("h", 1), _msg("h", 2)]
        first = assemble_context_within_budget(
            system=[{"role": "system", "content": "p"}],
            current_turn=[{"role": "user", "content": "now"}],
            history=history,
            budget_tokens=1024,
        )
        # 调用方拿着结果乱改，不该影响下一次装配
        first.layers["history"].clear()
        second = assemble_context_within_budget(
            system=[{"role": "system", "content": "p"}],
            current_turn=[{"role": "user", "content": "now"}],
            history=history,
            budget_tokens=1024,
        )
        self.assertEqual(len(second.layers["history"]), 2)
        self.assertEqual(len(history), 2, "输入列表不能被改写")

    def test_two_different_scopes_do_not_influence_each_other(self) -> None:
        """并发/多群互不影响：同样的输入永远得到同样的结果（无共享状态）。"""

        def build() -> tuple[int, tuple[str, ...]]:
            assembly = assemble_context_within_budget(
                system=[{"role": "system", "content": "p"}],
                current_turn=[{"role": "user", "content": "now"}],
                history=[_msg("h", 1), _msg("h", 2), _msg("h", 3)],
                search_records=[_msg("s", 1)],
                budget_tokens=1024,
            )
            return assembly.used_tokens, tuple(
                trim.layer for trim in assembly.trims
            )

        self.assertEqual(build(), build())

    def test_non_mapping_items_are_ignored(self) -> None:
        assembly = assemble_context_within_budget(
            system=[{"role": "system", "content": "p"}],
            current_turn=[{"role": "user", "content": "now"}],
            history=["not-a-dict", None, _msg("h", 1)],  # type: ignore[list-item]
            budget_tokens=1024,
        )
        self.assertEqual(len(assembly.layers["history"]), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
