"""定价与预算原子预留（Task 6）。

被钉死的不变量：

* **关闭时零数据库读取**：护栏默认 OFF，off 快路径不得每次调用都读库。
* **预留发生在发送之前**：每次 retry / fallback / 工具步都要**各自**预留；
  只在最终成功后才记 quota 等于没设限。
* **并发下只放行额度内的条数**：单群每日 attempt=1，20 条并发只有 1 条真的
  到达 provider；另一个群不受影响。
* **safety 永远不被可选护栏挡下**：同群可选额度用尽、甚至预算存储异常时，
  审核仍然执行。
* **响应未知不退款**：已发出但超时/结果未知的 attempt 必须保留保守预留并
  显式标记 unknown，不能当成免费。
* **重启不重置当天额度**，过期租约可修复，不永久泄漏预留。
* **金额不是 0 也不是估算**：缺单价 → 该部分金额 unknown；不同币种不相加；
  改价不追改历史账。
* **缓存不重复计费**：input 100 / cache 40 / output 20 → 60×input + 40×cache-read
  + 20×output（除以一百万）。
* **操作次数配额**：非 LLM（TTS / 媒体 / 搜索）仍受功能操作次数限制，
  且明确标 unmetered，不伪装成有账单。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select

from bot.config import EmbedConfig, ModelConfig, Settings
from bot.db.engine import init_db
from bot.db.models import GroupBudget, GroupBudgetReservation
from bot.services import group_budget as budget
from bot.services import group_costs
from bot.services import pricing
from bot.services import usage_scope as scopes
from bot.services.llm import LLMService

GROUP_A = -100900000001
GROUP_B = -100900000002


def _model(name: str = "fixture/model-a") -> ModelConfig:
    return ModelConfig(
        model=name,
        provider="openai_compatible",
        api_base="https://gateway.example.invalid/v1",
        api_key="fixture-key",
        retry_attempts=1,
        retry_backoff_sec=0.0,
        total_deadline_sec=20.0,
    )


class PricingTests(unittest.TestCase):
    def test_price_catalog_validation_rejects_bad_values(self) -> None:
        with self.assertRaises(ValueError):
            pricing.PriceEntry(
                provider_key="fixture",
                model="m",
                currency="USD",
                input_per_million="-1",
            )
        with self.assertRaises(ValueError):
            pricing.PriceEntry(
                provider_key="fixture", model="m", currency="USD", input_per_million="NaN"
            )
        with self.assertRaises(ValueError):
            pricing.PriceEntry(
                provider_key="fixture", model="m", currency="", input_per_million="1"
            )

    def test_cache_is_not_double_counted(self) -> None:
        entry = pricing.PriceEntry(
            provider_key="fixture",
            model="m",
            currency="USD",
            input_per_million="3",
            output_per_million="15",
            cache_read_per_million="1",
            cache_write_per_million="4",
        )
        cost = pricing.estimate(
            entry,
            prompt_tokens=100,
            output_tokens=20,
            cached_tokens=40,
            cache_write_tokens=0,
        )
        # (100-40)×3 + 40×1 + 20×15 = 180+40+300 = 520，按百万计。
        self.assertEqual(Decimal(cost.micro), Decimal(520))
        self.assertEqual(cost.unknown_components, ())
        self.assertEqual(cost.uncached_input_tokens, 60)
        self.assertEqual(cost.cache_read_tokens, 40)

    def test_missing_component_price_is_unknown_not_zero(self) -> None:
        entry = pricing.PriceEntry(
            provider_key="fixture", model="m", currency="USD", input_per_million="3"
        )
        cost = pricing.estimate(
            entry, prompt_tokens=100, output_tokens=20, cached_tokens=0
        )
        self.assertIsNone(cost.micro)
        self.assertIn("output", cost.unknown_components)

    def test_unknown_price_key_yields_a_null_amount(self) -> None:
        catalog = pricing.PriceCatalog(version=1, entries=())
        cost = pricing.estimate_for(
            catalog, provider_key="fixture", model="m", prompt_tokens=100
        )
        self.assertIsNone(cost.micro)

    def test_different_currencies_are_never_added(self) -> None:
        catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="a", model="m1", currency="USD", input_per_million="1"
                ),
                pricing.PriceEntry(
                    provider_key="b", model="m2", currency="CNY", input_per_million="1"
                ),
            ),
        )
        first = pricing.estimate_for(
            catalog, provider_key="a", model="m1", prompt_tokens=1_000_000
        )
        second = pricing.estimate_for(
            catalog, provider_key="b", model="m2", prompt_tokens=1_000_000
        )
        self.assertEqual(first.currency, "USD")
        self.assertEqual(second.currency, "CNY")
        total = pricing.sum_amounts([first, second])
        self.assertIsNone(total, "不同币种不能相加")


class BudgetReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        group_costs.reset()
        group_costs.configure(self.session_factory)
        self.catalog = pricing.PriceCatalog(
            version=7,
            entries=(
                pricing.PriceEntry(
                    provider_key="openai_compatible",
                    model="fixture/model-a",
                    currency="USD",
                    input_per_million="3",
                    output_per_million="15",
                    cache_read_per_million="1",
                    cache_write_per_million="4",
                ),
            ),
        )
        self.service = budget.GroupBudgetService(
            session_factory=self.session_factory,
            catalog_provider=lambda: self.catalog,
        )
        self.llm = LLMService(
            _model(), _model("fixture/decision"),
            embed=EmbedConfig(model="fixture/embed", provider="openai_compatible",
                              api_base="https://gateway.example.invalid/v1",
                              api_key="k"),
        )
        self.settings = Settings(_env_file=None)

    async def asyncTearDown(self) -> None:
        await group_costs.flush(force=True)
        group_costs.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _enable(
        self,
        group_id: int = GROUP_A,
        *,
        attempts: int | None = None,
        tokens: int | None = None,
        money_micro: int | None = None,
        operations: dict[str, int] | None = None,
    ) -> GroupBudget:
        async with self.session_factory() as session:
            async with session.begin():
                row = GroupBudget(
                    group_id=group_id,
                    enabled=True,
                    revision=1,
                    currency=self.catalog.entries[0].currency,
                    daily_attempt_limit=attempts,
                    daily_token_limit=tokens,
                    daily_money_micro_limit=money_micro,
                    operation_limits=operations or {},
                )
                session.add(row)
                await session.flush()
            return row

    # ------------------------------------------------------------------
    # 默认关闭
    # ------------------------------------------------------------------
    async def test_budget_is_off_by_default_and_never_reads_the_database(self) -> None:
        calls = {"n": 0}

        async def counting_reserve(**kwargs):
            calls["n"] += 1
            return await self.service.reserve(**kwargs)

        self.service._reserve_impl = counting_reserve  # type: ignore[attr-defined]
        reservation = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        self.assertEqual(calls["n"], 0)
        self.assertTrue(reservation.admitted)
        self.assertFalse(reservation.enforced)

    async def test_safety_bypasses_the_guard_even_when_exhausted(self) -> None:
        await self._enable(attempts=0)
        self.service.invalidate(GROUP_A)
        reservation = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_SAFETY, stage="moderation",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        self.assertTrue(reservation.admitted)
        self.assertFalse(reservation.enforced)

    # ------------------------------------------------------------------
    # 并发预留
    # ------------------------------------------------------------------
    async def test_concurrent_optional_calls_only_one_reaches_the_provider(self) -> None:
        await self._enable(attempts=1)
        self.service.invalidate(GROUP_A)
        reached = {"n": 0}
        gate = asyncio.Semaphore(0)

        async def fake_send(**kwargs):
            reached["n"] += 1
            return await gate.acquire()

        async def one_call(index: int) -> bool:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                reservation = await self.service.reserve(
                    group_id=GROUP_A,
                    purpose=scopes.PURPOSE_OPTIONAL,
                    stage="main",
                    provider="openai_compatible",
                    model="fixture/model-a",
                    prompt_tokens=100,
                )
                if not reservation.admitted:
                    return False
                await self.service.settle(
                    reservation.reservation_id,
                    usage={"prompt_tokens": 100, "completion_tokens": 20},
                )
                return True

        results = await asyncio.gather(*(one_call(i) for i in range(20)))
        admitted = sum(1 for item in results if item)
        self.assertEqual(admitted, 1, "单群每日 attempt=1 时只应放行 1 条")
        rejected = [r for r in results if not r]
        self.assertTrue(rejected)

        async with self.session_factory() as session:
            rows = list((await session.scalars(select(GroupBudgetReservation))).all())
        statuses = {str(row.status) for row in rows}
        self.assertIn("settled", statuses)
        self.assertEqual(
            len([r for r in rows if str(r.status) == "settled"]), 1
        )

    async def test_other_group_is_unaffected(self) -> None:
        await self._enable(GROUP_A, attempts=2)
        await self._enable(GROUP_B, attempts=5)
        self.service.invalidate()
        for index in range(2):
            result = await self.service.reserve(
                group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
                provider="openai_compatible", model="fixture/model-a",
                prompt_tokens=100,
            )
            self.assertTrue(result.admitted, index)
            await self.service.settle(
                result.reservation_id,
                usage={"prompt_tokens": 100, "completion_tokens": 20},
            )
        denied = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        self.assertFalse(denied.admitted)
        for index in range(3):
            ok = await self.service.reserve(
                group_id=GROUP_B, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
                provider="openai_compatible", model="fixture/model-a",
                prompt_tokens=100,
            )
            self.assertTrue(ok.admitted, index)
            await self.service.settle(
                ok.reservation_id,
                usage={"prompt_tokens": 100, "completion_tokens": 20},
            )

    async def test_retry_is_a_second_reservation(self) -> None:
        await self._enable(attempts=2)
        self.service.invalidate(GROUP_A)
        for _ in range(2):
            result = await self.service.reserve(
                group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
                provider="openai_compatible", model="fixture/model-a",
                prompt_tokens=100,
            )
            self.assertTrue(result.admitted)
            await self.service.settle(
                result.reservation_id,
                usage={"prompt_tokens": 100, "completion_tokens": 20},
            )
        third = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        self.assertFalse(third.admitted)

    async def test_restart_keeps_the_day_exhausted(self) -> None:
        await self._enable(attempts=1)
        self.service.invalidate(GROUP_A)
        first = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        self.assertTrue(first.admitted)
        await self.service.settle(
            first.reservation_id,
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )
        # 重启 = 换一个 service 实例（缓存全空），数据库状态不变。
        rebooted = budget.GroupBudgetService(
            session_factory=self.session_factory,
            catalog_provider=lambda: self.catalog,
        )
        again = await rebooted.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        self.assertFalse(again.admitted, "重启不得把当天额度清零")

    async def test_unknown_response_keeps_a_conservative_reserve(self) -> None:
        await self._enable(attempts=5)
        self.service.invalidate(GROUP_A)
        result = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        await self.service.settle(result.reservation_id, usage=None, unknown=True)
        async with self.session_factory() as session:
            row = await session.get(GroupBudgetReservation, result.reservation_id)
            self.assertEqual(str(row.status), "unknown")
        day = await self.service.day_state(GROUP_A)
        self.assertEqual(day.unknown_attempts, 1)
        self.assertEqual(day.used_attempts, 1, "未知结果仍然占额度，不当免费")

    async def test_cancel_before_send_releases_the_reservation(self) -> None:
        await self._enable(attempts=1)
        self.service.invalidate(GROUP_A)
        result = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        await self.service.release(result.reservation_id)
        again = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        self.assertTrue(again.admitted, "发送前取消必须退还预留")

    async def test_stale_leases_are_repaired_not_leaked_forever(self) -> None:
        await self._enable(attempts=2)
        self.service.invalidate(GROUP_A)
        result = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.get(GroupBudgetReservation, result.reservation_id)
                row.lease_expires_at = row.created_at
        repaired = await self.service.reap_stale_reservations(GROUP_A)
        self.assertGreaterEqual(repaired, 1)
        async with self.session_factory() as session:
            row = await session.get(GroupBudgetReservation, result.reservation_id)
            self.assertEqual(str(row.status), "unknown")
        day = await self.service.day_state(GROUP_A)
        self.assertEqual(day.unknown_attempts, 1)
        # 租约过期只说明"我们不知道那次发没发出去"：不退还 attempt，所以额度
        # 既不泄漏给下一次调用，也**不**被凭空还回去。
        again = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )
        self.assertTrue(again.admitted, "额度 2，已用 1（unknown），仍可发 1 次")

    async def test_safety_still_runs_when_the_budget_store_fails(self) -> None:
        await self._enable(attempts=1)
        self.service.invalidate(GROUP_A)
        await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a", prompt_tokens=100,
        )

        def broken(*args, **kwargs):
            raise RuntimeError("budget store unavailable")

        self.service.invalidate(GROUP_A)
        original_factory = self.service._session_factory
        self.service._session_factory = broken
        try:
            result = await self.service.reserve(
                group_id=GROUP_A, purpose=scopes.PURPOSE_SAFETY, stage="moderation",
                provider="openai_compatible", model="fixture/model-a",
                prompt_tokens=100,
            )
        finally:
            self.service._session_factory = original_factory
        self.assertTrue(result.admitted, "预算存储故障不得挡住安全审核")

    async def test_enabled_guard_failure_fails_closed_for_optional(self) -> None:
        await self._enable(attempts=1)
        self.service.invalidate(GROUP_A)

        def broken(*args, **kwargs):
            raise RuntimeError("budget store unavailable")

        self.service.invalidate(GROUP_A)
        original_factory = self.service._session_factory
        self.service._session_factory = broken
        try:
            result = await self.service.reserve(
                group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
                provider="openai_compatible", model="fixture/model-a",
                prompt_tokens=100,
            )
        finally:
            self.service._session_factory = original_factory
        self.assertFalse(result.admitted, "启用护栏时核验失败必须明确拒绝，不能偷偷放行")
        self.assertEqual(result.reason, "budget_store_unavailable")

    # ------------------------------------------------------------------
    # 金额
    # ------------------------------------------------------------------
    async def test_money_reservation_is_reconciled_against_actual_usage(self) -> None:
        """预留用**保守上界**（假设全部输入都按 input 价，没有缓存信息），
        结算时换成真实金额。"""

        # 上界取最贵的一支：max(input=3, cache_read=1, cache_write=4)=4，
        # 于是 100×4 + 20×15 = 700。结算仍按真实 usage（520）。
        await self._enable(money_micro=700)
        self.service.invalidate(GROUP_A)
        result = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a",
            prompt_tokens=100, max_output_tokens=20,
        )
        self.assertTrue(result.admitted)
        self.assertEqual(result.reserved_money_micro, 700)
        await self.service.settle(
            result.reservation_id,
            usage={"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 40},
        )
        day = await self.service.day_state(GROUP_A)
        # 真实金额含 40 个缓存命中：60×3 + 40×1 + 20×15 = 520。
        self.assertEqual(day.used_money_micro, 520)
        denied = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a",
            prompt_tokens=100, max_output_tokens=20,
        )
        self.assertFalse(denied.admitted, "已用 520 + 新预留上界 700 超过 700 上限")

    async def test_money_cap_is_refused_when_pricing_is_incomplete(self) -> None:
        """计价不完整时不允许启用 money hard cap（也不允许假装成立）。"""

        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    GroupBudget(group_id=GROUP_B, enabled=False, revision=1)
                )
        with self.assertRaises(budget.BudgetError):
            await self.service.save_budget(
                GROUP_B,
                expected_revision=1,
                enabled=True,
                currency="USD",
                daily_money_micro_limit=100,
                money_covers=[("openai_compatible", "fixture/unpriced-model")],
            )
        # 配齐单价的同一个群可以设金额上限。
        ok = await self.service.save_budget(
            GROUP_B,
            expected_revision=1,
            enabled=True,
            currency="USD",
            daily_money_micro_limit=100,
            money_covers=[("openai_compatible", "fixture/model-a")],
        )
        self.assertEqual(int(ok.daily_money_micro_limit), 100)

    async def test_price_change_does_not_rewrite_history(self) -> None:
        await self._enable(money_micro=520_000_000)
        self.service.invalidate(GROUP_A)
        first = await self.service.reserve(
            group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL, stage="main",
            provider="openai_compatible", model="fixture/model-a",
            prompt_tokens=100, max_output_tokens=20,
        )
        await self.service.settle(
            first.reservation_id,
            usage={"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 40},
        )
        before = await self.service.day_state(GROUP_A)
        # 改价：新一版目录金额翻倍。
        self.service._catalog_provider = lambda: pricing.PriceCatalog(  # type: ignore[attr-defined]
            version=8,
            entries=(
                pricing.PriceEntry(
                    provider_key="openai_compatible",
                    model="fixture/model-a",
                    currency="USD",
                    input_per_million="6",
                    output_per_million="30",
                    cache_read_per_million="2",
                    cache_write_per_million="8",
                ),
            ),
        )
        self.service.invalidate(GROUP_A)
        after = await self.service.day_state(GROUP_A)
        self.assertEqual(after.used_money_micro, before.used_money_micro)

    # ------------------------------------------------------------------
    # 操作次数配额（非 LLM）
    # ------------------------------------------------------------------
    async def test_operation_quota_limits_unmetered_features(self) -> None:
        await self._enable(operations={"tts": 1})
        self.service.invalidate(GROUP_A)
        first = await self.service.consume_operation(GROUP_A, feature="tts")
        self.assertTrue(first.admitted)
        second = await self.service.consume_operation(GROUP_A, feature="tts")
        self.assertFalse(second.admitted)
        # 没设限的功能不受影响。
        other = await self.service.consume_operation(GROUP_A, feature="media")
        self.assertTrue(other.admitted)

    async def test_unmetered_work_is_labelled_not_priced(self) -> None:
        await self._enable(operations={"tts": 10})
        self.service.invalidate(GROUP_A)
        result = await self.service.consume_operation(GROUP_A, feature="tts")
        self.assertTrue(result.admitted)
        self.assertEqual(result.reserved_money_micro, 0)
        self.assertTrue(result.metered is False)

    # ------------------------------------------------------------------
    # 端到端
    # ------------------------------------------------------------------
    async def test_optional_llm_call_is_blocked_end_to_end(self) -> None:
        await self._enable(attempts=1)
        self.service.invalidate(GROUP_A)
        sent: list[str] = []

        async def provider(**kwargs):
            sent.append("sent")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None),
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=100, completion_tokens=20,
                    cached_tokens=40, cache_creation_input_tokens=0, thinking_tokens=0,
                ),
            )

        from types import SimpleNamespace

        with patch("bot.services.llm.litellm.acompletion", provider):
                with scopes.usage_scope(
                    kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
                ):
                    gate = budget.llm_budget_guard(self.service)
                    first = await self.llm.chat(
                        [{"role": "user", "content": "hi"}],
                        cost_guard=gate("openai_compatible", "fixture/model-a"),
                    )
                    second = await self.llm.chat(
                        [{"role": "user", "content": "hi again"}],
                        cost_guard=gate("openai_compatible", "fixture/model-a"),
                    )
        self.assertEqual(first, "ok")
        self.assertEqual(second, "")
        self.assertEqual(len(sent), 1)

    async def test_safety_llm_call_runs_after_optional_exhaustion(self) -> None:
        await self._enable(attempts=1)
        self.service.invalidate(GROUP_A)
        sent: list[str] = []

        from types import SimpleNamespace

        async def provider(**kwargs):
            sent.append("sent")
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="clean", tool_calls=None),
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=1,
                    cached_tokens=0, cache_creation_input_tokens=0, thinking_tokens=0,
                ),
            )

        with patch("bot.services.llm.litellm.acompletion", provider):
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                await self.llm.chat(
                    [{"role": "user", "content": "hi"}],
                    cost_guard=budget.llm_budget_guard(self.service)(
                        "openai_compatible", "fixture/model-a"
                    ),
                )
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_SAFETY
            ):
                verdict = await self.llm.moderation("system", "user")
        self.assertEqual(verdict, "clean")
        self.assertEqual(len(sent), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()