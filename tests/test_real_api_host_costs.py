"""合成浏览器宿主的播种必须真的是「发出时冻结」的账。

为什么单独钉住这件事：v70 的浏览器金额验收
（``tools/settings-ui-harness/checks/real-api-costs.mjs``）把「从库里原始行**算出来**
的金额字符串」当成期望值，再去断言界面里出现了这些字符串。如果播种自己把价格
版本搞错了（第一版就是：第一条记录用到了 reset 末尾设的**最新**版本，于是"改价
不追改历史"这条自己就是假的），浏览器就会拿一个自己编出来的期望值去比对自己编
出来的界面 —— 全绿，却什么都没证明。

所以这里不开浏览器，直接驱动真实宿主：建库 → 播种 → 读**原始表** → 逐行断言，
再让同一支真实 ``/api/v1/groups/{id}/costs`` 在这个合成库上跑一遍授权与形状。
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
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text

from bot.services import group_costs
from bot.services import pricing
from bot.services import usage_scope as scopes
from bot.web.settings_api import configure_price_store, set_price_catalog

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS_DIR = _ROOT / "tools" / "settings-ui-harness"

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
MEMBER_A = 8
GROUP_A = -1001234567890


def _load_host_module():
    """按路径加载合成宿主（``tools/`` 不进镜像，测试里按绝对路径加载）。"""

    if str(_HARNESS_DIR) not in sys.path:
        sys.path.insert(0, str(_HARNESS_DIR))
    spec = importlib.util.spec_from_file_location(
        "real_api_host_under_test", _HARNESS_DIR / "real_api_host.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _signed_init_data(user_id: int) -> str:
    pairs = {
        "auth_date": str(int(time.time())),
        "query_id": f"AAF-costs-{user_id}-{int(time.time() * 1000) % 100000}",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


class _SyntheticHostTestCase(unittest.IsolatedAsyncioTestCase):
    """每个用例一个真实的合成库 + 真实 VerifyWebServer app。"""

    async def asyncSetUp(self) -> None:
        self.host_module = _load_host_module()
        self.host = await self.host_module.build_host(listen_port=0)
        self.addAsyncCleanup(self._dispose_host)

    async def _dispose_host(self) -> None:
        # 顺序不能反：``host.close()`` 会把本用例的临时库删掉，而产品模块级的
        # ``_PRICE_STORE`` 还指着那个库。先摘掉存储，再关宿主 —— 否则下一次
        # ``build_host`` 里的 ``restore_price_catalog()`` 会在一个已被删掉的
        # 文件上重新开库，报 "no such table: model_price_catalog"，
        # 看起来像"新表没建"，其实是读了一个空的旧句柄。
        configure_price_store(None)
        group_costs.reset()
        scopes.reset()
        set_price_catalog(pricing.PriceCatalog(version=1))
        await self.host.close()

    async def _priced_rows(self, **filters):
        query = "SELECT * FROM llm_cost_priced_daily"
        if filters:
            conditions = " AND ".join(f"{column} = :{column}" for column in filters)
            query += f" WHERE {conditions}"
        query += " ORDER BY model ASC, price_version ASC, currency ASC"
        async with self.host.engine.connect() as conn:
            result = await conn.execute(text(query), dict(filters))
            return [dict(row) for row in result.mappings().all()]

    async def _main_rows(self):
        async with self.host.engine.connect() as conn:
            result = await conn.execute(
                text("SELECT * FROM llm_cost_daily ORDER BY model ASC")
            )
            return [dict(row) for row in result.mappings().all()]

    async def _batch_ids(self):
        async with self.host.engine.connect() as conn:
            result = await conn.execute(text("SELECT batch_id FROM llm_cost_flush_batches"))
            return sorted(str(row[0]) for row in result.all())

    @staticmethod
    def _snapshot(row: dict):
        """原生 SQL 读 JSON 列拿到的是**文本**（ORM 才会解成对象）。

        ``json.loads('null')`` 是 ``None``：缺价那条落库就是 JSON null，
        它在报表里必须仍然算"没有价格条目"。
        """

        raw = row.get("price_snapshot")
        if isinstance(raw, str):
            return json.loads(raw)
        return raw

    def _row(self, model: str, version: int, currency: str) -> dict:
        matches = [
            row
            for row in self.rows
            if row["model"] == model
            and int(row["price_version"]) == version
            and str(row["currency"]) == currency
        ]
        self.assertEqual(
            len(matches), 1, f"{model} v{version} {currency!r} 应当恰好一行，实际 {len(matches)}"
        )
        return matches[0]


class SeededSendTimeFreezeTests(_SyntheticHostTestCase):
    """播种出来的每一条都必须是"用当时的价算出来的"，而且版本各自可辨。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.rows = await self._priced_rows()

    async def test_each_attempt_keeps_the_price_version_that_was_current_when_sent(self):
        self.assertEqual(len(self.rows), 5, f"计价表行数：{len(self.rows)}")

        v1 = self._row("gpt-4o-mini", 1, "USD")
        v2 = self._row("gpt-4o-mini", 2, "USD")
        cny = self._row("cny-model", 3, "CNY")
        # 10 input × 1 + 1 output × 2 = 12；改价到 v2 之后仍然是 12
        self.assertEqual(int(v1["amount_micro"]), 12)
        self.assertEqual(int(v1["priced_attempts"]), 1)
        self.assertEqual(int(v1["unknown_attempts"]), 0)
        self.assertEqual((self._snapshot(v1) or {}).get("input_per_million"), "1")
        # 10 × 100 + 1 × 200 = 1200
        self.assertEqual(int(v2["amount_micro"]), 1200)
        self.assertEqual((self._snapshot(v2) or {}).get("input_per_million"), "100")
        # CNY 条目：10 × 7 + 0 × 0 = 70
        self.assertEqual(int(cny["amount_micro"]), 70)
        self.assertEqual((self._snapshot(cny) or {}).get("input_per_million"), "7")
        # 两个币种各自成桶：绝不折算成一个数
        self.assertNotEqual(str(v1["currency"]), str(cny["currency"]))

    async def test_missing_usage_is_unknown_not_zero(self):
        v3 = self._row("gpt-4o-mini", 3, "USD")
        self.assertEqual(int(v3["priced_attempts"]), 0, "缺用量的调用不得计成已计价")
        self.assertEqual(int(v3["unknown_usage_attempts"]), 1)
        self.assertEqual(int(v3["amount_micro"]), 0, "金额列留着 0 但 priced_attempts=0")
        self.assertEqual(int(v3["attempts"]), 1)

    async def test_missing_price_is_its_own_currency_bucket(self):
        bucket = await self._priced_rows(currency="")
        self.assertEqual(len(bucket), 1, "空币种桶应当只有那条缺价的调用")
        row = bucket[0]
        self.assertEqual(row["model"], "no-price-model")
        self.assertEqual(int(row["unknown_price_attempts"]), 1)
        self.assertIsNone(self._snapshot(row), "发送时就缺价 → 快照必须是空的")

    async def test_legacy_rows_are_never_repriced(self):
        main = {row["model"]: row for row in await self._main_rows()}
        self.assertEqual(int(main["legacy-model"]["attempts"]), 3)
        self.assertFalse(
            [row for row in self.rows if row["model"] == "legacy-model"],
            "旧历史没有发送时元数据 → 计价表里一行都不能有（不能拿今天的价回填）",
        )
        self.assertEqual(int(main["gpt-4o-mini"]["attempts"]), 3)

    async def test_one_reset_writes_exactly_one_frozen_batch(self):
        self.assertEqual(len(await self._batch_ids()), 1)


