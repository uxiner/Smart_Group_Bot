"""修复批 P1-2 / B-25：摘要退避与失败计数不再只活在进程内存里。

复现的原缺陷（``AUDIT-B`` B-25）：

``GroupSummaryScheduler._backoff_until`` / ``_failures`` 是两个进程内 dict，时钟是
``time.monotonic``，**零 DB 写入**。于是：容器滚动重启 = 所有持续失败的群从
``failure_backoff_seconds``（60s）重新爬阶梯，永远到不了 ``failure_backoff_max_seconds``
（3600s）——重启一次就等于发一次「立即重试」。

修法：新增 ``group_summary_failure_states`` 小表（``group_id`` 主键 +
``failure_count`` + **墙钟** ``backoff_until``），失败时 UPSERT、成功发布时删除、
调度器启动时读回。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import GroupSummaryFailureState
from bot.services.group_summary import (
    GroupSummaryConfig,
    GroupSummaryScheduler,
    SqlGroupSummaryStore,
)
from bot.utils.timezone import now_shanghai_naive
from tests.test_group_summary import FakeLLM, FakeStore, _rows

GROUP_ID = 1


class _PersistentStore(FakeStore):
    """FakeStore + 真 DB 的退避台账（读/写/删三个方法）。"""

    def __init__(self, rows, session_factory) -> None:  # noqa: ANN001
        super().__init__({GROUP_ID: rows})
        self._session_factory = session_factory
        self._real = SqlGroupSummaryStore(session_factory)

    async def load_failure_states(self):
        return await self._real.load_failure_states()

    async def save_failure_state(self, group_id, *, failure_count, backoff_until):
        return await self._real.save_failure_state(
            group_id, failure_count=failure_count, backoff_until=backoff_until
        )

    async def clear_failure_state(self, group_id):
        return await self._real.clear_failure_state(group_id)


class FailureBackoffPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _rows_in_db(self) -> list[tuple[int, int]]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        GroupSummaryFailureState.group_id,
                        GroupSummaryFailureState.failure_count,
                    )
                )
            ).all()
        return [(int(row[0]), int(row[1])) for row in rows]

    def _cfg(self) -> GroupSummaryConfig:
        return GroupSummaryConfig(
            enabled=True,
            min_refresh_seconds=0.0,
            failure_backoff_seconds=60.0,
            failure_backoff_max_seconds=3600.0,
            deadline_seconds=30.0,
        )

    async def test_failures_are_persisted_and_survive_a_restart(self) -> None:
        cfg = self._cfg()
        store = _PersistentStore(_rows(GROUP_ID, 600), self.session_factory)
        first = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        # 连续失败 4 次 → 阶梯 60 / 120 / 240 / 480
        for expected_attempts in (1, 2, 3, 4):
            first._register_failure(GROUP_ID, cfg)
            self.assertEqual(first._failures[GROUP_ID], expected_attempts)
        # 落库任务是 fire-and-forget：让出控制权让它跑完
        for _ in range(50):
            if await self._rows_in_db():
                break
            await __import__("asyncio").sleep(0.01)
        self.assertEqual(await self._rows_in_db(), [(GROUP_ID, 4)])

        # 「重启」：全新调度器 + 全新内存，必须把台账读回来
        second = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        self.assertEqual(second._failures, {}, "新进程的内存确实是空的")
        await second._restore_failure_states()
        self.assertEqual(
            second._failures[GROUP_ID],
            4,
            "重启后失败计数必须恢复（否则阶梯回到 60s，永远到不了 3600s）",
        )
        self.assertGreater(
            second._backoff_until.get(GROUP_ID, 0.0),
            second._clock(),
            "退避到期之前不得重试",
        )

    async def test_next_failure_continues_the_ladder_after_restart(self) -> None:
        cfg = self._cfg()
        store = _PersistentStore(_rows(GROUP_ID, 600), self.session_factory)
        first = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        for _ in range(3):
            first._register_failure(GROUP_ID, cfg)
        for _ in range(50):
            if await self._rows_in_db():
                break
            await __import__("asyncio").sleep(0.01)

        second = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        await second._restore_failure_states()
        before = second._clock()
        second._register_failure(GROUP_ID, cfg)
        self.assertEqual(second._failures[GROUP_ID], 4, "阶梯跨重启继续往上走")
        # 第 4 次失败的退避 = 60 * 2**3 = 480s（不是重新从 60s 开始）
        self.assertGreaterEqual(second._backoff_until[GROUP_ID] - before, 400.0)

    async def test_expired_backoff_keeps_the_count_but_allows_retry(self) -> None:
        cfg = self._cfg()
        store = _PersistentStore(_rows(GROUP_ID, 600), self.session_factory)
        await store.save_failure_state(
            GROUP_ID,
            failure_count=5,
            backoff_until=now_shanghai_naive() - timedelta(seconds=30),
        )
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        await scheduler._restore_failure_states()
        self.assertEqual(scheduler._failures[GROUP_ID], 5, "过期的退避仍保留失败计数")
        self.assertNotIn(
            GROUP_ID, scheduler._backoff_until, "退避已到期 → 立刻可重试"
        )

    async def test_publish_clears_the_persisted_state(self) -> None:
        cfg = self._cfg()
        store = _PersistentStore(_rows(GROUP_ID, 600), self.session_factory)
        await store.save_failure_state(
            GROUP_ID,
            failure_count=3,
            backoff_until=now_shanghai_naive() + timedelta(seconds=600),
        )
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        scheduler.notify(GROUP_ID)
        self.assertEqual(await scheduler.run_group(GROUP_ID, cfg), "published")
        for _ in range(50):
            if not await self._rows_in_db():
                break
            await __import__("asyncio").sleep(0.01)
        self.assertEqual(
            await self._rows_in_db(), [], "发布成功必须删掉台账，否则重启又恢复旧阶梯"
        )

    async def test_aux_tasks_do_not_consume_the_pump_execution_slots(self) -> None:
        """回归：fire-and-forget 的台账任务不能挤掉摘要的执行槽。

        ``_pump`` 的并发判定是 ``while len(self._tasks) < limit``（limit 默认 2）。
        辅助任务若混进 ``_tasks``，两次落库就能让 ``_pump`` 以为已满、整条摘要
        流水线停摆。
        """

        cfg = self._cfg()
        store = _PersistentStore(_rows(GROUP_ID, 600), self.session_factory)
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        for _ in range(4):
            scheduler._register_failure(GROUP_ID, cfg)
        # 落库任务确实挂起了（还没给事件循环机会跑完）
        self.assertGreaterEqual(len(scheduler._aux_tasks), 1)
        self.assertEqual(
            len(scheduler._tasks),
            0,
            "辅助任务必须在 _aux_tasks 里，不得进入 _tasks 的执行槽预算",
        )
        self.assertLess(
            len(scheduler._aux_tasks),
            scheduler._effective_concurrency(cfg) + 4,
            "辅助任务数量不应影响并发判定",
        )

    async def test_shutdown_cancels_the_aux_tasks(self) -> None:
        cfg = self._cfg()
        store = _PersistentStore(_rows(GROUP_ID, 600), self.session_factory)
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        scheduler._register_failure(GROUP_ID, cfg)
        await scheduler.shutdown(timeout_seconds=1.0)
        self.assertEqual(
            len([t for t in scheduler._aux_tasks if not t.done()]), 0
        )

    async def test_store_without_the_table_is_survivable(self) -> None:
        """读不到台账（老库/替身）时按「无退避」处理，绝不抛。"""

        cfg = self._cfg()
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=FakeStore({GROUP_ID: _rows(GROUP_ID, 600)}),
            config_provider=lambda: cfg,
            slot_waiter=None,
        )
        await scheduler._restore_failure_states()
        scheduler._register_failure(GROUP_ID, cfg)
        self.assertEqual(scheduler._failures[GROUP_ID], 1, "内存口径照旧可用")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
