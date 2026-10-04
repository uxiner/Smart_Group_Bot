"""模型上下文窗口元数据（``bot/services/model_limits.py``）的回归测试。

这一期要修的是 2026-10-04 的生产事故：网关自报主模型窗口 1,000,000，但全项目拿
``max_context_tokens=278528`` 当全局硬上限，最终闸门把装得下的载荷判成超限，主模型与
备用**都没发 HTTP**。所以这里锁的是：

* **来源优先级**：原样 id 的网关 ``/models`` 元数据 → 直连厂商注册表 → 保守降级；
* **不冒认**：未知别名绝不去蹭别人的窗口；走网关（``api_base``）的 endpoint 不采信
  "官方同名模型"的注册表窗口；
* **只查已配置的 endpoint**：没有 ``api_base`` 或没有 key 的一律不探测；
* **缓存与路由变更**：成功 TTL 长、失败 TTL 短、``force`` 能重取；
* **不泄密**：日志与快照里绝不出现 api_key，api_base 只留 host。
"""

from __future__ import annotations

import asyncio
import json
import logging
import unittest
from types import SimpleNamespace

import httpx

from bot.config import ChatEndpointConfig, ModelConfig
from bot.services import model_limits as ml


def _endpoint(
    *,
    model: str = "home_work2api/cn:deepseek-v4.1-flash",
    api_base: str = "http://gw.internal:8080/v1",
    api_key: str | None = "sk-super-secret",
    provider: str = "home_work2api",
) -> ChatEndpointConfig:
    return ChatEndpointConfig(
        model=model,
        provider=provider,
        api_base=api_base,
        api_key=api_key,
    )


def _catalog_response(entries: list[dict], *, kind: str = "openai") -> httpx.Response:
    key = "data" if kind == "openai" else "models"
    return httpx.Response(200, json={key: entries})


class ParseModelsPayloadTests(unittest.TestCase):
    def test_openai_shape_maps_window_fields(self) -> None:
        parsed = ml.parse_models_payload(
            {
                "data": [
                    {
                        "id": "cn:deepseek-v4.1-flash",
                        "context_length": 1_000_000,
                        "max_output_tokens": 128_000,
                    },
                    {"id": "no-window-info"},
                    {"id": "explicit-input", "max_input_tokens": 200_000},
                ]
            }
        )

        self.assertEqual(parsed["cn:deepseek-v4.1-flash"]["total_window"], 1_000_000)
        self.assertEqual(
            parsed["cn:deepseek-v4.1-flash"]["max_output_tokens"], 128_000
        )
        self.assertNotIn("no-window-info", parsed)
        self.assertEqual(parsed["explicit-input"]["max_input_tokens"], 200_000)

    def test_gemini_shape_maps_token_limits(self) -> None:
        parsed = ml.parse_models_payload(
            {
                "models": [
                    {
                        "name": "models/gemini-3.8-flash-high",
                        "inputTokenLimit": 1_048_576,
                        "outputTokenLimit": 65_536,
                    }
                ]
            }
        )

        info = parsed["gemini-3.8-flash-high"]
        # inputTokenLimit 是**显式输入上限**：不能再重复减输出。
        self.assertEqual(info["max_input_tokens"], 1_048_576)
        self.assertEqual(info["max_output_tokens"], 65_536)
        self.assertIsNone(info["total_window"])

    def test_max_tokens_is_deliberately_not_treated_as_a_window(self) -> None:
        """``max_tokens`` 各家定义不一致（多为输出上限），绝不能当总窗口。"""

        parsed = ml.parse_models_payload(
            {"data": [{"id": "x", "max_tokens": 4096}]}
        )

        self.assertEqual(parsed, {})

    def test_garbage_payloads_are_ignored(self) -> None:
        for payload in ("not json", None, 42, {"data": "nope"}, {"data": [1, 2]}):
            self.assertEqual(ml.parse_models_payload(payload), {})

    def test_credential_bearing_api_base_is_redacted(self) -> None:
        self.assertEqual(
            ml.redact_base("http://user:pass@gw.internal:8080/v1"),
            "http://gw.internal:8080",
        )
        self.assertEqual(ml.redact_base(""), "")


