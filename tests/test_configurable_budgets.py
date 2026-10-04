"""业务预算配置化（提交①）的回归。

用户口径：业务总预算 / 输出工具预留 / 群历史单次条数都必须**运行时可读写**，代码常量只是
默认值；显式配置不被隐藏常量截断；配置不正、预留 ≥ 总预算、条数不合法要**显式校验**，
绝不能用 0 关闭门禁。默认仍是 272Ki（输入上限 245760），推荐值而不是硬上限。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from bot.config import BotConfig, Settings
from bot.services import context_gate, group_context, model_limits as ml, private_chat
from bot.services.llm import LLMService
from bot.services.memory import MemoryService
from bot.services.runtime_config import (
    BotBehaviorConfig,
    RuntimeConfig,
    _normalize_deprecated_runtime_payload,
)
from bot.utils.budget import (
    BUSINESS_CONTEXT_WINDOW_TOKENS,
    BUSINESS_INPUT_BUDGET_TOKENS,
    validate_business_budget,
    validate_group_history_max_messages,
)

GATEWAY = "http://gw.internal:8080/v1"


def _model(*, window: int | None = 1_000_000):
    from bot.config import ModelConfig

    return ModelConfig(
        model="home_work2api/cn:deepseek-v4.1-flash",
        provider="home_work2api",
        api_key="k",
        api_base=GATEWAY,
        max_tokens=2048,
        retry_attempts=1,
    )


def _bot_config(*, window: int | None = 1_000_000, **overrides) -> BotConfig:
    payload = {
        "main_model": _model(),
        "max_context_tokens": 278_528,
        "context_budget_tokens": 278_528,
        "context_reserve_tokens": 32_768,
        "group_history_max_messages": 1000,
        "group_history_token_budget": 278_528,
        "group_history_reserve_tokens": 32_768,
        "private_chat_history_token_budget": 278_528,
        "max_output_tokens": 2048,
    }
    payload.update(overrides)
    return BotConfig(**payload)


class _StubLLM:
    class main:
        model = "home_work2api/cn:deepseek-v4.1-flash"


def _settings(**overrides) -> SimpleNamespace:
    return SimpleNamespace(bot=_bot_config(**overrides))


class ValidationTests(unittest.TestCase):
    def test_defaults_are_the_recommended_values(self) -> None:
        self.assertEqual(BotConfig().context_budget_tokens, 278_528)
        self.assertEqual(BotConfig().context_reserve_tokens, 32_768)
        self.assertEqual(BotConfig().group_history_max_messages, 1000)
        self.assertEqual(BotBehaviorConfig().context_budget_tokens, 278_528)
        self.assertEqual(BotBehaviorConfig().context_reserve_tokens, 32_768)
        self.assertEqual(BotBehaviorConfig().group_history_max_messages, 1000)
        self.assertEqual(BUSINESS_CONTEXT_WINDOW_TOKENS, 272 * 1024)
        self.assertEqual(BUSINESS_INPUT_BUDGET_TOKENS, 245_760)

    def test_reserve_must_be_smaller_than_the_budget(self) -> None:
        with self.assertRaises(ValidationError):
            BotConfig(context_budget_tokens=4096, context_reserve_tokens=4096)
        with self.assertRaises(ValidationError):
            BotBehaviorConfig(context_budget_tokens=4096, context_reserve_tokens=8192)
        self.assertIn(
            "小于",
            validate_business_budget(4096, 8192),
        )
        self.assertEqual(validate_business_budget(278_528, 32_768), "")

    def test_zero_is_never_a_way_to_disable_the_gate(self) -> None:
        for kwargs in (
            {"context_budget_tokens": 0},
            {"context_reserve_tokens": 0},
            {"group_history_max_messages": 0},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValidationError):
                    BotConfig(**kwargs)
        self.assertIn("0 不能关闭门禁", validate_business_budget(0, 1024))
        self.assertIn("条数", validate_group_history_max_messages(0))

    def test_explicit_large_budget_is_not_hidden_truncated(self) -> None:
        """合法运维配置（>2M）不许被隐藏常量截断：配置层接受，装配口径跟着走。"""

        ml.reset_model_limits_for_tests()
        self.addCleanup(ml.reset_model_limits_for_tests)
        config = _bot_config(context_budget_tokens=3_000_000, context_reserve_tokens=32_768)
        settings = SimpleNamespace(bot=config)

        self.assertEqual(config.context_budget_tokens, 3_000_000)
        for window in (1_000_000, 4_000_000):
            with self.subTest(window=window):
                ml.reset_model_limits_for_tests()
                ml.MODEL_LIMITS.record(config.main_model, total_window=window)
                # 与模型真实窗口取小，而不是被隐藏常量截到 2M 或 272Ki。
                expected = min(window, 3_000_000)
                self.assertEqual(ml.effective_context_window(settings), expected)
                self.assertEqual(context_gate.context_token_budget(settings), expected)
                llm = LLMService(
                    config.main_model,
                    config.main_model,
                    compress=config.main_model,
                    business_context_tokens=config.context_budget_tokens,
                    context_reserve_tokens=config.context_reserve_tokens,
                )
                self.assertEqual(
                    llm.input_token_budget(llm._chat_candidates(llm.main)[0]),
                    expected - 32_768,
                )

    def test_runtime_defensive_clamp_never_disables_the_gate(self) -> None:
        """配置层挡住了非法值；万一运行时还是拿到非法值，也要收紧而不是关掉门禁。"""

        bad = SimpleNamespace(
            context_budget_tokens=4096,
            context_reserve_tokens=999_999,
            max_context_tokens=278_528,
        )

        reserve = ml.configured_reserve_tokens(bad)

        self.assertLess(reserve, 4096)
        self.assertGreaterEqual(reserve, 1)


class EffectiveBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        ml.reset_model_limits_for_tests()
        self.addCleanup(ml.reset_model_limits_for_tests)

    def test_smaller_business_budget_tightens_every_chain(self) -> None:
        settings = _settings(context_budget_tokens=100_000, context_reserve_tokens=20_000)
        ml.MODEL_LIMITS.record(settings.bot.main_model, total_window=1_000_000)

        llm = LLMService(
            settings.bot.main_model,
            settings.bot.main_model,
            compress=settings.bot.main_model,
            business_context_tokens=100_000,
            context_reserve_tokens=20_000,
        )
        memory = MemoryService(
            settings.bot,
            _StubLLM(),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
        )

        self.assertEqual(context_gate.context_token_budget(settings), 100_000)
        self.assertEqual(
            group_context.group_history_token_budget(settings), 100_000 - 20_000
        )
        self.assertEqual(
            private_chat.private_history_token_budget(settings), 100_000 - 20_000
        )
        self.assertEqual(memory.max_context, 100_000)
        self.assertEqual(memory.group_history_token_budget, 80_000)
        self.assertEqual(memory.group_history_reserve_tokens, 20_000)
        self.assertEqual(
            llm.input_token_budget(llm._chat_candidates(llm.main)[0]), 80_000
        )

    def test_larger_business_budget_loosens_up_to_the_model_window(self) -> None:
        settings = _settings(
            context_budget_tokens=1_500_000, context_reserve_tokens=100_000
        )
        ml.MODEL_LIMITS.record(settings.bot.main_model, total_window=4_000_000)
        llm = LLMService(
            settings.bot.main_model,
            settings.bot.main_model,
            compress=settings.bot.main_model,
            business_context_tokens=1_500_000,
            context_reserve_tokens=100_000,
        )

        self.assertEqual(context_gate.context_token_budget(settings), 1_500_000)
        self.assertEqual(
            llm.input_token_budget(llm._chat_candidates(llm.main)[0]), 1_400_000
        )

        # 模型只有 1M → 与模型窗口取小
        ml.MODEL_LIMITS.record(settings.bot.main_model, total_window=1_000_000)
        self.assertEqual(context_gate.context_token_budget(settings), 1_000_000)
        self.assertEqual(
            llm.input_token_budget(llm._chat_candidates(llm.main)[0]), 900_000
        )

    def test_smaller_model_window_still_tightens(self) -> None:
        settings = _settings()
        ml.MODEL_LIMITS.record(settings.bot.main_model, total_window=131_072)
        llm = LLMService(
            settings.bot.main_model,
            settings.bot.main_model,
            compress=settings.bot.main_model,
            business_context_tokens=278_528,
            context_reserve_tokens=32_768,
        )

        self.assertEqual(context_gate.context_token_budget(settings), 131_072)
        self.assertEqual(
            llm.input_token_budget(llm._chat_candidates(llm.main)[0]), 131_072 - 32_768
        )

    def test_configured_group_history_max_messages_is_used(self) -> None:
        memory = MemoryService(
            _bot_config(group_history_max_messages=250),
            _StubLLM(),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
        )

        self.assertEqual(memory.group_history_max_messages, 250)
        self.assertEqual(
            group_context.group_history_max_messages(
                SimpleNamespace(bot=_bot_config(group_history_max_messages=250))
            ),
            250,
        )
        # 默认仍是 1000
        self.assertEqual(
            group_context.group_history_max_messages(_settings()), 1000
        )


class ReservePrecedenceTests(unittest.TestCase):
    """旧字段兼容按"显式设置过哪个字段"判定，绝不按"值看起来等于默认"猜。"""

    def test_legacy_field_wins_only_when_explicitly_set(self) -> None:
        # 只显式设了旧字段（旧库/旧代码）→ 用旧字段
        legacy_only = BotConfig(group_history_reserve_tokens=8192)
        self.assertEqual(ml.configured_reserve_tokens(legacy_only), 8192)

        # 新字段被显式设回默认 32768 → 以新字段为准，不被旧字段静默覆盖
        explicit_default = BotConfig(
            context_reserve_tokens=32_768,
            group_history_reserve_tokens=8192,
        )
        self.assertEqual(ml.configured_reserve_tokens(explicit_default), 32_768)

        # 两个都显式设置 → 新字段为准（数据库是权威）
        both = BotConfig(context_reserve_tokens=40_000, group_history_reserve_tokens=8192)
        self.assertEqual(ml.configured_reserve_tokens(both), 40_000)

    def test_non_pydantic_fakes_fall_back_to_the_legacy_field(self) -> None:
        self.assertEqual(
            ml.configured_reserve_tokens(
                SimpleNamespace(group_history_reserve_tokens=4096)
            ),
            4096,
        )
        self.assertEqual(
            ml.configured_reserve_tokens(SimpleNamespace(context_reserve_tokens=8192)),
            8192,
        )

    def test_auto_group_history_respects_an_explicitly_smaller_budget(self) -> None:
        """auto 默认是 245760（总窗口 − 余量），但运维显式配更小必须生效。"""

        default = _settings()
        self.assertEqual(group_context.group_history_token_budget(default), 245_760)

        small = _settings(group_history_token_budget=100_000)
        self.assertEqual(group_context.group_history_token_budget(small), 100_000)

        # fixed 逃生舱保持迁移前口径（读配置值，不预扣余量）
        fixed = _settings(context_window_mode="fixed")
        self.assertEqual(group_context.group_history_token_budget(fixed), 278_528)


class ReconfigureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ml.reset_model_limits_for_tests()
        self.addCleanup(ml.reset_model_limits_for_tests)

    async def test_runtime_config_round_trips_and_reconfigure_applies(self) -> None:
        """配置保存 → 回读 → apply/reconfigure 三条路径得到同一个有效预算。"""

        memory = MemoryService(
            _bot_config(),
            _StubLLM(),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
        )
        self.assertEqual(memory.max_context, 278_528)

        settings = Settings(_env_file=None)
        payload = RuntimeConfig.model_validate(
            {
                "bot": {
                    "context_budget_tokens": 120_000,
                    "context_reserve_tokens": 24_000,
                    "group_history_max_messages": 333,
                }
            }
        )
        payload.apply_to_settings(settings, apply_prompts=False)

        # 回读（配置对象）与写穿（Settings / bot）一致
        self.assertEqual(payload.bot.context_budget_tokens, 120_000)
        self.assertEqual(settings.context_budget_tokens, 120_000)
        self.assertEqual(settings.bot.context_budget_tokens, 120_000)
        self.assertEqual(settings.bot.context_reserve_tokens, 24_000)
        self.assertEqual(settings.bot.group_history_max_messages, 333)
        self.assertEqual(context_gate.context_token_budget(settings), 120_000)

        ml.MODEL_LIMITS.record(settings.bot.main_model, total_window=1_000_000)
        memory.reconfigure(settings.bot)
        self.assertEqual(memory.max_context, 120_000)
        self.assertEqual(memory.group_history_token_budget, 96_000)
        self.assertEqual(memory.group_history_reserve_tokens, 24_000)
        self.assertEqual(memory.group_history_max_messages, 333)

        llm = LLMService(
            settings.bot.main_model,
            settings.bot.main_model,
            compress=settings.bot.main_model,
            business_context_tokens=settings.bot.context_budget_tokens,
            context_reserve_tokens=settings.bot.context_reserve_tokens,
        )
        self.assertEqual(
            llm.input_token_budget(llm._chat_candidates(llm.main)[0]), 96_000
        )

    async def test_tool_rounds_share_one_reserve_and_one_budget(self) -> None:
        """工具每跳用同一份配置预算：预留只扣一次，不按跳数累加。"""

        from bot.services.model_limits import estimate_messages_tokens

        config = _bot_config(context_budget_tokens=60_000, context_reserve_tokens=10_000)
        llm = LLMService(
            config.main_model,
            config.main_model,
            compress=config.main_model,
            business_context_tokens=60_000,
            context_reserve_tokens=10_000,
        )
        ml.MODEL_LIMITS.record(config.main_model, total_window=1_000_000)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "t",
                    "description": "x" * 100,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        messages = [
            {"role": "system", "content": "人设"},
            {"role": "user", "content": "问题"},
        ]
        tool_call = [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
        ]
        first = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=tool_call))],
            usage=None,
        )
        second = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="done", tool_calls=[]))],
            usage=None,
        )
        mock = AsyncMock(side_effect=[first, second])
        follow_up = [
            *messages,
            {"role": "assistant", "content": "", "tool_calls": tool_call},
            {"role": "tool", "tool_call_id": "c1", "content": "结果" * 5_000},
        ]

        with (
            patch("bot.services.llm.litellm.acompletion", mock),
            patch(
                "bot.services.llm.litellm.token_counter",
                side_effect=RuntimeError("unavailable"),
            ),
        ):
            await llm.complete_with_tools(messages=list(messages), tools=tools, label="skill")
            await llm.complete_with_tools(messages=follow_up, tools=tools, label="skill")

        budget = llm.input_token_budget(llm._chat_candidates(llm.main)[0])
        self.assertEqual(budget, 50_000)
        for call in mock.await_args_list:
            counted = estimate_messages_tokens(
                call.kwargs["messages"], call.kwargs["tools"]
            )
            self.assertLessEqual(counted, budget)
        # 两跳都用 50000（没有被第二轮再扣一次 1000 预留）
        self.assertEqual(
            llm.input_token_budget(llm._chat_candidates(llm.main)[0]), 50_000
        )


class LegacyConfigTests(unittest.TestCase):
    def test_old_payload_gets_recommended_defaults(self) -> None:
        migrated, _changed = _normalize_deprecated_runtime_payload(
            {"bot": {"max_context_tokens": 278_528, "drop_pending_updates": False}}
        )
        config = RuntimeConfig.model_validate(migrated)

        self.assertEqual(config.bot.context_budget_tokens, 278_528)
        self.assertEqual(config.bot.context_reserve_tokens, 32_768)
        self.assertEqual(config.bot.group_history_max_messages, 1000)

    def test_explicit_legacy_reserve_is_preserved(self) -> None:
        """旧库只配了 ``group_history_reserve_tokens``：显式配置必须保留。"""

        migrated, _changed = _normalize_deprecated_runtime_payload(
            {
                "bot": {
                    "group_history_reserve_tokens": 40_000,
                    "drop_pending_updates": False,
                }
            }
        )
        config = RuntimeConfig.model_validate(migrated)

        self.assertEqual(config.bot.context_reserve_tokens, 40_000)
        settings = Settings(_env_file=None)
        config.apply_to_settings(settings, apply_prompts=False)
        self.assertEqual(settings.bot.context_reserve_tokens, 40_000)
        self.assertEqual(
            ml.configured_reserve_tokens(settings),
            40_000,
        )

    def test_old_bot_config_without_new_fields_uses_recommended_defaults(self) -> None:
        """代码里的旧配置对象（没有新字段）也要拿到推荐默认值，而不是 0/1024。"""

        legacy = SimpleNamespace(max_context_tokens=278_528)

        self.assertEqual(ml.configured_business_tokens(legacy), 278_528)
        self.assertEqual(ml.configured_reserve_tokens(legacy), 32_768)
        self.assertEqual(ml.configured_group_history_max_messages(legacy), 1000)


if __name__ == "__main__":
    unittest.main()
