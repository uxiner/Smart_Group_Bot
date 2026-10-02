"""用图反查番号（第三方帧级索引）的单元测试：解析、阈值、限流闸门、降级、接线。

口径来自 2026-10-02 的真流量实测（见技能
``smart-bot-operations / references/vision-image-input.md``）：

- 真命中：把索引里的命中帧回喂 → ``REAL-195 98.91%``；裁 30% 还有 ``90.25%``；
  假候选：番号封面丢进去 → 最高 ``69.42%``，其余 5 条全在 68% 附近。
  所以阈值 85 是「真命中全过、假候选全挡」的分界；
- 连发 7 次就被 ``HTTP 429``，所以并发 ≤2 + 每用户冷却 + 429 全局退避；
- 任何异常都必须降级成「没反查到」，绝不能影响原有的识图链路。

所有网络调用都是假的，不触网。
"""

from __future__ import annotations

import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import ModelConfig, Settings
from bot.handlers import commands
from bot.services import av_image_reverse as rev
from bot.services.av_image_reverse import (
    AV_SCAN_BACKOFF_SEC,
    AV_SCAN_COOLDOWN_SEC,
    AVScanHit,
    AVScanRateLimited,
    decode_image_data_uri,
    parse_av_scan_payload,
    pick_av_scan_code,
    search_av_image,
    try_reverse_image_lookup,
)

USER_ID = 900123
KB = 1024
#: 上游真实响应形状（2026-10-02 抓到的原文，字段名照抄）。
LIVE_PAYLOAD = {
    "results": [
        {
            "video_code": "REAL-195",
            "best_similarity": 69.42,
            "frames": [
                {"image_name": "REAL-195_02-59-19.jpg", "similarity": 69.42, "thumb": "/thumb/a/REAL-195/x.webp"},
                {"image_name": "REAL-195_02-59-23.jpg", "similarity": 68.0, "thumb": "/thumb/b/REAL-195/y.webp"},
            ],
        },
        {"video_code": "SCG-008", "best_similarity": 68.82, "frames": [{"image_name": "x.jpg"}]},
        {"video_code": "mdte-018", "best_similarity": "95.5", "frames": []},
    ]
}
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes"


def _data_uri(payload: bytes = PNG_BYTES) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode()


def _settings(**overrides) -> Settings:
    settings = Settings(_env_file=None)
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False


class _FakeSession:
    """假的 aiohttp 会话：记下请求，回一个预置响应。"""

    def __init__(self, response: _FakeResponse, calls: list) -> None:
        self._response = response
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        return False

    def post(self, url, data=None, headers=None):
        self._calls.append({"url": url, "data": data, "headers": headers})
        return self._response


def _patch_session(status: int, body: bytes):
    calls: list = []
    return calls, patch.object(
        rev.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(_FakeResponse(status, body), calls)
    )


class ParseTests(unittest.TestCase):
    def test_live_payload_is_sorted_and_normalized(self) -> None:
        hits = parse_av_scan_payload(LIVE_PAYLOAD)
        self.assertEqual([h.code for h in hits], ["MDTE-018", "REAL-195", "SCG-008"])
        by_code = {h.code: h for h in hits}
        self.assertAlmostEqual(by_code["REAL-195"].similarity, 69.42, places=2)
        self.assertEqual(by_code["REAL-195"].frames, 2)
        # 字符串形式的相似度也要能收；番号统一大写。
        self.assertAlmostEqual(by_code["MDTE-018"].similarity, 95.5, places=2)

    def test_limit_keeps_the_best(self) -> None:
        hits = parse_av_scan_payload(LIVE_PAYLOAD, limit=2)
        self.assertEqual([h.code for h in hits], ["MDTE-018", "REAL-195"])

    def test_malformed_payloads_return_empty(self) -> None:
        for payload in (None, [], "x", {}, {"results": None}, {"results": "x"}, {"results": [1, "a"]}):
            with self.subTest(payload=payload):
                self.assertEqual(parse_av_scan_payload(payload), [])

    def test_rows_without_code_are_skipped(self) -> None:
        hits = parse_av_scan_payload({"results": [{"best_similarity": 99}, {"video_code": "  "}]})
        self.assertEqual(hits, [])


class PickTests(unittest.TestCase):
    def test_threshold_is_inclusive(self) -> None:
        self.assertEqual(pick_av_scan_code([AVScanHit("A-1", 85.0)]), ("A-1", 85.0))
        self.assertIsNone(pick_av_scan_code([AVScanHit("A-1", 84.99)]))

    def test_true_hit_passes_false_candidates_dont(self) -> None:
        # 真命中（回喂命中帧）与假候选（封面）的真实相似度。
        real = [AVScanHit("REAL-195", 98.91), AVScanHit("NHDTA-164", 77.34)]
        cover = [AVScanHit("REAL-195", 69.42), AVScanHit("SCG-008", 68.82)]
        self.assertEqual(pick_av_scan_code(real)[0], "REAL-195")
        self.assertIsNone(pick_av_scan_code(cover))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(pick_av_scan_code([]))


class DataUriTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        self.assertEqual(decode_image_data_uri(_data_uri()), PNG_BYTES)

    def test_rejects_other_shapes(self) -> None:
        for raw in ("", "https://x/y.jpg", "data:image/jpeg,plain", "data:image/jpeg;base64,"):
            with self.subTest(raw=raw):
                self.assertEqual(decode_image_data_uri(raw), b"")


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1000.0
        self.guard = rev.AVScanGuard(clock=lambda: self.now)

    def test_second_call_within_cooldown_is_blocked(self) -> None:
        self.assertTrue(self.guard.allow(USER_ID))
        self.assertFalse(self.guard.allow(USER_ID))
        self.now += AV_SCAN_COOLDOWN_SEC
        self.assertTrue(self.guard.allow(USER_ID))

    def test_other_user_is_not_blocked(self) -> None:
        self.assertTrue(self.guard.allow(USER_ID))
        self.assertTrue(self.guard.allow(USER_ID + 1))

    def test_rate_limit_blocks_everyone_until_backoff_ends(self) -> None:
        self.guard.note_rate_limited()
        self.assertGreater(self.guard.blocked_seconds_left(), 0)
        self.assertFalse(self.guard.allow(USER_ID))
        self.assertFalse(self.guard.allow(USER_ID + 1))
        self.now += AV_SCAN_BACKOFF_SEC
        self.assertEqual(self.guard.blocked_seconds_left(), 0)
        self.assertTrue(self.guard.allow(USER_ID + 1))


class SearchTests(unittest.TestCase):
    def test_ok_returns_hits_and_posts_the_file_field(self) -> None:
        calls, patcher = _patch_session(200, json.dumps(LIVE_PAYLOAD).encode())
        with patcher:
            hits = asyncio.run(search_av_image(PNG_BYTES))
        self.assertEqual(hits[0].code, "MDTE-018")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["url"].endswith("/search"))
        # 字段名必须就是 file（上游字段名不对时会回 422 Field required）。
        fields = repr(calls[0]["data"]._fields)
        self.assertIn("'name': 'file'", fields)
        self.assertIn("probe.jpg", fields)

    def test_429_raises_rate_limited(self) -> None:
        _calls, patcher = _patch_session(429, b"")
        with patcher:
            with self.assertRaises(AVScanRateLimited):
                asyncio.run(search_av_image(PNG_BYTES))

    def test_non_200_returns_empty(self) -> None:
        _calls, patcher = _patch_session(500, b"boom")
        with patcher:
            self.assertEqual(asyncio.run(search_av_image(PNG_BYTES)), [])

    def test_bad_json_returns_empty(self) -> None:
        _calls, patcher = _patch_session(200, b"<html>not json</html>")
        with patcher:
            self.assertEqual(asyncio.run(search_av_image(PNG_BYTES)), [])

    def test_empty_image_never_sends(self) -> None:
        calls, patcher = _patch_session(200, b"{}")
        with patcher:
            self.assertEqual(asyncio.run(search_av_image(b"")), [])
        self.assertEqual(calls, [])


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        rev.reset_av_scan_guard()

    def test_disabled_never_calls_upstream(self) -> None:
        settings = _settings(av_reverse_enabled=False)
        with patch.object(rev, "search_av_image", AsyncMock()) as fake:
            self.assertEqual(asyncio.run(try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID)), "")
        self.assertFalse(fake.called)

    def test_hit_above_threshold_returns_code(self) -> None:
        settings = _settings()
        payload = {"results": [{"video_code": "sone-666", "best_similarity": 92.4, "frames": [{}]}]}
        calls, patcher = _patch_session(200, json.dumps(payload).encode())
        with patcher:
            code = asyncio.run(try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID))
        self.assertEqual(code, "SONE-666")

    def test_below_threshold_returns_empty(self) -> None:
        settings = _settings()
        payload = {"results": [{"video_code": "REAL-195", "best_similarity": 69.42, "frames": []}]}
        _calls, patcher = _patch_session(200, json.dumps(payload).encode())
        with patcher:
            self.assertEqual(
                asyncio.run(try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID)), ""
            )

    def test_custom_threshold_is_respected(self) -> None:
        settings = _settings(av_reverse_min_similarity=60.0)
        payload = {"results": [{"video_code": "REAL-195", "best_similarity": 69.42, "frames": []}]}
        _calls, patcher = _patch_session(200, json.dumps(payload).encode())
        with patcher:
            self.assertEqual(
                asyncio.run(try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID)), "REAL-195"
            )

    def test_429_degrades_and_sets_backoff(self) -> None:
        settings = _settings()
        hits, patcher = _patch_session(429, b"")
        with patcher:
            self.assertEqual(
                asyncio.run(try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID)), ""
            )
        self.assertGreater(rev.av_scan_guard().blocked_seconds_left(), 0)

    def test_cooldown_skips_second_call(self) -> None:
        settings = _settings()
        payload = {"results": [{"video_code": "SONE-666", "best_similarity": 95.0, "frames": []}]}
        calls, patcher = _patch_session(200, json.dumps(payload).encode())
        with patcher:
            first = asyncio.run(try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID))
            second = asyncio.run(try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID))
        self.assertEqual(first, "SONE-666")
        self.assertEqual(second, "")
        self.assertEqual(len(calls), 1)

    def test_exception_degrades_quietly(self) -> None:
        settings = _settings()
        with patch.object(rev, "search_av_image", AsyncMock(side_effect=RuntimeError("boom"))):
            self.assertEqual(
                asyncio.run(try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID)), ""
            )

    def test_timeout_degrades_quietly(self) -> None:
        settings = _settings()
        with patch.object(rev, "search_av_image", AsyncMock(side_effect=asyncio.TimeoutError())):
            self.assertEqual(
                asyncio.run(try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID)), ""
            )

    def test_empty_image_degrades_quietly(self) -> None:
        settings = _settings()
        with patch.object(rev, "search_av_image", AsyncMock()) as fake:
            self.assertEqual(
                asyncio.run(try_reverse_image_lookup("data:image/jpeg,notbase64", settings, user_id=USER_ID)), ""
            )
        self.assertFalse(fake.called)


