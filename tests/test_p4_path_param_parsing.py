"""P4-5（A-24 = D3-20）：``settings_api`` 的路径参数不再裸 ``int(...)``，非法一律 400。

现象（审计 D3-20 的精确清单在本文件末尾逐条列出）：``delete_authorized_group_api`` /
``list_group_admins_api`` / ``list_telegram_admins_api`` / ``create_group_admin_api`` /
``delete_group_admin_api`` 直接写 ``int(request.match_info["id"])``。URL 里的 ``{id}``
不是数字时 ``ValueError`` 冒泡到装饰器兜底 ``except Exception`` → **500
``internal_error``**——明明是客户端把 URL 打错了，回的却是「服务器炸了」，既误导
管理员，又让真正的 5xx 混在这条噪声里。

同文件里其余端点（群设置 / 群规 / 永久记忆）本来就用 try/except 回 400，本次把
它们和上面 5 处**统一到同一个模块级解析入口**（``_path_int`` / ``_group_id``），
避免以后再各写各的。

成功路径必须逐字不变——本文件同时钉住合法 ``{id}`` 的响应体。
"""

from __future__ import annotations

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

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer
from bot.web import settings_api

BOT_TOKEN = "42:TEST_TOKEN"

#: D3-20 清单里**改前**是裸 int() 的 5 个端点：
#: (method, 非法 {id} 时的路径, 期望 code, 非法 {次级 id} 时的路径, 期望 code)
_BARE_INT_ENDPOINTS = (
    ("delete", "/api/v1/authorized-groups/not-a-number", "invalid_group_id", None, None),
    ("get", "/api/v1/groups/not-a-number/admins", "invalid_group_id", None, None),
    ("get", "/api/v1/groups/not-a-number/telegram-admins", "invalid_group_id", None, None),
    ("post", "/api/v1/groups/not-a-number/admins", "invalid_group_id", None, None),
    (
        "delete",
        "/api/v1/groups/not-a-number/admins/900001",
        "invalid_group_id",
        "/api/v1/groups/-1001/admins/not-a-number",
        "invalid_user_id",
    ),
)

#: 本次统一到同一个 helper 的另外几个端点（改前就已经是 400，行为必须保持）。
_ALREADY_MAPPED_ENDPOINTS = (
    ("put", "/api/v1/groups/not-a-number/settings", "invalid_group_id"),
    ("patch", "/api/v1/groups/-1001/rules/not-a-number", "invalid_rule_id"),
    ("delete", "/api/v1/groups/-1001/rules/not-a-number", "invalid_rule_id"),
    ("patch", "/api/v1/groups/-1001/memories/not-a-number", "invalid_memory_id"),
    ("delete", "/api/v1/groups/-1001/memories/not-a-number", "invalid_memory_id"),
)


def _signed_init_data(user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF-p4-d3-20-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class PathParameterParsingTests(unittest.IsolatedAsyncioTestCase):
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
            config_master_key="p4-d3-20-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8482
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-p4-d3-20.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            restrict_chat_member=AsyncMock(),
            get_chat_administrators=AsyncMock(return_value=[]),
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

    async def _call(self, method: str, path: str) -> tuple[int, dict]:
        handler = getattr(self.client, method)
        response = await handler(path, headers=self._headers())
        return response.status, await response.json()

    async def test_non_numeric_path_ids_return_400_not_500(self) -> None:
        for method, path, code, _alt_path, _alt_code in _BARE_INT_ENDPOINTS:
            with self.subTest(endpoint=path):
                status, body = await self._call(method, path)
                self.assertEqual(status, 400, f"{method.upper()} {path} -> {status}")
                self.assertFalse(body["ok"])
                self.assertEqual(body["error"]["code"], code)
                self.assertTrue(body["error"]["message"])

    async def test_non_numeric_secondary_path_ids_return_400(self) -> None:
        for method, _path, _code, alt_path, alt_code in _BARE_INT_ENDPOINTS:
            if alt_path is None:
                continue
            with self.subTest(endpoint=alt_path):
                status, body = await self._call(method, alt_path)
                self.assertEqual(status, 400, f"{method.upper()} {alt_path} -> {status}")
                self.assertEqual(body["error"]["code"], alt_code)

    async def test_already_mapped_endpoints_keep_their_400(self) -> None:
        for method, path, code in _ALREADY_MAPPED_ENDPOINTS:
            with self.subTest(endpoint=path):
                status, body = await self._call(method, path)
                self.assertEqual(status, 400, f"{method.upper()} {path} -> {status}")
                self.assertEqual(body["error"]["code"], code)

    async def test_missing_or_empty_path_ids_return_400(self) -> None:
        """``{id}`` 缺失（空段被 aiohttp 路由成 404）或空白字符都要落到 400。"""

        status, body = await self._call("get", "/api/v1/groups/%20/admins")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_group_id")

    async def test_valid_path_ids_keep_their_success_responses(self) -> None:
        created = await self.client.post(
            "/api/v1/authorized-groups",
            headers=self._headers(),
            json={"group_id": -1001, "title": "正常群"},
        )
        self.assertEqual(created.status, 200)
        self.assertEqual(await created.json(), {"ok": True, "created": True})

        status, body = await self._call("get", "/api/v1/groups/-1001/admins")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "admins": []})

        status, body = await self._call("get", "/api/v1/groups/-1001/telegram-admins")
        self.assertEqual(status, 200)
        self.assertIn("admins", body)

        status, body = await self._call("get", "/api/v1/groups/-1001/rules")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "rules": []})

    async def test_the_helper_is_the_single_entry_point(self) -> None:
        """全文件不再有裸 ``int(request.match_info[...])``。"""

        from pathlib import Path

        source = Path(settings_api.__file__).read_text(encoding="utf-8")
        # 解析入口本身（``int(request.match_info[key])``）是这个 helper 的实现，
        # 别的地方一律经由它。
        self.assertEqual(source.count("int(request.match_info"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
