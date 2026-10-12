"""预算与计价的原子性（第六阶段返工）。

这组用例全部只做一件事：**证明钱不会被算错，也不会被退掉**。

* 金额上限必须绑定到**实际候选**；未知模型 / 跨币种 / 上界不可信都要拒绝，
  不得当 0 放行。
* 价格快照必须**持久化**：在飞改价、重启之后结算，都按发送时那一版。
* 上界必须覆盖**所有**输入分支（缓存可能比普通输入更贵）且不能被小数吃掉。
* usage 缺失/畸形**不得**退额度。
* settle / release / 预算保存必须**原子 CAS**（并发双退 = 额度凭空多出来）。
* 回退候选按**它自己**的价格计价。
* 调用者取消后立即结算为 unknown，不等租约超时。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from sqlalchemy import select

from bot.config import ModelConfig
from bot.db.engine import init_db
from bot.db.models import GroupBudget, GroupBudgetDay, GroupBudgetReservation
from bot.services import group_budget as budget
from bot.services import pricing
from bot.services.llm import LLMService

GROUP_A = -100900000001
GROUP_B = -100900000002


def _catalog(**overrides) -> pricing.PriceCatalog:
    entry = dict(
        provider_key="slot-a",
        model="fixture/main",
        currency="USD",
        input_per_million="3",
        output_per_million="15",
        cache_read_per_million="1",
        cache_write_per_million="4",
    )
    entry.update(overrides)
    return pricing.PriceCatalog(version=overrides.pop("version", 1), entries=(pricing.PriceEntry(**entry),))


class BudgetAtomicityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.catalog = _catalog()
        self.service = budget.GroupBudgetService(
            session_factory=self.session_factory,
            catalog_provider=lambda: self.catalog,
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _day(self, group_id: int = GROUP_A) -> GroupBudgetDay:
        async with self.session_factory() as session:
            row = await session.get(GroupBudgetDay, (group_id, budget._today()))
            self.assertIsNotNone(row)
            return row

    async def _reservations(self) -> list[GroupBudgetReservation]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(GroupBudgetReservation))).all())

    # ------------------------------------------------------------------
    # P6-1 金额上限必须绑定实际候选 / 币种 / 上界
    # ------------------------------------------------------------------
    async def test_money_cap_refuses_an_unpriced_candidate(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_money_micro_limit=1000,
            money_covers=[("slot-a", "fixture/main")],
        )
        self.service.invalidate()
        unknown = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-zzz",
            model="unpriced/model",
            prompt_tokens=100,
            max_output_tokens=20,
        )
        self.assertFalse(unknown.admitted, "未知模型不得当 0 金额放行")
        self.assertEqual(unknown.reason, budget.REASON_MONEY_UNPRICED)

    async def test_money_cap_refuses_a_currency_mismatch(self) -> None:
        self.catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="slot-a",
                    model="fixture/main",
                    currency="CNY",
                    input_per_million="3",
                    output_per_million="15",
                ),
            ),
        )
        with self.assertRaises(budget.BudgetError):
            await self.service.save_budget(
                GROUP_A,
                expected_revision=0,
                enabled=True,
                currency="USD",
                daily_money_micro_limit=1000,
                money_covers=[("slot-a", "fixture/main")],
            )

    async def test_money_cap_refuses_an_untrusted_upper_bound(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_money_micro_limit=1000,
            money_covers=[("slot-a", "fixture/main")],
        )
        self.service.invalidate()
        # 上界不可信（prompt_tokens 为 None）：不得按 0 元放行。
        # （max_output_tokens=None 同理：没有输出上限就没有金额上界。）
        result = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=None,
            max_output_tokens=None,
        )
        self.assertFalse(result.admitted)
        self.assertIn(
            result.reason, budget.REASON_MONEY_UNPRICED,
        )

    async def test_settlement_uses_the_stored_snapshot_even_if_the_catalog_is_empty(
        self,
    ) -> None:
        """目录被清空后结算仍按**发送时**那一份单价，而不是变成 0 或未知。"""

        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_attempt_limit=10,
        )
        self.service.invalidate()
        result = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=100,
            max_output_tokens=20,
        )
        self.assertTrue(result.admitted)
        self.catalog = pricing.PriceCatalog(version=2, entries=())
        await self.service.settle(
            result.reservation_id, usage={"prompt_tokens": 100, "completion_tokens": 20}
        )
        day = await self._day()
        # 快照还在行上，所以金额照常算出来（600），而不是被当成 0 或未知。
        self.assertEqual(int(day.used_money_micro), 600)

    # ------------------------------------------------------------------
    # P6-2 价格快照必须持久化
    # ------------------------------------------------------------------
    async def test_settlement_uses_the_send_time_price_not_the_current_one(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_attempt_limit=10,
            daily_money_micro_limit=10_000_000,
        )
        self.service.invalidate()
        first = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=100,
            max_output_tokens=20,
        )
        self.assertTrue(first.admitted)
        self.assertEqual(first.reserved_money_micro, 100 * 4 + 20 * 15)

        # 在飞把价格翻三倍。
        self.catalog = pricing.PriceCatalog(
            version=2,
            entries=(
                pricing.PriceEntry(
                    provider_key="slot-a",
                    model="fixture/main",
                    currency="USD",
                    input_per_million="9",
                    output_per_million="45",
                    cache_read_per_million="3",
                    cache_write_per_million="12",
                ),
            ),
        )
        await self.service.settle(
            first.reservation_id,
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )
        day = await self._day()
        self.assertEqual(
            int(day.used_money_micro),
            600,
            "在飞改价不得改写已发出那次调用的账",
        )

    async def test_settlement_uses_the_snapshot_after_a_restart(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_attempt_limit=10,
            daily_money_micro_limit=10_000_000,
        )
        self.service.invalidate()
        result = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=100,
            max_output_tokens=20,
        )
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(GroupBudgetReservation, result.reservation_id)
                self.assertEqual(int(row.price_version), 1)
                self.assertIsNotNone(
                    getattr(row, "price_snapshot", None),
                    "预留行必须持久化完整价格快照，只存版本号不够",
                )
        # 换一个新 service（= 重启），并把目录清空：结算仍按快照。
        rebooted = budget.GroupBudgetService(
            session_factory=self.session_factory,
            catalog_provider=lambda: pricing.PriceCatalog(version=9, entries=()),
        )
        await rebooted.settle(
            result.reservation_id,
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )
        day = await self._day()
        self.assertEqual(int(day.used_money_micro), 600)

    # ------------------------------------------------------------------
    # P6-3 上界必须覆盖所有输入分支
    # ------------------------------------------------------------------
    async def test_upper_bound_covers_an_expensive_cache_branch(self) -> None:
        """缓存单价可能比普通输入更贵：上界必须按最贵的那一支算。"""

        entry = pricing.PriceEntry(
            provider_key="slot-a",
            model="fixture/main",
            currency="USD",
            input_per_million="1",
            cache_read_per_million="10",
            cache_write_per_million="20",
            output_per_million="1",
        )
        bound = pricing.conservative_upper_bound(
            entry, prompt_tokens=100, max_output_tokens=0
        )
        # 上界取 max(input=1, cache_read=10, cache_write=20) = 20 —— 连写入那支
        # 都要覆盖，因为发送前无法知道最终会不会写缓存。
        self.assertEqual(bound.micro, 100 * 20)
        # 真结算回 usage（全部命中缓存）自然不超过这个上界。
        actual = pricing.estimate(
            entry, prompt_tokens=100, output_tokens=0, cached_tokens=100
        )
        self.assertLessEqual(int(actual.micro or 0), int(bound.micro or 0))

    async def test_tiny_prices_are_not_rounded_to_free(self) -> None:
        entry = pricing.PriceEntry(
            provider_key="slot-a",
            model="m",
            currency="USD",
            input_per_million="0.1",
            output_per_million="0.1",
        )
        cost = pricing.estimate(entry, prompt_tokens=1, output_tokens=1)
        # 0.1 + 0.1 = 0.2 micro，不足 1 micro → **向上取整到 1**，绝不能是 0
        # （0 意味着这类模型的调用永远免费）。
        self.assertEqual(cost.micro, 1)

    async def test_unbounded_output_cannot_claim_a_money_cap_holds(self) -> None:
        entry = pricing.PriceEntry(
            provider_key="slot-a",
            model="m",
            currency="USD",
            input_per_million="1",
            output_per_million="1",
        )
        bound = pricing.conservative_upper_bound(entry, prompt_tokens=100, max_output_tokens=None)
        self.assertIsNone(bound.micro, "输出无硬上限时不能声称金额上界成立")

    # ------------------------------------------------------------------
    # P6-4 畸形 usage 不得退额度
    # ------------------------------------------------------------------
    async def test_missing_usage_does_not_refund_the_reserve(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_attempt_limit=10,
            daily_token_limit=120,
        )
        self.service.invalidate()
        first = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=100,
            max_output_tokens=20,
        )
        self.assertTrue(first.admitted)
        await self.service.settle(first.reservation_id, usage={})
        day = await self._day()
        self.assertGreaterEqual(
            int(day.used_tokens), 100, "usage={} 不得把预留退光"
        )
        self.assertGreaterEqual(int(day.unknown_attempts), 0)

    async def test_negative_usage_does_not_refund_or_go_negative(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_attempt_limit=10,
        )
        self.service.invalidate()
        first = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=100,
            max_output_tokens=20,
        )
        await self.service.settle(
            first.reservation_id,
            usage={"prompt_tokens": -200, "completion_tokens": -5},
        )
        day = await self._day()
        self.assertGreaterEqual(int(day.used_tokens), 0, "负 usage 让累计变负 = 额度凭空多出来")
        self.assertGreaterEqual(int(day.used_tokens), 100)

    # ------------------------------------------------------------------
    # P6-5 settle / release 原子
    # ------------------------------------------------------------------
    async def test_concurrent_settle_settles_exactly_once(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_attempt_limit=10,
            daily_token_limit=1000,
        )
        self.service.invalidate()
        result = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=100,
            max_output_tokens=20,
        )
        await asyncio.gather(
            *(
                self.service.settle(
                    result.reservation_id,
                    usage={"prompt_tokens": 50, "completion_tokens": 0},
                )
                for _ in range(2)
            )
        )
        day = await self._day()
        self.assertEqual(int(day.used_tokens), 50, "并发 settle 退了两倍")

    async def test_concurrent_release_releases_exactly_once(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_attempt_limit=10,
        )
        self.service.invalidate()
        first = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=100,
        )
        second = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=100,
        )
        outcomes = await asyncio.gather(
            self.service.release(first.reservation_id),
            self.service.release(first.reservation_id),
        )
        self.assertEqual(sum(1 for item in outcomes if item), 1)
        day = await self._day()
        self.assertEqual(
            int(day.used_attempts), 1, "重复 release 把另一条的在飞占用也退了"
        )
        rows = await self._reservations()
        self.assertEqual(
            sorted(str(row.status) for row in rows), ["released", "reserved"]
        )
        self.assertIsNotNone(second.reservation_id)

    # ------------------------------------------------------------------
    # P6-6 预算保存的 CAS
    # ------------------------------------------------------------------
    async def test_concurrent_budget_saves_do_not_lose_updates(self) -> None:
        await self.service.save_budget(
            GROUP_A, expected_revision=0, enabled=False, currency="USD"
        )
        results = await asyncio.gather(
            *(
                self.service.save_budget(
                    GROUP_A,
                    expected_revision=1,
                    enabled=True,
                    currency="USD",
                    daily_attempt_limit=index + 1,
                )
                for index in range(2)
            ),
            return_exceptions=True,
        )
        ok = [item for item in results if not isinstance(item, BaseException)]
        conflicts = [
            item for item in results if isinstance(item, budget.BudgetConflict)
        ]
        self.assertEqual(len(ok), 1, [type(r).__name__ for r in results])
        self.assertEqual(len(conflicts), 1)
        async with self.session_factory() as session:
            row = await session.get(GroupBudget, GROUP_A)
        self.assertEqual(int(row.revision), 2, "只成功一次时版本号只能 +1")

    async def test_malformed_limits_are_refused_not_treated_as_unlimited(self) -> None:
        for bad in ("not-an-integer", 2.5, True, "-3"):
            with self.subTest(bad=bad):
                with self.assertRaises(budget.BudgetError):
                    await self.service.save_budget(
                        GROUP_A,
                        expected_revision=0,
                        enabled=True,
                        daily_attempt_limit=bad,
                    )

    # ------------------------------------------------------------------
    # P6-7 回退候选按自己的价格
    # ------------------------------------------------------------------
    async def test_fallback_is_priced_with_its_own_candidate(self) -> None:
        """**已知未关闭**：这条用例当前仍红（结算落在主候选而非回退候选）。

        保留它而不是删掉：它描述的是一条真实未闭合的缺陷 —— 回退候选的预留与
        结算还没有按**它自己**的槽身份对齐。下一轮从这里接手。
        """

        self.catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                # 四段单价必须**齐备**：缺任何一段就报不出金额上界，护栏会
                # （正确地）拒绝放行而不是按 0 放行。
                pricing.PriceEntry(
                    provider_key="slot-a",
                    model="fixture/main",
                    currency="USD",
                    input_per_million="3",
                    output_per_million="15",
                    cache_read_per_million="3",
                    cache_write_per_million="3",
                ),
                pricing.PriceEntry(
                    provider_key="slot-b",
                    model="fixture/main",
                    currency="USD",
                    input_per_million="30",
                    output_per_million="150",
                    cache_read_per_million="30",
                    cache_write_per_million="30",
                ),
            ),
        )
        primary = ModelConfig(
            model="fixture/main", slot="slot-a",
            provider="openai_compatible", api_key="k", retry_attempts=1,
            retry_backoff_sec=0.0, api_base="https://a.example.invalid/v1",
        )
        fallback = ModelConfig(
            model="fixture/main", slot="slot-b",
            provider="openai_compatible", api_key="k",
            api_base="https://b.example.invalid/v1",
        )
        service = LLMService(primary, ModelConfig(
            model="fixture/decision", slot="slot-a",
            provider="openai_compatible", api_key="k",
        ))
        service.main.fallbacks = [fallback]
        guard_service = budget.GroupBudgetService(
            session_factory=self.session_factory,
            catalog_provider=lambda: self.catalog,
        )
        guard = budget.llm_budget_guard(guard_service)

        from types import SimpleNamespace

        async def provider(**kwargs):
            if "a.example.invalid" in str(kwargs.get("api_base") or ""):
                raise RuntimeError("primary down")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="b", tool_calls=None))],
                usage=SimpleNamespace(
                    prompt_tokens=100, completion_tokens=20,
                    cached_tokens=0, cache_creation_input_tokens=0, thinking_tokens=0,
                ),
            )

        from unittest.mock import patch

        from bot.services import llm as llm_module
        from bot.services import usage_scope as scopes

        await guard_service.save_budget(
            GROUP_A, expected_revision=0, enabled=True, currency="USD",
            daily_attempt_limit=10, daily_money_micro_limit=10_000_000,
        )
        guard_service.invalidate()
        with patch.object(llm_module.litellm, "acompletion", provider):
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                text = await service.chat(
                    [{"role": "user", "content": "hi"}],
                    cost_guard=guard("slot-a", "fixture/main"),
                )
        self.assertEqual(text, "b")
        rows = await self._reservations()
        # 成功的那次必须按 slot-b 的价格预留，不能按主模型 slot-a。
        settled = [row for row in rows if str(row.status) == "settled"]
        self.assertEqual(len(settled), 1, [str(r.status) for r in rows])
        self.assertEqual(str(settled[0].provider), "slot-b")
        self.assertEqual(int(settled[0].actual_money_micro), 100 * 30 + 20 * 150)

    # ------------------------------------------------------------------
    # P6-8 调用者取消后立即结算
    # ------------------------------------------------------------------
    async def test_caller_cancellation_settles_immediately_as_unknown(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_attempt_limit=10,
        )
        self.service.invalidate()
        result = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=100,
        )
        rows = await self._reservations()
        self.assertEqual(str(rows[0].status), "reserved")
        await self.service.release(result.reservation_id, reason="cancelled_after_send")
        rows = await self._reservations()
        self.assertEqual(
            str(rows[0].status), "unknown",
            "已发出的调用取消后必须立刻 unknown，不能等 300s 租约回收",
        )

    # ------------------------------------------------------------------
    # P6-10 默认与性能合同
    # ------------------------------------------------------------------
    async def test_zero_cap_is_a_real_cap_not_unlimited(self) -> None:
        await self.service.save_budget(
            GROUP_A,
            expected_revision=0,
            enabled=True,
            currency="USD",
            daily_attempt_limit=0,
        )
        self.service.invalidate()
        result = await self.service.reserve(
            group_id=GROUP_A,
            purpose="optional",
            stage="main",
            provider="slot-a",
            model="fixture/main",
            prompt_tokens=1,
        )
        self.assertFalse(result.admitted)
        snapshot = await self.service.snapshot(GROUP_A)
        self.assertTrue(
            snapshot["protecting"], "上限 0 是有效零额度，不得显示成未启用"
        )

    async def test_disabled_budget_does_not_touch_the_database(self) -> None:
        """关闭快路径不得每次调用都开一次 session。

        契约是「**一个进程一次**探测之后就不再读库」：探测本身要读一次（否则
        根本不知道有没有人配过预算），但配置一改就 invalidate，所以代价是常数级
        而不是每次调用一次。
        """

        await self.service.save_budget(
            GROUP_A, expected_revision=0, enabled=False, currency="USD"
        )
        self.service.invalidate()
        # 第一次调用付出那次探测。
        await self.service.reserve(
            group_id=GROUP_A, purpose="optional", stage="main",
            provider="slot-a", model="fixture/main", prompt_tokens=10,
        )

        reads = {"n": 0}

        class _Counting:
            def __init__(self, inner):
                self._inner = inner

            def __call__(self, *args, **kwargs):
                reads["n"] += 1
                return self._inner()

        original = self.service._session_factory
        self.service._session_factory = _Counting(original)
        try:
            for _ in range(20):
                result = await self.service.reserve(
                    group_id=GROUP_A,
                    purpose="optional",
                    stage="main",
                    provider="slot-a",
                    model="fixture/main",
                    prompt_tokens=10,
                )
                self.assertTrue(result.admitted)
                self.assertFalse(result.enforced)
        finally:
            self.service._session_factory = original
        self.assertEqual(reads["n"], 0, "探测之后关闭快路径仍然在开 session")

    async def test_unknown_store_failure_is_ignored_when_disabled(self) -> None:
        # 先让服务知道"全局没有任何预算配置"，存储故障才可能被正确地当成"无护栏"。
        original = self.service._session_factory

        def explode(*args, **kwargs):
            raise RuntimeError("budget store unavailable")

        self.service._session_factory = explode
        self.service.invalidate()
        # invalidate 会把进程级标记清回 None；必须在它**之后**才声明"没配过预算"，
        # 否则服务面对故障只能 fail closed（那正是我们要区分的两种情况）。
        self.service._any_budget_seen = False
        try:
            result = await self.service.reserve(
                group_id=GROUP_A,
                purpose="optional",
                stage="main",
                provider="slot-a",
                model="fixture/main",
                prompt_tokens=10,
            )
            # 已知关闭 → 存储故障不该把它变成拒绝。
            self.assertTrue(result.admitted, result.reason)
        finally:
            self.service._session_factory = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()