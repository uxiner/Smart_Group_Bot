"""台账在压力与不确定提交下的正确性（第八阶段必修第 5 条）。

父代理实测的三件事：

* 4097 个**真实**桶触发 ``threading.Lock`` 内的重入 → 死锁、进程 exit124；
* commit 已落库随后抛 ``CancelledError`` → flush 重试把 attempt 记成 2、
  token 记成两倍；
* 完整 usage 与坏 usage 的四种组合聚合后 ``usage_unknown`` 仍是 0，
  报表看起来"完整"。

这里都用真实并发与真实提交，不靠"只造 300 个桶"绕开。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import unittest

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import LlmCostDaily
from bot.services import group_costs
from bot.services import usage_scope as scopes

GROUP_A = -100900000001
MODEL_COUNT = 4097


class LedgerPressureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        group_costs.reset()
        group_costs.configure(self.session_factory)

    async def asyncTearDown(self) -> None:
        await group_costs.flush(force=True)
        group_costs.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _rows(self) -> list[LlmCostDaily]:
        await group_costs.flush(force=True)
        async with self.session_factory() as session:
            return list((await session.scalars(select(LlmCostDaily))).all())

    async def test_thousands_of_real_buckets_do_not_deadlock(self) -> None:
        """4097 个真实桶 + 真落盘：必须完成，不许卡死在锁里。"""

        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
        ):
            for index in range(MODEL_COUNT):
                attempt = group_costs.record_attempt(
                    stage="skill", provider="fixture", model=f"m{index:05d}"
                )
                group_costs.record_result(
                    attempt_id=attempt, usage={"prompt_tokens": 1}
                )
        rows = await asyncio.wait_for(self._rows(), timeout=120)
        self.assertEqual(len(rows), MODEL_COUNT)
        self.assertEqual(sum(int(row.attempts) for row in rows), MODEL_COUNT)
        self.assertEqual(sum(int(row.prompt_tokens or 0) for row in rows), MODEL_COUNT)


class UncertainCommitTests(unittest.IsolatedAsyncioTestCase):
    """提交成功之后才抛 CancelledError：重试**不得**重复计数。"""

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
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _record(self) -> None:
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
        ):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model="m"
            )
            group_costs.record_result(
                attempt_id=attempt,
                usage={"prompt_tokens": 10, "completion_tokens": 2},
            )

    async def _read(self) -> tuple[int, int, int]:
        async with self.session_factory() as session:
            rows = list((await session.scalars(select(LlmCostDaily))).all())
        return (
            sum(int(r.attempts) for r in rows),
            sum(int(r.prompt_tokens or 0) for r in rows),
            sum(int(r.output_tokens or 0) for r in rows),
        )

    async def test_retry_after_a_successful_commit_does_not_double_count(self) -> None:
        self._record()
        inner = self.session_factory

        class _CancelAfterCommit:
            """真实会话、真实 commit，``__aexit__`` 之后才抛 CancelledError。"""

            def __init__(self, session):
                self._inner = session

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def __aenter__(self):
                return await self._inner.__aenter__()

            async def __aexit__(self, exc_type, exc, tb):
                await self._inner.__aexit__(exc_type, exc, tb)
                raise asyncio.CancelledError()

        class _Factory:
            def __call__(self, *args, **kwargs):
                return _CancelAfterCommit(inner())

        original = group_costs._session_factory
        group_costs._session_factory = _Factory()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await group_costs.flush(force=True)
        finally:
            group_costs._session_factory = original

        # 数据已经在库里（提交成功过）；重试不得再加一次。
        self.assertEqual(await self._read(), (1, 10, 2))
        group_costs.configure(self.session_factory)
        # 模拟"提交后进程没来得及合并内存、又被重试"的现实路径：
        # 批次仍在内存里，再 flush 一次。
        group_costs._merge_back({
            key: dict(value) for key, value in list(group_costs._last_flushed.items())
        }) if hasattr(group_costs, "_last_flushed") else None
        await group_costs.flush(force=True)
        self.assertEqual(await self._read(), (1, 10, 2))

    async def test_failure_before_commit_is_still_recoverable(self) -> None:
        """互补控制：提交前失败必须仍能补记，不能被幂等标记吞掉。"""

        self._record()

        class _Broken:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("database is gone")

        original = group_costs._session_factory
        group_costs._session_factory = _Broken()
        try:
            self.assertEqual(await group_costs.flush(force=True), 0)
        finally:
            group_costs._session_factory = original
        group_costs.configure(self.session_factory)
        self.assertEqual(await group_costs.flush(force=True), 1)
        self.assertEqual(await self._read(), (1, 10, 2))


class UsageCoverageTests(unittest.IsolatedAsyncioTestCase):
    """完整 / 坏 usage 的两种顺序、跨 flush 与否，四臂都要留下 unknown。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        group_costs.reset()
        group_costs.configure(self.session_factory)

    async def asyncTearDown(self) -> None:
        await group_costs.flush(force=True)
        group_costs.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _run(self, order: str, cross_flush: bool, factory) -> int:
        payloads = [
            ("good", {"prompt_tokens": 10, "completion_tokens": 2}),
            ("bad", {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": -3}),
        ]
        if order == "bad-first":
            payloads.reverse()
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
        ):
            for index, (_tag, usage) in enumerate(payloads):
                attempt = group_costs.record_attempt(
                    stage="main", provider="fixture", model=f"m{index}"
                )
                group_costs.record_result(attempt_id=attempt, usage=usage)
                if cross_flush:
                    await group_costs.flush(force=True)
        await group_costs.flush(force=True)
        async with factory() as session:
            rows = list((await session.scalars(select(LlmCostDaily))).all())
        return sum(int(row.usage_unknown) for row in rows)

    async def _one_arm(self, order: str, cross_flush: bool) -> int:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine, factory = await init_db(f"sqlite+aiosqlite:///{path}")
        original = group_costs._session_factory
        group_costs.configure(factory)
        try:
            return await self._run(order, cross_flush, factory)
        finally:
            group_costs._session_factory = original
            group_costs.reset()
            await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

    async def test_unknown_survives_every_order_and_flush_boundary(self) -> None:
        for order in ("good-first", "bad-first"):
            for cross_flush in (False, True):
                with self.subTest(order=order, cross_flush=cross_flush):
                    unknown = await self._one_arm(order, cross_flush)
                    self.assertEqual(
                        unknown, 1,
                        f"{order} / 跨flush={cross_flush}: 坏 usage 没留下 unknown",
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class BatchIdentityTests(unittest.IsolatedAsyncioTestCase):
    """批次身份必须与**内容**解耦（父审 v26 第 2 条三红）。

    原实现用内容 hash 当身份，于是分不清「内容相同的新请求」和「同一批的重试」。
    要求：冻结的原批次重试保持同一 ID；新请求形成独立批次；幂等标记与累加
    同一事务；不丢新量、不重复计旧量。
    """

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

    def _record(self, prompt: int = 10, completion: int = 2) -> None:
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
        ):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model="m"
            )
            group_costs.record_result(
                attempt_id=attempt,
                usage={"prompt_tokens": prompt, "completion_tokens": completion},
            )

    async def _durable(self) -> tuple[int, int, int]:
        async with self.session_factory() as session:
            rows = list((await session.scalars(select(LlmCostDaily))).all())
        return (
            sum(int(r.attempts) for r in rows),
            sum(int(r.prompt_tokens or 0) for r in rows),
            sum(int(r.output_tokens or 0) for r in rows),
        )

    def _cancel_after_commit(self):
        """真实 session、真实 commit，``__aexit__`` 之后才抛 CancelledError。"""

        inner = self.session_factory

        class _Session:
            def __init__(self, session):
                self._inner = session

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def __aenter__(self):
                return await self._inner.__aenter__()

            async def __aexit__(self, exc_type, exc, tb):
                await self._inner.__aexit__(exc_type, exc, tb)
                raise asyncio.CancelledError()

        class _Factory:
            def __call__(self, *args, **kwargs):
                return _Session(inner())

        return _Factory()

    async def test_retry_after_uncertain_commit_does_not_double_count(self) -> None:
        self._record()
        original = group_costs._session_factory
        group_costs._session_factory = self._cancel_after_commit()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await group_costs.flush(force=True)
        finally:
            group_costs._session_factory = original
        self.assertEqual(await self._durable(), (1, 10, 2))
        group_costs.configure(self.session_factory)
        await group_costs.flush(force=True)
        self.assertEqual(
            await self._durable(), (1, 10, 2),
            "提交已生效后重试又记了一遍",
        )

    async def test_new_usage_at_an_uncertain_boundary_is_kept(self) -> None:
        self._record()
        original = group_costs._session_factory
        group_costs._session_factory = self._cancel_after_commit()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await group_costs.flush(force=True)
        finally:
            group_costs._session_factory = original
        # 同一提交边界之后又来了一次真实请求。
        self._record(prompt=17, completion=3)
        group_costs.configure(self.session_factory)
        await group_costs.flush(force=True)
        self.assertEqual(
            await self._durable(), (2, 27, 5),
            "新请求的新批次被旧批次的身份挡住了，或被重复计了",
        )
