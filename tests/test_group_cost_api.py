"""成本 / 预算 API 的权限与诚实性（Task 7 邻接）。

* 本群管理员只看得到本群；另一个群的申请人不许读 A 群成本。
* 未启用的预算必须显示成「未启用」，**不得**显示成正在保护。
* 缺单价的金额是 null，不是 0；不同币种不相加。
* 改价不追改历史：同一批 token 在两版价格下算出的钱按各自版本算。
* 普通群管理员不能改预算（仅超管），也不能改价格目录。
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
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Admin, AuthorizedGroup, LlmCostDaily
from bot.services import pricing
from bot.services import usage_scope as scopes
from bot.services import group_costs
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer
from bot.utils.timezone import now_shanghai_naive

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_A = -1001234567890
GROUP_B = -1009876543210
ADMIN_A = 7


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-cost-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def _telegram() -> SimpleNamespace:
    async def get_chat(chat_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=int(chat_id), type="supergroup")

    async def get_chat_administrators(chat_id: int):
        return [
            SimpleNamespace(
                user=SimpleNamespace(id=ADMIN_A, is_bot=False), status="administrator"
            ),
            SimpleNamespace(
                user=SimpleNamespace(id=SUPER_ADMIN_ID, is_bot=False),
                status="administrator",
            ),
        ]

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


class CostBudgetApiTests(unittest.IsolatedAsyncioTestCase):
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
            config_master_key="cost-api-test-key",
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
            legacy_config_path="/tmp/nonexistent-cost-api.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        from bot.web.auth import reset_init_data_replay_budget

        reset_init_data_replay_budget()
        scopes.reset()
        group_costs.reset()
        # 价格目录是模块级状态：上个用例 PUT 过的话，这里必须回到空目录，
        # 否则「未配置单价」的判定会被上一个用例的副作用污染。
        from bot.web.settings_api import set_price_catalog

        set_price_catalog(pricing.PriceCatalog(version=1))
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    AuthorizedGroup(group_id=GROUP_A, authorized_by=SUPER_ADMIN_ID)
                )
                session.add(
                    AuthorizedGroup(group_id=GROUP_B, authorized_by=SUPER_ADMIN_ID)
                )
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    Admin(group_id=GROUP_A, user_id=ADMIN_A, role="admin")
                )
        await self._seed_usage()

    async def _seed_usage(self) -> None:
        today = now_shanghai_naive().date().isoformat()
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    LlmCostDaily(
                        usage_date=today,
                        scope="group",
                        group_id=GROUP_A,
                        purpose="optional",
                        stage="main",
                        provider="openai_compatible",
                        model="fixture/model-a",
                        attempts=4,
                        successes=3,
                        failures=1,
                        timeouts=0,
                        empty_responses=0,
                        parse_errors=0,
                        usage_unknown=1,
                        prompt_tokens=1000,
                        output_tokens=200,
                        cached_tokens=300,
                        cache_write_tokens=None,
                        thinking_tokens=0,
                    )
                )
                session.add(
                    LlmCostDaily(
                        usage_date=today,
                        scope="group",
                        group_id=GROUP_B,
                        purpose="optional",
                        stage="main",
                        provider="openai_compatible",
                        model="fixture/model-a",
                        attempts=99,
                        successes=99,
                        prompt_tokens=1,
                        output_tokens=1,
                        cached_tokens=0,
                        cache_write_tokens=0,
                        thinking_tokens=0,
                    )
                )

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _boot(self) -> None:
        server = VerifyWebServer(
            bot=_telegram(),
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()

    def _headers(self, user_id: int) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    # ------------------------------------------------------------------
    async def test_group_admin_reads_only_its_own_costs(self) -> None:
        await self._boot()
        mine = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/costs?days=7", headers=self._headers(ADMIN_A)
        )
        self.assertEqual(mine.status, 200)
        payload = await mine.json()
        self.assertEqual(payload["group_id"], GROUP_A)
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(int(payload["rows"][0]["attempts"]), 4)
        # 未知项原样是 null，绝不是 0。
        self.assertIsNone(payload["rows"][0]["cache_write_tokens"])

        other = await self.client.get(
            f"/api/v1/groups/{GROUP_B}/costs", headers=self._headers(ADMIN_A)
        )
        self.assertEqual(other.status, 403)

    async def test_snapshot_failure_is_a_503_without_numbers(self) -> None:
        """建立不了一致读快照 → 503（前端显示"状态未知"），绝不返回金额与次数。"""

        await self._boot()

        async def _boom(session) -> bool:
            raise group_costs.ReportSnapshotUnavailable("快照建不起来")

        with patch.object(group_costs, "_open_report_read_snapshot", _boom):
            response = await self.client.get(
                f"/api/v1/groups/{GROUP_A}/costs?days=7",
                headers=self._headers(ADMIN_A),
            )
        self.assertEqual(response.status, 503)
        payload = await response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["error"]["code"], "cost_report_snapshot_unavailable"
        )
        # 关键：没有数字。金额与次数一个都不许给出去。
        for field in ("rows", "totals", "currencies", "reconciliation"):
            self.assertNotIn(field, payload)

    async def test_costs_window_is_bounded(self) -> None:
        await self._boot()
        for bad in ("0", "91", "abc"):
            response = await self.client.get(
                f"/api/v1/groups/{GROUP_A}/costs?days={bad}",
                headers=self._headers(ADMIN_A),
            )
            self.assertEqual(response.status, 400, bad)

    async def test_budget_reports_not_protecting_when_disabled(self) -> None:
        await self._boot()
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/budget", headers=self._headers(ADMIN_A)
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertFalse(payload["budget"]["enabled"])
        self.assertFalse(payload["budget"]["protecting"])

    async def test_group_admin_cannot_change_budget_or_prices(self) -> None:
        await self._boot()
        budget_write = await self.client.put(
            f"/api/v1/groups/{GROUP_A}/budget",
            json={"revision": 0, "enabled": True, "daily_attempt_limit": 1},
            headers=self._headers(ADMIN_A),
        )
        self.assertEqual(budget_write.status, 403)
        price_write = await self.client.put(
            "/api/v1/prices",
            json={"entries": []},
            headers=self._headers(ADMIN_A),
        )
        self.assertEqual(price_write.status, 403)

    async def test_super_admin_enables_budget_and_sees_it_protecting(self) -> None:
        await self._boot()
        response = await self.client.put(
            f"/api/v1/groups/{GROUP_A}/budget",
            json={"revision": 0, "enabled": True, "daily_attempt_limit": 10},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(response.status, 200, await response.text())
        payload = await response.json()
        self.assertTrue(payload["budget"]["enabled"])
        self.assertTrue(payload["budget"]["protecting"])
        self.assertEqual(payload["budget"]["limits"]["daily_attempt_limit"], 10)

    async def test_money_cap_without_prices_is_refused_with_a_clear_reason(self) -> None:
        await self._boot()
        response = await self.client.put(
            f"/api/v1/groups/{GROUP_A}/budget",
            json={
                "revision": 0,
                "enabled": True,
                "currency": "USD",
                "daily_money_micro_limit": 100,
                "money_covers": [
                    {"provider_key": "openai_compatible", "model": "fixture/model-a"}
                ],
            },
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "invalid_budget")

    async def test_price_catalog_is_super_admin_only_and_versioned(self) -> None:
        await self._boot()
        first = await self.client.get("/api/v1/prices", headers=self._headers(SUPER_ADMIN_ID))
        self.assertEqual(first.status, 200)
        before = int((await first.json())["catalog"]["version"])

        updated = await self.client.put(
            "/api/v1/prices",
            json={
                "expected_version": before,
                "entries": [
                    {
                        "provider_key": "openai_compatible",
                        "model": "fixture/model-a",
                        "currency": "USD",
                        "input_per_million": "3",
                        "output_per_million": "15",
                        "cache_read_per_million": "1",
                        "cache_write_per_million": "4",
                    }
                ],
            },
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(updated.status, 200, await updated.text())
        catalog = (await updated.json())["catalog"]
        self.assertEqual(int(catalog["version"]), before + 1)
        self.assertNotIn("api_key", json.dumps(catalog).lower())

        # 定价不完整时金额是 null，不是 0。
        partial = pricing.PriceCatalog(
            version=catalog["version"],
            entries=(
                pricing.PriceEntry(
                    provider_key="openai_compatible",
                    model="fixture/model-a",
                    currency="USD",
                    input_per_million="3",
                ),
            ),
        )
        cost = pricing.estimate_for(
            partial,
            provider_key="openai_compatible",
            model="fixture/model-a",
            prompt_tokens=1000,
            output_tokens=200,
            cached_tokens=300,
        )
        self.assertIsNone(cost.micro)

    async def test_ledger_query_matches_the_detail_table_only(self) -> None:
        await self._boot()
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/costs", headers=self._headers(SUPER_ADMIN_ID)
        )
        payload = await response.json()
        self.assertEqual(payload["source_table"], "llm_cost_daily")
        self.assertIn("llm_usage_daily", payload["source_note"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()