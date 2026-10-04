"""按实际模型上限自动匹配上下文（2026-10-04 生产事故的端到端回归）。

事故现场（group history=2003，2026-10-04 11:42–11:46）：网关宣告主模型窗口
``1,000,000``，但全项目用 ``max_context_tokens = 278528``（272K）当**全局硬上限**，
最终闸门把一个 78 万字符的载荷按"一字符一 token"估成 783800 并标成 ``exact``，
于是主模型与备用**都没发 HTTP** 就被 ``skipping_model``，最后由硬编码话术顶上。

这个文件锁住修复后的形状：

* 已知模型（实测 1M）不再被 272K 压住：装配预算与最终闸门都按真实窗口；
* 主模型 1M、备用更小窗口：备用**重新裁剪**适配自己，而不是整条跳过；
* 长载荷（≥100K 字符，中英 JSON 混合）照发；超窗口的先裁再发，本轮与核心块必留；
* 固定层自己就装不下 → 诚实失败（不发请求），不假装成功；
* 未知模型保守降级并明确日志，但**不会**让已知主模型跟着变小；
* 分词器一直卡住也不阻塞事件循环，且如实标 ``exact=False``。
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import BotConfig, ChatEndpointConfig, ModelConfig
from bot.services import model_limits as ml
from bot.services import context_gate, group_context, private_chat
from bot.services.llm import LLMService
from bot.services.memory import MemoryService
from bot.services.payload_fit import CTX_LAYER_KEY, LAYER_HISTORY

GATEWAY = "http://gw.internal:8080/v1"


def _gateway_model(*, fallbacks: list[ChatEndpointConfig] | None = None) -> ModelConfig:
    return ModelConfig(
        model="home_work2api/cn:deepseek-v4.1-flash",
        provider="home_work2api",
        api_key="sk-super-secret",
        api_base=GATEWAY,
        max_tokens=2048,
        retry_attempts=1,
        retry_backoff_sec=0.0,
        retry_timeout_multiplier=1.0,
        fallbacks=list(fallbacks or []),
    )


def _small_fallback() -> ChatEndpointConfig:
    return ChatEndpointConfig(
        model="pipio/gemini-3.8-flash-high",
        provider="pipio",
        api_key="sk-super-secret",
        api_base="http://pipio.internal:9000/v1",
        max_tokens=2048,
        retry_attempts=1,
        retry_backoff_sec=0.0,
        retry_timeout_multiplier=1.0,
    )


def _bot_config(**overrides) -> BotConfig:
    payload = {
        "main_model": _gateway_model(),
        "max_context_tokens": 278_528,
        "group_history_token_budget": 278_528,
        "group_history_reserve_tokens": 32_768,
        "private_chat_history_token_budget": 278_528,
        "max_output_tokens": 2048,
    }
    payload.update(overrides)
    return BotConfig(**payload)


def _settings(**overrides) -> SimpleNamespace:
    return SimpleNamespace(bot=_bot_config(**overrides))


def _chat_resp(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=[]))],
        usage=None,
    )


class _StubLLM:
    """最小替身：让 MemoryService 走"按配置/注册表解析窗口"的那条路。"""

    class main:
        model = "home_work2api/cn:deepseek-v4.1-flash"


def _long_history_messages(
    *,
    rows: int,
    body: str,
    current: str = "现在这条问题",
) -> list[dict]:
    return [
        {"role": "system", "content": "核心人设与围栏"},
        *[
            {"role": "user", "content": body, CTX_LAYER_KEY: LAYER_HISTORY}
            for _ in range(rows)
        ],
        {"role": "user", "content": current},
    ]


class ContextWindowModeMigrationTests(unittest.TestCase):
    """既有部署的语义迁移：老库里没有 ``context_window_mode`` → 真正变成 auto。"""

    def _settings(self):
        from bot.config import Settings

        return Settings(_env_file=None)

    def test_legacy_payload_migrates_to_auto_and_keeps_the_old_field(self) -> None:
        from bot.services.runtime_config import (
            RuntimeConfig,
            _normalize_deprecated_runtime_payload,
        )

        migrated, changed = _normalize_deprecated_runtime_payload(
            {"bot": {"max_context_tokens": 278_528, "drop_pending_updates": False}}
        )

        self.assertTrue(changed)
        self.assertEqual(migrated["bot"]["context_window_mode"], "auto")
        config = RuntimeConfig.model_validate(migrated)
        # 旧字段兼容保存（只是不再当全局硬上限）。
        self.assertEqual(config.bot.max_context_tokens, 278_528)
        self.assertEqual(config.bot.context_window_mode, "auto")

    def test_an_explicit_fixed_choice_is_never_overwritten(self) -> None:
        from bot.services.runtime_config import _normalize_deprecated_runtime_payload

        migrated, _changed = _normalize_deprecated_runtime_payload(
            {"bot": {"context_window_mode": "fixed", "drop_pending_updates": False}}
        )

        self.assertEqual(migrated["bot"]["context_window_mode"], "fixed")

    def test_migration_is_idempotent(self) -> None:
        from bot.services.runtime_config import _normalize_deprecated_runtime_payload

        once, first = _normalize_deprecated_runtime_payload(
            {"bot": {"max_context_tokens": 278_528, "drop_pending_updates": False}}
        )
        again, second = _normalize_deprecated_runtime_payload(once)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(again["bot"]["context_window_mode"], "auto")

    def test_apply_to_settings_writes_the_mode_to_both_shapes(self) -> None:
        from bot.services.runtime_config import RuntimeConfig

        settings = self._settings()
        config = RuntimeConfig.model_validate(
            {"bot": {"context_window_mode": "fixed", "max_context_tokens": 300_000}}
        )

        config.apply_to_settings(settings, apply_prompts=False)

        self.assertEqual(settings.bot.context_window_mode, "fixed")
        self.assertEqual(settings.context_window_mode, "fixed")
        self.assertEqual(settings.bot.max_context_tokens, 300_000)

    def test_legacy_toml_carries_the_mode_into_the_import_document(self) -> None:
        import tempfile
        from pathlib import Path

        from bot.services.runtime_config import build_legacy_runtime_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                '[bot]\ncontext_window_mode = "fixed"\n', encoding="utf-8"
            )
            imported = build_legacy_runtime_config(
                str(path),
                settings=self._settings(),
                raw_env={},
            )

        self.assertEqual(imported.bot.context_window_mode, "fixed")


class AutoWindowBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        ml.reset_model_limits_for_tests()
        self.addCleanup(ml.reset_model_limits_for_tests)

    def test_known_one_million_window_is_no_longer_capped_at_272k(self) -> None:
        settings = _settings()
        # 未知时的口径（迁移前语义）：装配函数读的就是配置值，MemoryService 再按
        # "窗口 − 固定余量" 夹一次（见 test_memory_service_follows_the_measured_window）。
        self.assertEqual(context_gate.context_token_budget(settings), 278_528)
        self.assertEqual(group_context.group_history_token_budget(settings), 278_528)
        self.assertEqual(
            private_chat.private_history_token_budget(settings), 278_528
        )

        ml.MODEL_LIMITS.record(
            settings.bot.main_model, total_window=1_000_000, max_output_tokens=128_000
        )

        self.assertEqual(context_gate.context_token_budget(settings), 1_000_000)
        self.assertEqual(
            group_context.group_history_token_budget(settings),
            1_000_000 - 32_768,
        )
        self.assertEqual(
            private_chat.private_history_token_budget(settings),
            1_000_000 - 32_768,
        )

    def test_unknown_model_keeps_the_legacy_depth_and_does_not_infect_the_primary(
        self,
    ) -> None:
        main = _gateway_model(fallbacks=[_small_fallback()])
        settings = SimpleNamespace(bot=_bot_config(main_model=main))
        ml.MODEL_LIMITS.record(main, total_window=1_000_000)

        # 备用没有任何元数据 → 它自己保守降级；主模型仍然是 1M。
        fallback = ml.MODEL_LIMITS.resolve(main.fallbacks[0], legacy_total_window=278_528)
        self.assertEqual(fallback.source, ml.LIMIT_SOURCE_UNKNOWN)
        self.assertEqual(fallback.total_window, 278_528)
        self.assertEqual(context_gate.context_token_budget(settings), 1_000_000)

    def test_memory_service_follows_the_measured_window(self) -> None:
        memory = MemoryService(
            _bot_config(),
            _StubLLM(),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
        )
        self.assertEqual(memory.max_context, 278_528)
        self.assertEqual(memory.group_history_token_budget, 245_760)

        ml.MODEL_LIMITS.record(_gateway_model(), total_window=1_000_000)
        memory.reconfigure(_bot_config())

        self.assertEqual(memory.max_context, 1_000_000)
        self.assertEqual(memory.group_history_token_budget, 1_000_000 - 32_768)
        self.assertLessEqual(
            memory.group_history_token_budget + memory.group_history_reserve_tokens,
            memory.max_context,
        )

    def test_fixed_mode_escape_hatch_still_reads_the_configured_value(self) -> None:
        settings = _settings(context_window_mode="fixed")
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=1_000_000)

        self.assertEqual(context_gate.context_token_budget(settings), 278_528)
        self.assertEqual(group_context.group_history_token_budget(settings), 278_528)


class LlmEndpointBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        ml.reset_model_limits_for_tests()
        self.addCleanup(ml.reset_model_limits_for_tests)

    def _llm(self, *, fallbacks: list[ChatEndpointConfig] | None = None) -> LLMService:
        main = _gateway_model(fallbacks=fallbacks)
        return LLMService(main, main, compress=main)

    def test_input_budget_distinguishes_total_window_from_explicit_input_limit(self) -> None:
        llm = self._llm()
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=1_000_000)

        self.assertEqual(llm.input_token_budget(llm._chat_candidates(llm.main)[0]), 997_952)

        # 有显式输入上限时不再重复减输出预留。
        ml.reset_model_limits_for_tests()
        llm = self._llm()
        ml.MODEL_LIMITS.record(_gateway_model(), max_input_tokens=900_000)
        self.assertEqual(llm.input_token_budget(llm._chat_candidates(llm.main)[0]), 900_000)

    async def test_incident_shaped_payload_is_sent_to_the_primary_model(self) -> None:
        """事故复现：约 78 万字符、装配装得下 → 必须真的发出 HTTP。"""

        llm = self._llm()
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=1_000_000, max_output_tokens=128_000)
        body = "[history] user said " + "x" * 60
        messages = _long_history_messages(rows=10_000, body=body)
        self.assertGreater(sum(len(item["content"]) for item in messages), 700_000)

        mock = AsyncMock(return_value=_chat_resp("answer"))
        with (
            patch("bot.services.llm.litellm.acompletion", mock),
            patch(
                "bot.services.llm.litellm.token_counter",
                side_effect=RuntimeError("tokenizer unavailable"),
            ),
        ):
            out = await llm._chat_with_fallbacks(
                messages=messages,
                candidates=llm._chat_candidates(llm.main),
                label="skill",
                preview_limit=40,
            )

        self.assertEqual(out, "answer")
        mock.assert_awaited_once()
        sent = mock.await_args.kwargs["messages"]
        self.assertEqual(len(sent), len(messages))
        self.assertTrue(any("现在这条问题" in item["content"] for item in sent))

    async def test_long_mixed_language_payload_over_100k_chars_is_sent(self) -> None:
        llm = self._llm()
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=1_000_000)
        mixed = (
            "群聊历史：这条是中文正文，混着 English prose and JSON "
            '{"key": "value", "list": [1, 2, 3]} '
        ) * 1_500
        messages = _long_history_messages(rows=1, body=mixed)
        self.assertGreater(sum(len(item["content"]) for item in messages), 100_000)

        mock = AsyncMock(return_value=_chat_resp("mixed-ok"))
        with (
            patch("bot.services.llm.litellm.acompletion", mock),
            patch(
                "bot.services.llm.litellm.token_counter",
                side_effect=RuntimeError("tokenizer unavailable"),
            ),
        ):
            out = await llm._chat_with_fallbacks(
                messages=messages,
                candidates=llm._chat_candidates(llm.main),
                label="skill",
                preview_limit=40,
            )

        self.assertEqual(out, "mixed-ok")
        mock.assert_awaited_once()

    async def test_smaller_fallback_window_is_refitted_not_skipped(self) -> None:
        """主模型 1M、备用保守 272K：备用必须重新裁剪适配自己。"""

        llm = self._llm(fallbacks=[_small_fallback()])
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=1_000_000)
        # 约 30 万保守 token：装得进主模型（997,952），装不进备用（276,480）。
        messages = _long_history_messages(rows=1_000, body="x" * 900)

        mock = AsyncMock(side_effect=[RuntimeError("primary down"), _chat_resp("fallback-ok")])
        with (
            patch("bot.services.llm.litellm.acompletion", mock),
            patch(
                "bot.services.llm.litellm.token_counter",
                side_effect=RuntimeError("tokenizer unavailable"),
            ),
        ):
            out = await llm._chat_with_fallbacks(
                messages=messages,
                candidates=llm._chat_candidates(llm.main),
                label="skill",
                preview_limit=40,
            )

        self.assertEqual(out, "fallback-ok")
        self.assertEqual(mock.await_count, 2)
        self.assertEqual(
            [call.kwargs["model"] for call in mock.await_args_list],
            [
                "home_work2api/cn:deepseek-v4.1-flash",
                "pipio/gemini-3.8-flash-high",
            ],
        )
        primary_messages = mock.await_args_list[0].kwargs["messages"]
        fallback_messages = mock.await_args_list[1].kwargs["messages"]
        self.assertEqual(len(primary_messages), len(messages))
        self.assertLess(len(fallback_messages), len(primary_messages))
        self.assertTrue(
            any("现在这条问题" in item["content"] for item in fallback_messages)
        )

    async def test_over_window_payload_is_trimmed_then_sent_with_the_current_turn(self) -> None:
        llm = self._llm()
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=1_000_000)
        # 远超窗口：3.6M 字符 ⇒ 保守上界约 1.2M token。
        messages = _long_history_messages(rows=12_000, body="x" * 300)

        mock = AsyncMock(return_value=_chat_resp("trimmed-ok"))
        with (
            patch("bot.services.llm.litellm.acompletion", mock),
            patch(
                "bot.services.llm.litellm.token_counter",
                side_effect=RuntimeError("tokenizer unavailable"),
            ),
        ):
            out = await llm._chat_with_fallbacks(
                messages=messages,
                candidates=llm._chat_candidates(llm.main),
                label="skill",
                preview_limit=40,
            )

        self.assertEqual(out, "trimmed-ok")
        mock.assert_awaited_once()
        sent = mock.await_args.kwargs["messages"]
        self.assertLess(len(sent), len(messages))
        self.assertTrue(any("现在这条问题" in item["content"] for item in sent))
        self.assertEqual(sent[0]["content"], "核心人设与围栏")

    async def test_core_layers_too_big_fail_honestly_without_http(self) -> None:
        llm = self._llm()
        # 网关只宣告 5000 的窗口：核心块自己就装不下。
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=5_000)
        messages = [
            {"role": "system", "content": "核心人设" * 3_000},
            {"role": "user", "content": "现在这条"},
        ]

        mock = AsyncMock(return_value=_chat_resp("should-not-run"))
        with (
            patch("bot.services.llm.litellm.acompletion", mock),
            patch(
                "bot.services.llm.litellm.token_counter",
                side_effect=RuntimeError("tokenizer unavailable"),
            ),
        ):
            out = await llm._chat_with_fallbacks(
                messages=messages,
                candidates=llm._chat_candidates(llm.main),
                label="skill",
                preview_limit=40,
            )

        self.assertEqual(out, "")
        mock.assert_not_awaited()

    async def test_long_tool_result_is_truncated_and_the_pair_is_kept(self) -> None:
        llm = self._llm()
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=30_000)
        messages = [
            {"role": "system", "content": "核心人设"},
            {"role": "user", "content": "查一下这个"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "websearch", "arguments": '{"query":"x"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "websearch",
                "content": "工具结果正文" * 20_000,
            },
        ]

        mock = AsyncMock(return_value=_chat_resp("tool-ok"))
        with (
            patch("bot.services.llm.litellm.acompletion", mock),
            patch(
                "bot.services.llm.litellm.token_counter",
                side_effect=RuntimeError("tokenizer unavailable"),
            ),
        ):
            out = await llm._chat_with_fallbacks(
                messages=messages,
                candidates=llm._chat_candidates(llm.main),
                label="skill",
                preview_limit=40,
            )

        self.assertEqual(out, "tool-ok")
        sent = mock.await_args.kwargs["messages"]
        roles = [item["role"] for item in sent]
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)
        tool_message = next(item for item in sent if item["role"] == "tool")
        self.assertEqual(tool_message["tool_call_id"], "call-1")
        self.assertLess(len(tool_message["content"]), len("工具结果正文" * 20_000))

    async def test_internal_layer_markers_never_reach_the_provider(self) -> None:
        """层标记只用于进程内决定裁剪顺序：绝不进请求体，也绝不改写原载荷。"""

        llm = self._llm()
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=1_000_000)
        messages = _long_history_messages(rows=3, body="正文" * 50)
        original = [dict(item) for item in messages]

        mock = AsyncMock(return_value=_chat_resp("clean-ok"))
        with (
            patch("bot.services.llm.litellm.acompletion", mock),
            patch(
                "bot.services.llm.litellm.token_counter",
                side_effect=RuntimeError("tokenizer unavailable"),
            ),
        ):
            out = await llm._chat_with_fallbacks(
                messages=messages,
                candidates=llm._chat_candidates(llm.main),
                label="skill",
                preview_limit=40,
            )

        self.assertEqual(out, "clean-ok")
        for item in mock.await_args.kwargs["messages"]:
            self.assertNotIn(CTX_LAYER_KEY, item)
            self.assertFalse(any(str(key).startswith("_") for key in item))
        self.assertEqual(messages, original)

    async def test_unknown_window_degrades_with_an_explicit_log(self) -> None:
        llm = self._llm()
        candidate = llm._chat_candidates(llm.main)[0]

        with self.assertLogs("bot.services.llm", level="WARNING") as captured:
            limits = llm.endpoint_limits(candidate)

        self.assertEqual(limits.source, ml.LIMIT_SOURCE_UNKNOWN)
        self.assertEqual(limits.total_window, 278_528)
        joined = "\n".join(captured.output)
        self.assertIn("context window unknown", joined)
        self.assertNotIn("sk-super-secret", joined)

    async def test_hanging_tokenizer_never_blocks_concurrent_replies(self) -> None:
        llm = self._llm()
        ml.MODEL_LIMITS.record(_gateway_model(), total_window=1_000_000)
        messages = _long_history_messages(rows=50, body="正文" * 200)
        release = threading.Event()
        mock = AsyncMock(return_value=_chat_resp("ok"))

        def blocking_token_counter(**_kwargs: object) -> int:
            release.wait(timeout=1.0)
            return 10

        failsafe = threading.Timer(1.0, release.set)
        failsafe.daemon = True
        failsafe.start()
        started = time.monotonic()
        try:
            with (
                patch("bot.services.llm.litellm.acompletion", mock),
                patch(
                    "bot.services.llm.litellm.token_counter",
                    side_effect=blocking_token_counter,
                ),
                patch("bot.services.llm._LLM_TOKENIZER_THREAD_TIMEOUT_SECONDS", 0.05),
            ):
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            llm._chat_with_fallbacks(
                                messages=list(messages),
                                candidates=llm._chat_candidates(llm.main),
                                label="skill",
                                preview_limit=20,
                            )
                            for _ in range(4)
                        )
                    ),
                    timeout=3.0,
                )
        finally:
            release.set()
            failsafe.cancel()

        self.assertEqual(results, ["ok", "ok", "ok", "ok"])
        self.assertEqual(mock.await_count, 4)
        self.assertLess(time.monotonic() - started, 3.0)


if __name__ == "__main__":
    unittest.main()
