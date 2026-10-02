"""F-025：默认不把用户图片外发到第三方站点。

审查结论：``av_reverse_enabled`` 默认 True、``av_reverse_endpoint`` 写死
``https://avscan.cc/search``，于是**每个新部署在运维没做过任何决定的情况下**
就会把用户图片字节发到第三方；又因为没有 API key，失败也不会有任何东西提醒。

修好之后的口径（本文件锁定）：

- ``Settings(_env_file=None)`` 的默认值就是"不外发"：开关关闭、endpoint 为空；
- 默认设置下 ``try_reverse_image_lookup`` 直接返回空串，**一个 HTTP 请求都不发**；
- 只开开关不配 endpoint：照样不发请求，并留下 WARNING；
- 两个都显式配置后才会真正 POST（正例，保证功能没被改坏）；
- 启动日志把数据流向说清楚（默认 INFO 说明，真正外发时 WARNING 且带目标主机）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from bot.config import Settings, log_av_reverse_privacy_state
from bot.services import av_image_reverse as rev

USER_ID = 900123
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes"
LIVE_PAYLOAD = {
    "results": [
        {
            "video_code": "SONE-666",
            "best_similarity": 95.0,
            "frames": [{"image_name": "x.jpg", "similarity": 95.0}],
        }
    ]
}


def _data_uri(payload: bytes = PNG_BYTES) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(payload).decode()


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
    """假的 aiohttp 会话：把每次 post 记下来，便于断言"没发请求"。"""

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


def _patch_session():
    calls: list = []
    return calls, patch.object(
        rev.aiohttp,
        "ClientSession",
        lambda *a, **k: _FakeSession(
            _FakeResponse(200, json.dumps(LIVE_PAYLOAD).encode()), calls
        ),
    )


class _LoggingCase(unittest.TestCase):
    def setUp(self) -> None:
        rev.reset_av_scan_guard()
        rev.reset_av_reverse_cache()


class DefaultSettingsArePrivateTests(_LoggingCase):
    def test_bootstrap_defaults_disable_reverse_lookup(self) -> None:
        settings = Settings(_env_file=None)

        self.assertFalse(settings.av_reverse_enabled)
        self.assertEqual(settings.av_reverse_endpoint, "")

    def test_no_endpoint_means_no_provider(self) -> None:
        provider = rev.resolve_av_reverse_provider(Settings(_env_file=None))

        self.assertIsNone(provider)

    def test_disabled_default_makes_no_http_request(self) -> None:
        calls, patcher = _patch_session()
        with patcher:
            result = asyncio.run(
                rev.try_reverse_image_lookup(
                    _data_uri(), Settings(_env_file=None), user_id=USER_ID
                )
            )

        self.assertEqual(result, "")
        self.assertEqual(calls, [], "默认设置下不许发起任何外发请求")

    def test_enabled_without_endpoint_makes_no_http_request(self) -> None:
        settings = Settings(_env_file=None, av_reverse_enabled=True)
        calls, patcher = _patch_session()
        with self.assertLogs("bot.services.av_image_reverse", level="WARNING") as logs:
            with patcher:
                result = asyncio.run(
                    rev.try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID)
                )

        self.assertEqual(result, "")
        self.assertEqual(calls, [], "只开开关、没配 endpoint 时也不许外发")
        self.assertTrue(
            any("av_reverse_endpoint" in line for line in logs.output), logs.output
        )

    def test_explicit_opt_in_sends_the_request(self) -> None:
        settings = Settings(
            _env_file=None,
            av_reverse_enabled=True,
            av_reverse_endpoint="https://mirror.example/search",
        )
        calls, patcher = _patch_session()
        with patcher:
            result = asyncio.run(
                rev.try_reverse_image_lookup(_data_uri(), settings, user_id=USER_ID)
            )

        self.assertEqual(result, "SONE-666")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "https://mirror.example/search")


class StartupVisibilityTests(_LoggingCase):
    def test_disabled_default_is_logged_as_info(self) -> None:
        with self.assertLogs("bot.config", level="INFO") as logs:
            log_av_reverse_privacy_state(Settings(_env_file=None))

        joined = "\n".join(logs.output)
        self.assertIn("未启用", joined)
        self.assertIn("AV_REVERSE_ENABLED", joined)

    def test_enabled_but_unconfigured_is_logged_as_warning(self) -> None:
        with self.assertLogs("bot.config", level="WARNING") as logs:
            log_av_reverse_privacy_state(
                Settings(_env_file=None, av_reverse_enabled=True)
            )

        self.assertIn(
            "实际不会外发任何图片", "\n".join(logs.output)
        )

    def test_real_egress_is_logged_with_the_target_host(self) -> None:
        with self.assertLogs("bot.config", level="WARNING") as logs:
            log_av_reverse_privacy_state(
                Settings(
                    _env_file=None,
                    av_reverse_enabled=True,
                    av_reverse_endpoint="https://mirror.example/search",
                )
            )

        joined = "\n".join(logs.output)
        self.assertIn("数据离开本服务", joined)
        self.assertIn("mirror.example", joined)
