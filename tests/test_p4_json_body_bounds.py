"""P4-8（D3-22）：``settings_api`` 的请求体读取有明确的大小上界，超限回 413。

现状（改前）：``request.json()`` 本身没有大小检查，唯一的上界是 ``verify_web``
建 app 时给的 ``client_max_size=1 MiB``，命中后 aiohttp 抛
``HTTPRequestEntityTooLarge``，由框架回一个**纯文本** 413
（``Maximum request body size 1048576 exceeded.``）——和本文件其它接口统一的
``{"ok": false, "error": {...}}`` 信封对不上，Mini App 只能把它当未知错误。

改动（**默认值与今天一致**）：
* 新增 ``_JSON_BODY_MAX_BYTES = 1 MiB``，与 app 级 ``client_max_size`` 同值；
* ``_read_request_json`` 在开读之前先看 ``Content-Length``，超限直接 413，
  不把正文读进内存（没有 Content-Length 的分块请求仍由 aiohttp 兜底）；
* ``_json_object`` 把 aiohttp 的 ``HTTPRequestEntityTooLarge`` 映射成同款 413
  信封。

读超时（``_JSON_BODY_TIMEOUT_SECONDS = 5.0``）本来就存在（既有用例
``JsonBodyDeadlineTests`` 钉着 408），本条一并把「上界 + 超时」两件事钉全。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web_exceptions import HTTPRequestEntityTooLarge

from bot.config import Settings
from bot.db.engine import init_db
from bot.services import verify_web
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer
from bot.web import settings_api

BOT_TOKEN = "42:TEST_TOKEN"


def _signed_init_data(user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF-p4-d3-22-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class JsonBodySizeBoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_handler_cap_equals_the_app_level_client_max_size(self) -> None:
        """默认上界不能比 app 级更严，也不能更松——否则「无行为变化」就不成立。"""

        self.assertEqual(
            settings_api._JSON_BODY_MAX_BYTES, verify_web._WEB_MAX_REQUEST_BYTES
        )
        self.assertEqual(settings_api._JSON_BODY_MAX_BYTES, 1024 * 1024)

    async def test_oversized_content_length_is_rejected_without_reading(self) -> None:
        reader = AsyncMock(return_value={})
        request = SimpleNamespace(
            content_length=settings_api._JSON_BODY_MAX_BYTES + 1,
            json=reader,
        )
        with self.assertRaises(settings_api._APIError) as caught:
            await settings_api._json_object(request)
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(caught.exception.code, "request_too_large")
        reader.assert_not_awaited()  # 正文一个字节都没读进内存

    async def test_content_length_exactly_at_the_cap_is_accepted(self) -> None:
        request = SimpleNamespace(
            content_length=settings_api._JSON_BODY_MAX_BYTES,
            json=AsyncMock(return_value={"ok": 1}),
        )
        self.assertEqual(await settings_api._json_object(request), {"ok": 1})

    async def test_a_body_at_the_cap_with_no_content_length_still_reads(self) -> None:
        """没有 Content-Length（分块请求）时不误杀，交给 aiohttp 的流式上界。"""

        request = SimpleNamespace(
            content_length=None,
            json=AsyncMock(return_value={"ok": 1}),
        )
        self.assertEqual(await settings_api._json_object(request), {"ok": 1})

    async def test_aiohttp_too_large_becomes_the_json_envelope(self) -> None:
        request = SimpleNamespace(
            content_length=None,
            json=AsyncMock(
                side_effect=HTTPRequestEntityTooLarge(
                    max_size=verify_web._WEB_MAX_REQUEST_BYTES,
                    actual_size=verify_web._WEB_MAX_REQUEST_BYTES * 4,
                )
            ),
        )
        with self.assertRaises(settings_api._APIError) as caught:
            await settings_api._json_object(request)
        self.assertEqual(caught.exception.status, 413)
        self.assertEqual(caught.exception.code, "request_too_large")
        self.assertTrue(caught.exception.message)

    async def test_the_read_deadline_is_still_honoured(self) -> None:
        """上界之外，超时这条既有保证不能被改坏。"""

        self.assertEqual(settings_api._JSON_BODY_TIMEOUT_SECONDS, 5.0)
        original = settings_api._JSON_BODY_TIMEOUT_SECONDS
        original_grace = settings_api._JSON_BODY_CANCEL_GRACE_SECONDS
        release = asyncio.Event()

        async def read_json():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await release.wait()
            return {}

        settings_api._JSON_BODY_TIMEOUT_SECONDS = 0.02
        settings_api._JSON_BODY_CANCEL_GRACE_SECONDS = 0.01
        request = SimpleNamespace(content_length=2, json=AsyncMock(side_effect=read_json))
        try:
            with self.assertRaises(settings_api._APIError) as caught:
                await settings_api._json_object(request)
            self.assertEqual(caught.exception.status, 408)
            self.assertEqual(caught.exception.code, "request_timeout")
        finally:
            release.set()
            for _ in range(20):
                if not settings_api._JSON_BODY_ORPHANS:
                    break
                await asyncio.sleep(0)
            settings_api._JSON_BODY_TIMEOUT_SECONDS = original
            settings_api._JSON_BODY_CANCEL_GRACE_SECONDS = original_grace
            settings_api._JSON_BODY_ORPHANS.clear()


class JsonBodySizeBoundOverHttpTests(unittest.IsolatedAsyncioTestCase):
    """真客户端 + 真 app：超限请求必须拿到 JSON 信封，正常请求不受影响。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=42,
            config_master_key="p4-d3-22-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8484
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-p4-d3-22.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            restrict_chat_member=AsyncMock(),
        )
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    @staticmethod
    def _headers(user_id: int = 42) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    async def test_oversized_request_gets_413_with_the_standard_envelope(self) -> None:
        document = await (
            await self.client.get("/api/v1/settings", headers=self._headers())
        ).json()
        payload = dict(document["config"])
        payload["padding"] = "x" * (settings_api._JSON_BODY_MAX_BYTES + 1024)
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(),
            json={"revision": document["revision"], "config": payload},
        )
        self.assertEqual(response.status, 413)
        body = await response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "request_too_large")
        self.assertNotIn("Maximum request body size", await response.text())

    async def test_an_ordinary_request_is_unaffected(self) -> None:
        document = await (
            await self.client.get("/api/v1/settings", headers=self._headers())
        ).json()
        payload = dict(document["config"])
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(),
            json={"revision": document["revision"], "config": payload},
        )
        self.assertEqual(response.status, 200)
        saved = await response.json()
        self.assertEqual(saved["revision"], document["revision"] + 1)

    async def test_invalid_json_still_gets_400(self) -> None:
        response = await self.client.put(
            "/api/v1/settings",
            headers={**self._headers(), "Content-Type": "text/plain"},
            data=b"not json",
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "invalid_json")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
