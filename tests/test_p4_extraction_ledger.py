"""P4-9（B-39）：每日提炼台账按保留期清理，全局次数不再每轮遍历整张表。

现象（审计原文说的「台账」在本仓库里是**进程内 dict**，不是表）：
``_DAILY_EXTRACTION_LEDGER`` 是 ``{scope:scope_id -> (自然日, 次数)}``，只增不减
——每碰过一个作用域就多一个条目，进程活多久留多少个；而
``extraction_runs_today()``（不带参数，全局额度判定用的就是它）每次都遍历整张
dict 求和。跑得越久、群越多，这两处都越慢。

改法：
* 全局当日合计改成记一次 +1 的计数器（``_DAILY_EXTRACTION_TOTAL``），读侧 O(1)；
* 每个自然日扫一次台账，摘掉保留期（``memory_extract_ledger_retention_days``，
  默认 1 = 只留当天）之外的条目。

**额度口径必须与改前一致**：读侧两条路径本来都按自然日过滤，过期条目对
「今天已用几次」的贡献恒为 0，所以清理不可能改变任何一次判定。本文件用
「达到上限仍然拒绝 / 未达上限仍然放行」和「新旧口径逐点相等」两组用例钉住。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from bot.services import long_term_memory as ltm

DAY1 = datetime(2026, 7, 1, 10, 0, 0)
DAY2 = datetime(2026, 7, 2, 10, 0, 0)
DAY3 = datetime(2026, 7, 3, 10, 0, 0)
DAY8 = datetime(2026, 7, 8, 10, 0, 0)


def _settings(retention_days: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(
            memory_extract_ledger_retention_days=retention_days,
            memory_extract_daily_cap=2,
            memory_extract_min_messages=1,
        )
    )


def _legacy_global_total(day: datetime) -> int:
    """改前 ``extraction_runs_today()`` 的算法：遍历整张表求和。"""

    return sum(
        count
        for key_day, count in ltm._DAILY_EXTRACTION_LEDGER.values()
        if key_day == ltm._day_key(day)
    )


class ExtractionLedgerRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        ltm.reset_extraction_ledger()

    def tearDown(self) -> None:
        ltm.reset_extraction_ledger()

    def test_default_retention_is_one_day(self) -> None:
        self.assertEqual(ltm.EXTRACT_LEDGER_RETENTION_DAYS, 1)
        self.assertEqual(ltm.memory_extract_ledger_retention_days(_settings()), 1)
        self.assertEqual(ltm.memory_extract_ledger_retention_days(SimpleNamespace()), 1)

    def test_retention_is_clamped(self) -> None:
        self.assertEqual(ltm.memory_extract_ledger_retention_days(_settings(0)), 1)
        self.assertEqual(ltm.memory_extract_ledger_retention_days(_settings(-5)), 1)
        self.assertEqual(ltm.memory_extract_ledger_retention_days(_settings(10_000)), 365)
        self.assertEqual(ltm.memory_extract_ledger_retention_days(_settings(7)), 7)

    def test_entries_older_than_the_retention_are_dropped(self) -> None:
        settings = _settings(1)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY1, settings=settings)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -2, now=DAY1, settings=settings)
        self.assertEqual(len(ltm._DAILY_EXTRACTION_LEDGER), 2)

        # 跨到第二天：昨天的两条必须被摘掉（改前它们会永远留着）。
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -3, now=DAY2, settings=settings)
        self.assertEqual(len(ltm._DAILY_EXTRACTION_LEDGER), 1)
        self.assertEqual(ltm._day_key(DAY2), ltm._day_key(DAY2))
        self.assertIn(f"{ltm.SCOPE_GROUP}:-3", ltm._DAILY_EXTRACTION_LEDGER)

    def test_the_ledger_stops_growing_with_the_number_of_scopes_over_time(self) -> None:
        settings = _settings(1)
        for day in (DAY1, DAY2, DAY3, DAY8):
            for group in range(-20, 0):
                ltm.note_extraction_run(ltm.SCOPE_GROUP, group, now=day, settings=settings)
        # 20 个作用域 × 4 天 = 80 次调用；改前 dict 会留下 80 个条目。
        self.assertEqual(len(ltm._DAILY_EXTRACTION_LEDGER), 20)

    def test_a_wider_retention_keeps_more_days(self) -> None:
        """保留期 3 天 vs 1 天：窗口内外条目的去留不同。

        自然日只向前走（生产语义），所以这里也按 DAY1 → DAY3 → DAY8 递增。
        """

        wide = _settings(3)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY1, settings=wide)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -2, now=DAY3, settings=wide)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -3, now=DAY8, settings=wide)
        kept = {key: value[0] for key, value in ltm._DAILY_EXTRACTION_LEDGER.items()}
        # DAY8 的窗口 = [07-06, 07-08]
        self.assertNotIn(f"{ltm.SCOPE_GROUP}:-1", kept)  # 07-01，窗口外
        self.assertNotIn(f"{ltm.SCOPE_GROUP}:-2", kept)  # 07-03，窗口外
        self.assertIn(f"{ltm.SCOPE_GROUP}:-3", kept)

        ltm.reset_extraction_ledger()
        narrow = _settings(1)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY1, settings=narrow)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -2, now=DAY3, settings=narrow)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -3, now=DAY8, settings=narrow)
        self.assertEqual(
            list(ltm._DAILY_EXTRACTION_LEDGER),
            [f"{ltm.SCOPE_GROUP}:-3"],
        )

    def test_pruning_happens_once_per_day(self) -> None:
        settings = _settings(1)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY1, settings=settings)
        self.assertEqual(ltm._DAILY_EXTRACTION_PRUNED_DAY, ltm._day_key(DAY1))
        for group in range(-29, -1):
            ltm.note_extraction_run(ltm.SCOPE_GROUP, group, now=DAY1, settings=settings)
        self.assertEqual(ltm._DAILY_EXTRACTION_PRUNED_DAY, ltm._day_key(DAY1))
        self.assertEqual(len(ltm._DAILY_EXTRACTION_LEDGER), 29)


class ExtractionQuotaVerdictUnchangedTests(unittest.TestCase):
    """额度判定：达到上限仍然拒绝，未达上限仍然放行。"""

    def setUp(self) -> None:
        ltm.reset_extraction_ledger()

    def tearDown(self) -> None:
        ltm.reset_extraction_ledger()

    def test_reaching_the_cap_still_blocks_the_next_run(self) -> None:
        settings = _settings(1)
        settings.bot.memory_extract_daily_cap = 2
        cap = ltm.memory_extract_daily_cap(settings)
        self.assertEqual(cap, 2)

        allowed = 0
        for group in range(-5, 0):
            if ltm.extraction_runs_today(now=DAY1) >= cap:
                break
            ltm.note_extraction_run(ltm.SCOPE_GROUP, group, now=DAY1, settings=settings)
            allowed += 1
        self.assertEqual(allowed, 2)
        self.assertEqual(ltm.extraction_runs_today(now=DAY1), 2)
        self.assertTrue(
            ltm.extraction_runs_today(now=DAY1) >= cap,
            "达到上限后必须仍然判定为「已达上限」",
        )

    def test_below_the_cap_still_admits(self) -> None:
        settings = _settings(1)
        settings.bot.memory_extract_daily_cap = 3
        cap = ltm.memory_extract_daily_cap(settings)
        for index, group in enumerate(range(-2, 0), start=1):
            self.assertLess(ltm.extraction_runs_today(now=DAY1), cap)
            ltm.note_extraction_run(ltm.SCOPE_GROUP, group, now=DAY1, settings=settings)
            self.assertEqual(ltm.extraction_runs_today(now=DAY1), index)

    def test_a_zero_cap_never_blocks(self) -> None:
        settings = _settings(1)
        settings.bot.memory_extract_daily_cap = 0
        cap = ltm.memory_extract_daily_cap(settings)
        self.assertEqual(cap, 0)
        for group in range(-4, 0):
            if cap > 0 and ltm.extraction_runs_today(now=DAY1) >= cap:
                break
            ltm.note_extraction_run(ltm.SCOPE_GROUP, group, now=DAY1, settings=settings)
        self.assertEqual(ltm.extraction_runs_today(now=DAY1), 4)

    def test_per_scope_counts_are_unchanged_across_days(self) -> None:
        settings = _settings(1)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY1, settings=settings)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY1, settings=settings)
        self.assertEqual(
            ltm.extraction_runs_today(ltm.SCOPE_GROUP, -1, now=DAY1), 2
        )
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY2, settings=settings)
        # 跨日：该作用域今天重新从 1 开始
        self.assertEqual(
            ltm.extraction_runs_today(ltm.SCOPE_GROUP, -1, now=DAY2), 1
        )
        self.assertEqual(
            ltm.extraction_runs_today(ltm.SCOPE_GROUP, -1, now=DAY1), 0
        )


class QuotaVerdictIsIdenticalOnBothRevisionsTests(unittest.TestCase):
    """这一组**只**用改前就有的调用签名（不传 ``settings``），因此在旧代码上也跑得通。

    它的作用是把「额度口径与今天一致」变成可对照的结论：同一串操作在改前/改后
    必须给出**同样的放行/拒绝判定**与同样的计数。
    """

    def setUp(self) -> None:
        ltm.reset_extraction_ledger()

    def tearDown(self) -> None:
        ltm.reset_extraction_ledger()

    def test_same_operations_give_the_same_verdicts(self) -> None:
        cap = 2
        verdicts: list[bool] = []
        for day in (DAY1, DAY2):
            for group in (-1, -2, -3, -4):
                allowed = ltm.extraction_runs_today(now=day) < cap
                verdicts.append(allowed)
                if not allowed:
                    continue
                ltm.note_extraction_run(ltm.SCOPE_GROUP, group, now=day)
                # 计数必须始终等于「改前遍历整张表求和」的结果。
                self.assertEqual(
                    ltm.extraction_runs_today(now=day),
                    _legacy_global_total(day),
                )
        # DAY1 放行 2 次后拒绝；DAY2 重新放行 2 次后拒绝。
        self.assertEqual(
            verdicts,
            [True, True, False, False, True, True, False, False],
        )

    def test_per_scope_counting_is_unchanged(self) -> None:
        ltm.note_extraction_run(ltm.SCOPE_PRIVATE, 42, now=DAY1)
        ltm.note_extraction_run(ltm.SCOPE_PRIVATE, 42, now=DAY1)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY1)
        self.assertEqual(ltm.extraction_runs_today(ltm.SCOPE_PRIVATE, 42, now=DAY1), 2)
        self.assertEqual(ltm.extraction_runs_today(ltm.SCOPE_GROUP, -1, now=DAY1), 1)
        self.assertEqual(ltm.extraction_runs_today(now=DAY1), 3)
        self.assertEqual(ltm.extraction_runs_today(ltm.SCOPE_GROUP, -404, now=DAY1), 0)


class GlobalCounterMatchesLegacyScanTests(unittest.TestCase):
    """新的 O(1) 计数器与「改前遍历求和」在每一步都必须相等。"""

    def setUp(self) -> None:
        ltm.reset_extraction_ledger()

    def tearDown(self) -> None:
        ltm.reset_extraction_ledger()

    def test_counter_equals_the_legacy_full_scan_at_every_step(self) -> None:
        settings = _settings(1)
        for day in (DAY1, DAY1, DAY2, DAY3):
            for group in (-1, -2, -3, -1):
                stamp = day
                ltm.note_extraction_run(ltm.SCOPE_GROUP, group, now=stamp, settings=settings)
                self.assertEqual(
                    ltm.extraction_runs_today(now=stamp),
                    _legacy_global_total(stamp),
                    f"O(1) 计数器与遍历求和不一致 @ {stamp}",
                )
                # 跨日读也要相等（昨天的合计不能泄漏到今天）。
                self.assertEqual(ltm.extraction_runs_today(now=DAY1), _legacy_global_total(DAY1))
                self.assertEqual(ltm.extraction_runs_today(now=DAY2), _legacy_global_total(DAY2))

    def test_reading_a_day_with_no_runs_returns_zero(self) -> None:
        settings = _settings(1)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY1, settings=settings)
        far_future = DAY1 + timedelta(days=30)
        self.assertEqual(ltm.extraction_runs_today(now=far_future), 0)
        self.assertEqual(_legacy_global_total(far_future), 0)

    def test_reset_clears_the_counter_too(self) -> None:
        settings = _settings(1)
        ltm.note_extraction_run(ltm.SCOPE_GROUP, -1, now=DAY1, settings=settings)
        self.assertEqual(ltm.extraction_runs_today(now=DAY1), 1)
        ltm.reset_extraction_ledger()
        self.assertEqual(ltm.extraction_runs_today(now=DAY1), 0)
        self.assertEqual(ltm._DAILY_EXTRACTION_TOTAL, ("", 0))
        self.assertEqual(ltm._DAILY_EXTRACTION_PRUNED_DAY, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
