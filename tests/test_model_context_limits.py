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

    def test_one_dirty_infinity_entry_does_not_void_the_whole_catalog(self) -> None:
        """``json.loads`` 默认接受 ``Infinity`` 字面量，旧代码只 catch ValueError。"""

        payload = (
            '{"data": [{"id": "dirty", "context_length": Infinity},'
            ' {"id": "cn:deepseek-v4.1-flash", "context_length": 1000000}]}'
        )
        self.assertEqual(
            ml.parse_models_payload(payload),
            {
                "cn:deepseek-v4.1-flash": {
                    "total_window": 1_000_000,
                    "max_input_tokens": None,
                    "max_output_tokens": None,
                }
            },
            "单条脏 entry 应当被跳过，其余条目照常解析",
        )

    def test_non_finite_windows_never_reach_the_budget(self) -> None:
        """``int(float('inf'))`` 是 OverflowError，不是 ValueError。"""

        for value in (float("inf"), float("-inf"), float("nan")):
            self.assertIsNone(ml._positive_int(value))
            self.assertLessEqual(ml._bounded_int(value, default=7, low=1, high=9), 9)

    def test_unparsable_port_is_dropped_instead_of_raising(self) -> None:
        """``parts.port`` 的属性访问会抛 ValueError，且过去在 ``try`` 之外。"""

        self.assertEqual(ml.redact_base("http://gw.internal:70000/v1"), "")
        self.assertEqual(ml.redact_base("gw.internal:not-a-port/v1"), "")


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
        self.assertEqual(report[key], "model_id_missing")
        limits = registry.resolve(cfg, legacy_total_window=278_528)
        self.assertEqual(limits.source, ml.LIMIT_SOURCE_UNKNOWN)
        self.assertEqual(limits.total_window, 278_528)

    async def test_refresh_failure_never_clobbers_a_cached_window(self) -> None:
        """网关抖动 / /models 只回了部分条目时，**已实测的 1M 绝不能被退回 272K**。"""

        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return _catalog_response(
                    [{"id": "cn:deepseek-v4.1-flash", "context_length": 1_000_000}]
                )
            if calls["n"] == 2:
                return httpx.Response(503)
            # 第三次：网关回来了，但只列了别的模型（部分列表）。
            return _catalog_response([{"id": "unrelated", "context_length": 32_000}])

        registry = self._registry(handler)
        cfg = _endpoint()

        await registry.refresh([cfg])
        self.assertEqual(registry.resolve(cfg).total_window, 1_000_000)

        degraded = await registry.refresh([cfg], force=True)
        key = "home_work2api|http://gw.internal:8080|home_work2api/cn:deepseek-v4.1-flash"
        self.assertEqual(degraded[key], "gateway_unavailable_keeping_cached")
        kept = registry.resolve(cfg, legacy_total_window=278_528)
        self.assertEqual(kept.total_window, 1_000_000)
        self.assertEqual(kept.source, ml.LIMIT_SOURCE_GATEWAY)
        self.assertEqual(kept.detail, "expired_cache")

        missing = await registry.refresh([cfg], force=True)
        self.assertEqual(missing[key], "model_id_missing_keeping_cached")
        still = registry.resolve(cfg, legacy_total_window=278_528)
        self.assertEqual(still.total_window, 1_000_000)

    async def test_expired_success_is_refetched(self) -> None:
        """成功 TTL 到期后必须真的重取（周期刷新才有意义）。"""

        hits = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal hits
            hits += 1
            return _catalog_response(
                [{"id": "cn:deepseek-v4.1-flash", "context_length": 1_000_000 * hits}]
            )

        registry = ml.ModelLimitRegistry(success_ttl_seconds=0.05, timeout_seconds=1.0)
        registry._transport = httpx.MockTransport(handler)
        cfg = _endpoint()

        await registry.refresh([cfg])
        self.assertEqual(hits, 1)
        self.assertEqual(registry.resolve(cfg).total_window, 1_000_000)

        await asyncio.sleep(0.06)
        await registry.refresh([cfg])

        self.assertEqual(hits, 2)
        self.assertEqual(registry.resolve(cfg).total_window, 2_000_000)
        self.assertNotEqual(registry.resolve(cfg).detail, "expired_cache")

    async def test_expired_negative_cache_is_retried_and_recovers(self) -> None:
        """负缓存过期后必须再试；网关恢复就立刻拿到真实窗口。"""

        healthy = False
        hits = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal hits
            hits += 1
            if not healthy:
                return httpx.Response(503)
            return _catalog_response(
                [{"id": "cn:deepseek-v4.1-flash", "context_length": 1_000_000}]
            )

        registry = ml.ModelLimitRegistry(failure_ttl_seconds=0.05, timeout_seconds=1.0)
        registry._transport = httpx.MockTransport(handler)
        cfg = _endpoint()

        await registry.refresh([cfg])
        self.assertEqual(hits, 1)
        self.assertEqual(registry.resolve(cfg, legacy_total_window=278_528).source, ml.LIMIT_SOURCE_UNKNOWN)
        # 负缓存 fresh：不重试（这就是"失败短 TTL"而不是"每次消息都查网"）。
        await registry.refresh([cfg])
        self.assertEqual(hits, 1)

        await asyncio.sleep(0.06)
        healthy = True
        await registry.refresh([cfg])

        self.assertEqual(hits, 2)
        self.assertEqual(registry.resolve(cfg).total_window, 1_000_000)

    async def test_three_million_window_is_not_clamped(self) -> None:
        """用户口径：按模型自报的上限自动匹配，没有 2M 人为上限。"""

        def handler(_request: httpx.Request) -> httpx.Response:
            return _catalog_response(
                [{"id": "cn:deepseek-v4.1-flash", "context_length": 3_000_000}]
            )

        registry = self._registry(handler)
        cfg = _endpoint()

        await registry.refresh([cfg])
        limits = registry.resolve(cfg)

        self.assertEqual(limits.total_window, 3_000_000)
        self.assertEqual(limits.context_total_tokens, 3_000_000)
        self.assertEqual(
            limits.input_budget_tokens(output_reserve=2_048), 2_997_952
        )

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

    async def test_a_bad_port_does_not_stop_the_other_endpoints(self) -> None:
        """一个 ``api_base`` 端口写错，不该让**所有** endpoint 的 TTL 永不续期。"""

        def handler(_request: httpx.Request) -> httpx.Response:
            return _catalog_response(
                [{"id": "cn:deepseek-v4.1-flash", "context_length": 1_000_000}]
            )

        registry = self._registry(handler)
        broken = _endpoint(model="broken/model", api_base="http://gw.internal:70000/v1")
        healthy = _endpoint()

        report = await registry.refresh([broken, healthy])

        self.assertEqual(registry.resolve(healthy).total_window, 1_000_000, "好端点必须照常刷新")
        self.assertEqual(
            sorted(report), ["broken||broken/model", "home_work2api|http://gw.internal:8080|home_work2api/cn:deepseek-v4.1-flash"]
        )


