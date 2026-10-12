"""首次预设与本群就绪体检（Task 3）。

两条铁律：

* **preview 零写入**：看一眼要改什么，必须与「真的改了」在库里完全无法区分地
  没有副作用。用例直接比对整张 ``groups.settings`` 的前后快照。
* **apply 只碰白名单**：预设能写的键必须是 ``_GroupSettingsUpdate`` 真正支持的
  那些，而且**不能**是审核规则/动作/阈值、不能是全局模型、不能是隐私留存、
  不能是密钥、不能是付费权益、不能是娱乐 AV。用例把整份文档逐键比对。

就绪体检的诚实性同样被钉死：

* 区分 ``configured`` / ``permission_missing`` / ``provider_missing`` /
  ``unverified``，不许把「没配」说成「已连通」；
* 每次加载**不**真的调 LLM（有用例断言 litellm 一次都没被碰）；
* 缺 ``can_invite_users`` 不影响审核能力就绪（按实际功能需求判权限）。
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot.config import ModelConfig, Settings
from bot.db.engine import init_db
from bot.db.models import Admin, AuthorizedGroup, Group, GroupOnboardingRequest
from bot.services import group_onboarding as onboarding
from bot.services import llm as llm_module
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009876543210
GROUP_ADMIN_ID = 7


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-preset-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _admin_member(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, is_bot=False),
        status="administrator",
    )


def _telegram_fixture() -> SimpleNamespace:
    admins = [_admin_member(GROUP_ADMIN_ID), _admin_member(SUPER_ADMIN_ID)]

    async def get_chat(chat_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=int(chat_id), type="supergroup")

    async def get_chat_administrators(chat_id: int):
        return list(admins)

    async def get_chat_member(chat_id: int, user_id: int) -> SimpleNamespace:
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

    async def get_me() -> SimpleNamespace:
        return SimpleNamespace(id=424242, username="fixture_bot")

    return SimpleNamespace(
        token=BOT_TOKEN,
        get_chat=get_chat,
        get_chat_administrators=get_chat_administrators,
        get_chat_member=get_chat_member,
        get_me=get_me,
        ban_chat_member=AsyncMock(),
        unban_chat_member=AsyncMock(),
        restrict_chat_member=AsyncMock(),
    )


class PresetAndReadinessTests(unittest.IsolatedAsyncioTestCase):
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
            config_master_key="preset-test-key",
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
            legacy_config_path="/tmp/nonexistent-preset.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        # 给全部模型槽位一个**确定已配置**的替身（keyless 的自定义网关）。
        # 默认配置没有凭据，那样每条能力都会判成 provider_missing，
        # 「权限够不够」这条线就被淹没了，测不到它想测的东西。
        self._install_configured_models()
        from bot.web.auth import reset_init_data_replay_budget

        reset_init_data_replay_budget()
        onboarding.reset_rate_limit()
        self.bot = _telegram_fixture()
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
                # 先落授权父行：admins.group_id 对 authorized_groups 有 CASCADE FK，
                # 同一个 flush 里插会被外键当场拒掉。
                session.add(
                    AuthorizedGroup(
                        group_id=GROUP_ID, authorized_by=SUPER_ADMIN_ID
                    )
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
                    Group(id=GROUP_ID, title="审核群", settings={"welcome_message": "你好"})
                )
                session.add(Group(id=OTHER_GROUP_ID, title="别群", settings={}))

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _install_configured_models(self) -> None:
        from bot.config import EmbedConfig

        def chat() -> ModelConfig:
            return ModelConfig(
                model="fixture/main-v1",
                provider="openai_compatible",
                api_base="https://gateway.example.invalid/v1",
                api_key="fixture-key",
            )

        def embed() -> EmbedConfig:
            return EmbedConfig(
                model="fixture/embed-v1",
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
        self.settings.bot.embed_model = embed()

    @staticmethod
    def _headers(user_id: int = GROUP_ADMIN_ID) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    async def _snapshot(self, group_id: int = GROUP_ID) -> dict:
        async with self.session_factory() as session:
            group = await session.get(Group, group_id)
            return copy.deepcopy(dict(group.settings or {}))

    async def _revision(self, group_id: int = GROUP_ID) -> str:
        response = await self.client.get(
            "/api/v1/groups", headers=self._headers()
        )
        groups = (await response.json())["groups"]
        return next(g["revision"] for g in groups if int(g["id"]) == group_id)

    # ------------------------------------------------------------------
    # 就绪体检
    # ------------------------------------------------------------------
    async def test_readiness_lists_every_capability(self) -> None:
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/readiness", headers=self._headers()
        )
        self.assertEqual(response.status, 200)
        payload = (await response.json())["readiness"]
        names = {item["capability"] for item in payload["capabilities"]}
        self.assertLessEqual(
            {"main", "decision", "moderation", "vision", "embed", "verification"},
            names,
        )
        for item in payload["capabilities"]:
            self.assertIn(
                item["state"],
                {
                    "configured",
                    "disabled",
                    "permission_missing",
                    "provider_missing",
                    "unverified",
                },
            )
        self.assertIn("remote_probe", payload)
        self.assertFalse(payload["remote_probe"])

    async def test_readiness_never_calls_the_model(self) -> None:
        with patch.object(
            llm_module.litellm, "acompletion", new=AsyncMock()
        ) as completion, patch.object(
            llm_module.litellm, "aembedding", new=AsyncMock()
        ) as embedding:
            for _ in range(3):
                response = await self.client.get(
                    f"/api/v1/groups/{GROUP_ID}/readiness", headers=self._headers()
                )
                self.assertEqual(response.status, 200)
        completion.assert_not_called()
        embedding.assert_not_called()

    async def test_readiness_requires_group_access(self) -> None:
        stranger = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/readiness",
            headers=self._headers(999),
        )
        self.assertEqual(stranger.status, 403)
        other = await self.client.get(
            f"/api/v1/groups/{OTHER_GROUP_ID}/readiness",
            headers=self._headers(),
        )
        self.assertEqual(other.status, 403)
        unauthenticated = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/readiness"
        )
        self.assertEqual(unauthenticated.status, 401)

    async def test_missing_credentials_report_provider_missing_not_configured(
        self,
    ) -> None:
        # 环境里有没有 OPENAI_API_KEY 会改变判定，所以先把这类环境变量摘掉：
        # 这条用例要测的是"配置缺凭据 → provider_missing"，不是"这台机器配没配"。
        for name in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.settings.bot.main_model = ModelConfig(
            model="openai/gpt-4o-mini", provider="openai", api_key=""
        )
        self.settings.bot.main_model.fallbacks = []
        self._install_configured_models()
        self.settings.bot.main_model = ModelConfig(
            model="openai/gpt-4o-mini", provider="openai", api_key=""
        )
        self.settings.bot.main_model.fallbacks = []
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/readiness", headers=self._headers()
        )
        payload = (await response.json())["readiness"]
        main = next(
            item for item in payload["capabilities"] if item["capability"] == "main"
        )
        self.assertEqual(main["state"], "provider_missing")
        self.assertTrue(main["detail"])
        self.assertNotIn("api_key", json.dumps(payload).lower())

    async def test_missing_invite_permission_does_not_block_moderation(self) -> None:
        """``can_invite_users=False`` 与审核能力无关：不得判成不就绪。"""

        response = await self.client.get(
            f"/api/v1/groups/{GROUP_ID}/readiness", headers=self._headers()
        )
        payload = (await response.json())["readiness"]
        moderation = next(
            item for item in payload["capabilities"] if item["capability"] == "moderation"
        )
        self.assertEqual(moderation["state"], "configured")
        self.assertNotIn("can_invite_users", json.dumps(payload))

    async def test_readiness_reports_missing_bot_permission(self) -> None:
        async def restricted_bot_member(chat_id: int, user_id: int):
            return SimpleNamespace(
                status="administrator",
                user=SimpleNamespace(id=424242, is_bot=True),
                can_delete_messages=False,
                can_restrict_members=False,
                can_pin_messages=False,
            )

        self.bot.get_chat_member = restricted_bot_member
        response = await self.client.post(
            "/api/v1/onboarding/check",
            json={"group_id": GROUP_ID, "desired_features": ["moderation"]},
            headers=self._headers(),
        )
        payload = await response.json()
        self.assertEqual(
            payload["missing_bot_permissions"], ["can_delete_messages"]
        )

    # ------------------------------------------------------------------
    # 申请人点「检查就绪」拿到的真实诊断（check 端点的就绪文档）
    # ------------------------------------------------------------------
    async def _check(self, **body) -> tuple[int, dict]:
        response = await self.client.post(
            "/api/v1/onboarding/check",
            json={"group_id": GROUP_ID, **body},
            headers=self._headers(),
        )
        return response.status, await response.json()

    async def _requests(self) -> list[GroupOnboardingRequest]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(GroupOnboardingRequest))).all())

    async def test_check_lists_every_capability_and_never_probes_a_provider(
        self,
    ) -> None:
        """一条可选诊断：列出全部能力，一条供应商都不调。"""

        before = await self._snapshot()
        with patch.object(
            llm_module.litellm, "acompletion", new=AsyncMock()
        ) as completion, patch.object(
            llm_module.litellm, "aembedding", new=AsyncMock()
        ) as embedding:
            status, payload = await self._check(
                preset_id=onboarding.PRESET_MODERATION_ID
            )
        self.assertEqual(status, 200, payload)
        readiness = payload["readiness"]
        names = {item["capability"] for item in readiness["capabilities"]}
        self.assertLessEqual(
            {
                "main",
                "decision",
                "moderation",
                "vision",
                "embed",
                "verification",
                "summary",
                "proactive",
                "tts",
            },
            names,
        )
        self.assertFalse(readiness["remote_probe"])
        self.assertEqual(completion.await_count, 0)
        self.assertEqual(embedding.await_count, 0)
        # 配置齐全 + 只依赖审核能力 → 这次是真就绪，不是"空文档恒绿"。
        self.assertEqual(readiness["selected_features"], ["moderation"])
        self.assertTrue(readiness["ready"], readiness["blockers"])
        self.assertEqual(readiness["blockers"], [])
        # 诊断不是一次写：群设置一个字节都不变。
        self.assertEqual(await self._snapshot(), before)

    async def test_check_without_a_preset_still_explains_the_default_set(
        self,
    ) -> None:
        """没选预设也要有可理解的诊断：身份/机器人/授权 + 默认功能集合。"""

        status, payload = await self._check()
        self.assertEqual(status, 200, payload)
        readiness = payload["readiness"]
        self.assertEqual(
            readiness["selected_features"],
            list(onboarding.DEFAULT_READINESS_FEATURES),
        )
        self.assertTrue(readiness["capabilities"])
        self.assertEqual(payload["chat_type"], "supergroup")
        self.assertEqual(readiness["chat_type"], "supergroup")
        self.assertTrue(readiness["bot_present"])
        self.assertTrue(readiness["bot_is_administrator"])
        self.assertIn("authorized", payload)
        for item in readiness["capabilities"]:
            self.assertIn(
                item["state"],
                {
                    "configured",
                    "disabled",
                    "permission_missing",
                    "provider_missing",
                    "unverified",
                },
            )
            self.assertTrue(item["detail"])
        # 拿不到条目才会是空壳；这里必须有内容。
        self.assertNotEqual(readiness["capabilities"], [])
        self.assertIsNone(payload["preset_id"])

    async def test_check_reports_still_disabled_capabilities_as_blockers(self) -> None:
        """选了带摘要/主动的预设、而本群还没应用 → 如实报成阻断，不假装就绪。"""

        definition = onboarding.preset_definition(onboarding.PRESET_COMMUNITY_ID)
        status, payload = await self._check(preset_id=onboarding.PRESET_COMMUNITY_ID)
        self.assertEqual(status, 200, payload)
        readiness = payload["readiness"]
        self.assertEqual(readiness["selected_features"], list(definition.features))
        self.assertFalse(readiness["ready"])
        self.assertIn("summary:disabled", readiness["blockers"])
        self.assertIn("proactive:disabled", readiness["blockers"])
        moderation = next(
            item for item in readiness["capabilities"] if item["capability"] == "moderation"
        )
        self.assertEqual(moderation["state"], "configured")

    async def test_check_reports_permission_missing_for_the_preset_it_checks(
        self,
    ) -> None:
        async def restricted_bot_member(chat_id: int, user_id: int):
            return SimpleNamespace(
                status="administrator",
                user=SimpleNamespace(id=424242, is_bot=True),
                can_delete_messages=False,
                can_restrict_members=False,
                can_pin_messages=False,
            )

        self.bot.get_chat_member = restricted_bot_member
        status, payload = await self._check(
            preset_id=onboarding.PRESET_COMPANION_ID
        )
        self.assertEqual(status, 200, payload)
        readiness = payload["readiness"]
        moderation = next(
            item for item in readiness["capabilities"] if item["capability"] == "moderation"
        )
        self.assertEqual(moderation["state"], "permission_missing")
        self.assertIn("机器人缺少本能力需要的群管理权限", moderation["detail"])
        self.assertIn("moderation:permission_missing", readiness["blockers"])
        self.assertFalse(readiness["ready"])
        self.assertEqual(payload["missing_bot_permissions"], ["can_delete_messages"])

    async def test_check_never_leaks_a_key_an_endpoint_or_another_group(self) -> None:
        status, payload = await self._check(
            preset_id=onboarding.PRESET_COMMUNITY_ID
        )
        self.assertEqual(status, 200, payload)
        raw = json.dumps(payload)
        for forbidden in (
            "fixture-key",
            "api_key",
            "api_base",
            "gateway.example.invalid",
            BOT_TOKEN,
            "can_invite_users",
            str(OTHER_GROUP_ID),
        ):
            self.assertNotIn(forbidden, raw)

    async def test_check_never_rewrites_an_existing_pending_requests_preset(
        self,
    ) -> None:
        """体检是只读诊断：已提交申请里的 preset_id 一个字都不能被改写。"""

        submit = await self.client.post(
            "/api/v1/onboarding/requests",
            json={
                "group_id": GROUP_ID,
                "preset_id": onboarding.PRESET_MODERATION_ID,
            },
            headers=self._headers(),
        )
        self.assertEqual(submit.status, 200, await submit.text())
        request_id = (await submit.json())["request"]["request_id"]
        before = await self._snapshot()

        status, payload = await self._check(preset_id=onboarding.PRESET_COMMUNITY_ID)
        self.assertEqual(status, 200, payload)
        # 本次诊断按**这次选的**预设算……
        self.assertEqual(payload["preset_id"], onboarding.PRESET_COMMUNITY_ID)
        self.assertEqual(
            payload["readiness"]["selected_features"],
            list(
                onboarding.preset_definition(
                    onboarding.PRESET_COMMUNITY_ID
                ).features
            ),
        )
        # ……但已提交的那条申请保持原样（幂等重试的正确行为）。
        self.assertEqual(payload["open_request"]["request_id"], request_id)
        self.assertEqual(
            payload["open_request"]["preset_id"], onboarding.PRESET_MODERATION_ID
        )
        rows = await self._requests()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].preset_id, onboarding.PRESET_MODERATION_ID)
        self.assertEqual(await self._snapshot(), before)

    async def test_check_reports_a_missing_provider_instead_of_ready(self) -> None:
        """关掉凭据后体检必须报 provider_missing，不能给出"可接入"的假象。"""

        for name in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self._install_configured_models()
        self.settings.bot.main_model = ModelConfig(
            model="openai/gpt-4o-mini", provider="openai", api_key=""
        )
        self.settings.bot.main_model.fallbacks = []
        status, payload = await self._check(preset_id=onboarding.PRESET_COMPANION_ID)
        self.assertEqual(status, 200, payload)
        readiness = payload["readiness"]
        main = next(
            item for item in readiness["capabilities"] if item["capability"] == "main"
        )
        self.assertEqual(main["state"], "provider_missing")
        self.assertFalse(readiness["ready"])
        self.assertIn("main:provider_missing", readiness["blockers"])
        # 机器人权限没问题：这一条阻断必须来自**配置**，不是权限。
        self.assertEqual(payload["missing_bot_permissions"], [])

    # ------------------------------------------------------------------
    # 预设 preview
    # ------------------------------------------------------------------
    async def _preview(self, preset_id: str) -> dict:
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-preview",
            json={"preset_id": preset_id},
            headers=self._headers(),
        )
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def test_preview_writes_nothing(self) -> None:
        before = await self._snapshot()
        for preset_id in onboarding.PRESET_IDS:
            payload = await self._preview(preset_id)
            self.assertIn("patch", payload)
            self.assertIn("risks", payload)
            self.assertTrue(payload["patch"])
        self.assertEqual(await self._snapshot(), before)

    async def test_preview_documents_every_patch_key(self) -> None:
        payload = await self._preview(onboarding.PRESET_COMMUNITY_ID)
        keys = {entry["key"] for entry in payload["patch"]}
        self.assertLessEqual(keys, onboarding.PRESET_WRITABLE_KEYS)
        for entry in payload["patch"]:
            self.assertIn("value", entry)
            self.assertIn("effect", entry)

    async def test_unknown_preset_is_rejected(self) -> None:
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-preview",
            json={"preset_id": "enable-everything"},
            headers=self._headers(),
        )
        self.assertEqual(response.status, 400)

    async def test_preview_and_apply_require_group_access(self) -> None:
        for suffix in ("preset-preview", "preset-apply"):
            stranger = await self.client.post(
                f"/api/v1/groups/{GROUP_ID}/{suffix}",
                json={"preset_id": onboarding.PRESET_MODERATION_ID, "revision": "x"},
                headers=self._headers(999),
            )
            self.assertEqual(stranger.status, 403, suffix)
            other = await self.client.post(
                f"/api/v1/groups/{OTHER_GROUP_ID}/{suffix}",
                json={"preset_id": onboarding.PRESET_MODERATION_ID, "revision": "x"},
                headers=self._headers(),
            )
            self.assertEqual(other.status, 403, suffix)

    # ------------------------------------------------------------------
    # 预设 apply
    # ------------------------------------------------------------------
    async def _apply(self, preset_id: str, revision: str | None = None) -> dict:
        body = {"preset_id": preset_id}
        if revision is not None:
            body["revision"] = revision
        else:
            body["revision"] = await self._revision()
        response = await self.client.post(
            f"/api/v1/groups/{GROUP_ID}/preset-apply",
            json=body,
            headers=self._headers(),
        )
        return {"status": response.status, "body": await response.json()}

    async def test_apply_writes_only_the_whitelisted_keys(self) -> None:
        before = await self._snapshot()
        result = await self._apply(onboarding.PRESET_COMMUNITY_ID)
        self.assertEqual(result["status"], 200, result["body"])
        after = await self._snapshot()

        changed = {
            key for key in set(before) | set(after) if before.get(key) != after.get(key)
        }
        self.assertLessEqual(changed, onboarding.PRESET_WRITABLE_KEYS)
        self.assertTrue(changed)
        # 未触及的既有草稿必须原样保留。
        self.assertEqual(after["welcome_message"], "你好")

    async def test_apply_never_touches_protected_surfaces(self) -> None:
        protected = {
            "moderation_rules",
            "rules",
            "actions",
            "thresholds",
            "global_model",
            "api_key",
            "retention",
            "paid_entitlement",
            "av_enabled",
        }
        await self._apply(onboarding.PRESET_COMMUNITY_ID)
        after = await self._snapshot()
        self.assertFalse(set(after) & protected, set(after) & protected)

    async def test_moderation_preset_stays_silent_and_keeps_safety(self) -> None:
        result = await self._apply(onboarding.PRESET_MODERATION_ID)
        self.assertEqual(result["status"], 200, result["body"])
        after = await self._snapshot()
        self.assertTrue(after.get("mute_all_replies"))
        self.assertFalse(after.get("at_reply_mode"))
        # 预设不得触碰审核规则本身。
        self.assertNotIn("moderation_rules", after)
        self.assertNotIn("llm", after)

    async def test_companion_preset_enables_replies_at_only(self) -> None:
        result = await self._apply(onboarding.PRESET_COMPANION_ID)
        self.assertEqual(result["status"], 200, result["body"])
        after = await self._snapshot()
        self.assertNotIn("mute_all_replies", after)
        self.assertTrue(after.get("at_reply_mode"))

    async def test_apply_requires_current_revision(self) -> None:
        stale = await self._apply(onboarding.PRESET_MODERATION_ID, revision="stale")
        self.assertEqual(stale["status"], 409)
        self.assertEqual(stale["body"]["error"]["code"], "group_revision_conflict")
        self.assertNotIn("mute_all_replies", await self._snapshot())

    async def test_apply_returns_the_new_revision(self) -> None:
        before = await self._revision()
        result = await self._apply(onboarding.PRESET_MODERATION_ID)
        self.assertNotEqual(result["body"]["group"]["revision"], before)
        self.assertEqual(await self._revision(), result["body"]["group"]["revision"])

    async def test_repeated_apply_is_stable(self) -> None:
        first = await self._apply(onboarding.PRESET_MODERATION_ID)
        snapshot = await self._snapshot()
        second = await self._apply(onboarding.PRESET_MODERATION_ID)
        self.assertEqual(second["status"], 200, second["body"])
        self.assertEqual(await self._snapshot(), snapshot)
        self.assertEqual(second["body"]["group"]["revision"], first["body"]["group"]["revision"])

    async def test_apply_does_not_clobber_a_concurrent_field_edit(self) -> None:
        """普通保存刚改的字段不能被随后 apply 覆盖（同一 revision + write intent）。"""

        revision = await self._revision()
        async with self.session_factory() as session:
            async with session.begin():
                group = await session.get(Group, GROUP_ID)
                stored = dict(group.settings or {})
                stored["welcome_message"] = "并发草稿"
                group.settings = stored
        stale = await self._apply(onboarding.PRESET_MODERATION_ID, revision=revision)
        self.assertEqual(stale["status"], 409)
        self.assertEqual((await self._snapshot())["welcome_message"], "并发草稿")
        self.assertNotIn("mute_all_replies", await self._snapshot())

    async def test_apply_does_not_create_a_rule_or_a_key(self) -> None:
        await self._apply(onboarding.PRESET_COMMUNITY_ID)
        from bot.db.models import GroupApiModelQuerySecret, ModerationRule

        from sqlalchemy import func

        async with self.session_factory() as session:
            rules = int(
                await session.scalar(
                    select(func.count())
                    .select_from(ModerationRule)
                    .where(ModerationRule.group_id == GROUP_ID)
                )
                or 0
            )
            secrets = int(
                await session.scalar(
                    select(func.count())
                    .select_from(GroupApiModelQuerySecret)
                    .where(GroupApiModelQuerySecret.group_id == GROUP_ID)
                )
                or 0
            )
        self.assertEqual(rules, 0)
        self.assertEqual(secrets, 0)

    async def test_preset_catalog_covers_exactly_three_entries(self) -> None:
        self.assertEqual(len(onboarding.PRESET_IDS), 3)
        self.assertLessEqual(
            set(onboarding.PRESET_WRITABLE_KEYS),
            {"mute_all_replies", "at_reply_mode", "tts_mode"},
        )
        self.assertNotIn("av_enabled", onboarding.PRESET_WRITABLE_KEYS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()