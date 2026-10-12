"""不确定提交窗口：新请求到来时不得丢、不得重复（父探针的正式回归版）。

事件序列（全部用事件同步，不靠 sleep 猜）：

1. 先用**普通**会话 warm 一次，并且**按真实的 ledger INSERT 是否已经执行**来
   限定注入：只有本次会话真的写过 ``llm_cost_daily`` 才挂住。纯粹"数这是第几次
   commit"是不可靠的边界 —— 提交发生的时机由驱动与事务决定，拿它当特征等于
   把不存在的机制写成事实；
2. 注入 commit：真实 ``inner.commit()`` **先**完成，随后在 wrapper 返回确认
   **之前**抛 CancelledError；
3. 独立 fresh SQL 读回：第一笔 10/2 **已经落库**；
4. 此刻新请求 17/3 进入活 counter；
5. 还原普通会话，retry 两次，reset 内存后 fresh SQL 与公共报表都必须是
   2/27/5。

与"提交后 context-close""提交前取消"是三个不同窗口，不能互相替代。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import LlmCostDaily
from bot.services import group_costs, usage_scope as scopes

GID = -100900000001
MODEL = "uncertain-new-event"


class UncertainCommitNewRequestTests(unittest.IsolatedAsyncioTestCase):
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
        scopes.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    def _record(self, prompt: int, completion: int) -> None:
        with scopes.usage_scope(
            kind="group", group_id=GID, purpose="optional"
        ):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model=MODEL
            )
            group_costs.record_result(
                attempt_id=attempt,
                usage={
                    "prompt_tokens": prompt, "completion_tokens": completion,
                    "cached_tokens": 0, "cache_creation_input_tokens": 0,
                    "reasoning_tokens": 0,
                },
            )

    async def _totals(self) -> dict:
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(LlmCostDaily).where(LlmCostDaily.model == MODEL)
                )
            ).all()
            report = [
                item
                for item in await group_costs.query_group_costs(session, group_id=GID)
                if item["model"] == MODEL
            ]
        fields = ("attempts", "prompt_tokens", "output_tokens")
        return {
            "durable": [sum(int(getattr(r, f) or 0) for r in rows) for f in fields],
            "report": [sum(int(r[f] or 0) for r in report) for f in fields],
        }

    async def test_new_request_during_unacknowledged_commit_is_kept_once(self) -> None:
        committed = asyncio.Event()
        cancel_now = asyncio.Event()
        inner = self.session_factory
        commit_calls: list[dict] = []

        class _Wrapper:
            def __init__(self, session):
                self.inner = session
                self.ledger_write = False

            def __getattr__(self, name):
                return getattr(self.inner, name)

            async def __aenter__(self):
                await self.inner.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self.inner.__aexit__(*args)

            async def execute(self, statement, *args, **kwargs):
                table = getattr(statement, "table", None)
                if (
                    getattr(table, "name", None) == LlmCostDaily.__tablename__
                    and getattr(statement, "is_insert", False)
                ):
                    self.ledger_write = True
                return await self.inner.execute(statement, *args, **kwargs)

            async def commit(self):
                commit_calls.append({"ledger_write": self.ledger_write})
                await self.inner.commit()
                if self.ledger_write and not committed.is_set():
                    # 落库已完成，但确认还没返回给调用方。
                    committed.set()
                    await asyncio.wait_for(cancel_now.wait(), 5)
                    raise asyncio.CancelledError(
                        "durable ledger commit before wrapper return"
                    )

        # 先 warm 普通会话；注入靠 ``ledger_write`` 这个真实特征（本次会话确实
        # 执行过 llm_cost_daily 的 INSERT），而不是"第几次 commit"。
        async with self.session_factory() as session:
            await session.execute(select(LlmCostDaily).limit(1))

        task = None
        with patch.object(group_costs, "_maybe_schedule_flush", lambda: None):
            self._record(10, 2)
            group_costs.configure(lambda: _Wrapper(inner()))
            task = asyncio.create_task(group_costs.flush(force=True))
            try:
                await asyncio.wait_for(committed.wait(), 5)
                before = await self._totals()
                self.assertEqual(
                    before["durable"], [1, 10, 2],
                    "取消之前第一笔必须已经落库",
                )
                # 提交窗口里来了第二次真实请求。
                self._record(17, 3)
                self.assertTrue(group_costs.snapshot(), "新请求没有进入活 counter")
                cancel_now.set()
                cancelled = False
                try:
                    await asyncio.wait_for(task, 5)
                except asyncio.CancelledError:
                    cancelled = True
            finally:
                cancel_now.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(cancelled, "wrapper 在返回前抛的 CancelledError 必须被看到")
        self.assertEqual(
            [c["ledger_write"] for c in commit_calls].count(True), 1,
            f"只有一次真正写了台账的提交：{commit_calls}",
        )

        group_costs.configure(self.session_factory)
        await group_costs.flush(force=True)
        await group_costs.flush(force=True)
        group_costs.reset()
        after = await self._totals()
        self.assertEqual(after["durable"], [2, 27, 5], after)
        self.assertEqual(after["report"], [2, 27, 5], after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
