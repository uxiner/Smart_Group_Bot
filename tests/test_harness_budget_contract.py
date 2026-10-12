"""harness 的预算回包必须是**真实 API** 的形状，不是我们发明的另一套。

为什么这条要单独钉住：真实后端的预算 GET 把三个限额和 ``operation_limits``
**嵌在 ``limits`` 里**，而 ``money_covers`` 根本不在回包里（它只用于一次保存的
校验）。harness 曾经把同一份数据摊在**顶层**（``daily_attempt_limit`` …），
于是浏览器检查在一个真实后端上并不存在的形状上全绿 —— 而真实页面上「重新读取」
之后所有额度显示为空，接着一次只改 token 的保存会把 attempts 和 operation_limits
清成 null / {}。额度就这么被静默撤掉了。

所以这里做两件事：

1. 用**真实** ``VerifyWebServer`` + 真实 ``init_db`` 拿一份真快照，逐层比对
   harness 回包的键集合；形状漂移直接红。
2. 对 harness 真实发 HTTP，验证它和真实后端**同为替换语义**：一次只带 token 的
   PUT 会把没带的额度清掉（因此前端必须提交完整草稿）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Admin, AuthorizedGroup
from bot.services import group_costs
from bot.services import pricing
from bot.services import usage_scope as scopes
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS_DIR = _ROOT / "tools" / "settings-ui-harness"

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_A = -1001234567890
ADMIN_A = 7

#: 前端「按群预算」编辑器实际渲染/提交的顶层字段。harness 和真实后端都必须
#: 接受这一组，多一个少一个都算合同漂移。
_BUDGET_BODY_FIELDS = {
    "revision",
    "enabled",
    "currency",
    "daily_attempt_limit",
    "daily_token_limit",
    "daily_money_micro_limit",
    "operation_limits",
    "money_covers",
}


def _load_harness_module():
    """按路径加载 harness（它自己会把所在目录塞进 ``sys.path``）。"""

    import importlib.util

    if str(_HARNESS_DIR) not in sys.path:
        sys.path.insert(0, str(_HARNESS_DIR))
    spec = importlib.util.spec_from_file_location(
        "settings_ui_harness_under_test", _HARNESS_DIR / "harness.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _signed_init_data(user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF-harness-budget-contract",
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
            )
        ]

    async def get_chat_member(chat_id: int, user_id: int) -> SimpleNamespace:
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


class RealBudgetSnapshotShapeTests(unittest.IsolatedAsyncioTestCase):
    """真实后端：``GET /budget`` 到底长什么样。"""

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
            config_master_key="harness-contract-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-harness-contract.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        from bot.web.auth import reset_init_data_replay_budget
        from bot.web.settings_api import set_price_catalog

        reset_init_data_replay_budget()
        scopes.reset()
        group_costs.reset()
        set_price_catalog(pricing.PriceCatalog(version=1))
        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    AuthorizedGroup(group_id=GROUP_A, authorized_by=SUPER_ADMIN_ID)
                )
        async with self.session_factory() as session:
            async with session.begin():
                session.add(Admin(group_id=GROUP_A, user_id=ADMIN_A, role="admin"))
        server = VerifyWebServer(
            bot=_telegram(),
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

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(SUPER_ADMIN_ID)}"}

    async def _put(self, body: dict):
        return await self.client.put(
            f"/api/v1/groups/{GROUP_A}/budget", json=body, headers=self._headers()
        )

    async def _get(self) -> dict:
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/budget", headers=self._headers()
        )
        self.assertEqual(response.status, 200)
        return (await response.json())["budget"]

    async def test_snapshot_nests_limits_and_hides_money_covers(self) -> None:
        response = await self._put(
            {
                "revision": 0,
                "enabled": True,
                "currency": "USD",
                "daily_attempt_limit": 9,
                "daily_token_limit": 20,
                "daily_money_micro_limit": None,
                "operation_limits": {"tts": 3},
                "money_covers": [],
            }
        )
        self.assertEqual(response.status, 200, await response.text())
        snapshot = await self._get()
        # 三个限额与 operation_limits 在 limits 里，**不是**顶层。
        self.assertEqual(
            set(snapshot["limits"]),
            {
                "daily_attempt_limit",
                "daily_token_limit",
                "daily_money_micro_limit",
                "operation_limits",
            },
        )
        self.assertEqual(snapshot["limits"]["daily_attempt_limit"], 9)
        self.assertEqual(snapshot["limits"]["daily_token_limit"], 20)
        self.assertEqual(snapshot["limits"]["operation_limits"], {"tts": 3})
        for flat in (
            "daily_attempt_limit",
            "daily_token_limit",
            "daily_money_micro_limit",
            "operation_limits",
        ):
            self.assertNotIn(flat, snapshot, flat)
        # money_covers 不回读：它只用于那一次保存的校验。
        self.assertNotIn("money_covers", snapshot)

    async def test_put_replaces_every_limit_it_omits(self) -> None:
        """真实后端是**整份替换**：没带的额度就是 null。

        这条不是缺陷，是合同 —— 它正是前端必须提交完整草稿的原因。
        """

        first = await self._put(
            {
                "revision": 0,
                "enabled": True,
                "currency": "USD",
                "daily_attempt_limit": 9,
                "daily_token_limit": 20,
                "operation_limits": {"tts": 3},
            }
        )
        self.assertEqual(first.status, 200, await first.text())
        revision = (await first.json())["budget"]["revision"]
        second = await self._put({"revision": revision, "enabled": True, "daily_token_limit": 17})
        self.assertEqual(second.status, 200, await second.text())
        snapshot = await self._get()
        self.assertEqual(snapshot["limits"]["daily_token_limit"], 17)
        self.assertIsNone(snapshot["limits"]["daily_attempt_limit"])
        self.assertEqual(snapshot["limits"]["operation_limits"], {})

    async def test_money_covers_map_is_rejected_array_is_accepted(self) -> None:
        bad = await self._put(
            {
                "revision": 0,
                "enabled": True,
                "currency": "USD",
                "daily_money_micro_limit": 1000,
                "money_covers": {"fixture": 1},
            }
        )
        self.assertEqual(bad.status, 400)
        self.assertEqual((await bad.json())["error"]["code"], "invalid_budget_field")

    async def test_real_api_rejects_unknown_field(self) -> None:
        response = await self._put(
            {"revision": 0, "enabled": False, "daily_attempt_limit": 1, "nope": 1}
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "unknown_budget_field")


class HarnessBudgetContractTests(unittest.IsolatedAsyncioTestCase):
    """harness 必须复刻上面那份真合同，否则浏览器证据没有意义。"""

    async def asyncSetUp(self) -> None:
        self.harness = await asyncio.to_thread(_load_harness_module)
        self.fixture = json.loads(
            (self.harness.FIXTURE).read_text(encoding="utf-8")
        )
        self.servers: list = []

    async def asyncTearDown(self) -> None:
        for server in self.servers:
            await asyncio.to_thread(server.shutdown)
            await asyncio.to_thread(server.server_close)

    async def _serve(self, **state_kwargs) -> str:
        fixture = self.fixture
        state = self.harness.State(
            fixture,
            group_admin=state_kwargs.pop("group_admin", False),
            fail_save=state_kwargs.pop("fail_save", False),
            no_groups=False,
            **state_kwargs,
        )
        handler = type("BoundHandler", (self.harness.Handler,), {"state": state})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    async def _call(self, base: str, method: str, path: str, body: dict | None = None):
        def _do():
            data = None if body is None else json.dumps(body).encode()
            request = urllib.request.Request(
                base + path, data=data, method=method
            )
            if data is not None:
                request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read().decode())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode())

        return await asyncio.to_thread(_do)

    async def test_harness_document_matches_real_snapshot_keys(self) -> None:
        base = await self._serve(
            budget_seed={
                "enabled": True,
                "currency": "USD",
                "attempt": 9,
                "token": 20,
                "operation_limits": {"tts": 3},
            }
        )
        status, payload = await self._call(
            base, "GET", f"/api/v1/groups/{GROUP_A}/budget"
        )
        self.assertEqual(status, 200)
        snapshot = payload["budget"]
        self.assertNotIn("money_covers", snapshot)
        for flat in (
            "daily_attempt_limit",
            "daily_token_limit",
            "daily_money_micro_limit",
            "operation_limits",
        ):
            self.assertNotIn(flat, snapshot, flat)
        self.assertEqual(
            set(snapshot["limits"]),
            {
                "daily_attempt_limit",
                "daily_token_limit",
                "daily_money_micro_limit",
                "operation_limits",
            },
        )
        self.assertEqual(snapshot["limits"]["daily_attempt_limit"], 9)
        self.assertEqual(snapshot["limits"]["daily_token_limit"], 20)
        self.assertEqual(snapshot["limits"]["operation_limits"], {"tts": 3})
        # today 也要是真实 DayState.document() 的键。
        self.assertEqual(
            set(snapshot["today"]),
            {
                "group_id",
                "budget_date",
                "used_attempts",
                "used_tokens",
                "used_money_micro",
                "unknown_attempts",
                "used_operations",
            },
        )

    async def test_harness_put_is_replace_semantics_like_the_real_api(self) -> None:
        base = await self._serve(
            budget_seed={
                "enabled": True,
                "currency": "USD",
                "attempt": 9,
                "token": 20,
                "operation_limits": {"tts": 3},
            }
        )
        status, _ = await self._call(
            base,
            "PUT",
            f"/api/v1/groups/{GROUP_A}/budget",
            {"revision": 1, "enabled": True, "daily_token_limit": 17},
        )
        self.assertEqual(status, 200)
        status, payload = await self._call(
            base, "GET", f"/api/v1/groups/{GROUP_A}/budget"
        )
        limits = payload["budget"]["limits"]
        self.assertEqual(limits["daily_token_limit"], 17)
        self.assertIsNone(limits["daily_attempt_limit"])
        self.assertEqual(limits["operation_limits"], {})

    async def test_harness_rejects_money_covers_map_and_accepts_array(self) -> None:
        base = await self._serve()
        status, payload = await self._call(
            base,
            "PUT",
            f"/api/v1/groups/{GROUP_A}/budget",
            {
                "revision": 0,
                "enabled": True,
                "currency": "USD",
                "daily_money_micro_limit": 1000,
                "money_covers": {"fixture": 1},
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_budget_field")

        status, _ = await self._call(
            base,
            "PUT",
            f"/api/v1/groups/{GROUP_A}/budget",
            {
                "revision": 0,
                "enabled": True,
                "currency": "USD",
                "daily_money_micro_limit": 1000,
                "money_covers": [
                    {"provider_key": "fixture", "model": "gpt-4o-mini"}
                ],
            },
        )
        self.assertEqual(status, 200)

    async def test_harness_money_without_currency_is_invalid_budget(self) -> None:
        base = await self._serve()
        status, payload = await self._call(
            base,
            "PUT",
            f"/api/v1/groups/{GROUP_A}/budget",
            {
                "revision": 0,
                "enabled": True,
                "currency": "",
                "daily_money_micro_limit": 1000,
                "money_covers": [{"provider_key": "fixture", "model": "gpt-4o-mini"}],
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_budget")

    async def test_harness_revision_conflict_is_the_real_error_code(self) -> None:
        base = await self._serve()
        status, payload = await self._call(
            base,
            "PUT",
            f"/api/v1/groups/{GROUP_A}/budget",
            {"revision": 99, "enabled": False},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "budget_revision_conflict")

    async def test_harness_rejects_unknown_field_and_bad_limits(self) -> None:
        base = await self._serve()
        status, payload = await self._call(
            base,
            "PUT",
            f"/api/v1/groups/{GROUP_A}/budget",
            {"revision": 0, "enabled": False, "nope": 1},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "unknown_budget_field")

        for bad in (1.5, "17.5", True, -3):
            status, payload = await self._call(
                base,
                "PUT",
                f"/api/v1/groups/{GROUP_A}/budget",
                {"revision": 0, "enabled": False, "daily_token_limit": bad},
            )
            self.assertEqual(status, 400, bad)
            self.assertEqual(payload["error"]["code"], "invalid_budget", bad)

        # 真实后端接受纯十进制字符串（strict_int 的口径），harness 不许更严。
        status, _ = await self._call(
            base,
            "PUT",
            f"/api/v1/groups/{GROUP_A}/budget",
            {"revision": 0, "enabled": False, "daily_token_limit": "17"},
        )
        self.assertEqual(status, 200)

    def _payload_literal(self) -> str:
        source = (_ROOT / "bot" / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        start = source.index("  function collectBudgetDraft(")
        payload_at = source.index("payload: {", start)
        body = source[payload_at + len("payload: {") :]
        depth = 1
        for index, char in enumerate(body):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return body[:index]
        raise AssertionError("collectBudgetDraft 的 payload 字面量没有闭合")

    async def test_frontend_sends_money_covers_as_the_real_array_shape(self) -> None:
        """真实 API 只收 ``[{provider_key, model}]``；发对象映射直接 400。

        旧实现发的是 ``Object.fromEntries(… [row.key, Number(row.value)])`` ——
        一个 ``{provider_key: number}`` 映射，服务端连解析都过不去。
        """

        literal = self._payload_literal()
        self.assertIn("provider_key", literal)
        self.assertIn("model", literal)
        self.assertNotIn(
            "money_covers: Object.fromEntries(",
            literal,
            "money_covers 又被发成了对象映射",
        )
        # 两个模型字段都是**文本**，不能被 Number() 吞掉（"gpt-4o" → NaN）。
        covers_expr = literal[literal.index("money_covers:") :]
        self.assertNotIn("Number(", covers_expr)
        self.assertIn("model: row.value", covers_expr)

    async def test_frontend_reads_limits_from_the_nested_real_snapshot(self) -> None:
        """真实快照把限额放在 ``limits`` 里；从顶层读会全部显示为空。"""

        source = (_ROOT / "bot" / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        start = source.index("  function budgetDraftFromServer(")
        end = source.index("\n  function ", start + 10)
        body = source[start:end]
        self.assertIn("budget.limits", body)
        self.assertIn("limits.daily_attempt_limit", body)
        self.assertIn("limits.daily_token_limit", body)
        self.assertIn("limits.daily_money_micro_limit", body)
        self.assertIn("limits.operation_limits", body)
        for flat in (
            "budget.daily_attempt_limit",
            "budget.daily_token_limit",
            "budget.daily_money_micro_limit",
            "budget.operation_limits",
        ):
            self.assertNotIn(flat, body, flat)
        # money_covers 不在真实回包里；只允许按**数组**读，绝不按对象读。
        self.assertNotIn("budget.money_covers || {}", body)
        self.assertIn("Array.isArray(budget?.money_covers)", body)

    async def test_frontend_payload_fields_are_all_accepted_by_the_harness(self) -> None:
        """前端真正提交的字段集合 == harness（与真实后端）接受的集合。"""

        import re as _re

        literal = self._payload_literal()
        keys = set(
            _re.findall(r"^\s{8}([A-Za-z_][A-Za-z0-9_]*):", literal, _re.M)
        )
        self.assertEqual(keys, _BUDGET_BODY_FIELDS, literal)

    async def test_harness_accepts_exactly_those_fields_and_rejects_others(self) -> None:
        base = await self._serve()
        probe = {
            "revision": 0,
            "enabled": False,
            "currency": "USD",
            "daily_attempt_limit": None,
            "daily_token_limit": None,
            "daily_money_micro_limit": None,
            "operation_limits": {},
            "money_covers": [],
        }
        status, payload = await self._call(
            base, "PUT", f"/api/v1/groups/{GROUP_A}/budget", dict(probe)
        )
        self.assertEqual(status, 200, payload)

        # 上面那次 200 不能是"什么都收"换来的：多一个字段必须被明确拒绝。
        extra = dict(probe, bogus_field=1)
        status, payload = await self._call(
            base, "PUT", f"/api/v1/groups/{GROUP_A}/budget", extra
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "unknown_budget_field")