class PeriodicRefreshTests(unittest.IsolatedAsyncioTestCase):
    """周期刷新：缓存 fresh 零网络、失败不退出循环、回调拿到新窗口。"""

    def _registry(self, handler) -> ml.ModelLimitRegistry:
        registry = ml.ModelLimitRegistry(timeout_seconds=1.0)
        registry._transport = httpx.MockTransport(handler)
        return registry

    async def test_fresh_cache_means_zero_network_across_ticks(self) -> None:
        hits = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal hits
            hits += 1
            return _catalog_response(
                [{"id": "cn:deepseek-v4.1-flash", "context_length": 1_000_000}]
            )

        registry = self._registry(handler)
        cfg = _endpoint()
        refresher = ml.PeriodicModelMetadataRefresh(
            lambda: registry.refresh([cfg]),
            interval_seconds=0.01,
        )

        await refresher.refresh_once()
        self.assertEqual(hits, 1)
        for _ in range(3):
            await refresher.refresh_once()

        # 成功 TTL 6h 内：周期轮询一次网络都不发。
        self.assertEqual(hits, 1)
        self.assertEqual(refresher.ticks, 4)
        self.assertEqual(
            refresher.last_report[
                "home_work2api|http://gw.internal:8080|home_work2api/cn:deepseek-v4.1-flash"
            ],
            ml.LIMIT_SOURCE_GATEWAY,
        )

    async def test_expired_entries_are_refreshed_by_the_periodic_loop(self) -> None:
        hits = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal hits
            hits += 1
            return _catalog_response(
                [{"id": "cn:deepseek-v4.1-flash", "context_length": 500_000 * hits}]
            )

        registry = ml.ModelLimitRegistry(success_ttl_seconds=0.01, timeout_seconds=1.0)
        registry._transport = httpx.MockTransport(handler)
        cfg = _endpoint()
        seen: list[int | None] = []
        refresher = ml.PeriodicModelMetadataRefresh(
            lambda: registry.refresh([cfg]),
            interval_seconds=0.02,
            on_refreshed=lambda _report: seen.append(
                registry.resolve(cfg).total_window
            ),
        )

        task = refresher.start()
        try:
            deadline = asyncio.get_running_loop().time() + 2.0
            while len(seen) < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertGreaterEqual(hits, 2)
        self.assertEqual(seen[0], 500_000)
        self.assertEqual(seen[1], 1_000_000)

    async def test_a_failing_tick_never_kills_the_loop(self) -> None:
        calls = {"n": 0}

        async def flaky() -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("gateway down")
            return {"ok": "source"}

        seen: list[dict] = []
        refresher = ml.PeriodicModelMetadataRefresh(
            flaky,
            interval_seconds=0.01,
            on_refreshed=seen.append,
        )

        task = refresher.start()
        try:
            deadline = asyncio.get_running_loop().time() + 2.0
            while not seen and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            self.assertFalse(task.done())
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(seen, [{"ok": "source"}])

    async def test_callback_failure_also_keeps_the_loop_alive(self) -> None:
        async def refresh() -> dict:
            return {"ok": "source"}

        def boom(_report: dict) -> None:
            raise RuntimeError("memory reconfigure blew up")

        refresher = ml.PeriodicModelMetadataRefresh(
            refresh,
            interval_seconds=0.01,
            on_refreshed=boom,
        )

        task = refresher.start()
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            self.assertGreaterEqual(refresher.ticks, 2)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_async_callback_is_awaited(self) -> None:
        awaited: list[int] = []

        async def refresh() -> dict:
            return {"ok": "source"}

        async def on_refreshed(_report: dict) -> None:
            awaited.append(1)

        refresher = ml.PeriodicModelMetadataRefresh(
            refresh,
            interval_seconds=0.01,
            on_refreshed=on_refreshed,
        )

        await refresher.refresh_once()
        # refresh_once 不跑回调（回调在 run 的循环里）；这里直接验证 run 的第一次迭代。
        task = refresher.start()
        try:
            deadline = asyncio.get_running_loop().time() + 2.0
            while not awaited and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(awaited, [1])


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
        """发现到的模型窗口只用于"更紧"；1M/4M 不会放松 272Ki 业务预算。"""

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

            # 1M：自动发现（auto_window_for）拿到真实值，业务有效窗口仍是 272Ki。
            ml.MODEL_LIMITS.record(model, total_window=1_000_000)
            self.assertEqual(ml.auto_window_for(settings), 1_000_000)
            self.assertEqual(ml.effective_context_window(settings), 278_528)

            # 100K：比业务预算更小 → 生效窗口跟着变小。
            ml.reset_model_limits_for_tests()
            ml.MODEL_LIMITS.record(model, total_window=100_000)
            self.assertEqual(ml.auto_window_for(settings), 100_000)
            self.assertEqual(ml.effective_context_window(settings), 100_000)
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