class AuthHeaderTests(unittest.TestCase):
    def test_gemini_native_host_uses_the_native_header_only(self) -> None:
        """官方 host 上再带一个 Bearer 会让 Google 把 key 当 OAuth token 校验而 401。"""

        cfg = _endpoint(
            model="gemini/gemini-2.0-flash",
            provider="gemini",
            api_base="https://generativelanguage.googleapis.com/v1beta",
        )

        headers = ml._auth_headers(cfg)

        self.assertEqual(headers.get("x-goog-api-key"), "sk-super-secret")
        self.assertNotIn("Authorization", headers)

    def test_gemini_bridge_gets_the_bearer_header_too(self) -> None:
        cfg = _endpoint(
            model="pipio/gemini-3.8-flash-high",
            provider="gemini",
            api_base="http://pipio.internal:9000/v1",
        )

        headers = ml._auth_headers(cfg)

        self.assertEqual(headers.get("Authorization"), "Bearer sk-super-secret")
        self.assertEqual(headers.get("x-goog-api-key"), "sk-super-secret")

    def test_missing_key_means_no_request_at_all(self) -> None:
        self.assertEqual(ml._auth_headers(_endpoint(api_key=None)), {})


class MatchingTests(unittest.TestCase):
    def test_exact_id_wins_over_the_provider_stripped_id(self) -> None:
        self.assertEqual(
            ml.candidate_model_ids("home_work2api/cn:deepseek-v4.1-flash"),
            ["home_work2api/cn:deepseek-v4.1-flash", "cn:deepseek-v4.1-flash"],
        )

    def test_unknown_alias_is_never_mapped_to_another_model(self) -> None:
        """别名不冒认：``openai/some-alias`` 只能匹配同名 id。"""

        registry = ml.ModelLimitRegistry()
        registry._catalog[("openai", "http://gw/v1")] = {
            "gpt-4o": {"total_window": 128_000, "max_input_tokens": None, "max_output_tokens": None}
        }
        matched, info = registry._match_model(
            registry._catalog[("openai", "http://gw/v1")],
            "openai/some-alias",
        )

        self.assertEqual(matched, "")
        self.assertIsNone(info)

    def test_metadata_url_normalizes_request_paths(self) -> None:
        self.assertEqual(
            ml.metadata_url("http://gw/v1/chat/completions"), "http://gw/v1/models"
        )
        self.assertEqual(ml.metadata_url("http://gw/v1"), "http://gw/v1/models")
        self.assertEqual(ml.metadata_url("http://gw/v1/models"), "http://gw/v1/models")
        self.assertEqual(ml.metadata_url(""), "")


