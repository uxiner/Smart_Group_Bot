"""申请人「检查就绪」走**真实**后端（合成宿主）时的诊断合同。

父代理在真实浏览器上看到的缺陷是：点了「检查就绪情况」只显示一行标题，
下面一条诊断都没有 —— 因为 ``POST /api/v1/onboarding/check`` 当时只回
身份/授权/pending，没有 ``readiness``。这里用真实 ``VerifyWebServer`` +
真实 ``init_db`` 的合成宿主钉住这条只读诊断：

* 选预设 → 按**该预设真实需要的功能**诊断；不选 → 用就绪面板同一个默认集合；
* 缺模型凭据 / 机器人缺群权限 → 明确的 ``provider_missing`` /
  ``permission_missing`` 与阻断 token，而不是"就绪"；
* 身份失败（不是该群管理员）→ 403，绝不回诊断；
* 全程**只读**：不写申请、不写授权、不调任何供应商；
* 不泄漏全局 endpoint / apiKey / 别的群。

浏览器那一层（``tools/settings-ui-harness/checks/real-api-check.mjs``）验的是
同一个流程在页面上的呈现；这个文件验的是后端合同本身。
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text

from bot.services import group_onboarding as onboarding
from bot.services import group_costs
from bot.services import llm as llm_module
from bot.services import pricing
from bot.services import usage_scope as scopes
from bot.web.settings_api import configure_price_store, set_price_catalog

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS_DIR = _ROOT / "tools" / "settings-ui-harness"

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_A = -1001234567890
GROUP_B = -1009876543210
ADMIN_A = 7
APPLICANT_A = 9001
MEMBER_A = 8


def _load_host_module():
    """按路径加载合成宿主（``tools/`` 不进镜像，测试里按绝对路径加载）。"""

    if str(_HARNESS_DIR) not in sys.path:
        sys.path.insert(0, str(_HARNESS_DIR))
    spec = importlib.util.spec_from_file_location(
        "real_api_host_check_under_test", _HARNESS_DIR / "real_api_host.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _signed_init_data(user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": f"AAF-check-{user_id}-{int(time.time() * 1000) % 100000}",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class OnboardingCheckDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    """真实宿主上的申请人只读体检。"""

    async def asyncSetUp(self) -> None:
        self.host_module = _load_host_module()
        onboarding.reset_rate_limit()
        self.host = await self.host_module.build_host(listen_port=0)
        self.client = TestClient(TestServer(self.host.app))
        await self.client.start_server()
        self.addAsyncCleanup(self._dispose)

    async def _dispose(self) -> None:
        await self.client.close()
        configure_price_store(None)
        group_costs.reset()
        scopes.reset()
        set_price_catalog(pricing.PriceCatalog(version=1))
        await self.host.close()

    def _headers(self, user_id: int = APPLICANT_A) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    async def _check(self, *, user_id: int = APPLICANT_A, **body) -> tuple[int, dict]:
        response = await self.client.post(
            "/api/v1/onboarding/check",
            json={"group_id": GROUP_A, **body},
            headers=self._headers(user_id),
        )
        return response.status, await response.json()

    async def _request_rows(self) -> list[dict]:
        async with self.host.engine.connect() as conn:
            result = await conn.execute(
                text("SELECT * FROM group_onboarding_requests ORDER BY id")
            )
            return [dict(row) for row in result.mappings().all()]

    async def _authorized_rows(self) -> list[dict]:
        async with self.host.engine.connect() as conn:
            result = await conn.execute(
                text("SELECT * FROM authorized_groups ORDER BY group_id")
            )
            return [dict(row) for row in result.mappings().all()]

    async def test_check_fills_the_panel_and_writes_nothing(self) -> None:
        before_requests = await self._request_rows()
        before_authorized = await self._authorized_rows()
        with patch.object(
            llm_module.litellm, "acompletion", new=AsyncMock()
        ) as completion, patch.object(
            llm_module.litellm, "aembedding", new=AsyncMock()
        ) as embedding:
            status, payload = await self._check()
        self.assertEqual(status, 200, payload)
        readiness = payload["readiness"]
        self.assertTrue(readiness["capabilities"], readiness)
        self.assertEqual(
            readiness["selected_features"], list(onboarding.DEFAULT_READINESS_FEATURES)
        )
        self.assertIs(readiness["remote_probe"], False)
        self.assertEqual(payload["chat_type"], "supergroup")
        self.assertTrue(payload["bot_present"])
        self.assertEqual(completion.await_count, 0)
        self.assertEqual(embedding.await_count, 0)
        # 只读：没有新申请、没有授权、行没被改写。
        self.assertEqual(await self._request_rows(), before_requests)
        self.assertEqual(await self._authorized_rows(), before_authorized)

    async def test_check_uses_the_selected_presets_real_features(self) -> None:
        status, payload = await self._check(preset_id=onboarding.PRESET_COMPANION_ID)
        self.assertEqual(status, 200, payload)
        expected = list(
            onboarding.preset_definition(onboarding.PRESET_COMPANION_ID).features
        )
        self.assertEqual(payload["preset_id"], onboarding.PRESET_COMPANION_ID)
        self.assertEqual(payload["requested_features"], expected)
        self.assertEqual(payload["readiness"]["selected_features"], expected)

    async def test_check_reports_permission_missing_when_permissions_are_flipped(
        self,
    ) -> None:
        """翻转合成机器人权限 → 诊断必须变成明确的缺权，而不是"就绪"。"""

        flipped = await self.client.post(
            "/__synthetic/telegram-permissions",
            json={"permissions": {"can_delete_messages": False}},
        )
        self.assertEqual(flipped.status, 200, await flipped.text())
        status, payload = await self._check(
            preset_id=onboarding.PRESET_MODERATION_ID
        )
        self.assertEqual(status, 200, payload)
        readiness = payload["readiness"]
        moderation = next(
            item
            for item in readiness["capabilities"]
            if item["capability"] == "moderation"
        )
        self.assertEqual(moderation["state"], "permission_missing")
        self.assertIn("moderation:permission_missing", readiness["blockers"])
        self.assertFalse(readiness["ready"])
        self.assertEqual(payload["missing_bot_permissions"], ["can_delete_messages"])
        # 复位后同一份体检回到不阻断。
        reset = await self.client.post("/__synthetic/reset")
        self.assertEqual(reset.status, 200, await reset.text())
        status, payload = await self._check(
            preset_id=onboarding.PRESET_MODERATION_ID
        )
        self.assertEqual(status, 200, payload)
        self.assertNotIn(
            "moderation:permission_missing", payload["readiness"]["blockers"]
        )

    async def test_check_denies_a_member_and_returns_no_diagnosis(self) -> None:
        status, payload = await self._check(user_id=MEMBER_A)
        self.assertEqual(status, 403, payload)
        raw = json.dumps(payload)
        self.assertNotIn("readiness", raw)
        self.assertNotIn("capabilities", raw)

    async def test_check_never_leaks_global_config_or_the_other_group(self) -> None:
        status, payload = await self._check(preset_id=onboarding.PRESET_COMMUNITY_ID)
        self.assertEqual(status, 200, payload)
        raw = json.dumps(payload)
        for forbidden in (
            "api_key",
            "api_base",
            "fixture-key",
            "gateway.example.invalid",
            BOT_TOKEN,
            str(GROUP_B),
            "synthetic_fixture_bot",
        ):
            self.assertNotIn(forbidden, raw, forbidden)

    async def test_host_reset_clears_the_process_rate_limit_so_a_second_run_is_not_429(
        self,
    ) -> None:
        """宿主 reset 必须清掉**进程级**限次表。

        检查端点的限次表是内存里的（每个申请人 10 次/60 秒），删合成行清不掉它。
        浏览器套件给同一个申请人发 9 次 check，连跑第二轮时第 10 次之后就会被
        429 拒掉 —— 看上去像产品缺陷，其实是套件自己的状态残留。这里先真的把
        限次用满（证明限次本身还在生效，没被关掉），再 reset，然后必须又能查。
        """

        for index in range(onboarding.RATE_LIMIT_MAX_CALLS):
            status, payload = await self._check()
            self.assertEqual(status, 200, f"第 {index + 1} 次：{payload}")

        status, payload = await self._check()
        self.assertEqual(status, 429, payload)
        self.assertEqual(payload.get("error", {}).get("code"), "rate_limited")

        reset = await self.client.post("/__synthetic/reset")
        self.assertEqual(reset.status, 200, await reset.text())

        status, payload = await self._check()
        self.assertEqual(status, 200, payload)
        self.assertEqual(len(payload["readiness"]["capabilities"]), 9)
