"""价格目录的版本化与持久化口径（Task 6 邻接）。

* 价格**按版本**取快照：改价不追改历史账；
* ``provider_key`` 是内部配置槽标识，**绝不能**是 API Key，也绝不出现
  带查询串的 endpoint URL；
* 计价缺任何一段单价 → 该部分金额 unknown（不是 0）；
* Anthropic 的 input/cache 语义单独验证：缓存写入单列，输入不重复计费。
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from bot.services import pricing


class CatalogVersioningTests(unittest.TestCase):
    def test_versions_are_independent_snapshots(self) -> None:
        first = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="p", model="m", currency="USD", input_per_million="3"
                ),
            ),
        )
        second = pricing.PriceCatalog(
            version=2,
            entries=(
                pricing.PriceEntry(
                    provider_key="p", model="m", currency="USD", input_per_million="9"
                ),
            ),
        )
        one = pricing.estimate_for(
            first, provider_key="p", model="m", prompt_tokens=1_000_000
        )
        two = pricing.estimate_for(
            second, provider_key="p", model="m", prompt_tokens=1_000_000
        )
        self.assertEqual(one.price_version, 1)
        self.assertEqual(two.price_version, 2)
        self.assertEqual(one.micro, 3_000_000)
        self.assertEqual(two.micro, 9_000_000)

    def test_provider_key_is_normalised_and_not_a_secret(self) -> None:
        entry = pricing.PriceEntry(
            provider_key="  OpenAI_Compatible ",
            model=" vendor/model ",
            currency="usd",
            input_per_million="1",
        )
        self.assertEqual(entry.provider_key, "openai_compatible")
        self.assertEqual(entry.currency, "USD")
        blob = str(entry.document())
        self.assertNotIn("sk-", blob)
        self.assertNotIn("http", blob)

    def test_anthropic_cache_write_is_billed_separately_from_input(self) -> None:
        """Anthropic 的 ``cache_creation_input_tokens`` 不含在 prompt 里。"""

        entry = pricing.PriceEntry(
            provider_key="anthropic",
            model="claude-x",
            currency="USD",
            input_per_million="3",
            output_per_million="15",
            cache_read_per_million="0.3",
            cache_write_per_million="3.75",
        )
        cost = pricing.estimate(
            entry,
            prompt_tokens=100,
            output_tokens=20,
            cached_tokens=40,
            cache_write_tokens=1000,
        )
        # 网关归一化后的口径：``prompt_tokens`` 包含缓存命中部分，所以按 input
        # 价计的是 100-40=60；缓存读 40 按 cache-read 价；缓存写入 1000 不在
        # prompt 里，单独按 cache-write 价（Anthropic 口径）。
        expected = (
            Decimal(60) * Decimal("3")
            + Decimal(40) * Decimal("0.3")
            + Decimal(1000) * Decimal("3.75")
            + Decimal(20) * Decimal("15")
        )
        self.assertEqual(Decimal(cost.micro), expected)

    def test_zero_token_components_need_no_price(self) -> None:
        entry = pricing.PriceEntry(
            provider_key="p", model="m", currency="USD", input_per_million="3"
        )
        cost = pricing.estimate(entry, prompt_tokens=100, output_tokens=0)
        self.assertTrue(cost.known)
        self.assertEqual(cost.unknown_components, ())

    def test_catalog_requires_a_positive_version(self) -> None:
        with self.assertRaises(ValueError):
            pricing.PriceCatalog(version=0)

    def test_sum_rejects_unknown_amounts(self) -> None:
        known = pricing.CostEstimate(
            currency="USD", micro=10, uncached_input_tokens=1,
            cache_read_tokens=0, cache_write_tokens=0, output_tokens=0,
        )
        unknown = pricing.CostEstimate(
            currency="USD", micro=None, uncached_input_tokens=1,
            cache_read_tokens=0, cache_write_tokens=0, output_tokens=0,
            unknown_components=("input",),
        )
        self.assertEqual(pricing.sum_amounts([known, known]), 20)
        self.assertIsNone(pricing.sum_amounts([known, unknown]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