class ResetIsolationTests(_SyntheticHostTestCase):
    """一次 reset 就是一条独立的账：上一臂的行与批次都不能漏进来。"""

    async def test_reset_is_idempotent(self):
        before = await self._priced_rows()
        await self.host.reset()
        after = await self._priced_rows()
        self.assertEqual(
            [(r["model"], r["price_version"], r["currency"], r["attempts"], r["amount_micro"])
             for r in before],
            [(r["model"], r["price_version"], r["currency"], r["attempts"], r["amount_micro"])
             for r in after],
            "同一次播种重放必须得到同一份账（不能累加、不能漂版本）",
        )
        self.assertEqual(len(await self._batch_ids()), 1, "reset 后批次 marker 也只剩一条")

    async def test_reset_clears_an_earlier_arms_rows(self):
        from bot.db.models import LlmCostDaily

        async with self.host.session_factory() as session:
            async with session.begin():
                session.add(
                    LlmCostDaily(
                        usage_date="2000-01-01",
                        scope="group",
                        group_id=GROUP_A,
                        purpose="optional",
                        stage="main",
                        provider="fixture",
                        model="leftover-model",
                        attempts=99,
                    )
                )
        self.assertTrue(
            [row for row in await self._main_rows() if row["model"] == "leftover-model"],
            "先确认上一臂的行真的写进去了",
        )
        await self.host.reset()
        self.assertEqual(
            [row for row in await self._main_rows() if row["model"] == "leftover-model"],
            [],
            "reset 必须把上一臂的台账行清掉",
        )
        self.assertEqual(len(await self._priced_rows()), 5)


