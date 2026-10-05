"""litellm 升级守卫：真实 SDK API 面 + 请求构建契约。

`tests/test_llm_*.py` 绝大多数用 mock 打桩 litellm 调用，它们能验证**本项目**的重试 /
超时 / 解析逻辑，但**看不到 litellm 自身 API 面的变化**——升级后如果某个符号被改名、
删除或改了签名，这类用例仍会全绿。

这里直接对着 `import litellm` 拿到的真实对象做断言，不发任何网络请求：

1. 仓库引用的每个 litellm 符号都存在（含 `getattr(..., None)` 兜底的那几个，
   它们一旦消失，运行时才会炸在生产请求上）。
2. 三个调用面 ``acompletion`` / ``aresponses`` / ``aembedding`` 仍然接受本项目
   ``_build_*_kwargs`` 实际产出的每一个键——签名不兼容会在升级当天就红。
3. ``_PROTECTED_REQUEST_PARAMS`` 仍然拦得住 ``api_base`` / ``base_url`` / ``model``
   等路由与凭据字段。这条与 PYSEC-2026-4066 同一类风险：即使 litellm 自己的 proxy
   修好了，本项目也不能让「provider 私有 JSON 参数」重新获得改写路由的能力。
"""

import inspect
import unittest
from collections.abc import Mapping

import litellm

from bot.config import ChatEndpointConfig, EmbedEndpointConfig
from bot.services.llm import LLMService


# 仓库在 bot/ 下引用的全部 litellm 符号。
_REQUIRED_SYMBOLS = (
    "acompletion",
    "aembedding",
    "token_counter",
    "get_model_info",
    "provider_list",
    "responses",
    "aresponses",
    "suppress_debug_info",
    "set_verbose",
)


