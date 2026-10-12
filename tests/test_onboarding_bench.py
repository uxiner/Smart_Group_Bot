"""``tools/onboarding_bench.py`` 的正式回归（小规模，不 mock guard/helper）。

早先那版 ``measure_moderation_dispatch`` 只改了一个输出标签，**没有**真的写
``save_budget(enabled=...)``，所以"开关对审核调度的影响"根本没被测过；正式测试
就是防止退回那种标签。

这里用工具自己的真实函数、小 rounds，断言：开关真的落库且两臂不同、真实供应商
被真实调用、安全用途不产生任何预算预留、可选功能的正/负控制由开关驱动、
触发比例要看落库而不是只看返回值。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tools import onboarding_bench as bench

GROUP = bench.GROUP_ID


class OnboardingBenchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from bot.db.engine import init_db

        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.addAsyncCleanup(self.engine.dispose)

    async def asyncTearDown(self) -> None:
        from bot.services import group_budget, group_costs

        group_budget.configure_alert_enqueuer(None)
        group_budget.reset_shared_service()
        group_costs.reset()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    # ------------------------------------------------------------------
    async def test_instrumentation_reports_percentiles(self) -> None:
        result = await bench.measure_instrumentation(rounds=200)
        self.assertEqual(result["rounds"], 200)
        self.assertGreater(result["p95_ms"], 0.0)
        self.assertLess(result["p95_ms"], 50.0, "内存埋点 p95 异常偏高")
        self.assertIn("target_met", result)

    async def test_switch_is_really_saved_and_fresh_read(self) -> None:
        """两个臂必须**真的**写 enabled，且 fresh read 与之一致。"""

        off = await bench.measure_moderation_dispatch(
            self.session_factory, with_budget=False, rounds=5
        )
        on = await bench.measure_moderation_dispatch(
            self.session_factory, with_budget=True, rounds=5
        )
        self.assertFalse(off["configured_enabled"])
        self.assertFalse(off["effective_enabled"])
        self.assertFalse(off["fresh_enabled"])
        self.assertTrue(on["configured_enabled"])
        self.assertTrue(on["effective_enabled"])
        self.assertTrue(on["fresh_enabled"])
        self.assertTrue(off["effective_revision"] >= 1)
        self.assertTrue(on["effective_revision"] >= 1)

    async def test_safety_reaches_the_supplier_and_reserves_nothing(self) -> None:
        for with_budget in (False, True):
            with self.subTest(with_budget=with_budget):
                result = await bench.measure_moderation_dispatch(
                    self.session_factory, with_budget=with_budget, rounds=6
                )
                self.assertEqual(
                    result["provider_calls"], 6,
                    "安全审核没有全部到达供应商",
                )
                self.assertEqual(
                    result["budget_reservations_created"], 0,
                    "安全用途占用了可选额度",
                )
                self.assertEqual(result["guard_admit_entries"], 6)
                self.assertIn("not exercised", result["telegram_calls"])

    async def test_optional_positive_and_negative_control(self) -> None:
        """同一实例：enabled + cap0 → 供应商 0；disabled → 供应商 ≥1。"""

        result = await bench.measure_optional_positive_control(
            self.session_factory
        )
        self.assertEqual(result["enabled_cap0_provider_calls"], 0)
        self.assertGreaterEqual(result["disabled_provider_calls"], 1)
        self.assertTrue(result["switch_drives_guard"], result)

    async def test_trigger_ratio_checks_persisted_attempts_and_alerts(self) -> None:
        tight = await bench.measure_trigger_ratio(
            self.session_factory, limit=1, concurrent=8
        )
        self.assertEqual(tight["admitted"], 1)
        self.assertEqual(tight["denied"], 7)
        self.assertEqual(tight["persisted_attempts"], 1)
        # 真实 outbox：cap=1 一次就跨过 80% 与 100%，两档各一条。
        self.assertEqual(
            tight["alert_counts"], {"warn": 1, "pause": 1}, tight["alert_counts"]
        )
        self.assertEqual(tight["alerts_total"], 2)

        loose = await bench.measure_trigger_ratio(
            self.session_factory, limit=20, concurrent=8
        )
        self.assertEqual(loose["admitted"], 8)
        self.assertEqual(loose["denied"], 0)
        self.assertEqual(loose["persisted_attempts"], 8)
        # 8/20 = 40%：没到 80%，不该有任何告警。
        self.assertEqual(loose["alert_counts"], {}, loose["alert_counts"])

    async def test_unexpected_crash_is_not_certified_as_a_denial(self) -> None:
        """护栏"看起来拒绝了"不能靠吞异常得出。

        只要 ``chat`` 内部抛了非预期异常，测量必须**炸掉**，而不是因为
        供应商调用 0 次就认证成"可选被正确拒绝"。
        """

        from unittest.mock import AsyncMock, patch

        from bot.services import llm as llm_module

        boom = RuntimeError("synthetic internal crash")
        with patch.object(
            llm_module.LLMService, "chat", AsyncMock(side_effect=boom)
        ):
            with self.assertRaises(RuntimeError) as caught:
                await bench.measure_optional_positive_control(
                    self.session_factory
                )
        self.assertIs(caught.exception, boom)

    async def test_trigger_ratio_blocks_an_illegal_result(self) -> None:
        """工具自己拦住非法结果：放行数与配置上限不符就必须报错。

        这里让 ``reserve`` 永远放行，于是"20 并发 / limit=1"会得到 20 个 admitted，
        与 ``min(limit, concurrent) == 1`` 冲突 —— 测量必须炸掉，而不是把非法
        结果照样打印成"完成"。
        """

        from unittest.mock import AsyncMock, patch

        from bot.services import group_budget

        async def always_admit(self, *args, **kwargs):
            return group_budget.Reservation(
                None, True, True, ""
            )

        with patch.object(
            group_budget.GroupBudgetService, "reserve", always_admit
        ):
            with self.assertRaises(AssertionError) as caught:
                await bench.measure_trigger_ratio(
                    self.session_factory, limit=1, concurrent=4
                )
        self.assertIn("放行数", str(caught.exception))

    async def test_run_writes_a_json_snapshot(self) -> None:
        results = await bench.run(rounds=50, safety_rounds=4)
        self.assertIn("environment", results)
        for key in ("moderation_off", "moderation_on", "optional_positive_control"):
            self.assertIn(key, results)
        self.assertEqual(len(results["triggers"]), 2)
        # 报告里必须明说哪些东西**没有**被测到。
        self.assertIn("NOT exercised", results["environment"]["note"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
