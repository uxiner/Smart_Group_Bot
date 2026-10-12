"""价格目录 HTTP API 的**期望版本 CAS**（真实 Web 服务 + 真实 SQLite）。

背景：旧的 ``PUT /api/v1/prices`` 总是写
``version = 进程内缓存版本 + 1``。两个会话都读到 v3 时都能"成功"：A 写 v4
（input=33），B 拿着过期草稿写 v5 并把 input 悄悄改回 1。这里验证 CAS 改为
读**数据库**里的最新版本，过期写入必须 409 且不得改动任何字段。

覆盖：``expected_version`` / 未知顶层字段 / 全新库版本往返 / 过期写入 /
同基线并发恰一个赢家 / 重启后 CAS 基线仍来自数据库 / 普通群管理员 403。
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy import func, select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import ModelPriceCatalog
from bot.services import pricing
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
NORMAL_ADMIN_ID = 7


def _signed_init_data(user_id: int, *, auth_date: int | None = None) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-price-cas-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "Owner"}, separators=(",", ":")
        ),
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
                user=SimpleNamespace(id=NORMAL_ADMIN_ID, is_bot=False),
                status="administrator",
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


def _entry(
    *,
    model: str = "fixture/model-a",
    input_per_million: object = "3",
    output_per_million: object = "15",
) -> dict[str, object]:
    return {
        "provider_key": "openai_compatible",
        "model": model,
        "currency": "USD",
        "input_per_million": input_per_million,
        "output_per_million": output_per_million,
    }


class PriceCatalogCasTests(unittest.IsolatedAsyncioTestCase):
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
            config_master_key="price-cas-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8484
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-price-cas.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        from bot.web.auth import reset_init_data_replay_budget

        reset_init_data_replay_budget()
        from bot.web.settings_api import set_price_catalog

        # 价格目录是模块级状态：每个用例都从「空库基线 version=1」开始，
        # 避免上一个用例的 PUT 副作用污染。
        set_price_catalog(pricing.PriceCatalog(version=1))

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

    async def _get_catalog(self) -> dict:
        response = await self.client.get(
            "/api/v1/prices", headers=self._headers(SUPER_ADMIN_ID)
        )
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["catalog"]

    async def _put(self, payload: dict):
        return await self.client.put(
            "/api/v1/prices", json=payload, headers=self._headers(SUPER_ADMIN_ID)
        )

    async def _durable_max_version(self) -> int | None:
        async with self.session_factory() as session:
            value = (
                await session.execute(select(func.max(ModelPriceCatalog.version)))
            ).scalar()
        return None if value is None else int(value)

    # ------------------------------------------------------------------
    async def test_expected_version_must_be_an_explicit_strict_integer(self) -> None:
        await self._boot()
        missing = {"entries": []}
        bad_values = {
            "null": None,
            "false": False,
            "true": True,
            "float": 3.0,
            "nan": float("nan"),
            "numeric-string": "3",
            "negative": -1,
            "zero": 0,
        }
        cases = [("missing", missing)] + [
            (name, {"expected_version": value, "entries": []})
            for name, value in bad_values.items()
        ]
        for name, payload in cases:
            response = await self._put(payload)
            self.assertEqual(response.status, 400, name)
            self.assertEqual(
                (await response.json())["error"]["code"],
                "invalid_expected_version",
                name,
            )

    async def test_unknown_top_level_fields_are_rejected(self) -> None:
        await self._boot()
        for key in ("currency", "version", "foo"):
            payload = {"expected_version": 1, "entries": [], key: "x"}
            response = await self._put(payload)
            self.assertEqual(response.status, 400, key)
            self.assertEqual(
                (await response.json())["error"]["code"], "unknown_price_field", key
            )

    async def test_fresh_store_round_trips_entries_without_truncation(self) -> None:
        await self._boot()
        before = await self._get_catalog()
        self.assertEqual(int(before["version"]), 1)
        self.assertEqual(before["entries"], [])

        entry = {
            "provider_key": "openai_compatible",
            "model": "fixture/model-a",
            "currency": "USD",
            "input_per_million": "0",  # 0 必须原样是 "0"，不能变成 falsy/省略
            "output_per_million": "",  # 空 → None（未知），不是 0
            "cache_read_per_million": "1.5",  # 小数不得被截成 1
            "cache_write_per_million": None,
        }
        response = await self._put({"expected_version": 1, "entries": [entry]})
        self.assertEqual(response.status, 200, await response.text())
        catalog = (await response.json())["catalog"]
        self.assertEqual(int(catalog["version"]), 2)
        self.assertEqual(len(catalog["entries"]), 1)
        stored = catalog["entries"][0]
        self.assertEqual(stored["input_per_million"], "0")
        self.assertIsNone(stored["output_per_million"])
        self.assertEqual(stored["cache_read_per_million"], "1.5")
        self.assertIsNone(stored["cache_write_per_million"])
        # 回读 GET 也必须与 PUT 响应一致。
        self.assertEqual(await self._get_catalog(), catalog)

    async def test_stale_write_is_rejected_and_does_not_revert_fields(self) -> None:
        """父任务复现：A 写 input=33，B 拿着旧草稿不能把 input 改回去。"""

        await self._boot()
        current = int((await self._get_catalog())["version"])
        self.assertEqual(current, 1)

        first = await self._put(
            {
                "expected_version": current,
                "entries": [_entry(input_per_million="33", output_per_million="15")],
            }
        )
        self.assertEqual(first.status, 200, await first.text())
        self.assertEqual(int((await first.json())["catalog"]["version"]), current + 1)

        # B 仍在旧草稿上：期望版本还是 N，却带着过期的 input=1。
        stale = await self._put(
            {
                "expected_version": current,
                "entries": [_entry(input_per_million="1", output_per_million="44")],
            }
        )
        self.assertEqual(stale.status, 409, await stale.text())
        error = (await stale.json())["error"]
        self.assertEqual(error["code"], "price_version_conflict")
        # 消息必须同时说清服务端当前版本与客户端期望版本。
        self.assertIn(str(current + 1), error["message"])
        self.assertIn(str(current), error["message"])

        # 被拒的写入没有改动第一版的任何字段，版本也没有前进。
        catalog = await self._get_catalog()
        self.assertEqual(int(catalog["version"]), current + 1)
        stored = catalog["entries"][0]
        self.assertEqual(stored["input_per_million"], "33")
        self.assertEqual(stored["output_per_million"], "15")
        self.assertEqual(await self._durable_max_version(), current + 1)

    async def test_same_baseline_concurrent_writes_have_exactly_one_winner(self) -> None:
        """同一 expected_version 并发：恰好一个 200、一个 409，版本只进一格。

        aiosqlite 下这里的确定性来自两点：CAS 的 ``SELECT MAX`` 与 INSERT 在
        同一事务里；且两个会话的 INSERT 竞争版本主键，输的那个必出
        IntegrityError。所以"恰好一个赢家"不是概率断言。为降低调度偶然性，
        同一基线并发跑 3 轮，每轮都断言一次。
        """

        await self._boot()
        expected = int((await self._get_catalog())["version"])
        for round_no in range(3):
            left, right = await asyncio.gather(
                self._put(
                    {
                        "expected_version": expected,
                        "entries": [_entry(input_per_million=str(40 + round_no))],
                    }
                ),
                self._put(
                    {
                        "expected_version": expected,
                        "entries": [_entry(input_per_million=str(50 + round_no))],
                    }
                ),
            )
            statuses = sorted([left.status, right.status])
            self.assertEqual(statuses, [200, 409], f"round {round_no}")
            loser = left if left.status == 409 else right
            self.assertEqual(
                (await loser.json())["error"]["code"],
                "price_version_conflict",
                f"round {round_no}",
            )
            expected += 1
            self.assertEqual(await self._durable_max_version(), expected)
            self.assertEqual(int((await self._get_catalog())["version"]), expected)

    async def test_cas_baseline_survives_a_process_restart(self) -> None:
        await self._boot()
        response = await self._put({"expected_version": 1, "entries": [_entry()]})
        self.assertEqual(response.status, 200, await response.text())
        durable = (await response.json())["catalog"]

        # 模拟重启：清空进程内缓存，只从数据库恢复。
        from bot.web.settings_api import restore_price_catalog, set_price_catalog

        set_price_catalog(pricing.PriceCatalog(version=1))
        restored = await restore_price_catalog()
        self.assertEqual(int(restored.version), int(durable["version"]))
        self.assertEqual(restored.document(), durable)
        # 恢复后的 CAS 基线来自数据库：再写必须拿新版本当 expected。
        stale = await self._put({"expected_version": 1, "entries": [_entry()]})
        self.assertEqual(stale.status, 409, await stale.text())
        advanced = await self._put({"expected_version": int(durable["version"]), "entries": []})
        self.assertEqual(advanced.status, 200, await advanced.text())
        self.assertEqual(int((await advanced.json())["catalog"]["version"]), int(durable["version"]) + 1)

    async def test_normal_group_admin_is_forbidden_on_read_and_write(self) -> None:
        await self._boot()
        read = await self.client.get(
            "/api/v1/prices", headers=self._headers(NORMAL_ADMIN_ID)
        )
        self.assertEqual(read.status, 403)
        write = await self.client.put(
            "/api/v1/prices",
            json={"expected_version": 1, "entries": []},
            headers=self._headers(NORMAL_ADMIN_ID),
        )
        self.assertEqual(write.status, 403)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
