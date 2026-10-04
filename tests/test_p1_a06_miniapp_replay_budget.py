"""A-06 缓解：Mini App 的 initData 不能再当无限重放的 bearer 用。

报告事实：``bot/web/auth.py`` 验签后**不签发任何会话**，无 nonce / 设备绑定 /
重放记录，``MAX_INIT_DATA_AGE_SECONDS = 600`` 是唯一的时效约束；面板接口也没有
任何失败计数或限流。

本批只做**最小可行缓解**，不动前端协议：验签 + 时效通过后，按 initData 的
SHA-256 记一份服务端重放预算（滑动窗口 + 总次数上限 + 有界表），用尽后回 429。

一次性 nonce 在这里**不可用**——面板一次打开会并发打出 ``/api/v1/session``、
``/api/v1/groups`` 和每个群的 8 类资源，共 ~10 个请求，全部带同一份
``tg.initData`` 字符串（``app.js:419`` 每次请求现取，但值在页面生命周期内不变）。
一次性 nonce 会让后 9 个直接 401，把面板打死。完整方案（换发自有会话）见
``FIX-p1-upstream.md``。
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
from unittest.mock import patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer
from bot.web import auth

BOT_TOKEN = "42:TEST_TOKEN"


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-a06-test",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class ReplayBudgetTests(unittest.IsolatedAsyncioTestCase):
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
            config_master_key="a06-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8483
        self.settings.join_verification_public_base_url = self.settings.miniapp_public_base_url
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-a06.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        server = VerifyWebServer(
            bot=SimpleNamespace(token=BOT_TOKEN),
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()
        self.saved_limits = (
            getattr(auth, "INIT_DATA_WINDOW_REQUESTS", None),
            getattr(auth, "INIT_DATA_MAX_REQUESTS", None),
        )
        reset = getattr(auth, "reset_init_data_replay_budget", None)
        if callable(reset):
            reset()

    async def asyncTearDown(self) -> None:
        for name, saved in zip(
            ("INIT_DATA_WINDOW_REQUESTS", "INIT_DATA_MAX_REQUESTS"), self.saved_limits
        ):
            if saved is not None:
                setattr(auth, name, saved)
        reset = getattr(auth, "reset_init_data_replay_budget", None)
        if callable(reset):
            reset()
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    @staticmethod
    def _require_budget_api() -> None:
        if not hasattr(auth, "INIT_DATA_WINDOW_REQUESTS"):
            raise AssertionError(
                "bot.web.auth 没有 initData 重放预算：initData 仍是 10 分钟内可无限"
                "重放、不可撤销的 bearer（A-06）"
            )

    async def test_panel_burst_of_parallel_requests_is_not_throttled(self) -> None:
        headers = {"Authorization": f"tma {_signed_init_data(42)}"}
        # A single panel open fires this many requests with the same initData.
        responses = await asyncio_gather(
            self.client.get("/api/v1/session", headers=headers) for _ in range(10)
        )

        statuses = {response.status for response in responses}
        self.assertEqual(
            statuses,
            {200},
            "面板一次打开会并发打出多个请求，一次性 nonce 会把它们打死",
        )
        for response in responses:
            response.close()

    async def test_replayed_init_data_runs_out_of_budget(self) -> None:
        self._require_budget_api()
        auth.INIT_DATA_WINDOW_REQUESTS = 5
        auth.INIT_DATA_MAX_REQUESTS = 8
        headers = {"Authorization": f"tma {_signed_init_data(42)}"}

        statuses = []
        for _ in range(12):
            response = await self.client.get("/api/v1/session", headers=headers)
            statuses.append(response.status)
            response.close()

        self.assertEqual(
            statuses[:5],
            [200] * 5,
            f"预算之内必须照常放行，实际：{statuses}",
        )
        self.assertEqual(
            set(statuses[5:]),
            {429},
            f"预算用尽后必须拒绝，实际：{statuses}",
        )

    async def test_budget_is_per_init_data(self) -> None:
        self._require_budget_api()
        auth.INIT_DATA_WINDOW_REQUESTS = 1
        auth.INIT_DATA_MAX_REQUESTS = 1
        first = {"Authorization": f"tma {_signed_init_data(42)}"}
        second = {
            "Authorization": f"tma {_signed_init_data(42, auth_date=int(time.time()) - 1)}"
        }

        first_ok = await self.client.get("/api/v1/session", headers=first)
        first_ok.close()
        first_repeat = await self.client.get("/api/v1/session", headers=first)
        first_repeat.close()
        other = await self.client.get("/api/v1/session", headers=second)
        other.close()

        self.assertEqual(first_ok.status, 200)
        self.assertEqual(first_repeat.status, 429)
        self.assertEqual(other.status, 200, "另一枚 initData 有自己的预算")

    async def test_forged_init_data_never_spends_the_budget(self) -> None:
        self._require_budget_api()
        auth.INIT_DATA_WINDOW_REQUESTS = 5
        auth.INIT_DATA_MAX_REQUESTS = 5
        valid = {"Authorization": f"tma {_signed_init_data(42)}"}
        for _ in range(50):
            forged = await self.client.get(
                "/api/v1/session",
                headers={"Authorization": f"tma {_signed_init_data(42, auth_date=1)}"},
            )
            forged.close()

        for _ in range(5):
            response = await self.client.get("/api/v1/session", headers=valid)
            response.close()

        self.assertEqual(
            auth._INIT_DATA_REPLAY_LEDGER[auth._init_data_digest(
                str(valid["Authorization"]).split(None, 1)[1]
            )][1],
            5,
            "伪造的 initData 在验签阶段就被拒，不该消耗合法用户的预算",
        )

    async def test_ledger_is_bounded(self) -> None:
        self._require_budget_api()
        with patch.object(auth, "INIT_DATA_LEDGER_MAX_ENTRIES", 16):
            for index in range(200):
                auth.consume_init_data_budget(f"init-data-{index}")

        self.assertLessEqual(
            len(auth._INIT_DATA_REPLAY_LEDGER),
            16,
            "预算表本身不能变成新的无界内存增长点",
        )

    async def test_ledger_never_stores_the_credential(self) -> None:
        self._require_budget_api()
        init_data = _signed_init_data(42)
        auth.consume_init_data_budget(init_data)

        stored = list(auth._INIT_DATA_REPLAY_LEDGER)
        self.assertEqual(
            stored,
            [hashlib.sha256(init_data.encode()).digest()],
            "预算表只能存摘要，绝不能把 bearer 本身留在内存里",
        )


async def asyncio_gather(coros: list) -> list:
    import asyncio

    return list(await asyncio.gather(*coros))
