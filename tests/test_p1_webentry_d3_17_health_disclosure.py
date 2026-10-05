"""修复批 P1-3 / D3-17：``/healthz`` 不再把内部指标外发给任何访问者。

修前 ``/healthz``（**无鉴权**、服务还可能监听所有网卡）在 F-009 把自由文本降级成
布尔之后，**结构化计数器与 ``config_revision`` 仍原样返回**::

    # bot/services/verify_web.py
    return web.json_response(
        {
            "ok": ok,
            "delivery_mode": self._delivery_mode,
            "webhook_accepting_updates": self._webhook_accepting_updates,
            "webhook": _redact_health_diagnostics(webhook_health),
            "resources": _redact_health_diagnostics(resources),
            "config_revision": self.runtime_config.revision ...,
        },
        status=200 if ok else 503,
    )

任何人都能读到 worker/队列存活数、队列积压、各优先级队列年龄、
accepted/failed/dead_lettered 累计值、cgroup/磁盘水位，以及 ``config_revision``
（每次配置保存 +1，可用于推断管理员的操作节奏与时间点）；``status=503`` 还额外
泄露「系统当前正在降级」。

修法：``/healthz`` 只回 ``{"ok": bool}``（docker healthcheck 与编排也只需要这个），
原详情整体搬到**需超管鉴权**的 ``/api/v1/health``。

本文件走**真实端到端**：真 ``VerifyWebServer`` + 真签名 initData + 真 SQLite，
只把 Telegram 侧换成假客户端。**不打真实 Telegram API。**
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from bot.db.engine import init_db
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 777


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    """Build initData signed the way Telegram signs Mini App payloads."""
    import hashlib
    import hmac
    from urllib.parse import urlencode

    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "admin"}, separators=(",", ":")
        ),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class HealthEndpointDisclosureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            restrict_chat_member=AsyncMock(),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
            edit_message_text=AsyncMock(),
            delete_message=AsyncMock(return_value=True),
            send_message=AsyncMock(),
        )
        from bot.config import Settings

        self.settings = Settings(_env_file=None)
        self.settings.super_admin_id = SUPER_ADMIN_ID
        self.server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
        )
        self.client = TestClient(TestServer(self.server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def test_public_healthz_returns_only_the_boolean(self) -> None:
        self.server.mark_polling_active()
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(list(payload), ["ok"])
        self.assertTrue(payload["ok"])

    async def test_public_healthz_does_not_leak_config_revision(self) -> None:
        self.server.mark_polling_active()
        raw = await (await self.client.get("/healthz")).text()
        for leaked in ("config_revision", "resources", "webhook", "delivery_mode"):
            self.assertNotIn(leaked, raw)

    async def test_detail_endpoint_requires_authentication(self) -> None:
        response = await self.client.get("/api/v1/health")
        self.assertEqual(response.status, 401)

    async def test_detail_endpoint_rejects_a_non_super_admin(self) -> None:
        response = await self.client.get(
            "/api/v1/health",
            headers={
                "Authorization": f"tma {_signed_init_data(SUPER_ADMIN_ID + 1)}"
            },
        )
        self.assertEqual(response.status, 403)

    async def test_super_admin_sees_the_full_health_picture(self) -> None:
        self.server.mark_polling_active()
        response = await self.client.get(
            "/api/v1/health",
            headers={
                "Authorization": f"tma {_signed_init_data(SUPER_ADMIN_ID)}"
            },
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        for key in ("ok", "delivery_mode", "webhook", "resources", "config_revision"):
            self.assertIn(key, payload)