class WiringTests(unittest.TestCase):
    """接线：反查命中 → 直接用番号，**不调视觉模型**；未命中 → 照旧走识图。"""

    def setUp(self) -> None:
        rev.reset_av_scan_guard()

    def _settings(self) -> Settings:
        settings = Settings(_env_file=None)
        settings.bot.enable_typing = False  # typing_action 直接 yield，不需要真 bot
        settings.bot.auto_delete_seconds = 0
        settings.bot.main_model = ModelConfig(model="main-model-for-test")
        settings.bot.vision_model = ModelConfig(model="vision-model-for-test")
        return settings

    def _message(self) -> SimpleNamespace:
        return SimpleNamespace(
            chat=SimpleNamespace(id=USER_ID),
            from_user=SimpleNamespace(id=USER_ID, full_name="tester", username="tester"),
            bot=SimpleNamespace(send_chat_action=AsyncMock()),
            photo=[SimpleNamespace(file_id="f1", file_size=142 * KB)],
        )

    def _run(self, reverse_code: str):
        message = self._message()
        vision = AsyncMock(return_value="")
        reverse = AsyncMock(return_value=reverse_code)
        with patch.object(
            commands, "select_av_image_file", lambda *a, **k: ("f1", "image/jpeg", 142 * KB)
        ), patch.object(
            commands, "build_av_image_data_uri_for", AsyncMock(return_value=_data_uri())
        ), patch.object(
            commands, "_av_vision_text", vision
        ), patch.object(
            commands, "try_reverse_image_lookup", reverse
        ):
            outcome = asyncio.run(
                commands._av_image_vision_with_escalation(
                    message, self._settings(), user_id=USER_ID
                )
            )
        return outcome, vision, reverse

    def test_hit_short_circuits_the_vision_model(self) -> None:
        outcome, vision, reverse = self._run("SONE-666")
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.vision_text, "SONE-666")
        self.assertFalse(outcome.escalated)
        self.assertTrue(reverse.called)
        self.assertFalse(vision.called, "反查命中时不该再调视觉模型")

    def test_miss_falls_back_to_the_vision_model(self) -> None:
        outcome, vision, reverse = self._run("")
        self.assertIsNotNone(outcome)
        self.assertTrue(reverse.called)
        self.assertTrue(vision.called, "反查未命中时必须回到原有识图链路")

    def test_reverse_lookup_sees_the_downloaded_image(self) -> None:
        _outcome, _vision, reverse = self._run("")
        sent_uri = reverse.await_args.args[0]
        self.assertTrue(sent_uri.startswith("data:image/"))
        self.assertEqual(reverse.await_args.kwargs.get("user_id"), USER_ID)


if __name__ == "__main__":
    unittest.main()
