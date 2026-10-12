"""预算预留与真实请求生命周期的边界（父探针的正式回归版）。

四臂：

1. 排队中**主动取消** → provider 一次没进 → 预留释放、计数全零；
2. 排队中**超时**（模型 timeout 很小、整体 deadline 很宽，确保走到的是 attempt
   超时出口而不是总 deadline）→ 同样释放；
3. provider 确实进入、吞掉取消后迟到返回 known usage → 预算**对账**成
   settled/12/60，而不只是遥测补上；
4. 重复 unknown 再 known → 恰好一次、未知计数归零。

等待迟到完成用**真实在飞清零** + 有界 timeout，不拿"某个工厂对象非空"
当条件，也不把"等待结束"当成"请求完成"。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace as N
from unittest.mock import patch

from sqlalchemy import select

from bot.config import ModelConfig
from bot.db.engine import init_db
from bot.db.models import GroupBudgetReservation
from bot.services import group_budget as b, group_costs as g, pricing as p
from bot.services import llm as l
from bot.services import usage_scope as u

KNOWN = {
    "prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 0,
    "cache_creation_input_tokens": 0,
}


def _response() -> N:
    return N(
        choices=[N(message=N(content="ok", tool_calls=[]))],
        usage=N(
            prompt_tokens=10, completion_tokens=2, cached_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class LifecycleBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        g.reset()
        g.configure(self.session_factory)
        self.catalog = p.PriceCatalog(
            version=1,
            entries=(
                p.PriceEntry(
                    provider_key="fixture", model="fixture/m", currency="USD",
                    input_per_million="3", output_per_million="15",
                    cache_read_per_million="1", cache_write_per_million="4",
                ),
            ),
        )
        self.svc = b.GroupBudgetService(
            session_factory=self.session_factory,
            catalog_provider=lambda: self.catalog,
        )
        self._original_semaphore = l._LLM_REQUEST_SEMAPHORE
        self._semaphore = asyncio.Semaphore(l._LLM_REQUEST_CAPACITY)
        l._LLM_REQUEST_SEMAPHORE = self._semaphore

    async def asyncTearDown(self) -> None:
        l._LLM_REQUEST_SEMAPHORE = self._original_semaphore
        l.configure_cost_guard(None)
        g.reset()
        u.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    async def _prepare(self, group: int, **model_kwargs) -> l.LLMService:
        await self.svc.save_budget(
            group, expected_revision=0, enabled=True, daily_attempt_limit=5
        )
        cfg = ModelConfig(
            model="fixture/m", provider="openai_compatible", slot="fixture",
            api_base="https://fixture.example.invalid/v1", api_key="fixture",
            retry_attempts=1, retry_backoff_sec=0, **model_kwargs,
        )
        return l.LLMService(cfg, cfg)

    async def _wait_for_reservation(self, group: int) -> None:
        """有界地等"预留已落库"这个真实事件（不用 sleep 猜）。"""

        async with asyncio.timeout(5):
            while True:
                async with self.session_factory() as session:
                    found = (
                        await session.scalars(
                            select(GroupBudgetReservation.reservation_id).where(
                                GroupBudgetReservation.group_id == group
                            )
                        )
                    ).first()
                if found is not None:
                    return
                await asyncio.sleep(0)

    async def _row(self, group: int) -> dict:
        async with self.session_factory() as session:
            row = (
                await session.scalars(
                    select(GroupBudgetReservation).where(
                        GroupBudgetReservation.group_id == group
                    )
                )
            ).one()
        return {
            key: getattr(row, key)
            for key in ("status", "actual_tokens", "actual_money_micro")
        }

    async def _call(self, service, group: int, guard) -> str:
        with u.usage_scope(kind="group", group_id=group):
            return await service.chat(
                [{"role": "user", "content": "synthetic"}], cost_guard=guard
            )

    # ------------------------------------------------------------------
    async def test_cancel_while_queued_releases_the_reservation(self) -> None:
        group = -1001234567890
        service = await self._prepare(group)
        calls = {"n": 0}

        async def provider(**kwargs):
            calls["n"] += 1
            return _response()

        with patch.object(
            l, "_LLM_REQUEST_SEMAPHORE", asyncio.Semaphore(0)
        ), patch.object(l.litellm, "acompletion", provider):
            task = asyncio.create_task(
                self._call(service, group, b.CostGuard(self.svc))
            )
            # 预留已经建立、请求还排在 admission 之外。
            await asyncio.wait_for(self._wait_for_reservation(group), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(calls["n"], 0, "provider 根本不该被进入")
        row = await self._row(group)
        self.assertEqual(row["status"], "released", row)
        state = (await self.svc.day_state(group)).document()
        self.assertEqual(int(state["used_attempts"]), 0, state)
        self.assertEqual(int(state["used_tokens"]), 0, state)

    async def test_timeout_while_queued_releases_the_reservation(self) -> None:
        """模型 timeout 很小、整体 deadline 保持宽，走 attempt 超时而不是总期限。"""

        group = -1001234567890
        service = await self._prepare(
            group, timeout_sec=0.05, total_deadline_sec=30.0
        )
        calls = {"n": 0}

        async def provider(**kwargs):
            calls["n"] += 1
            return _response()

        with patch.object(
            l, "_LLM_REQUEST_SEMAPHORE", asyncio.Semaphore(0)
        ), patch.object(l.litellm, "acompletion", provider):
            await asyncio.wait_for(
                self._call(service, group, b.CostGuard(self.svc)), 10
            )

        self.assertEqual(calls["n"], 0, "provider 根本不该被进入")
        row = await self._row(group)
        self.assertEqual(row["status"], "released", row)
        state = (await self.svc.day_state(group)).document()
        self.assertEqual(int(state["used_attempts"]), 0, state)
        self.assertEqual(int(state["used_tokens"]), 0, state)
        self.assertEqual(int(state["used_money_micro"]), 0, state)
        self.assertEqual(int(state["unknown_attempts"]), 0, state)

    async def test_late_provider_usage_reconciles_the_budget(self) -> None:
        group = -1001234567890
        service = await self._prepare(
            group, timeout_sec=0.05, total_deadline_sec=30.0
        )
        entered = asyncio.Event()
        released = asyncio.Event()

        async def late_provider(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # 不合作：吞掉取消，等外部放行才返回真实 usage。
                released.set()
                return _response()

        with patch.object(l.litellm, "acompletion", late_provider):
            await asyncio.wait_for(
                self._call(service, group, b.CostGuard(self.svc)), 10
            )
            await asyncio.wait_for(released.wait(), 5)
            # 等**真实在飞**归零（有界），不靠固定 sleep 猜它完成了。
            async with asyncio.timeout(5):
                while g.inflight_attempts():
                    await asyncio.sleep(0)

        self.assertEqual(g.inflight_attempts(), 0)
        row = await self._row(group)
        self.assertEqual(row["status"], "settled", row)
        self.assertEqual(int(row["actual_tokens"]), 12, row)
        self.assertEqual(int(row["actual_money_micro"]), 60, row)

    async def test_repeated_unknown_then_known_settles_exactly_once(self) -> None:
        group = -1001234567890
        await self.svc.save_budget(
            group, expected_revision=0, enabled=True, daily_attempt_limit=5
        )
        result = await self.svc.reserve(
            group_id=group, purpose="optional", provider="fixture",
            model="fixture/m", prompt_tokens=10, max_output_tokens=2,
        )
        await self.svc.settle(result.reservation_id, usage=None, unknown=True)
        await self.svc.settle(result.reservation_id, usage=None, unknown=True)
        before = (await self.svc.day_state(group)).document()
        self.assertEqual(int(before["unknown_attempts"]), 1, before)
        await self.svc.settle(result.reservation_id, usage=KNOWN)
        after = (await self.svc.day_state(group)).document()
        self.assertEqual(int(after["unknown_attempts"]), 0, after)
        self.assertEqual(int(after["used_attempts"]), 1, after)
        self.assertEqual(int(after["used_tokens"]), 12, after)
        self.assertEqual(int(after["used_money_micro"]), 60, after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
