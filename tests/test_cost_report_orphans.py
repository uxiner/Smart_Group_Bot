"""对账必须遍历**两侧身份的全集**：计价表独有的孤儿行不许被当成合法 legacy。

父代理在冻结树 ``31bda38`` 上实测到的缺口（v71 合同 B）：``build_cost_report``
的 reconciliation 只遍历 ``main_by_identity``，于是"计价表有、主表没有"的行完全
不进对账 —— 同一个本来空的合成群只写进一条计价行时，报表给出

    attempts=0 / priced_attempts=1 / amount_by_currency.USD=43000 / amount_complete=True

即"声称完整"的同时给出互相矛盾的次数与金额。孤儿**不是** legacy：legacy 的定义是
"主台账记了、计价表没记"（旧数据没有价格元数据），方向正好相反，不能拿它当解释。

这里全部是 :func:`bot.services.group_costs.build_cost_report` 的**纯函数**回归，
不连接数据库。
"""

from __future__ import annotations

import unittest

from bot.services.group_costs import build_cost_report

DATE = "2026-01-01"
PROVIDER = "fixture"
MODEL = "m1"


def _main_row(
    *,
    model: str = MODEL,
    purpose: str = "optional",
    stage: str = "chat",
    attempts: int = 1,
    usage_unknown: int = 0,
) -> dict:
    return {
        "usage_date": DATE,
        "purpose": purpose,
        "stage": stage,
        "provider": PROVIDER,
        "model": model,
        "attempts": attempts,
        "successes": attempts,
        "failures": 0,
        "timeouts": 0,
        "empty_responses": 0,
        "usage_unknown": usage_unknown,
        "prompt_tokens": 100,
        "output_tokens": 20,
        "cached_tokens": None,
        "cache_write_tokens": None,
        "thinking_tokens": None,
    }


def _priced_row(
    *,
    model: str = MODEL,
    purpose: str = "optional",
    stage: str = "chat",
    attempts: int = 1,
    priced_attempts: int = 1,
    unknown_attempts: int = 0,
    amount_micro: int = 43000,
    currency: str = "USD",
    price_version: int = 1,
    price_snapshot: dict | None = None,
) -> dict:
    return {
        "usage_date": DATE,
        "purpose": purpose,
        "stage": stage,
        "provider": PROVIDER,
        "model": model,
        "price_version": price_version,
        "currency": currency,
        "price_snapshot": price_snapshot if price_snapshot is not None else {"input": "1"},
        "attempts": attempts,
        "priced_attempts": priced_attempts,
        "unknown_attempts": unknown_attempts,
        "unknown_usage_attempts": 0,
        "unknown_price_attempts": 0,
        "amount_micro": amount_micro,
    }


class OrphanReconciliationTests(unittest.TestCase):
    def test_priced_only_row_is_unmatched_not_legacy(self) -> None:
        """计价表独有 = 对不上，不是"旧数据没价格"。"""

        report = build_cost_report([], [_priced_row()], days=7)

        self.assertEqual(report["reconciliation"]["unmatched_priced_attempts"], 1)
        self.assertEqual(report["reconciliation"]["legacy_attempts"], 0)
        self.assertEqual(report["totals"]["legacy_attempts"], 0)
        self.assertEqual(report["totals"]["attempts"], 0)
        self.assertEqual(report["totals"]["priced_attempts"], 1)
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": 43000})
        self.assertFalse(report["amount_complete"])

    def test_orphan_does_not_hide_a_real_legacy_row(self) -> None:
        """主表里另一个身份的旧记录照旧算 legacy，孤儿**另外**计数。"""

        report = build_cost_report(
            [_main_row(model="legacy-model", attempts=2)],
            [_priced_row(model="orphan-model", amount_micro=43000)],
            days=7,
        )

        self.assertEqual(report["reconciliation"]["legacy_attempts"], 2)
        self.assertEqual(report["reconciliation"]["unmatched_priced_attempts"], 1)
        self.assertEqual(report["reconciliation"]["main_attempts"], 2)
        self.assertEqual(report["reconciliation"]["priced_table_attempts"], 1)
        self.assertFalse(report["amount_complete"])

        by_currency = {bucket["currency"]: bucket for bucket in report["currencies"]}
        # legacy 落在独立的未知桶里，孤儿**没有**被算成 legacy。
        self.assertEqual(by_currency[""]["legacy_attempts"], 2)
        self.assertEqual(by_currency["USD"]["attempts"], 1)

    def test_same_identity_overflow_is_still_unmatched(self) -> None:
        """已经对得上的身份里多出来的 attempts 照旧算对不上（原有行为）。"""

        report = build_cost_report(
            [_main_row(attempts=1)],
            [_priced_row(attempts=3, priced_attempts=3, amount_micro=129000)],
            days=7,
        )

        self.assertEqual(report["reconciliation"]["unmatched_priced_attempts"], 2)
        self.assertEqual(report["reconciliation"]["legacy_attempts"], 0)
        self.assertFalse(report["amount_complete"])

    def test_main_only_row_is_legacy_with_no_orphan(self) -> None:
        """主表独有 = 旧数据没有价格元数据（legacy），不许说成对不上。"""

        report = build_cost_report([_main_row(attempts=1)], [], days=7)

        self.assertEqual(report["reconciliation"]["legacy_attempts"], 1)
        self.assertEqual(report["reconciliation"]["unmatched_priced_attempts"], 0)
        self.assertFalse(report["amount_complete"])  # 金额未知，不是完整成本

    def test_balanced_pair_is_not_flagged_as_unmatched(self) -> None:
        """正控：两侧身份完全对齐时不能被误判成对不上。"""

        report = build_cost_report([_main_row()], [_priced_row()], days=7)

        self.assertEqual(report["reconciliation"]["unmatched_priced_attempts"], 0)
        self.assertEqual(report["reconciliation"]["legacy_attempts"], 0)
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": 43000})
        self.assertTrue(report["amount_complete"])

    def test_orphan_on_its_own_identity_never_becomes_legacy(self) -> None:
        """孤儿的 amount 仍按币种展示，但**不能**因此说报表完整。"""

        report = build_cost_report(
            [_main_row(model="a", attempts=1)],
            [
                _priced_row(model="a", attempts=1, amount_micro=43000),
                _priced_row(model="orphan", attempts=2, priced_attempts=2, amount_micro=86000),
            ],
            days=7,
        )

        self.assertEqual(report["reconciliation"]["unmatched_priced_attempts"], 2)
        self.assertEqual(report["reconciliation"]["legacy_attempts"], 0)
        self.assertFalse(report["amount_complete"])
        self.assertEqual(report["totals"]["amount_by_currency"], {"USD": 129000})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
