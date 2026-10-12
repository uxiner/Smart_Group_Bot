"""预算预留必须跟**真实请求生命周期**对账（父审 v26 第 3 条）。

两个红：

* 饱和 admission semaphore、admit 已返回但 provider **还没进入**时取消 →
  provider_calls=0，durable reservation 却是 unknown、attempts1/tokens2057/
  money30756/unknown1。请求压根没发出去，不该耗费本群额度。
* provider **确实进入**、超时后吞掉取消并迟到返回 known usage → 遥测已补上，
  但预算仍是 unknown、实际 tokens/money 都是 null。遥测有结果不代表预算对账完。

边界用**事件**建立，不用任意 sleep 推定"应该已经发出去了"。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace

from sqlalchemy import select

from bot.config import ModelConfig
from bot.db.engine import init_db
from bot.db.models import GroupBudgetDay, GroupBudgetReservation
from bot.services import group_budget as budget
from bot.services import llm as llm_module
from bot.services import pricing, usage_scope as scopes
from bot.services.llm import LLMService

GROUP_ID = -100900000001


def _model() -> ModelConfig:
    return ModelConfig(
        model="fixture/main", slot="slot-a", provider="openai_compatible",
        api_base="https://gateway.example.invalid/v1", api_key="k",
        retry_attempts=1, retry_backoff_sec=0.0, total_deadline_sec=30.0,
        timeout_sec=30.0,
    )


def _usage(prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, cached_tokens=0,
        cache_creation_input_tokens=0, thinking_tokens=0,
    )


class RequestLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # IsolatedAsyncioTestCase 每个用例一个新事件循环，而 admission 的
        # Semaphore 是**模块级**的：上一例把它绑在自己的 loop 上，这一例再 acquire
        # 就会得到 "Semaphore bound to a different event loop"（表现为
        # provider0 却记成 unknown，掩盖真正的逻辑）。这里给本例装一个属于
        # 本 loop 的隔离信号量，退出时还原模块全局值。
        self._original_semaphore = llm_module._LLM_REQUEST_SEMAPHORE
        self._semaphore = asyncio.Semaphore(llm_module._LLM_REQUEST_CAPACITY)
        llm_module._LLM_REQUEST_SEMAPHORE = self._semaphore
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
                    provider_key="slot-a", model="fixture/main", currency="USD",
                    input_per_million="3", output_per_million="15",
                    cache_read_per_million="3", cache_write_per_million="3",
                ),
            ),
        )
        self.service = budget.shared_service(
            self.session_factory, lambda: self.catalog
        )
        await self.service.save_budget(
            GROUP_ID, expected_revision=0, enabled=True, currency="USD",
            daily_attempt_limit=100, daily_token_limit=1_000_000,
            daily_money_micro_limit=10_000_000,
        )
        self.service.invalidate()
        self.guard = budget.llm_budget_guard(self.service)("slot-a", "fixture/main")
        self.llm = LLMService(_model(), _model())

    async def asyncTearDown(self) -> None:
        # 先把本例的隔离信号量还回去，再清账；顺序反了会让下一次 setUp 拿到
        # 一个属于上一个 loop 的对象。
        llm_module._LLM_REQUEST_SEMAPHORE = self._original_semaphore
        budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _rows(self) -> list[GroupBudgetReservation]:
        async with self.session_factory() as session:
            return list(
                (await session.scalars(select(GroupBudgetReservation))).all()
            )

    async def _day(self) -> GroupBudgetDay:
        async with self.session_factory() as session:
            return await session.get(GroupBudgetDay, (GROUP_ID, budget._today()))

    # ------------------------------------------------------------------
    async def test_cancel_before_provider_entry_releases_the_reservation(self) -> None:
        """占满 admission 槽 + admit 已返回 + 取消 → 请求没发，额度要还回来。"""

        entered = asyncio.Event()
        release = asyncio.Event()
        provider_calls = {"n": 0}

        async def provider(**kwargs):
            provider_calls["n"] += 1
            entered.set()
            await release.wait()
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=_usage(100, 20),
            )

        # 手动占满真实 admission 槽，让第一次请求停在入口之前。
        capacity = llm_module._LLM_REQUEST_CAPACITY
        held = [
            asyncio.create_task(self._semaphore.acquire())
            for _ in range(capacity)
        ]
        await asyncio.gather(*held)
        self.assertEqual(
            int(self._semaphore._value), 0,
            "没能占满 admission 槽，这条用例测不到排队取消",
        )
        try:
            with patch_acompletion(provider):
                with scopes.usage_scope(
                    kind=scopes.SCOPE_GROUP, group_id=GROUP_ID,
                    purpose=scopes.PURPOSE_OPTIONAL,
                ):
                    task = asyncio.create_task(
                        self.llm.chat(
                            [{"role": "user", "content": "hi"}],
                            cost_guard=self.guard,
                        )
                    )
                    # 预留已经发生，但请求还排在 admission 之外。
                    await asyncio.wait_for(
                        self._wait_for_reservation(), timeout=5.0
                    )
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
        finally:
            release.set()
            for task in held:
                self._semaphore.release()
            await asyncio.gather(*held, return_exceptions=True)

        self.assertEqual(provider_calls["n"], 0, "provider 根本没被进入")
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertIn(
            str(rows[0].status), {"released", "reserved"},
            f"请求没发出去却记成 {rows[0].status}（消耗了本群额度）",
        )
        day = await self._day()
        self.assertEqual(int(day.used_attempts), 0)
        self.assertEqual(int(day.used_tokens), 0)
        self.assertEqual(int(day.used_money_micro), 0)

    async def _wait_for_reservation(self) -> None:
        for _ in range(200):
            rows = await self._rows()
            if rows:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("预留始终没有落库")

    # ------------------------------------------------------------------
    async def test_late_known_usage_reconciles_the_budget_once(self) -> None:
        """provider 确实进入、吞取消后迟到返回 → 预算按真实用量**一次**对账。"""

        entered = asyncio.Event()
        release = asyncio.Event()
        orphan_done = asyncio.Event()
        provider_calls = {"n": 0}

        async def stubborn(**kwargs):
            provider_calls["n"] += 1
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                # 不合作：吞掉取消，等外部放行才返回。
                await release.wait()
                orphan_done.set()
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content="late", tool_calls=None)
                        )
                    ],
                    usage=_usage(100, 20),
                )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=_usage(1, 1),
            )

        with patch_acompletion(stubborn):
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_ID,
                purpose=scopes.PURPOSE_OPTIONAL,
            ):
                task = asyncio.create_task(
                    self.llm.chat(
                            [{"role": "user", "content": "hi"}],
                            cost_guard=self.guard,
                        )
                )
                await asyncio.wait_for(entered.wait(), timeout=5.0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                # 放行孤儿请求，让它带着真实 usage 回来。
                release.set()
                await asyncio.wait_for(orphan_done.wait(), timeout=5.0)
                # 等真实在飞归零（不靠 sleep 猜）。
                for _ in range(400):
                    if llm_module.cost_guard_factory() is not None:
                        from bot.services import group_costs

                        if group_costs.inflight_attempts() == 0:
                            break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.05)

        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            str(rows[0].status), "settled",
            f"迟到 known usage 之后预算仍停在 {rows[0].status}、"
            f"tokens={rows[0].actual_tokens} money={rows[0].actual_money_micro}",
        )
        self.assertEqual(int(rows[0].actual_tokens), 120)
        day = await self._day()
        # 按真实用量 100×3 + 20×15 = 600，且**不重复**扣。
        self.assertEqual(int(day.used_tokens), 120)
        self.assertEqual(int(day.used_money_micro), 600)
        self.assertEqual(int(day.unknown_attempts), 0)

    async def test_unsent_timeout_releases_the_reservation(self) -> None:
        """semaphore 满 → 排队超时、provider 一次没进 → 预留必须**释放**。

        和"已发出但结果未知"是两件事：请求压根没交给上游，不该扣本群额度。
        父探针实测这里曾记成 unknown 且扣掉 1 attempt / 2057 tokens。
        """

        entered = asyncio.Event()
        provider_calls = {"n": 0}

        async def provider(**kwargs):
            provider_calls["n"] += 1
            entered.set()
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=_usage(10, 2),
            )

        capacity = llm_module._LLM_REQUEST_CAPACITY
        held = [
            asyncio.create_task(self._semaphore.acquire())
            for _ in range(capacity)
        ]
        await asyncio.gather(*held)
        self.assertEqual(int(self._semaphore._value), 0)
        try:
            with patch_acompletion(provider):
                with scopes.usage_scope(
                    kind=scopes.SCOPE_GROUP, group_id=GROUP_ID,
                    purpose=scopes.PURPOSE_OPTIONAL,
                ):
                    # 排��里等不到许可 → 走到真实的 attempt 超时出口。
                    await self.llm.chat(
                        [{"role": "user", "content": "hi"}],
                        cost_guard=self.guard,
                    )
        finally:
            for task in held:
                self._semaphore.release()
            await asyncio.gather(*held, return_exceptions=True)

        self.assertFalse(entered.is_set(), "provider 根本不该被进入")
        self.assertEqual(provider_calls["n"], 0)
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            str(rows[0].status), "released",
            f"没发出的请求记成了 {rows[0].status}",
        )
        day = await self._day()
        self.assertEqual(int(day.used_attempts), 0)
        self.assertEqual(int(day.used_tokens), 0)
        self.assertEqual(int(day.used_money_micro), 0)
        self.assertEqual(int(day.unknown_attempts), 0)

    async def test_safety_is_not_blocked_by_an_exhausted_optional_budget(self) -> None:
        """可选额度用尽时，安全审核照常（它绕开可选准入，不是绕开 admission 槽）。"""

        sent = {"n": 0}

        async def provider(**kwargs):
            sent["n"] += 1
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="clean", tool_calls=None)
                    )
                ],
                usage=_usage(10, 2),
            )

        await self.service.save_budget(
            GROUP_ID, expected_revision=1, enabled=True, currency="USD",
            daily_attempt_limit=0,
        )
        self.service.invalidate()
        with patch_acompletion(provider):
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_ID,
                purpose=scopes.PURPOSE_SAFETY,
            ):
                verdict = await self.llm.moderation(
                    "system", "user", cost_guard=self.guard
                )
        self.assertEqual(verdict, "clean")
        self.assertEqual(sent["n"], 1)



def patch_acompletion(provider):
    return unittest.mock.patch.object(llm_module.litellm, "acompletion", provider)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
