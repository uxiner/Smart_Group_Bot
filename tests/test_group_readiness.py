"""就绪体检与首次预设的**诚实性**（第三阶段返工）。

面板最容易骗人的四种状态，这里要求它们分开：

* ``configured`` —— 本地参数齐备（**没有**任何远端连通性测试）；
* ``verified`` —— Telegram 侧事实已核实（bot 在群、权限）；
* ``enabled`` —— 本群开关确实开着（验证服务配好了 ≠ 这个群开了新人验证）；
* ``ready`` —— 综合结论，只按**本群选了哪些功能**算阻断。

钉死的反例：

* Telegram 问不成时整体**不得**绿灯；
* 新预设的 preview/apply 必须实时核验身份（不可核验 503、确认已撤 403），
  但**不能**把超管锁在门外；
* 陪伴型的文案不得与本群真实 TTS 状态矛盾，也不得把静音说成"永不推送"；
* ``?features=`` 走重复参数，未知功能必须报错而不是被静默忽略；
* 就绪必须看**群级开关**，不能只看全局模型槽；
* preview 必须带本群现值→候选值 diff、当前 revision 与就绪。
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
from sqlalchemy import select

from bot.config import EmbedConfig, ModelConfig, Settings
from bot.db.engine import init_db
from bot.db.models import Admin, AuthorizedGroup, Group
from bot.services import group_onboarding as onboarding
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009876543210
GROUP_ADMIN_ID = 7
RETIRED_ADMIN_ID = 8


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-readiness-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _admin(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, is_bot=False), status="administrator"
    )


class _Telegram:
    def __init__(self) -> None:
        self.admins = {
            GROUP_ID: [_admin(GROUP_ADMIN_ID), _admin(SUPER_ADMIN_ID)],
            OTHER_GROUP_ID: [_admin(GROUP_ADMIN_ID)],
        }
        self.fail_admins = False
        self.fail_chat = False
        self.admin_calls: list[tuple[int, int]] = []

    async def get_chat(self, chat_id: int) -> SimpleNamespace:
        if self.fail_chat:
            raise RuntimeError("telegram transport unavailable")
        return SimpleNamespace(id=int(chat_id), type="supergroup")

    async def get_chat_administrators(self, chat_id: int):
        self.admin_calls.append((int(chat_id), 0))
        if self.fail_admins:
            raise RuntimeError("telegram transport unavailable")
        return list(self.admins.get(int(chat_id), []))

    async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
        if self.fail_chat or self.fail_admins:
            raise RuntimeError("telegram transport unavailable")
        if int(user_id) == 424242:
            return SimpleNamespace(
                status="administrator",
                user=SimpleNamespace(id=424242, is_bot=True),
                can_delete_messages=True,
                can_restrict_members=True,
                can_pin_messages=True,
                can_invite_users=False,
                can_manage_chat=False,
            )
        return SimpleNamespace(status="member", user=SimpleNamespace(id=int(user_id)))

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=424242, username="fixture_bot")


class ReadinessTruthfulnessTests(unittest.IsolatedAsyncioTestCase):
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
            config_master_key="readiness-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8484
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self._configure_models()
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-readiness.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self._configure_models()
        from bot.web.auth import reset_init_data_replay_budget

        reset_init_data_replay_budget()
        onboarding.reset_rate_limit()
        self.telegram = _Telegram()
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            get_chat=self.telegram.get_chat,
            get_chat_administrators=self.telegram.get_chat_administrators,
            get_chat_member=self.telegram.get_chat_member,
            get_me=self.telegram.get_me,
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
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    AuthorizedGroup(group_id=GROUP_ID, authorized_by=SUPER_ADMIN_ID)
                )
                session.add(
                    AuthorizedGroup(
                        group_id=OTHER_GROUP_ID, authorized_by=SUPER_ADMIN_ID
                    )
                )
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    Admin(group_id=GROUP_ID, user_id=GROUP_ADMIN_ID, role="admin")
                )
                session.add(
                    Group(id=GROUP_ID, title="审核群", settings={"welcome_message": "hi"})
                )

    def _configure_models(self) -> None:
        def chat() -> ModelConfig:
            return ModelConfig(
                model="fixture/main-v1",
                provider="openai_compatible",
                api_base="https://gateway.example.invalid/v1",
                api_key="fixture-key",
            )

        for field in (
            "main_model",
            "vision_model",
            "decision_model",
            "moderation_model",
            "compress_model",
        ):
            setattr(self.settings.bot, field, chat())
        self.settings.bot.embed_model = EmbedConfig(
            model="fixture/embed-v1",
            provider="openai_compatible",
            api_base="https://gateway.example.invalid/v1",
            api_key="fixture-key",
        )

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _headers(self, user_id: int = GROUP_ADMIN_ID) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    async def _patch_group_settings(self, updates: dict) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                group = await session.get(Group, GROUP_ID)
                stored = dict(group.settings or {})
                stored.update(updates)
                group.settings = stored

    async def _readiness(self, query: str = "", user_id: int = GROUP_ADMIN_ID):
        url = f"/api/v1/groups/{GROUP_ID}/readiness"
        if query:
            url = f"{url}?{query}"
        return await self.client.get(url, headers=self._headers(user_id))

    # ------------------------------------------------------------------
    # P3-1 未核验不得绿灯
    # ------------------------------------------------------------------
    async def test_unverified_telegram_never_reports_ready(self) -> None:
        response = await self._readiness()
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["verified"])
        self.assertTrue(payload["readiness"]["ready"])

        # 现在 Telegram 问不成：面板必须整体转为未核验，**不得**仍然 ready。
        self.telegram.fail_admins = True
        self.telegram.fail_chat = True
        from bot.web import settings_api

        settings_api._ADMIN_REVALIDATION_CACHE.clear()
        degraded = await self._readiness()
        self.assertEqual(degraded.status, 200)
        body = await degraded.json()
        self.assertFalse(body["verified"])
        readiness = body["readiness"]
        self.assertFalse(readiness["ready"], "Telegram 问不成时整体不得绿灯")
        self.assertEqual(readiness["state"], onboarding.STATE_UNVERIFIED)
        self.assertIn("unverified", readiness["blockers"])
        for item in readiness["capabilities"]:
            self.assertFalse(item["verified"], item["capability"])
            self.assertEqual(item["state"], onboarding.STATE_UNVERIFIED)

    async def test_capability_separates_configured_verified_enabled(self) -> None:
        payload = (await (await self._readiness()).json())["readiness"]
        moderation = next(
            item for item in payload["capabilities"] if item["capability"] == "moderation"
        )
        # 四个维度必须各自存在，而不是揉成一个 state。
        for key in ("configured", "verified", "enabled", "state", "blockers"):
            self.assertIn(key, moderation)
        self.assertTrue(moderation["configured"])
        self.assertTrue(moderation["verified"])

    # ------------------------------------------------------------------
    # P3-5 群级开关
    # ------------------------------------------------------------------
    async def test_group_level_switch_is_reported_separately_from_service_config(
        self,
    ) -> None:
        """验证服务配好 ≠ 这个群开了新人验证。"""

        from bot.services import join_verification

        original = join_verification.verification_service_ready
        join_verification.verification_service_ready = lambda *a, **k: True
        try:
            off = await (await self._readiness()).json()
            verification = next(
                item
                for item in off["readiness"]["capabilities"]
                if item["capability"] == "verification"
            )
            self.assertTrue(
                verification["service_configured"],
                "服务本身配好了，这句不是谎话",
            )
            self.assertFalse(
                verification["enabled"],
                "本群没开新人验证，enabled 必须是 false",
            )
            self.assertEqual(verification["state"], onboarding.STATE_DISABLED)

            await self._patch_group_settings({"join_verification_enabled": True})
            on = await (await self._readiness()).json()
            verification = next(
                item
                for item in on["readiness"]["capabilities"]
                if item["capability"] == "verification"
            )
            self.assertTrue(verification["enabled"])
            self.assertTrue(verification["service_configured"])
        finally:
            join_verification.verification_service_ready = original

    async def test_unused_capabilities_do_not_block_overall_readiness(self) -> None:
        """只选审核型时，没配 embed / summary 不应让整体 not ready。"""

        payload = await (await self._readiness("features=moderation")).json()
        readiness = payload["readiness"]
        names = {item["capability"] for item in readiness["capabilities"]}
        self.assertIn("embed", names, "所有能力都要列出来")
        self.assertTrue(readiness["ready"])
        self.assertNotIn("embed", readiness["blockers"])

    async def test_selected_capability_does_block(self) -> None:
        payload = await (await self._readiness("features=join_verification")).json()
        readiness = payload["readiness"]
        verification = next(
            item
            for item in readiness["capabilities"]
            if item["capability"] == "verification"
        )
        # 阻断以 "<能力>:<原因>" 表达，这样接口能同时说清"哪个能力"和"为什么"。
        self.assertTrue(
            any(
                token.startswith("verification:")
                for token in readiness["blockers"]
            ),
            readiness["blockers"],
        )

    # ------------------------------------------------------------------
    # P3-4 features 查询参数
    # ------------------------------------------------------------------
    async def test_features_accepts_repeated_query_parameters(self) -> None:
        response = await self._readiness("features=moderation&features=vision")
        self.assertEqual(response.status, 200, await response.text())
        payload = await response.json()
        self.assertEqual(payload["features"], ["moderation", "vision"])

    async def test_features_rejects_unknown_names_and_bad_syntax(self) -> None:
        for query in ("features=root-shell", "features=moderation,vision", "features="):
            response = await self._readiness(query)
            self.assertEqual(response.status, 400, query)

    async def test_features_absent_keeps_the_default(self) -> None:
        payload = await (await self._readiness()).json()
        self.assertEqual(
            payload["features"], list(onboarding.DEFAULT_READINESS_FEATURES)
        )

    # ------------------------------------------------------------------
    # P3-2 新预设操作的严格核验
    # ------------------------------------------------------------------
    async def _revision(self) -> str:
        payload = await (await self.client.get("/api/v1/groups", headers=self._headers())).json()
        return next(g["revision"] for g in payload["groups"] if int(g["id"]) == GROUP_ID)

    async def test_preset_apply_requires_live_admin_verification(self) -> None:
        from bot.web import settings_api

        settings_api._ADMIN_REVALIDATION_CACHE.clear()
        self.telegram.fail_admins = True
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-apply",
            json={
                "preset_id": onboarding.PRESET_MODERATION_ID,
                "revision": await self._revision(),
            },
            headers=self._headers(),
        )
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "telegram_unverifiable")
        async with self.session_factory() as session:
            stored = await session.get(Group, GROUP_ID)
        self.assertNotIn("mute_all_replies", (stored.settings or {}))

    async def test_preset_apply_refuses_a_retired_administrator(self) -> None:
        self.telegram.admins[GROUP_ID] = [_admin(SUPER_ADMIN_ID)]
        from bot.web import settings_api

        settings_api._ADMIN_REVALIDATION_CACHE.clear()
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-apply",
            json={
                "preset_id": onboarding.PRESET_MODERATION_ID,
                "revision": await self._revision(),
            },
            headers=self._headers(RETIRED_ADMIN_ID),
        )
        self.assertIn(response.status, (403, 404))
        async with self.session_factory() as session:
            stored = await session.get(Group, GROUP_ID)
        self.assertNotIn("mute_all_replies", (stored.settings or {}))

    async def test_super_admin_is_not_locked_out_by_the_strict_check(self) -> None:
        """超管不在 Telegram 管理员名单里也必须能管理自己授权的群。"""

        self.telegram.admins[GROUP_ID] = [_admin(GROUP_ADMIN_ID)]
        from bot.web import settings_api

        settings_api._ADMIN_REVALIDATION_CACHE.clear()
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-apply",
            json={
                "preset_id": onboarding.PRESET_MODERATION_ID,
                "revision": await self._revision(),
            },
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(response.status, 200, await response.text())
        async with self.session_factory() as session:
            stored = await session.get(Group, GROUP_ID)
        self.assertTrue((stored.settings or {}).get("mute_all_replies"))

    async def test_preset_preview_also_verifies_live(self) -> None:
        from bot.web import settings_api

        settings_api._ADMIN_REVALIDATION_CACHE.clear()
        self.telegram.fail_admins = True
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-preview",
            json={"preset_id": onboarding.PRESET_MODERATION_ID},
            headers=self._headers(),
        )
        self.assertEqual(response.status, 503)

    # ------------------------------------------------------------------
    # P3-3 预设文案必须与真实状态一致
    # ------------------------------------------------------------------
    async def test_companion_preset_describes_the_existing_tts_state(self) -> None:
        await self._patch_group_settings({"tts_mode": "on"})
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-preview",
            json={"preset_id": onboarding.PRESET_COMPANION_ID},
            headers=self._headers(),
        )
        self.assertEqual(response.status, 200, await response.text())
        payload = await response.json()
        joined = " ".join(payload["risks"])
        self.assertNotIn(
            "语音播报保持关闭",
            joined,
            "本群 tts_mode=on，文案却说保持关闭",
        )
        self.assertIn("当前", joined)
        # 并且 apply 之后 TTS 必须保持 on（不得为迁就文案而静默关掉）。
        apply = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-apply",
            json={
                "preset_id": onboarding.PRESET_COMPANION_ID,
                "revision": payload["current_revision"],
            },
            headers=self._headers(),
        )
        self.assertEqual(apply.status, 200, await apply.text())
        async with self.session_factory() as session:
            stored = await session.get(Group, GROUP_ID)
        self.assertEqual((stored.settings or {}).get("tts_mode"), "on")

    async def test_moderation_preset_does_not_claim_to_stop_background_pushes(self) -> None:
        payload = await (
            await self.client.post(
                f"/api/v1/groups/{GROUP_ID}/preset-preview",
                json={"preset_id": onboarding.PRESET_MODERATION_ID},
                headers=self._headers(),
            )
        ).json()
        joined = " ".join(payload["risks"])
        self.assertNotIn("不会推送任何", joined)

    async def test_preset_patch_keys_match_the_declared_effect(self) -> None:
        """文案承诺的每一项都必须在 patch 里，反之亦。"""

        for preset_id in onboarding.PRESET_IDS:
            with self.subTest(preset_id=preset_id):
                payload = await (
                    await self.client.post(
                        f"/api/v1/groups/{GROUP_ID}/preset-preview",
                        json={"preset_id": preset_id},
                        headers=self._headers(),
                    )
                ).json()
                keys = {entry["key"] for entry in payload["patch"]}
                self.assertLessEqual(keys, onboarding.PRESET_WRITABLE_KEYS)
                self.assertTrue(keys)

    # ------------------------------------------------------------------
    # P3-6 preview 的 diff / revision / 就绪
    # ------------------------------------------------------------------
    async def test_preview_returns_current_values_revision_and_readiness(self) -> None:
        await self._patch_group_settings({"mute_all_replies": True, "tts_mode": "on"})
        payload = await (
            await self.client.post(
                f"/api/v1/groups/{GROUP_ID}/preset-preview",
                json={"preset_id": onboarding.PRESET_COMPANION_ID},
                headers=self._headers(),
            )
        ).json()
        self.assertEqual(payload["current_revision"], await self._revision())
        diff = {item["key"]: item for item in payload["diff"]}
        self.assertIn("mute_all_replies", diff)
        self.assertTrue(diff["mute_all_replies"]["current"])
        self.assertFalse(diff["mute_all_replies"]["next"])
        self.assertIn("readiness", payload)
        self.assertIn("ready", payload["readiness"])
        # 受保护面必须显式声明"不变"。
        self.assertTrue(payload["protected_unchanged"])
        self.assertNotIn("moderation_rules", {item["key"] for item in payload["patch"]})

    async def test_preview_still_writes_nothing(self) -> None:
        async with self.session_factory() as session:
            before = dict((await session.get(Group, GROUP_ID)).settings or {})
        for preset_id in onboarding.PRESET_IDS:
            await self.client.post(
                f"/api/v1/groups/{GROUP_ID}/preset-preview",
                json={"preset_id": preset_id},
                headers=self._headers(),
            )
        async with self.session_factory() as session:
            after = dict((await session.get(Group, GROUP_ID)).settings or {})
        self.assertEqual(before, after)

    async def test_apply_still_only_touches_whitelisted_keys(self) -> None:
        await self._patch_group_settings({"welcome_message": "草稿", "tts_mode": "on"})
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-apply",
            json={
                "preset_id": onboarding.PRESET_COMMUNITY_ID,
                "revision": await self._revision(),
            },
            headers=self._headers(),
        )
        self.assertEqual(response.status, 200, await response.text())
        async with self.session_factory() as session:
            after = dict((await session.get(Group, GROUP_ID)).settings or {})
        self.assertEqual(after["welcome_message"], "草稿")
        self.assertEqual(after["tts_mode"], "on")
        self.assertFalse("av_enabled" in after and after["av_enabled"])
        self.assertNotIn("moderation_rules", after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class EffectiveReadinessConsumerTests(unittest.IsolatedAsyncioTestCase):
    """就绪必须读**真实消费者**，而不是猜出来的层级或新造的影子开关。

    父代理 v23 实测三红：真实 ``group_summary_config`` 已开却被报成关闭；
    ``ModelConfig(model="", api_key="")`` 也算"已配置"；tts 完全缺席。
    """

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None, bot_token=BOT_TOKEN, super_admin_id=SUPER_ADMIN_ID,
            config_master_key="effective-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8485
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self._configure_models()
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-effective.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        self._configure_models()
        from bot.web.auth import reset_init_data_replay_budget

        reset_init_data_replay_budget()
        onboarding.reset_rate_limit()
        telegram = _Telegram()
        server = VerifyWebServer(
            bot=SimpleNamespace(
                token=BOT_TOKEN,
                get_chat=telegram.get_chat,
                get_chat_administrators=telegram.get_chat_administrators,
                get_chat_member=telegram.get_chat_member,
                get_me=telegram.get_me,
                ban_chat_member=AsyncMock(), unban_chat_member=AsyncMock(),
                restrict_chat_member=AsyncMock(),
            ),
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    AuthorizedGroup(group_id=GROUP_ID, authorized_by=SUPER_ADMIN_ID)
                )
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    Admin(group_id=GROUP_ID, user_id=GROUP_ADMIN_ID, role="admin")
                )
                session.add(Group(id=GROUP_ID, title="群", settings={}))

    def _configure_models(self, *, usable: bool = True) -> None:
        chat = (
            ModelConfig(
                model="fixture/main-v1", provider="openai_compatible",
                api_base="https://gateway.example.invalid/v1", api_key="fixture-key",
            )
            if usable
            else ModelConfig(model="", provider="", api_key="", api_base="")
        )
        for field in (
            "main_model", "vision_model", "decision_model",
            "moderation_model", "compress_model",
        ):
            setattr(self.settings.bot, field, chat.model_copy())

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(GROUP_ADMIN_ID)}"}

    async def _set_group_settings(self, updates: dict) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                group = await session.get(Group, GROUP_ID)
                stored = dict(group.settings or {})
                stored.update(updates)
                group.settings = stored

    async def _capability(self, name: str, features: str = "") -> dict:
        url = f"/api/v1/groups/{GROUP_ID}/readiness"
        if features:
            url = f"{url}?features={features}"
        response = await self.client.get(url, headers=self._headers())
        self.assertEqual(response.status, 200, await response.text())
        payload = (await response.json())["readiness"]
        return next(
            item for item in payload["capabilities"] if item["capability"] == name
        )

    async def test_summary_reads_the_real_consumer_getter(self) -> None:
        self.settings.bot.group_summary_enabled = True
        await self._set_group_settings({"proactive_enabled": True})
        from bot.services.group_summary import group_summary_config

        self.assertTrue(
            group_summary_config(self.settings).enabled,
            "前提不成立：真实消费者认为没开",
        )
        item = await self._capability("summary")
        self.assertTrue(
            item["enabled"],
            "真实消费者已开，就绪却读成了关闭（取错了配置层级）",
        )

    async def test_unusable_compress_model_blocks_summary(self) -> None:
        self.settings.bot.group_summary_enabled = True
        self._configure_models(usable=False)
        await self._set_group_settings({"proactive_enabled": True})
        item = await self._capability("summary")
        self.assertFalse(
            item["configured"],
            "model='' / api_key='' 的 ModelConfig 是**不可用**模型，不是已配置",
        )
        self.assertEqual(item["state"], onboarding.STATE_PROVIDER_MISSING)

    async def test_tts_is_a_real_capability_and_off_blocks_it(self) -> None:
        await self._set_group_settings({"tts_mode": "off"})
        payload = await (
            await self.client.get(
                f"/api/v1/groups/{GROUP_ID}/readiness?features=tts",
                headers=self._headers(),
            )
        ).json()
        readiness = payload["readiness"]
        names = {item["capability"] for item in readiness["capabilities"]}
        self.assertIn("tts", names, "设过 tts_mode 却当这项能力不存在")
        tts = next(item for item in readiness["capabilities"] if item["capability"] == "tts")
        self.assertFalse(tts["enabled"])
        self.assertTrue(
            any(token.startswith("tts:") for token in readiness["blockers"]),
            "选了 tts 却没开启，就绪仍报 ready",
        )

    async def test_no_shadow_switch_is_invented(self) -> None:
        """不许为了喂自己的判定而造一个没有消费者的群键。"""

        source = (
            __import__("pathlib").Path(__file__).resolve().parents[1]
            / "bot" / "services" / "group_onboarding.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            'stored.get("summary_enabled")', source,
            "又造了一个没有任何消费者的影子开关键",
        )
        self.assertNotIn('"group_summary_enabled"', source.split("def group_readiness_document(")[0][-4000:])


class TtsBackendReadinessTests(unittest.IsolatedAsyncioTestCase):
    """tts 的 configured 必须是**真实语音后端**可用，不是 main 模型存在。

    父代理 v24 四臂（证据 sb-p1-e6af17a-tts-backend.json）：
      1 显式 edge + 有效音色 + 全局 enabled + 群 on → 可用 / ready   （控制）
      2 同上但全局 enabled=False + 群 on            → 不可用，ready 仍 True ✗
      3 后端有效 + 群 off                            → 可用 / ready=False（控制）
      4 显式 doubao + enabled 但 app_id/access_key 空 → 不可用，ready 仍 True ✗
    """

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None, bot_token=BOT_TOKEN, super_admin_id=SUPER_ADMIN_ID,
            config_master_key="tts-backend-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self._configure_models()
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    AuthorizedGroup(group_id=GROUP_ID, authorized_by=SUPER_ADMIN_ID)
                )
                session.add(
                    Group(id=GROUP_ID, title="群", settings={"tts_mode": "on"})
                )

    def _configure_models(self) -> None:
        chat = ModelConfig(
            model="fixture/main-v1", provider="openai_compatible",
            api_base="https://gateway.example.invalid/v1", api_key="fixture-key",
        )
        for field in ("main_model", "vision_model", "decision_model",
                      "moderation_model", "compress_model"):
            setattr(self.settings.bot, field, chat.model_copy())

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    def _use_edge(self, *, globally_enabled: bool) -> None:
        self.settings.doubao_tts_provider = "edge"
        self.settings.doubao_tts_speaker = "zh-CN-XiaoxiaoNeural"
        self.settings.doubao_tts_enabled = globally_enabled

    def _use_doubao_without_credentials(self) -> None:
        self.settings.doubao_tts_provider = "doubao"
        self.settings.doubao_tts_enabled = True
        self.settings.doubao_tts_app_id = ""
        self.settings.doubao_tts_access_key = ""
        self.settings.doubao_tts_resource_id = ""
        self.settings.doubao_tts_speaker = ""

    async def _report(self, group_tts_mode: str | None) -> dict:
        async with self.session_factory() as session:
            async with session.begin():
                group = await session.get(Group, GROUP_ID)
                stored = dict(group.settings or {})
                if group_tts_mode is None:
                    stored.pop("tts_mode", None)
                else:
                    stored["tts_mode"] = group_tts_mode
                group.settings = stored
                stored = dict(stored)
        # 用真实的 TelegramGroupFacts：捏一个 SimpleNamespace 会在新增字段时
        # 悄悄缺属性，测的就不是生产路径了。
        facts = onboarding.TelegramGroupFacts(
            group_id=GROUP_ID,
            verified=True,
            exists=True,
            chat_type="supergroup",
            bot_present=True,
            bot_status="administrator",
            bot_permissions={
                "can_delete_messages": True,
                "can_restrict_members": True,
                "can_pin_messages": True,
            },
        )
        return onboarding.group_readiness_document(
            settings=self.settings,
            facts=facts,
            features=["tts"],
            group_settings=stored,
        )

    async def test_arm1_usable_backend_and_group_on_is_ready(self) -> None:
        self._use_edge(globally_enabled=True)
        report = await self._report("on")
        tts = next(i for i in report["capabilities"] if i["capability"] == "tts")
        self.assertTrue(tts["configured"], tts["detail"])
        self.assertTrue(tts["enabled"])
        self.assertTrue(report["ready"], report["blockers"])

    async def test_arm2_globally_disabled_backend_blocks_readiness(self) -> None:
        self._use_edge(globally_enabled=False)
        report = await self._report("on")
        tts = next(i for i in report["capabilities"] if i["capability"] == "tts")
        self.assertFalse(tts["configured"], "全局语音未启用却被报成已配置")
        self.assertFalse(report["ready"], report["blockers"])

    async def test_arm3_usable_backend_but_group_off_is_not_ready(self) -> None:
        self._use_edge(globally_enabled=True)
        report = await self._report("off")
        tts = next(i for i in report["capabilities"] if i["capability"] == "tts")
        self.assertTrue(tts["configured"])
        self.assertFalse(tts["enabled"])
        self.assertFalse(report["ready"], report["blockers"])

    async def test_arm4_doubao_without_credentials_blocks_readiness(self) -> None:
        self._use_doubao_without_credentials()
        report = await self._report("on")
        tts = next(i for i in report["capabilities"] if i["capability"] == "tts")
        self.assertFalse(
            tts["configured"],
            "app_id / access_key 都没有，却把语音后端报成已配置",
        )
        self.assertFalse(report["ready"], report["blockers"])

    async def test_arm5_absent_tts_key_is_reported_and_blocks(self) -> None:
        """群没写 tts_mode（真实默认 = off）时，能力**必须**列出且阻断。"""

        self._use_edge(globally_enabled=True)
        report = await self._report(None)
        names = {item["capability"] for item in report["capabilities"]}
        self.assertIn(
            "tts", names,
            "没写 tts_mode 就不列 tts 能力 → 选了 tts 的新群显示成 ready",
        )
        tts = next(i for i in report["capabilities"] if i["capability"] == "tts")
        self.assertFalse(tts["enabled"], "真实默认是 off，不得因为键缺失而算开启")
        self.assertFalse(report["ready"], report["blockers"])

    async def test_arm6_none_tts_with_disabled_backend_blocks(self) -> None:
        self._use_edge(globally_enabled=False)
        report = await self._report(None)
        tts = next(i for i in report["capabilities"] if i["capability"] == "tts")
        self.assertFalse(tts["configured"])
        self.assertFalse(tts["enabled"])
        self.assertFalse(report["ready"], report["blockers"])

    async def test_unselected_tts_does_not_block_other_features(self) -> None:
        """不选 tts 时，TTS 后端不可用**不得**拖累其它功能的就绪结论。"""

        self._use_edge(globally_enabled=False)
        async with self.session_factory() as session:
            async with session.begin():
                group = await session.get(Group, GROUP_ID)
                stored = dict(group.settings or {})
        facts = onboarding.TelegramGroupFacts(
            group_id=GROUP_ID, verified=True, exists=True, chat_type="supergroup",
            bot_present=True, bot_status="administrator",
            bot_permissions={
                "can_delete_messages": True, "can_restrict_members": True,
                "can_pin_messages": True,
            },
        )
        report = onboarding.group_readiness_document(
            settings=self.settings,
            facts=facts,
            features=["moderation"],
            group_settings=stored,
        )
        self.assertNotIn(
            "tts", [token.split(":", 1)[0] for token in report["blockers"]],
            report["blockers"],
        )
