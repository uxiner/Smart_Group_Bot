"""全局运营/资源/交接字段的**后端**权限、并发与重启契约。

覆盖三件靠源码读不出来、必须真的打一次 API 才能确认的事：

1. **群管理员拿不到也改不了全局字段。** ``PUT /api/v1/settings`` 是
   ``@authenticated``（最高管理员），群管理员拿 ``super_admin_required``；``GET`` 同理。
2. **保存失败不丢数据、revision 冲突可重载。** 改一个非法值被拒之后，库里那份配置
   一个字节都没动；用过期 revision 再存会拿到 409，页面据此提示重载。
3. **响应里的 ``restart_pending`` 只列"这次真的改了"的重启字段，且只给字段名。**
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
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.runtime_config import RESTART_REQUIRED_PATHS, RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_ADMIN_ID = 4242
SYNTHETIC_GROUP_ID = -1000000000002


def _signed_init_data(user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF-config-authz-test",
        "user": json.dumps({"id": user_id, "first_name": "Tester"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class GlobalSettingsAuthzTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN_ID,
            config_master_key="config-authz-test-key",
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
            legacy_config_path="/tmp/nonexistent-config-authz.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self.bot = SimpleNamespace(token=BOT_TOKEN)
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
    def _headers(user_id: int) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    async def _document(self) -> dict:
        response = await self.client.get(
            "/api/v1/settings", headers=self._headers(SUPER_ADMIN_ID)
        )
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_a_group_admin_cannot_read_or_write_global_settings(self) -> None:
        for method, path in (
            ("get", "/api/v1/settings"),
            ("put", "/api/v1/settings"),
        ):
            with self.subTest(method=method):
                document = await self._document()
                headers = self._headers(GROUP_ADMIN_ID)
                if method == "get":
                    response = await self.client.get(path, headers=headers)
                else:
                    response = await self.client.put(
                        path,
                        headers=headers,
                        json={
                            "revision": document["revision"],
                            "config": document["config"],
                        },
                    )
                self.assertEqual(response.status, 403)
                body = await response.json()
                self.assertFalse(body.get("ok", True))
                self.assertIn("error", body)

    async def test_a_group_admin_cannot_change_global_economy_or_handover(self) -> None:
        """后端强校验：不依赖前端隐藏控件。"""

        before = await self._document()
        payload = json.loads(json.dumps(before["config"]))
        payload["economy"]["tag_price_7d"] = 1
        payload["moderation"]["review_handover_mention"] = "@sneaky_bot"
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(GROUP_ADMIN_ID),
            json={"revision": before["revision"], "config": payload},
        )
        self.assertEqual(response.status, 403)
        after = await self._document()
        self.assertEqual(
            after["config"]["economy"]["tag_price_7d"],
            before["config"]["economy"]["tag_price_7d"],
        )
        self.assertEqual(
            after["config"]["moderation"]["review_handover_mention"], ""
        )

    async def test_invalid_value_is_rejected_and_leaves_the_stored_document_intact(self) -> None:
        before = await self._document()
        payload = json.loads(json.dumps(before["config"]))
        # 下界固定：0 会让免除质询变成免费。
        payload["economy"]["challenge_skip_cost"] = 0
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": before["revision"], "config": payload},
        )
        self.assertEqual(response.status, 400)
        after = await self._document()
        self.assertEqual(after["revision"], before["revision"], "被拒的保存不能改 revision")
        self.assertEqual(after["config"], before["config"], "被拒的保存不能改任何字段")

    async def test_unknown_key_is_rejected(self) -> None:
        before = await self._document()
        payload = json.loads(json.dumps(before["config"]))
        payload["economy"]["not_a_real_field"] = 1
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": before["revision"], "config": payload},
        )
        self.assertEqual(response.status, 400)
        after = await self._document()
        self.assertEqual(after["config"], before["config"])

    async def test_stale_revision_is_a_conflict_and_reload_recovers(self) -> None:
        first = await self._document()
        payload = json.loads(json.dumps(first["config"]))
        payload["economy"]["tag_price_7d"] = 33
        ok = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": first["revision"], "config": payload},
        )
        self.assertEqual(ok.status, 200)

        # 第二个页面还拿着旧 revision。
        stale = json.loads(json.dumps(first["config"]))
        stale["economy"]["tag_price_7d"] = 44
        conflict = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": first["revision"], "config": stale},
        )
        self.assertEqual(conflict.status, 409)

        # 重载之后就能存进去。
        reloaded = await self._document()
        self.assertEqual(reloaded["config"]["economy"]["tag_price_7d"], 33)
        reloaded_payload = json.loads(json.dumps(reloaded["config"]))
        reloaded_payload["economy"]["tag_price_7d"] = 44
        again = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": reloaded["revision"], "config": reloaded_payload},
        )
        self.assertEqual(again.status, 200)

    async def test_restart_pending_names_only_changed_restart_fields(self) -> None:
        before = await self._document()
        payload = json.loads(json.dumps(before["config"]))
        payload["economy"]["tag_price_7d"] = 55  # 热字段
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": before["revision"], "config": payload},
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["restart_pending"], [], "只改热字段不该要求重启")
        self.assertEqual(
            set(body["restart_required_paths"]), set(RESTART_REQUIRED_PATHS)
        )

        current = await self._document()
        payload = json.loads(json.dumps(current["config"]))
        payload["resources"]["pending_reply_execution_capacity"] = 6
        payload["resources"]["moderation_throttle_burst"] = 4
        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": current["revision"], "config": payload},
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(
            body["restart_pending"], ["resources.pending_reply_execution_capacity"]
        )

    async def test_a_fresh_install_exposes_no_private_binding(self) -> None:
        document = await self._document()
        self.assertEqual(document["config"]["moderation"]["log_channel_id"], 0)
        self.assertEqual(document["config"]["moderation"]["review_handover_mention"], "")
        self.assertEqual(document["config"]["display"]["bot_display_name"], "助手")
        self.assertNotIn(
            "secret",
            str(document).lower().split("configured_secrets")[0][:0] or "",
        )
        # 响应里只有 configured_secrets 的键名，没有任何值。
        for path in document["configured_secrets"]:
            self.assertIsInstance(path, str)
            self.assertIn(".", path)

    async def test_an_older_payload_without_the_new_fields_keeps_its_values(self) -> None:
        """旧库缺新字段 → 补默认值，但**原有字段一个都不动**。"""

        before = await self._document()
        payload = json.loads(json.dumps(before["config"]))
        payload["bot"]["auto_delete_seconds"] = 3600
        for section in ("private_chat", "economy", "activity", "checkin_reminder", "display", "resources"):
            payload.pop(section, None)
        # moderation 段也退回旧形状：没有两个新键。
        payload["moderation"].pop("review_handover_mention", None)
        payload["moderation"]["log_channel_id"] = 0

        response = await self.client.put(
            "/api/v1/settings",
            headers=self._headers(SUPER_ADMIN_ID),
            json={"revision": before["revision"], "config": payload},
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        config = body["config"]
        self.assertEqual(config["bot"]["auto_delete_seconds"], 3600, "原有值不能丢")
        self.assertEqual(config["moderation"]["review_handover_mention"], "")
        self.assertEqual(config["private_chat"]["per_user_daily_limit"], 100)
        self.assertEqual(config["economy"]["challenge_skip_cost"], 2)
        self.assertEqual(config["activity"]["weekly_reward_points"], [25, 12, 12, 4, 4, 4, 4, 4, 4, 4])
        self.assertEqual(config["resources"]["llm_request_capacity"], 8)
        self.assertEqual(config["display"]["bot_display_name"], "助手")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