class ResolveFallbackTests(unittest.TestCase):
    def test_unknown_model_declines_to_infinity_and_uses_the_conservative_value(self) -> None:
        registry = ml.ModelLimitRegistry()
        cfg = _endpoint(model="home_work2api/nope")

        limits = registry.resolve(cfg, legacy_total_window=300_000)

        self.assertEqual(limits.source, ml.LIMIT_SOURCE_UNKNOWN)
        self.assertEqual(limits.total_window, 300_000)
        self.assertFalse(limits.known)

    def test_registry_is_used_only_when_not_routed_through_a_gateway(self) -> None:
        """走自建网关时不采信"官方同名模型"的注册表窗口（否则就是凭别名冒认）。"""

        registry = ml.ModelLimitRegistry()
        gateway_route = _endpoint(model="openai/gpt-4.1")
        direct_route = _endpoint(model="openai/gpt-4.1", api_base="", provider="openai")

        gateway_limits = registry.resolve(gateway_route, legacy_total_window=278_528)
        direct_limits = registry.resolve(direct_route, legacy_total_window=278_528)

        self.assertEqual(gateway_limits.source, ml.LIMIT_SOURCE_UNKNOWN)
        self.assertEqual(direct_limits.source, ml.LIMIT_SOURCE_REGISTRY)
        self.assertGreater(direct_limits.max_input_tokens or 0, 0)

    def test_expired_cache_is_still_better_than_unknown(self) -> None:
        registry = ml.ModelLimitRegistry()
        cfg = _endpoint()
        registry.record(cfg, total_window=1_000_000, max_output_tokens=128_000)
        registry.expire(cfg)

        limits = registry.resolve(cfg, legacy_total_window=278_528)

        self.assertEqual(limits.source, ml.LIMIT_SOURCE_GATEWAY)
        self.assertEqual(limits.total_window, 1_000_000)
        self.assertEqual(limits.detail, "expired_cache")

    def test_explicit_input_limit_is_not_reduced_by_the_output_reserve(self) -> None:
        limits = ml.ModelLimits(
            model="m", source=ml.LIMIT_SOURCE_GATEWAY, max_input_tokens=200_000
        )
        self.assertEqual(limits.input_budget_tokens(output_reserve=50_000), 200_000)

    def test_total_window_reserves_the_output_exactly_once(self) -> None:
        limits = ml.ModelLimits(
            model="m", source=ml.LIMIT_SOURCE_GATEWAY, total_window=1_000_000
        )
        self.assertEqual(limits.input_budget_tokens(output_reserve=2_048), 997_952)

    def test_degenerate_fixed_window_is_not_raised_by_a_floor(self) -> None:
        """``fixed`` 是逃生舱：配置 100 就是 100，不能被 1024 的下限抬高。"""

        registry = ml.ModelLimitRegistry()
        limits = registry.legacy_limits(_endpoint(), total_window=100)

        self.assertEqual(limits.total_window, 100)
        self.assertLess(limits.input_budget_tokens(output_reserve=2_048), 100)


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    def _registry(self, handler) -> ml.ModelLimitRegistry:
        registry = ml.ModelLimitRegistry(timeout_seconds=1.0)
        registry._transport = httpx.MockTransport(handler)
        return registry

    async def test_refresh_records_gateway_metadata(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            self.assertEqual(
                request.headers.get("authorization"), "Bearer sk-super-secret"
            )
            return _catalog_response(
                [
                    {
                        "id": "cn:deepseek-v4.1-flash",
                        "context_length": 1_000_000,
                        "max_output_tokens": 128_000,
                    },
                    {"id": "other-model", "context_length": 32_000},
                ]
            )

        registry = self._registry(handler)
        cfg = _endpoint()

        report = await registry.refresh([cfg])

        self.assertEqual(report["home_work2api|http://gw.internal:8080|home_work2api/cn:deepseek-v4.1-flash"], ml.LIMIT_SOURCE_GATEWAY)
        limits = registry.resolve(cfg)
        self.assertTrue(limits.known)
        self.assertEqual(limits.total_window, 1_000_000)
        self.assertEqual(limits.matched_id, "cn:deepseek-v4.1-flash")
        self.assertEqual(calls, ["http://gw.internal:8080/v1/models"])

    async def test_success_is_cached_and_force_refetches_after_a_route_change(self) -> None:
        hits = 0
        windows = [1_000_000, 200_000]

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal hits
            hits += 1
            return _catalog_response(
                [
                    {
                        "id": "cn:deepseek-v4.1-flash",
                        "context_length": windows[min(hits, len(windows)) - 1],
                    }
                ]
            )

        registry = self._registry(handler)
        cfg = _endpoint()

        await registry.refresh([cfg])
        self.assertEqual(hits, 1)
        # 成功 TTL 内不重复查询：回复/审核主链路绝不每条消息打网关。
        await registry.refresh([cfg])
        self.assertEqual(hits, 1)

        # 路由/模型变更 → force 重取，窗口跟着变。
        await registry.refresh([cfg], force=True)
        self.assertEqual(hits, 2)
        self.assertEqual(registry.resolve(cfg).total_window, 200_000)

    async def test_failure_is_short_cached_and_never_overwrites_with_a_guess(self) -> None:
        hits = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal hits
            hits += 1
            return httpx.Response(503, json={"error": "no_healthy_account"})

        registry = self._registry(handler)
        cfg = _endpoint()

        report = await registry.refresh([cfg])

        self.assertEqual(report["home_work2api|http://gw.internal:8080|home_work2api/cn:deepseek-v4.1-flash"], "gateway_unavailable")
        limits = registry.resolve(cfg, legacy_total_window=278_528)
        self.assertEqual(limits.source, ml.LIMIT_SOURCE_UNKNOWN)
        self.assertEqual(limits.total_window, 278_528)
        # 失败短 TTL 内不重试（避免每条消息一次网络查询），但一定会再试。
        await registry.refresh([cfg])
        self.assertEqual(hits, 1)

    async def test_model_id_missing_from_the_gateway_degrades_conservatively(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _catalog_response([{"id": "another-model", "context_length": 1_000_000}])

        registry = self._registry(handler)
        cfg = _endpoint(model="home_work2api/not-announced")

        report = await registry.refresh([cfg])

        key = "home_work2api|http://gw.internal:8080|home_work2api/not-announced"
        self.assertEqual(report[key], ml.LIMIT_SOURCE_UNKNOWN)
        limits = registry.resolve(cfg, legacy_total_window=278_528)
        self.assertEqual(limits.source, ml.LIMIT_SOURCE_UNKNOWN)
        self.assertEqual(limits.total_window, 278_528)

    async def test_unconfigured_endpoints_are_never_probed(self) -> None:
        probed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            probed.append(str(request.url))
            return _catalog_response([])

        registry = self._registry(handler)
        no_base = _endpoint(api_base="")
        no_key = _endpoint(api_key=None)

        report = await registry.refresh([no_base, no_key])

        self.assertEqual(probed, [])
        self.assertTrue(
            all(value == "skipped_no_credentials_or_api_base" for value in report.values())
        )
        self.assertEqual(len(report), 2)

    async def test_network_exception_degrades_instead_of_raising(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        registry = self._registry(handler)
        cfg = _endpoint()

        report = await registry.refresh([cfg])

        self.assertEqual(
            report["home_work2api|http://gw.internal:8080|home_work2api/cn:deepseek-v4.1-flash"],
            "gateway_unavailable",
        )

    async def test_one_gateway_is_queried_once_for_all_of_its_endpoints(self) -> None:
        hits = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal hits
            hits += 1
            return _catalog_response(
                [
                    {"id": "cn:deepseek-v4.1-flash", "context_length": 1_000_000},
                    {"id": "cn:deepseek-v4.1-mini", "context_length": 200_000},
                ]
            )

        registry = self._registry(handler)
        main = _endpoint()
        fallback = _endpoint(model="home_work2api/cn:deepseek-v4.1-mini")

        await registry.refresh([main, fallback])

        self.assertEqual(hits, 1)
        self.assertEqual(registry.resolve(main).total_window, 1_000_000)
        self.assertEqual(registry.resolve(fallback).total_window, 200_000)

    async def test_api_key_never_reaches_the_logs(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return _catalog_response(
                [{"id": "cn:deepseek-v4.1-flash", "context_length": 1_000_000}]
            )

        registry = self._registry(handler)
        cfg = _endpoint()
        with self.assertLogs("bot.services.model_limits", level=logging.DEBUG) as captured:
            await registry.refresh([cfg])
            snapshot = registry.snapshot()

        joined = "\n".join(captured.output)
        self.assertNotIn("sk-super-secret", joined)
        self.assertNotIn("sk-super-secret", json.dumps(snapshot))
        self.assertEqual(registry.resolve(cfg).total_window, 1_000_000)


class ConfigReaderTests(unittest.TestCase):
    def test_missing_mode_defaults_to_auto(self) -> None:
        """老配置里没有这个字段必须等价于 auto（本次修复的语义迁移）。"""

        self.assertEqual(
            ml.context_window_mode(SimpleNamespace(bot=SimpleNamespace())),
            ml.CONTEXT_MODE_AUTO,
        )
        self.assertTrue(ml.auto_mode_enabled(SimpleNamespace()))

    def test_fixed_mode_is_honored(self) -> None:
        settings = SimpleNamespace(
            bot=SimpleNamespace(context_window_mode="fixed", max_context_tokens=4096)
        )

        self.assertEqual(ml.effective_context_window(settings), 4096)
        self.assertFalse(ml.auto_mode_enabled(settings))

    def test_auto_mode_uses_the_measured_window_and_falls_back_to_the_legacy_value(self) -> None:
        ml.reset_model_limits_for_tests()
        try:
            model = ModelConfig(
                model="home_work2api/cn:deepseek-v4.1-flash",
                provider="home_work2api",
                api_key="k",
                api_base="http://gw.internal:8080/v1",
            )
            settings = SimpleNamespace(
                bot=SimpleNamespace(
                    main_model=model,
                    max_context_tokens=278_528,
                    context_window_mode="auto",
                )
            )
            self.assertEqual(ml.effective_context_window(settings), 278_528)
            self.assertIsNone(ml.auto_window_for(settings))

            ml.MODEL_LIMITS.record(model, total_window=1_000_000)
            self.assertEqual(ml.effective_context_window(settings), 1_000_000)
            self.assertEqual(ml.auto_window_for(settings), 1_000_000)
        finally:
            ml.reset_model_limits_for_tests()

    def test_configured_endpoints_deduplicates_and_skips_empty_roles(self) -> None:
        model = ModelConfig(
            model="home_work2api/a",
            provider="home_work2api",
            api_base="http://gw.internal:8080/v1",
            api_key="k",
            fallbacks=[
                ChatEndpointConfig(
                    model="home_work2api/b",
                    provider="home_work2api",
                    api_base="http://gw.internal:8080/v1",
                    api_key="k",
                )
            ],
        )
        settings = SimpleNamespace(
            bot=SimpleNamespace(
                main_model=model,
                skill_model=None,
                decision_model=ModelConfig(model=""),
                moderation_model=ModelConfig(model=""),
                vision_model=ModelConfig(model=""),
                compress_model=ModelConfig(model=""),
            )
        )

        models = [cfg.model for cfg in ml.configured_endpoints(settings)]

        self.assertEqual(models, ["home_work2api/a", "home_work2api/b"])

    def test_conservative_estimate_matches_the_shared_cjk_metric(self) -> None:
        """最终闸门与装配链路必须用同一个口径（事故根因就是两边不一致）。"""

        messages = [{"role": "user", "content": "中文" * 100}]
        estimate = ml.estimate_messages_tokens(messages)

        self.assertEqual(
            estimate,
            ml.MESSAGE_TOKEN_OVERHEAD
            + ml.estimate_text_tokens("中文" * 100),
        )


class RefreshConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_does_not_block_the_event_loop(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def handler(_request: httpx.Request) -> httpx.Response:
            started.set()
            await release.wait()
            return _catalog_response([])

        registry = ml.ModelLimitRegistry(timeout_seconds=5.0)
        registry._transport = httpx.MockTransport(handler)
        task = asyncio.create_task(registry.refresh([_endpoint()]))
        await asyncio.wait_for(started.wait(), timeout=1.0)

        ticks = 0
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1
        self.assertEqual(ticks, 5)

        release.set()
        await asyncio.wait_for(task, timeout=2.0)


if __name__ == "__main__":
    unittest.main()
