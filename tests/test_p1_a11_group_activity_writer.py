"""A-11：群活动写入器不得无限重试，pending 条目必须能出队。

``_run_group_activity_writer`` 原来在 ``except Exception`` 之后退避封顶 30 秒并
永久 ``continue``：既没有最大重试次数，也没有死信或丢弃出口，
``_GROUP_ACTIVITY_PENDING`` 里的那条记录因此永远清不掉。某个群若是**确定性**
失败（约束冲突、超长标题、连接被永久拒绝），它的后台 task 就会每 30 秒重开一个
session 循环到进程退出，而且健康快照看不到它。
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.handlers import group as group_module


def _session_factory() -> MagicMock:
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


def _yielding_sleep() -> AsyncMock:
    """A sleep stub that still yields to the loop.

    A plain ``AsyncMock`` returns without ever suspending, so a ``while True``
    retry loop that never yields would hang the whole test run instead of
    failing on the bound we are asserting.
    """

    real_sleep = asyncio.sleep

    async def _sleep(_delay: float) -> None:
        await real_sleep(0)

    return AsyncMock(side_effect=_sleep)


def _pending(group_id: int) -> object:
    return group_module._PendingGroupActivityWrite(
        session_factory=_session_factory(),
        title="测试群",
        settings=MagicMock(),
        activity_at=group_module.now_shanghai_naive(),
    )


class GroupActivityWriterRetryBudgetTests(unittest.IsolatedAsyncioTestCase):
    GROUP_ID = -100777

    def setUp(self) -> None:
        self._saved_pending = dict(group_module._GROUP_ACTIVITY_PENDING)
        self._saved_max_attempts = getattr(
            group_module, "_GROUP_ACTIVITY_MAX_ATTEMPTS", 8
        )
        self.max_attempts = self._saved_max_attempts
        group_module._GROUP_ACTIVITY_PENDING.clear()

    def tearDown(self) -> None:
        group_module._GROUP_ACTIVITY_PENDING.clear()
        group_module._GROUP_ACTIVITY_PENDING.update(self._saved_pending)
        if hasattr(group_module, "_GROUP_ACTIVITY_MAX_ATTEMPTS"):
            group_module._GROUP_ACTIVITY_MAX_ATTEMPTS = self._saved_max_attempts

    async def test_permanent_failure_gives_up_and_clears_pending(self) -> None:
        group_module._GROUP_ACTIVITY_PENDING[self.GROUP_ID] = _pending(self.GROUP_ID)
        persist = AsyncMock(side_effect=RuntimeError("constraint permanently violated"))

        with (
            patch.object(group_module, "_persist_group_activity_cas", new=persist),
            patch.object(group_module.asyncio, "sleep", new=_yielding_sleep()) as sleep_mock,
            self.assertLogs(group_module.log.name, level="ERROR") as logs,
        ):
            await asyncio.wait_for(
                group_module._run_group_activity_writer(self.GROUP_ID),
                timeout=5.0,
            )

        self.assertEqual(
            persist.await_count,
            self.max_attempts,
            "达到上限后必须停止重试，而不是每 30 秒重开一个 session 循环到进程退出",
        )
        self.assertNotIn(
            self.GROUP_ID,
            group_module._GROUP_ACTIVITY_PENDING,
            "放弃时必须把 pending 摘掉，否则这张表永远清不干净",
        )
        self.assertTrue(
            any("abandoned after" in line for line in logs.output),
            f"放弃必须留下 ERROR 级证据，实际日志：{logs.output}",
        )
        # 只有前 N-1 次失败才需要退避等待。
        self.assertEqual(
            sleep_mock.await_count,
            self.max_attempts,
        )

    async def test_transient_failure_is_still_retried(self) -> None:
        group_module._GROUP_ACTIVITY_PENDING[self.GROUP_ID] = _pending(self.GROUP_ID)
        calls = {"count": 0}

        async def _flaky(*_args: object, **_kwargs: object) -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("database is locked")

        with (
            patch.object(group_module, "_persist_group_activity_cas", new=AsyncMock(side_effect=_flaky)),
            patch.object(group_module.asyncio, "sleep", new=_yielding_sleep()),
        ):
            await asyncio.wait_for(
                group_module._run_group_activity_writer(self.GROUP_ID),
                timeout=5.0,
            )

        self.assertEqual(calls["count"], 2)
        self.assertNotIn(self.GROUP_ID, group_module._GROUP_ACTIVITY_PENDING)

    async def test_giving_up_does_not_drop_a_newer_pending_write(self) -> None:
        original = _pending(self.GROUP_ID)
        group_module._GROUP_ACTIVITY_PENDING[self.GROUP_ID] = original

        async def _replace_pending_then_fail(*_args: object, **_kwargs: object) -> None:
            # 模拟重试期间又来了一条新消息：pending 已经被换成新对象。
            group_module._GROUP_ACTIVITY_PENDING[self.GROUP_ID] = _pending(self.GROUP_ID)
            raise RuntimeError("constraint permanently violated")

        group_module._GROUP_ACTIVITY_MAX_ATTEMPTS = 2
        self.max_attempts = 2
        with (
            patch.object(
                group_module,
                "_persist_group_activity_cas",
                new=AsyncMock(side_effect=_replace_pending_then_fail),
            ),
            patch.object(group_module.asyncio, "sleep", new=_yielding_sleep()),
        ):
            await asyncio.wait_for(
                group_module._run_group_activity_writer(self.GROUP_ID),
                timeout=5.0,
            )

        self.assertIn(
            self.GROUP_ID,
            group_module._GROUP_ACTIVITY_PENDING,
            "只能摘掉自己那条；期间到来的新写入必须留给下一轮正常路径",
        )
        self.assertIsNot(group_module._GROUP_ACTIVITY_PENDING[self.GROUP_ID], original)