class SyntheticCostsApiTests(_SyntheticHostTestCase):
    """同一支真实 costs API 在合成库上的授权与形状。"""

    async def _get_costs(self, user_id: int | None):
        client = TestClient(TestServer(self.host.app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        headers = {} if user_id is None else {"Authorization": f"tma {_signed_init_data(user_id)}"}
        async with client.get(
            f"/api/v1/groups/{GROUP_A}/costs?days=7", headers=headers
        ) as response:
            return response.status, await response.json()

    async def test_super_admin_gets_a_per_currency_report(self):
        status, body = await self._get_costs(SUPER_ADMIN_ID)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        currencies = sorted(str(bucket["currency"]) for bucket in body["currencies"])
        self.assertEqual(currencies, ["", "CNY", "USD"])
        amounts = {
            str(bucket["currency"]): int(bucket["amount_micro"]) for bucket in body["currencies"]
        }
        self.assertEqual(amounts["USD"], 1212, "USD 桶 = v1 12 + v2 1200（各自按当时单价）")
        self.assertEqual(amounts["CNY"], 70)
        self.assertEqual(amounts[""], 0, "缺价的桶没有金额")
        self.assertIsNone(body["cross_currency_total"], "绝不提供跨币种合计")
        self.assertFalse(body["amount_complete"], "有未知 → 金额不完整")
        self.assertEqual(body["source_table"], "llm_cost_daily")
        self.assertEqual(body["amount_source_table"], "llm_cost_priced_daily")
        self.assertIn("llm_usage_daily", body["note"])
        # 用量表与计价表是同一次 attempt 的两个维度：次数只能相等，不能相加
        # 主表 8 次 attempt（legacy 3 + cny 1 + gpt 3 + 缺价 1），计价表 5 次
        self.assertEqual(body["reconciliation"]["main_attempts"], 8)
        self.assertEqual(body["reconciliation"]["priced_table_attempts"], 5)
        self.assertEqual(
            body["reconciliation"]["legacy_attempts"],
            3,
            "3 次旧历史没有发送时元数据 → 只能算 legacy，不能用今天的价回算",
        )

    async def test_member_is_denied_without_leaking_amounts(self):
        status, body = await self._get_costs(MEMBER_A)
        self.assertEqual(status, 403, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "group_access_denied")
        for key in ("currencies", "rows", "totals"):
            self.assertNotIn(key, body)

    async def test_unsigned_request_is_rejected(self):
        status, body = await self._get_costs(None)
        self.assertEqual(status, 401, body)
        self.assertEqual(body["error"]["code"], "authorization_required")

    async def test_tampered_signature_is_rejected(self):
        client = TestClient(TestServer(self.host.app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        forged = _signed_init_data(SUPER_ADMIN_ID).replace("hash=", "hash=0")
        async with client.get(
            f"/api/v1/groups/{GROUP_A}/costs?days=7",
            headers={"Authorization": f"tma {forged}"},
        ) as response:
            body = await response.json()
        self.assertEqual(response.status, 401)
        self.assertEqual(body["error"]["code"], "invalid_init_data")


class LeakedPriceStoreIsolationTests(unittest.IsolatedAsyncioTestCase):
    """上一个用例留下的价格目录存储，不能决定本次宿主去读哪张表。

    这不是假想：整片受影响用例一起跑时这里**全红**过 ——
    ``tests/test_price_catalog_cas.py`` 先把模块级 store 指向自己的临时库，
    teardown 又把库删了；下一条 ``build_host()`` 若没有重指 store，
    ``restore_price_catalog()`` 就会在一个空文件上查 ``model_price_catalog``，
    整片 ``no such table: model_price_catalog``。单独跑这个文件却是绿的，
    所以这条例外必须用一个"把 store 故意指歪"的用例钉住。
    """

    async def asyncSetUp(self) -> None:
        self.host_module = _load_host_module()
        self.leaked_path = None

    async def test_build_host_repoints_the_price_store_at_its_own_database(self):
        import os
        import tempfile

        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        descriptor, self.leaked_path = tempfile.mkstemp(suffix="-leaked.db")
        os.close(descriptor)
        self.addAsyncCleanup(self._drop_leaked_db)
        leaked_engine = create_async_engine(f"sqlite+aiosqlite:///{self.leaked_path}")
        self.addAsyncCleanup(leaked_engine.dispose)
        # 空库 = 上一个用例删库之后的形态：没有 model_price_catalog 这张表。
        configure_price_store(async_sessionmaker(leaked_engine, expire_on_commit=False))

        host = await self.host_module.build_host(listen_port=0)
        self.addAsyncCleanup(self._dispose_host, host)

        from bot.web.settings_api import price_catalog_provider

        self.assertEqual(price_catalog_provider().version, 3, "本次播种的最高版本")
        async with host.engine.connect() as conn:
            result = await conn.execute(
                text("SELECT version FROM model_price_catalog ORDER BY version ASC")
            )
            self.assertEqual([int(row[0]) for row in result.all()], [1, 2, 3])

    async def _dispose_host(self, host) -> None:
        configure_price_store(None)
        group_costs.reset()
        scopes.reset()
        set_price_catalog(pricing.PriceCatalog(version=1))
        await host.close()

    async def _drop_leaked_db(self) -> None:
        import os

        if self.leaked_path and os.path.exists(self.leaked_path):
            os.unlink(self.leaked_path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
