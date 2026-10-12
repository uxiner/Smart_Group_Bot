"""预留与结算的**严格取数**（第八阶段必修第 4 条）。

四条边界：

* prompt/output 必须是**非负整数**；负数不构成"上界为 0"，不得放行；
* 上界对任何正的小数**向上取整**（0.1/百万 的 prompt=1 不得算成 0）；
* 结算时缺失字段与**坏值**不同：上游协议可选 = 未知并标 unknown；
  显式给错值（负数 / bool / 浮点 / 非有限）同样标 unknown 且**不退款**；
* 缓存写入字段坏掉时，不得把整笔钱退光。
"""

from __future__ import annotations

import math
import asyncio
import os
import tempfile
import unittest

from bot.db.engine import init_db
from bot.services import group_budget as budget
from bot.services import pricing


def _entry(**overrides) -> pricing.PriceEntry:
    payload = dict(
        provider_key="slot-a",
        model="fixture/main",
        currency="USD",
        input_per_million="0.1",
        output_per_million="0.1",
        cache_read_per_million="0.1",
        cache_write_per_million="0.1",
    )
    payload.update(overrides)
    return pricing.PriceEntry(**payload)


class MoneyPrecisionTests(unittest.TestCase):
    def test_fractional_price_is_ceiled_not_truncated(self) -> None:
        bound = pricing.conservative_upper_bound(
            _entry(), prompt_tokens=1, max_output_tokens=0
        )
        self.assertEqual(bound.micro, 1, "0.1 micro 被 int() 截成 0 = hard cap 永不触发")

    def test_fractional_result_rounds_up(self) -> None:
        entry = pricing.PriceEntry(
            provider_key="slot-a", model="m", currency="USD",
            input_per_million="0.19",
        )
        cost = pricing.estimate(entry, prompt_tokens=10)
        self.assertEqual(cost.micro, 2, "1.9 micro 必须记 2 而是 1")

    def test_zero_cap_denies_a_call_that_costs_a_fraction(self) -> None:
        entry = _entry()
        bound = pricing.conservative_upper_bound(
            entry, prompt_tokens=1, max_output_tokens=1
        )
        self.assertGreater(bound.micro, 0)


class StrictReservationUsageTests(unittest.TestCase):
    def _estimate(self, **kwargs) -> pricing.CostEstimate:
        entry = _entry(input_per_million="3", output_per_million="15")
        params = {"prompt_tokens": 10, "max_output_tokens": 5}
        params.update(kwargs)
        return pricing.conservative_upper_bound(entry, **params)

    def test_fractional_token_counts_are_rejected(self) -> None:
        for bad in (10.75, 1.75):
            with self.subTest(bad=bad):
                bound = self._estimate(prompt_tokens=bad)
                self.assertIsNone(bound.micro, f"{bad} 被当成了 token 数")

    def test_negative_prompt_is_not_treated_as_zero(self) -> None:
        bound = self._estimate(prompt_tokens=-200)
        self.assertIsNone(bound.micro, "负 prompt 被夹成 0 = 白送一次")

    def test_negative_output_is_not_treated_as_zero(self) -> None:
        bound = self._estimate(max_output_tokens=-1)
        self.assertIsNone(bound.micro)

    def test_bool_token_counts_are_rejected(self) -> None:
        bound = self._estimate(prompt_tokens=True)
        self.assertIsNone(bound.micro)


class StrictSettlementUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        budget.reset_shared_service()
        self.catalog = pricing.PriceCatalog(version=1, entries=(_entry(),))
        self.service = budget.shared_service(
            self.session_factory, lambda: self.catalog
        )

    async def asyncTearDown(self) -> None:
        budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _reserve(self) -> budget.Reservation:
        await self.service.save_budget(
            -100900000001, expected_revision=0, enabled=True, currency="USD",
            daily_attempt_limit=10, daily_token_limit=10_000,
        )
        return await self.service.reserve(
            group_id=-100900000001, purpose="optional", stage="main",
            provider="slot-a", model="fixture/main",
            prompt_tokens=100, max_output_tokens=20,
        )

    async def _day(self):
        return await self.service.day_state(-100900000001)

    async def test_bad_cached_value_marks_unknown_without_refunding(self) -> None:
        result = await self._reserve()
        self.assertTrue(result.admitted)
        await self.service.settle(
            result.reservation_id,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cached_tokens": -5,          # 坏值
                "cache_creation_input_tokens": 0,
            },
        )
        day = await self._day()
        self.assertEqual(int(day.unknown_attempts), 1, "坏值必须标 unknown")
        self.assertGreaterEqual(
            int(day.used_tokens), 120,
            "带坏值的 usage 不得把整笔预留退光（上游已经收了钱）",
        )

    async def test_bool_cached_value_is_treated_as_bad(self) -> None:
        result = await self._reserve()
        await self.service.settle(
            result.reservation_id,
            usage={"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": True},
        )
        day = await self._day()
        self.assertEqual(int(day.unknown_attempts), 1)
        self.assertGreaterEqual(int(day.used_tokens), 120)

    async def test_invalid_cache_write_is_not_silently_zeroed(self) -> None:
        result = await self._reserve()
        await self.service.settle(
            result.reservation_id,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cached_tokens": 0,
                "cache_creation_input_tokens": "abc",
            },
        )
        day = await self._day()
        self.assertEqual(int(day.unknown_attempts), 1)
        self.assertGreaterEqual(int(day.used_tokens), 120)

    async def test_optional_missing_field_is_unknown_not_zero(self) -> None:
        result = await self._reserve()
        await self.service.settle(
            result.reservation_id,
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )
        day = await self._day()
        # 缓存字段缺失是**协议可选**：用量本身有效，因此不算 unknown，
        # 但金额必须保持保守预留而不是退成 0。
        self.assertGreaterEqual(int(day.used_tokens), 120)
        self.assertGreaterEqual(int(day.used_money_micro), result.reserved_money_micro)

    async def test_fractional_tokens_are_not_accepted_at_settlement(self) -> None:
        result = await self._reserve()
        await self.service.settle(
            result.reservation_id,
            usage={"prompt_tokens": 10.75, "completion_tokens": 1.75},
        )
        day = await self._day()
        self.assertEqual(int(day.unknown_attempts), 1)
        self.assertGreaterEqual(int(day.used_tokens), 100)

    async def test_non_finite_tokens_are_not_accepted(self) -> None:
        service = budget.shared_service(self.session_factory, lambda: self.catalog)
        await service.save_budget(
            -100900000001, expected_revision=0, enabled=True, currency="USD",
            daily_attempt_limit=50,
        )
        for index, bad in enumerate((math.inf, math.nan, -math.inf)):
            with self.subTest(bad=bad):
                result = await service.reserve(
                    group_id=-100900000001, purpose="optional",
                    stage=f"main-{index}",
                    provider="slot-a", model="fixture/main",
                    prompt_tokens=10, max_output_tokens=5,
                )
                await service.settle(
                    result.reservation_id,
                    usage={"prompt_tokens": bad, "completion_tokens": 1},
                )
                day = await service.day_state(-100900000001)
                self.assertGreaterEqual(int(day.unknown_attempts), index + 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class ReserveEntryValidationTests(unittest.IsolatedAsyncioTestCase):
    """在**真正 reserve 入口**校验，非法输入不得被夹成 0 后放行。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        budget.reset_shared_service()
        self.catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="slot-a", model="m", currency="USD",
                    input_per_million="3", output_per_million="15",
                    cache_read_per_million="3", cache_write_per_million="3",
                ),
            ),
        )
        self.service = budget.shared_service(
            self.session_factory, lambda: self.catalog
        )

    async def asyncTearDown(self) -> None:
        budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _enable(self) -> None:
        await self.service.save_budget(
            -100900000001, expected_revision=0, enabled=True, currency="USD",
            daily_attempt_limit=10,
        )

    async def _reserve(self, **kwargs) -> budget.Reservation:
        params = {
            "group_id": -100900000001, "purpose": "optional", "stage": "main",
            "provider": "slot-a", "model": "m", "prompt_tokens": 10,
            "max_output_tokens": 5,
        }
        params.update(kwargs)
        return await self.service.reserve(**params)

    async def test_negative_prompt_is_refused_not_clipped_to_zero(self) -> None:
        await self._enable()
        result = await self._reserve(prompt_tokens=-200)
        self.assertFalse(result.admitted, "负 prompt 被夹成 0 后放行 = 白送一次")
        day = await self.service.day_state(-100900000001)
        self.assertEqual(int(day.used_attempts), 0)

    async def test_negative_output_is_refused(self) -> None:
        await self._enable()
        result = await self._reserve(max_output_tokens=-1)
        self.assertFalse(result.admitted)

    async def test_float_and_bool_inputs_are_refused(self) -> None:
        await self._enable()
        for kwargs in (
            {"prompt_tokens": 10.75},
            {"prompt_tokens": True},
            {"max_output_tokens": False},
        ):
            with self.subTest(**kwargs):
                result = await self._reserve(**kwargs)
                self.assertFalse(result.admitted, kwargs)

    async def test_legitimate_zero_is_still_accepted(self) -> None:
        """0 是合法值：不能因为"拒绝坏值"把空请求也一起拒了。"""

        await self._enable()
        result = await self._reserve(prompt_tokens=0, max_output_tokens=0)
        self.assertTrue(result.admitted)

    async def test_string_digits_are_accepted(self) -> None:
        await self._enable()
        result = await self._reserve(prompt_tokens="100", max_output_tokens="20")
        self.assertTrue(result.admitted)

    async def test_disabled_budget_still_takes_the_fast_path(self) -> None:
        """护栏关闭时严格校验不得让每次调用去读库。"""

        reads = {"n": 0}
        inner = self.session_factory

        class _Counting:
            def __call__(self, *args, **kwargs):
                reads["n"] += 1
                return inner()

        self.service._session_factory = _Counting()
        for _ in range(20):
            result = await self._reserve(prompt_tokens=10, max_output_tokens=5)
            self.assertTrue(result.admitted)
        self.assertLessEqual(reads["n"], 1)

    async def test_safety_bypasses_validation_and_admission(self) -> None:
        await self._enable()
        result = await self._reserve(
            purpose="safety", stage="moderation", prompt_tokens=-200
        )
        self.assertTrue(result.admitted, "安全审核不得被可选护栏挡下")


class FloatAndUnknownReserveTests(unittest.IsolatedAsyncioTestCase):
    """float 一律拒；token-only cap 下的显式 None 不得当 0 放行。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        budget.reset_shared_service()
        self.catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="slot-a", model="m", currency="USD",
                    input_per_million="3", output_per_million="15",
                    cache_read_per_million="1", cache_write_per_million="4",
                ),
            ),
        )
        self.service = budget.shared_service(
            self.session_factory, lambda: self.catalog
        )

    async def asyncTearDown(self) -> None:
        budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _enable(self, **limits) -> None:
        await self.service.save_budget(
            -100900000001, expected_revision=0, enabled=True, currency="USD",
            **limits,
        )

    async def _reserve(self, **kwargs) -> budget.Reservation:
        params = {
            "group_id": -100900000001, "purpose": "optional", "stage": "main",
            "provider": "slot-a", "model": "m", "prompt_tokens": 10,
            "max_output_tokens": 2,
        }
        params.update(kwargs)
        return await self.service.reserve(**params)

    # ------------------------------------------------------------------
    async def test_integral_floats_are_rejected(self) -> None:
        """10.0 看起来像 10，但护栏只接受**无损**的整数。"""

        await self._enable(daily_attempt_limit=10)
        for kwargs in ({"prompt_tokens": 10.0}, {"max_output_tokens": 10.0}):
            with self.subTest(**kwargs):
                result = await self._reserve(**kwargs)
                self.assertFalse(result.admitted, kwargs)
        day = await self.service.day_state(-100900000001)
        self.assertEqual(int(day.used_attempts), 0)
        self.assertEqual(int(day.used_tokens), 0)

    async def test_explicit_none_is_refused_under_a_token_cap(self) -> None:
        await self._enable(daily_attempt_limit=10, daily_token_limit=100)
        for kwargs in ({"prompt_tokens": None}, {"max_output_tokens": None}):
            with self.subTest(**kwargs):
                result = await self._reserve(**kwargs)
                self.assertFalse(
                    result.admitted,
                    f"{kwargs} 是'未知上界'，在 token 上限下不能当成 0 放行",
                )
        day = await self.service.day_state(-100900000001)
        self.assertEqual(int(day.used_tokens), 0)
        self.assertEqual(int(day.used_attempts), 0)

    async def test_explicit_none_is_refused_under_a_money_cap(self) -> None:
        await self._enable(
            daily_attempt_limit=10, daily_money_micro_limit=1000,
        )
        result = await self._reserve(prompt_tokens=None)
        self.assertFalse(result.admitted)

    async def test_unknown_bound_is_fine_when_no_bound_is_in_force(self) -> None:
        """只设了 attempt 上限时，未知 token 不会让"免费"变成真实风险。"""

        await self._enable(daily_attempt_limit=10)
        result = await self._reserve(prompt_tokens=None, max_output_tokens=None)
        self.assertTrue(result.admitted)

    async def test_legitimate_zero_and_digit_strings_still_accepted(self) -> None:
        await self._enable(daily_attempt_limit=10, daily_token_limit=1000)
        self.assertTrue((await self._reserve(prompt_tokens=0, max_output_tokens=0)).admitted)
        self.assertTrue((await self._reserve(prompt_tokens="10", max_output_tokens="2")).admitted)

    async def test_safety_is_unaffected_by_input_strictness(self) -> None:
        await self._enable(daily_attempt_limit=0, daily_token_limit=1)
        result = await self._reserve(purpose="safety", stage="moderation")
        self.assertTrue(result.admitted, "安全审核不得被可选护栏的输入校验挡下")


class UnknownIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    """重复 unknown 幂等一次；known 只 reconcile 一次；状态可恢复。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        budget.reset_shared_service()
        self.catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="slot-a", model="m", currency="USD",
                    input_per_million="3", output_per_million="15",
                    cache_read_per_million="1", cache_write_per_million="4",
                ),
            ),
        )
        self.service = budget.shared_service(
            self.session_factory, lambda: self.catalog
        )
        await self.service.save_budget(
            -100900000001, expected_revision=0, enabled=True, currency="USD",
            daily_attempt_limit=10, daily_token_limit=10_000,
            daily_money_micro_limit=10_000_000,
        )
        self.service.invalidate()

    async def asyncTearDown(self) -> None:
        budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _fresh_row(self):
        from bot.db.models import GroupBudgetReservation
        from sqlalchemy import select

        async with self.session_factory() as session:
            return (
                await session.scalars(select(GroupBudgetReservation))
            ).one()

    async def _fresh_day(self):
        from bot.db.models import GroupBudgetDay
        from sqlalchemy import select

        async with self.session_factory() as session:
            return (
                await session.scalars(select(GroupBudgetDay))
            ).one()

    async def test_repeated_unknown_is_counted_once_and_stays_recoverable(self) -> None:
        result = await self.service.reserve(
            group_id=-100900000001, purpose="optional", stage="main",
            provider="slot-a", model="m", prompt_tokens=10, max_output_tokens=2,
        )
        await self.service.settle(result.reservation_id, usage=None, unknown=True)
        await self.service.settle(result.reservation_id, usage=None, unknown=True)
        day = await self._fresh_day()
        self.assertEqual(
            int(day.unknown_attempts), 1,
            "同一笔预留上报两次未知就累加了两次",
        )
        row = await self._fresh_row()
        self.assertEqual(str(row.status), "unknown", f"卡在 {row.status}，已知不了")

    async def test_known_usage_reconciles_once_after_repeated_unknown(self) -> None:
        result = await self.service.reserve(
            group_id=-100900000001, purpose="optional", stage="main",
            provider="slot-a", model="m", prompt_tokens=10, max_output_tokens=2,
        )
        await self.service.settle(result.reservation_id, usage=None, unknown=True)
        await self.service.settle(result.reservation_id, usage=None, unknown=True)
        known = {
            "prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 0,
            "cache_creation_input_tokens": 0, "reasoning_tokens": 0,
        }
        await self.service.settle(result.reservation_id, usage=known)
        await self.service.settle(result.reservation_id, usage=known)
        row = await self._fresh_row()
        self.assertEqual(str(row.status), "settled", f"fresh SQL 里是 {row.status}")
        self.assertEqual(int(row.actual_tokens), 12)
        self.assertEqual(
            int(row.actual_money_micro), 60,
            "实际费用必须取发送时价格快照且只调整一次（10×3 + 2×15 = 60）",
        )
        day = await self._fresh_day()
        self.assertEqual(int(day.unknown_attempts), 0)
        self.assertEqual(int(day.used_tokens), 12)
        self.assertEqual(int(day.used_money_micro), 60)
        self.assertEqual(int(day.used_attempts), 1)


class ConcurrentSettleTests(unittest.IsolatedAsyncioTestCase):
    """并发 settle：抢占与"先前状态"必须来自**同一个原子状态转移**。

    之前是「先 SELECT status 再条件 UPDATE」，而读时还没拿到项目写锁：
    两个调用同时读到 reserved，各自都以为自己是第一次转移。
    """

    KNOWN = {
        "prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        budget.reset_shared_service()
        self.catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="slot-a", model="m", currency="USD",
                    input_per_million="3", output_per_million="15",
                    cache_read_per_million="1", cache_write_per_million="4",
                ),
            ),
        )
        self.service = budget.shared_service(
            self.session_factory, lambda: self.catalog
        )

    async def asyncTearDown(self) -> None:
        budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _race(self, first_known: bool, second_known: bool) -> dict:
        import asyncio as _asyncio

        from bot.db.models import GroupBudgetReservation
        from sqlalchemy import select

        await self.service.save_budget(
            -100900000001, expected_revision=0, enabled=True, currency="USD",
            daily_attempt_limit=10, daily_token_limit=1000,
        )
        self.service.invalidate()
        result = await self.service.reserve(
            group_id=-100900000001, purpose="optional", stage="main",
            provider="slot-a", model="m", prompt_tokens=10, max_output_tokens=2,
        )

        async def settle(known: bool):
            return await self.service.settle(
                result.reservation_id,
                usage=self.KNOWN if known else None,
                unknown=not known,
            )

        # 确定性竞态：gate 的位置**随实现形态而变**——
        #   * 旧路径先 SELECT status：两次**读**都必须先完成，两边才会各自拿到
        #     "我是第一次转移"的错误结论；
        #   * 新路径第一条就是 UPDATE：在**进入这条写**之前同步。
        # 两种形态都把 gate 放在**读写之外**（读之后 / 写之前），绝不放在持写锁
        # 的事务里等对方 —— 那会直接死锁。first 等 second 到场，second 等
        # first 整个 settle 事务退出。
        from bot.db.sqlite_session import SQLiteSafeAsyncSession

        original_execute = SQLiteSafeAsyncSession.execute
        both_arrived = asyncio.Event()
        first_done = asyncio.Event()

        def is_write(statement) -> bool:
            return type(statement).__name__.lower().startswith("update")

        async def instrumented(session, statement, *args, **kwargs):
            name = asyncio.current_task().get_name()
            if is_write(statement):
                if name == "second":
                    both_arrived.set()
                    await asyncio.wait_for(first_done.wait(), 5)
                else:
                    await asyncio.wait_for(both_arrived.wait(), 5)
            return await original_execute(session, statement, *args, **kwargs)

        async def call(known: bool, name: str):
            try:
                return await settle(known)
            finally:
                if name == "first":
                    first_done.set()

        from unittest.mock import patch as _patch

        with _patch.object(SQLiteSafeAsyncSession, "execute", instrumented):
            outcomes = await asyncio.gather(
                asyncio.create_task(call(first_known, "first"), name="first"),
                asyncio.create_task(call(second_known, "second"), name="second"),
                return_exceptions=True,
            )
        async with self.session_factory() as session:
            row = (
                await session.scalars(select(GroupBudgetReservation))
            ).one()
            status = str(row.status)
        day = (await self.service.day_state(-100900000001)).document()
        return {
            "status": status,
            "unknown": int(day["unknown_attempts"]),
            "tokens": int(day["used_tokens"]),
            "money": int(day["used_money_micro"]),
            "errors": [repr(x) for x in outcomes if isinstance(x, BaseException)],
        }

    async def test_two_concurrent_unknowns_count_once(self) -> None:
        outcome = await self._race(False, False)
        self.assertEqual(outcome["errors"], [])
        self.assertEqual(outcome["status"], "unknown")
        self.assertEqual(outcome["unknown"], 1, "两次并发 unknown 各记了一次")

    async def test_unknown_then_concurrent_known_reconciles_once(self) -> None:
        outcome = await self._race(False, True)
        self.assertEqual(outcome["errors"], [])
        self.assertEqual(outcome["status"], "settled")
        self.assertEqual(outcome["unknown"], 0, "已知结果必须把 unknown 减回去")
        self.assertEqual(outcome["tokens"], 12)
        self.assertEqual(outcome["money"], 60, "10×3 + 2×15，只调整一次")

    async def test_known_then_concurrent_unknown_is_a_control(self) -> None:
        outcome = await self._race(True, False)
        self.assertEqual(outcome["errors"], [])
        self.assertEqual(outcome["status"], "settled")
        self.assertEqual(outcome["unknown"], 0)
        self.assertEqual(outcome["tokens"], 12)
        self.assertEqual(outcome["money"], 60)

    async def test_concurrent_settle_and_release_do_not_double_count(self) -> None:
        import asyncio as _asyncio

        from bot.db.models import GroupBudgetDay
        from sqlalchemy import select

        await self.service.save_budget(
            -100900000001, expected_revision=0, enabled=True, currency="USD",
            daily_attempt_limit=10,
        )
        self.service.invalidate()
        result = await self.service.reserve(
            group_id=-100900000001, purpose="optional", stage="main",
            provider="slot-a", model="m", prompt_tokens=100,
        )
        await _asyncio.gather(
            self.service.settle(result.reservation_id, usage=self.KNOWN),
            self.service.release(result.reservation_id),
            return_exceptions=True,
        )
        async with self.session_factory() as session:
            day = (await session.scalars(select(GroupBudgetDay))).one()
        self.assertGreaterEqual(int(day.used_attempts), 0)
        self.assertGreaterEqual(int(day.used_tokens), 0, "并发结算/释放不得把累计压成负数")
