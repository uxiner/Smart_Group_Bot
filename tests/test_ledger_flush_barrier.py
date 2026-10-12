"""force flush 是**持久化屏障**（父审 v31 交付要求）。

要固定的事件序列（全部用事件，不靠 sleep 猜）：

1. 自动 flush 已经进入真实 ``session.commit`` 并挂起；
2. 此时来第二个请求（有一条臂不带）；
3. 显式 ``flush(force=True)`` —— 它**不得**提前完成：另一把 flush 正拿着
   在飞批次，看到 counter 空就返回会让 fresh 读早于后台提交，凭空少记一次；
4. 挂起期间用**独立** session 只读，确认还没有落库；
5. 放行提交 → fresh 读的总数必须精确。

另有两条事务边界：**inner.commit 成功但 wrapper 返回前取消**（不确定提交，
与"提交后 context-close"是不同的窗口）、以及**两次内容相同的独立请求**。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
import unittest.mock

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import LlmCostDaily
from bot.services import group_costs, usage_scope as scopes

GROUP_ID = -100900000001
KNOWN = {
    "prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 0,
    "cache_creation_input_tokens": 0, "reasoning_tokens": 0,
}


class ForceFlushBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        group_costs.reset()
        group_costs.configure(self.session_factory)

    async def asyncTearDown(self) -> None:
        group_costs.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    def _record(self, prompt: int, completion: int, model: str = "barrier") -> None:
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_ID,
            purpose=scopes.PURPOSE_OPTIONAL,
        ):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model=model
            )
            group_costs.record_result(attempt_id=attempt, usage=dict(KNOWN, prompt_tokens=prompt, completion_tokens=completion))

    async def _totals(self, model: str = "barrier") -> list[int]:
        async with self.session_factory() as session:
            rows = list(
                (await session.scalars(
                    select(LlmCostDaily).where(LlmCostDaily.model == model)
                )).all()
            )
        return [
            sum(int(getattr(row, key) or 0) for row in rows)
            for key in ("attempts", "prompt_tokens", "output_tokens")
        ]

    async def _held_commit_factory(self, entered: asyncio.Event, release: asyncio.Event):
        """真实 session、真实 commit，**第一次** commit 挂起。

        不在 marker INSERT 处拦：那是实现细节；父探针的做法是拦真实
        ``commit``，并只在本次会话确实写过台账之后才挂起。
        """

        inner = self.session_factory
        state = {"held": False}

        class _Session:
            def __init__(self, session):
                self._inner = session

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def __aenter__(self):
                await self._inner.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self._inner.__aexit__(*args)

            async def commit(self):
                if not state["held"]:
                    state["held"] = True
                    entered.set()
                    await asyncio.wait_for(release.wait(), 5)
                return await self._inner.commit()

        def factory():
            return _Session(inner())

        return factory, state

    # ------------------------------------------------------------------
    async def _arm(self, new_request: bool) -> dict:
        entered = asyncio.Event()
        release = asyncio.Event()
        called = asyncio.Event()
        factory, state = await self._held_commit_factory(entered, release)
        group_costs.reset()
        group_costs.configure(factory)
        worker = None
        forced = None
        try:
            self._record(10, 2)
            # 触发真实的自动调度（不 patch 掉它）
            await asyncio.wait_for(entered.wait(), 5)
            worker = group_costs._flush_task
            self.assertIsNotNone(worker, "自动 flush 根本没有启动")
            if new_request:
                self._record(17, 3)

            async def force():
                called.set()
                return await group_costs.flush(force=True)

            forced = asyncio.create_task(force())
            await asyncio.wait_for(called.wait(), 2)
            early = False
            try:
                await asyncio.wait_for(asyncio.shield(forced), 0.15)
                early = True
            except asyncio.TimeoutError:
                pass
            # 挂起期间用**独立** session 只读：此刻必须还没落库。
            before = len(await self._durable_rows())
            release.set()
            await asyncio.wait_for(asyncio.gather(worker, forced), 10)
            return {
                "held": state["held"],
                "early": early,
                "before": before,
                "totals": await self._totals(),
            }
        finally:
            release.set()
            for task in (t for t in (worker, forced) if t is not None):
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True), 5
                )

    async def _durable_rows(self) -> list:
        async with self.session_factory() as session:
            return list(
                (await session.scalars(
                    select(LlmCostDaily).where(LlmCostDaily.model == "barrier")
                )).all()
            )

    async def test_force_does_not_finish_before_the_held_commit(self) -> None:
        outcome = await self._arm(new_request=False)
        self.assertTrue(outcome["held"], "没能挂住自动 flush 的真实 commit")
        self.assertFalse(
            outcome["early"],
            "force 在另一把 flush 持着在飞批次时提前返回了",
        )
        self.assertEqual(outcome["before"], 0, "挂起期间就已经落库了")
        self.assertEqual(outcome["totals"], [1, 10, 2])

    async def test_force_carries_the_new_request_arriving_during_the_hold(self) -> None:
        outcome = await self._arm(new_request=True)
        self.assertTrue(outcome["held"])
        self.assertFalse(outcome["early"])
        self.assertEqual(outcome["before"], 0)
        self.assertEqual(
            outcome["totals"], [2, 27, 5],
            "提交窗口里新到的请求没有作为**独立批次**落库",
        )

    # ------------------------------------------------------------------
    async def test_inner_commit_then_cancel_before_return_is_not_doubled(self) -> None:
        """不确定提交：真实 inner.commit 已完成，但调用方没收到确认。

        这与"提交后 context-close"是**不同**的窗口：前者数据已在库，而
        flush 认为失败并重试；后者 flush 知道已提交，不再重试。
        """

        inner = self.session_factory

        class _Session:
            def __init__(self, session):
                self._inner = session

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def __aenter__(self):
                await self._inner.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self._inner.__aexit__(*args)

            async def commit(self):
                await self._inner.commit()
                raise asyncio.CancelledError("commit 已落库但未被确认")

        group_costs.reset()
        group_costs.configure(lambda: _Session(inner()))
        with unittest.mock.patch.object(group_costs, "_maybe_schedule_flush"):
            self._record(10, 2)
            with self.assertRaises(asyncio.CancelledError):
                await group_costs.flush(force=True)

        group_costs.configure(self.session_factory)
        await group_costs.flush(force=True)
        await group_costs.flush(force=True)
        self.assertEqual(
            await self._totals(), [1, 10, 2],
            "已落库但未确认的那一批被重试记了第二次",
        )

    async def test_identical_independent_requests_count_twice(self) -> None:
        for _ in range(2):
            self._record(10, 2)
            await group_costs.flush(force=True)
        self.assertEqual(
            await self._totals(), [2, 20, 4],
            "两次内容相同但来源不同的请求被当成同一批",
        )

    async def test_unknown_coverage_survives_merges_and_reports(self) -> None:
        """好/坏 usage 的两种顺序、跨不跨 flush，四臂都要留下 unknown。"""

        bad = dict(KNOWN, prompt_tokens=-1)
        for split in (False, True):
            for bad_first in (False, True):
                with self.subTest(split=split, bad_first=bad_first):
                    group_costs.reset()
                    group_costs.configure(self.session_factory)
                    order = [bad, KNOWN] if bad_first else [KNOWN, bad]
                    model = f"cov-split{split}-{bad_first}"
                    for usage in order:
                        self._record(
                            usage["prompt_tokens"], usage["completion_tokens"],
                            model=model,
                        )
                        if split:
                            await group_costs.flush(force=True)
                    await group_costs.flush(force=True)
                    async with self.session_factory() as session:
                        rows = list(
                            (await session.scalars(
                                select(LlmCostDaily).where(
                                    LlmCostDaily.model == model
                                )
                            )).all()
                        )
                        report = [
                            item
                            for item in await group_costs.query_group_costs(
                                session, group_id=GROUP_ID
                            )
                            if item["model"] == model
                        ]
                    self.assertEqual(sum(int(r.usage_unknown) for r in rows), 1)
                    self.assertGreaterEqual(
                        sum(int(r["usage_unknown"]) for r in report), 1
                    )






if __name__ == "__main__":  # pragma: no cover
    unittest.main()