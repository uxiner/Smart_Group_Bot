"""P4-6（A-27 = D3-23）：内部异常原文不再原样回给 HTTP 客户端。

现象：``settings_api`` 的几处把 ``str(exc)`` 直接塞进响应体，管理员在 Mini App
里会看到：

* ``runtime config revision changed: expected 5, got 7``（409 revision_conflict）
* ``CONFIG_MASTER_KEY is required before saving secret settings`` /
  ``stored settings cannot be decrypted with CONFIG_MASTER_KEY``（400 密钥不可用）

前者暴露了 revision 数值，后者直接把**配置项的 env 变量名**告诉前端；两者对
管理员排障都没有帮助，细节却留在了浏览器里。

改法（任务书）：客户端只拿通用文案 + 一个能在服务端日志里对上的编号，详情进
服务端日志；**状态码与成功响应不变**。

同文件里 ``invalid_template_buttons`` / ``invalid_default_permissions`` /
``invalid_api_model_query_*`` / ``repair_reason`` 那几处 ``str(exc)`` 属于**用户
输入校验**文案（本来就设计成给管理员看的字段级提示，不含内部状态），本批不动，
理由见 FIX-p4.md。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.runtime_config import RuntimeConfigManager, SecretCipher
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"

_REF_PATTERN = re.compile(r"错误编号 ([0-9a-f]{8})")


def _signed_init_data(user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF-p4-d3-23-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class OpaqueInternalErrorTests(unittest.IsolatedAsyncioTestCase):
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
            config_master_key="p4-d3-23-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8483
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-p4-d3-23.toml",
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

    async def _document(self) -> dict:
        response = await self.client.get("/api/v1/settings", headers=self._headers())
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_revision_conflict_keeps_409_but_hides_internal_text(self) -> None:
        document = await self._document()
        payload = dict(document["config"])
        with self.assertLogs("bot.web.settings_api", level=logging.ERROR) as logs:
            response = await self.client.put(
                "/api/v1/settings",
                headers=self._headers(),
                json={"revision": 999, "config": payload, "secret_changes": {}},
            )
        body = await response.json()
        self.assertEqual(response.status, 409, "状态码不能变")
        self.assertEqual(body["error"]["code"], "revision_conflict")
        message = body["error"]["message"]
        self.assertNotIn("runtime config revision changed", message)
        self.assertNotIn("expected", message)
        self.assertNotIn("999", message)
        self.assertIn("请刷新后重试", message)
        # 编号能在服务端日志里对上。
        match = _REF_PATTERN.search(message)
        self.assertIsNotNone(match, f"文案里应当带可核对的编号：{message}")
        joined = "\n".join(logs.output)
        self.assertIn(match.group(1), joined)
        self.assertIn("revision changed", joined)

    async def test_encryption_failure_keeps_400_but_hides_the_env_var_name(self) -> None:
        document = await self._document()
        payload = dict(document["config"])
        # 未配置主密钥的 cipher：保存密钥时抛 RuntimeConfigEncryptionError。
        with (
            patch.object(self.manager, "_cipher", SecretCipher("")),
            self.assertLogs("bot.web.settings_api", level=logging.ERROR) as logs,
        ):
            response = await self.client.put(
                "/api/v1/settings",
                headers=self._headers(),
                json={
                    "revision": document["revision"],
                    "config": payload,
                    "secret_changes": {
                        "providers.gemini.api_key": {
                            "action": "replace",
                            "value": "provider-secret",
                        }
                    },
                },
            )
        body = await response.json()
        self.assertEqual(response.status, 400, "状态码不能变")
        self.assertEqual(body["error"]["code"], "secret_storage_unavailable")
        message = body["error"]["message"]
        self.assertNotIn("CONFIG_MASTER_KEY", message)
        self.assertIn("最高管理员", message)
        match = _REF_PATTERN.search(message)
        self.assertIsNotNone(match, f"文案里应当带可核对的编号：{message}")
        joined = "\n".join(logs.output)
        self.assertIn(match.group(1), joined)
        self.assertIn("CONFIG_MASTER_KEY", joined)

    async def test_unexpected_failure_is_a_generic_500_with_a_log_ref(self) -> None:
        document = await self._document()
        payload = dict(document["config"])
        with (
            patch.object(
                self.manager,
                "save",
                AsyncMock(
                    side_effect=RuntimeError(
                        "database is locked: /var/lib/smart-bot/data/bot.db"
                    )
                ),
            ),
            self.assertLogs("bot.web.settings_api", level=logging.ERROR) as logs,
        ):
            response = await self.client.put(
                "/api/v1/settings",
                headers=self._headers(),
                json={"revision": document["revision"], "config": payload},
            )
        body = await response.json()
        self.assertEqual(response.status, 500, "状态码不能变")
        self.assertEqual(body["error"]["code"], "internal_error")
        message = body["error"]["message"]
        self.assertNotIn("/var/lib/smart-bot", message)
        self.assertNotIn("database is locked", message)
        match = _REF_PATTERN.search(message)
        self.assertIsNotNone(match, f"文案里应当带可核对的编号：{message}")
        joined = "\n".join(logs.output)
        self.assertIn(match.group(1), joined)
        self.assertIn("/var/lib/smart-bot/data/bot.db", joined)

    async def test_two_failures_get_two_different_refs(self) -> None:
        document = await self._document()
        payload = dict(document["config"])
        refs = set()
        for _ in range(2):
            with patch.object(
                self.manager,
                "save",
                AsyncMock(side_effect=RuntimeError("boom")),
            ), self.assertLogs("bot.web.settings_api", level=logging.ERROR):
                response = await self.client.put(
                    "/api/v1/settings",
                    headers=self._headers(),
                    json={"revision": document["revision"], "config": payload},
                )
            body = await response.json()
            match = _REF_PATTERN.search(body["error"]["message"])
            self.assertIsNotNone(match)
            refs.add(match.group(1))
        self.assertEqual(len(refs), 2, "每次失败的编号必须互不相同")

    async def test_success_responses_are_untouched(self) -> None:
        document = await self._document()
        payload = dict(document["config"])
        payload["bot"] = dict(payload["bot"])
        payload["bot"]["enable_typing"] = False
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(),
            json={"revision": document["revision"], "config": payload},
        )
        self.assertEqual(response.status, 200)
        saved = await response.json()
        self.assertEqual(saved["revision"], document["revision"] + 1)
        self.assertFalse(saved["config"]["bot"]["enable_typing"])
        self.assertNotIn("错误编号", json.dumps(saved, ensure_ascii=False))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