def _accepted_kwargs(fn) -> set[str]:
    """返回 fn 能接受的参数名集合；签名不可解析时返回全集（跳过断言）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"*"}
    names = set()
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return {"*"}
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        names.add(param.name)
    return names


def _assert_accepts(test: unittest.TestCase, fn, kwargs: dict, label: str) -> None:
    accepted = _accepted_kwargs(fn)
    if "*" in accepted:
        return
    unsupported = sorted(k for k in kwargs if k not in accepted)
    test.assertEqual(
        unsupported,
        [],
        f"litellm {label} 不再接受本项目构造的参数: {unsupported}",
    )


class LitellmSymbolSurfaceTests(unittest.TestCase):
    def test_required_symbols_exist(self) -> None:
        missing = [name for name in _REQUIRED_SYMBOLS if not hasattr(litellm, name)]
        self.assertEqual(missing, [], f"litellm 缺少仓库依赖的符号: {missing}")

    def test_close_helper_is_callable_or_absent(self) -> None:
        """llm.py:402 用 getattr(..., None) 兜底，但存在时必须可调用。"""
        close = getattr(litellm, "close_litellm_async_clients", None)
        if close is not None:
            self.assertTrue(callable(close))

    def test_provider_list_is_iterable(self) -> None:
        known = {str(getattr(item, "value", item)) for item in litellm.provider_list}
        self.assertIn("openai", known)
        self.assertIn("anthropic", known)

    def test_get_model_info_still_returns_a_mapping(self) -> None:
        """仓库两处都按 Mapping 消费：llm.py model_input_token_limit、model_limits.py。"""
        info = litellm.get_model_info(model="gpt-4.1")
        self.assertIsInstance(info, Mapping)
        self.assertIn("max_input_tokens", info)
        self.assertIsInstance(info["max_input_tokens"], int)


class ChatCallSurfaceTests(unittest.TestCase):
    def _cfg(self) -> ChatEndpointConfig:
        return ChatEndpointConfig(
            model="openai/gpt-4.1",
            provider="openai",
            api_key="sk-not-a-real-key",
            api_base="https://gateway.example.com/v1",
            temperature=0.3,
            max_tokens=256,
            timeout_sec=7.5,
        )

    def test_chat_kwargs_accepted_by_acompletion(self) -> None:
        kwargs = LLMService._build_chat_kwargs(self._cfg())
        _assert_accepts(self, litellm.acompletion, kwargs, "acompletion()")

    def test_chat_kwargs_carry_exactly_the_routing_fields_we_intend(self) -> None:
        kwargs = LLMService._build_chat_kwargs(self._cfg())
        self.assertEqual(kwargs["model"], "openai/gpt-4.1")
        self.assertEqual(kwargs["api_key"], "sk-not-a-real-key")
        self.assertEqual(kwargs["api_base"], "https://gateway.example.com/v1")
        self.assertEqual(kwargs["timeout"], 7.5)

    def test_responses_kwargs_accepted_by_aresponses(self) -> None:
        cfg = self._cfg().model_copy(update={"chat_endpoint": "responses"})
        kwargs = LLMService._build_responses_kwargs(cfg)
        # Responses 面用 max_output_tokens 而不是 max_tokens。
        self.assertIn("max_output_tokens", kwargs)
        self.assertNotIn("max_tokens", kwargs)
        _assert_accepts(self, litellm.aresponses, kwargs, "aresponses()")
        _assert_accepts(self, litellm.responses, kwargs, "responses()")

    def test_exclude_params_still_removed_from_built_kwargs(self) -> None:
        cfg = self._cfg()
        kwargs = LLMService._build_chat_kwargs(
            cfg, exclude_params={"temperature", "api_base"}
        )
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("api_base", kwargs)
        self.assertIn("model", kwargs)

    def test_extra_body_survives_as_custom_field(self) -> None:
        cfg = self._cfg().model_copy(
            update={"request_params": {"reasoning_effort": "low", "top_k": 5}}
        )
        kwargs = LLMService._build_chat_kwargs(cfg)
        _assert_accepts(self, litellm.acompletion, kwargs, "acompletion() with extra_body")
        self.assertEqual(
            kwargs.get("extra_body"),
            {"reasoning_effort": "low", "top_k": 5},
        )


class EmbeddingCallSurfaceTests(unittest.TestCase):
    def test_embed_kwargs_accepted_by_aembedding(self) -> None:
        cfg = EmbedEndpointConfig(
            model="openai/text-embedding-3-small",
            provider="openai",
            api_key="sk-not-a-real-key",
            api_base="https://gateway.example.com/v1",
        )
        kwargs = LLMService._build_embed_kwargs(cfg, ["hello"])
        self.assertEqual(kwargs["input"], ["hello"])
        _assert_accepts(self, litellm.aembedding, kwargs, "aembedding()")


class RoutingParameterGuardTests(unittest.TestCase):
    """GHSA-3cv6-jpf6-8222 同类防护：私有 JSON 参数不得改写路由/凭据。"""

    #: 公告点名的路由/凭据字段中，本项目**已经**挡住的。升级后必须继续挡住。
    _PROTECTED = (
        "api_base",
        "base_url",
        "api_key",
        "model",
        "custom_llm_provider",
        "provider",
    )

    #: 公告点名、但本项目 allowlist **尚未**覆盖的字段。
    #:
    #: TODO(P3-litrouting): 见 FIX-p2-litellm.md ② 节的「已知缺口」。
    #: `litellm_params` 能内嵌任意路由/凭据，`model_list` / `fallbacks` 是公告原文
    #: 点名的路由字段。补进 `_PROTECTED_REQUEST_PARAMS` 属于业务逻辑改动，
    #: 不在 P2（纯依赖升级）范围内，故此处只把现状钉住并显式记录。
    _KNOWN_GAPS = ("litellm_params", "model_list", "fallbacks")

    def test_protected_params_are_still_denied(self) -> None:
        for name in self._PROTECTED:
            with self.subTest(param=name):
                self.assertIn(
                    name,
                    LLMService._PROTECTED_REQUEST_PARAMS,
                    f"{name} 不在 _PROTECTED_REQUEST_PARAMS 里，私有参数可改写路由",
                )

    def test_known_gap_remains_gap(self) -> None:
        """缺口一旦被补上，这条会红——提醒把上面的 TODO 改成 _PROTECTED。"""
        for name in self._KNOWN_GAPS:
            with self.subTest(param=name):
                self.assertNotIn(
                    name,
                    LLMService._PROTECTED_REQUEST_PARAMS,
                    f"{name} 已被加入 allowlist，请把 TODO(P3-litrouting) 移入 _PROTECTED",
                )

    def test_routing_params_cannot_be_injected_via_request_params(self) -> None:
        cfg = ChatEndpointConfig(
            model="openai/gpt-4.1",
            provider="openai",
            api_key="sk-not-a-real-key",
            api_base="https://gateway.example.com/v1",
            request_params={
                "api_base": "https://attacker.example.net/v1",
                "base_url": "https://attacker.example.net/v1",
                "model": "openai/evil",
                "api_key": "attacker-key",
                "thinking": {"type": "enabled"},
            },
        )
        kwargs = LLMService._build_chat_kwargs(cfg)
        self.assertEqual(kwargs["api_base"], "https://gateway.example.com/v1")
        self.assertEqual(kwargs["api_key"], "sk-not-a-real-key")
        self.assertEqual(kwargs["model"], "openai/gpt-4.1")
        # 合法自定义字段仍然放行。
        self.assertEqual(kwargs.get("extra_body"), {"thinking": {"type": "enabled"}})

    def test_embedding_routing_params_cannot_be_injected_either(self) -> None:
        cfg = EmbedEndpointConfig(
            model="openai/text-embedding-3-small",
            provider="openai",
            api_key="sk-not-a-real-key",
            api_base="https://gateway.example.com/v1",
            request_params={"api_base": "https://attacker.example.net/v1"},
        )
        kwargs = LLMService._build_embed_kwargs(cfg, ["hi"])
        self.assertEqual(kwargs["api_base"], "https://gateway.example.com/v1")


class TokenCounterSurfaceTests(unittest.TestCase):
    def test_token_counter_accepts_messages_and_model(self) -> None:
        count = litellm.token_counter(
            model="gpt-4.1",
            messages=[{"role": "user", "content": "hello"}],
        )
        self.assertGreater(count, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
