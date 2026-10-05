"""P4-3（A-22）：Telegram 后台任务失败不再被静默吞掉。

现象：``_observe_telegram_background_task`` 是所有 detached Telegram 任务
（删除按钮挂载、定时清理、投递确认……）的统一回收点，改前是

    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass

于是任何一个后台任务炸了，进程里**没有任何痕迹**：既没有日志、也没有告警，
排障时只能看到「群里那句话没出现」，查不出是谁炸的。

``typing_action`` 的输入状态发送是同一类问题（``except Exception: return False``），
一并补上痕迹。

要求（任务书）：只落日志，**不改变任务的成败语义、不加重试**。本文件因此同时钉住
「失败被记录」与「行为不变」（记账照旧、异常不外抛、上下文管理器照常退出）。
"""

from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.utils import telegram


class ObserveBackgroundTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await telegram.flush_telegram_background_tasks(timeout_seconds=0.2)

    async def _failing_task(self, name: str = "probe-task") -> asyncio.Task[object]:
        async def _boom() -> None:
            raise RuntimeError("telegram said no")

        task = asyncio.create_task(_boom(), name=name)
        telegram._track_telegram_background_task(task)
        with self.assertLogs("bot.utils.telegram", level=logging.ERROR) as logs:
            # 任务自己的异常不能顺着 await 冒出来（那正是「观察器要接住」的东西）。
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
        self._logs = logs.output
        return task

    async def test_a_failed_task_logs_its_name_error_and_traceback(self) -> None:
        await self._failing_task("delete-button:-1001:7")
        joined = "\n".join(self._logs)
        self.assertIn("Telegram background task failed", joined)
        # 任务名 = 发起位置（排障要能直接定位到是哪一个投递）
        self.assertIn("delete-button:-1001:7#", joined)
        self.assertIn("error=RuntimeError", joined)
        self.assertIn("telegram said no", joined)
        # 完整 traceback，而不是只有一行 message
        self.assertIn("Traceback (most recent call last)", joined)
        self.assertIn("_boom", joined)

    async def test_the_failure_is_recorded_exactly_once(self) -> None:
        task = await self._failing_task("single-shot")
        failures = [
            line for line in self._logs if "Telegram background task failed" in line
        ]
        self.assertEqual(len(failures), 1)
        # 回收两次不会把账算错，也不会重复刷日志。
        with self.assertNoLogs("bot.utils.telegram", level=logging.ERROR):
            telegram._observe_telegram_background_task(task)

    async def test_bookkeeping_is_unchanged_by_the_new_logging(self) -> None:
        task = await self._failing_task()
        self.assertNotIn(task, telegram._TELEGRAM_BACKGROUND_TASKS)
        self.assertNotIn(task, telegram._TELEGRAM_BACKGROUND_STARTED)
        # 健康度口径不受影响：失败任务不算「还在跑」。
        snapshot = telegram.telegram_background_health_snapshot()
        self.assertEqual(snapshot["task_count"], 0)

    async def test_a_successful_task_logs_nothing(self) -> None:
        async def _ok() -> str:
            return "sent"

        task = asyncio.create_task(_ok(), name="quiet-task")
        telegram._track_telegram_background_task(task)
        with self.assertNoLogs("bot.utils.telegram", level=logging.DEBUG):
            await task
            await asyncio.sleep(0)
        self.assertTrue(task.result() == "sent")
        self.assertNotIn(task, telegram._TELEGRAM_BACKGROUND_TASKS)

    async def test_a_cancelled_task_logs_nothing(self) -> None:
        async def _forever() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever(), name="cancel-me")
        telegram._track_telegram_background_task(task)
        task.cancel()
        with self.assertNoLogs("bot.utils.telegram", level=logging.DEBUG):
            await asyncio.gather(task, return_exceptions=True)
            telegram._observe_telegram_background_task(task)

    async def test_observer_does_not_swallow_a_base_exception_task(self) -> None:
        """``raise exc`` 兜底分支：``BaseException`` 也不能把观察器自己炸掉。

        用自定义 ``BaseException`` 子类而不是 SystemExit/KeyboardInterrupt：
        那两个会被 asyncio Task 重新抛回事件循环（``Task.__step``），没法在用例里接。
        """

        class _Fatal(BaseException):
            pass

        async def _fatal() -> None:
            raise _Fatal("hard stop")

        task = asyncio.create_task(_fatal(), name="base-exception-task")
        telegram._track_telegram_background_task(task)
        with self.assertLogs("bot.utils.telegram", level=logging.ERROR) as logs:
            await asyncio.gather(task, return_exceptions=True)
            telegram._observe_telegram_background_task(task)
        joined = "\n".join(logs.output)
        self.assertIn("Telegram background task failed", joined)
        self.assertIn("error=_Fatal", joined)


class TypingActionFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await telegram.flush_telegram_background_tasks(timeout_seconds=0.2)

    async def test_a_failing_typing_send_is_logged_and_still_returns_cleanly(self) -> None:
        async def _refuse(**_kwargs: object) -> None:
            raise RuntimeError("typing not allowed")

        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001),
            bot=SimpleNamespace(send_chat_action=AsyncMock(side_effect=_refuse)),
        )
        with self.assertLogs("bot.utils.telegram", level=logging.WARNING) as logs:
            async with telegram.typing_action(message, enabled=True, interval=60.0):
                # 成败语义不变：输入状态失败绝不能把调用方的上下文炸掉。
                self.assertTrue(True)
        joined = "\n".join(logs.output)
        self.assertIn("Telegram typing action send failed", joined)
        self.assertIn("typing-send:-1001#", joined)
        self.assertIn("error=RuntimeError", joined)

    async def test_a_healthy_typing_send_is_not_logged(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001),
            bot=SimpleNamespace(send_chat_action=AsyncMock(return_value=None)),
        )
        with self.assertNoLogs("bot.utils.telegram", level=logging.DEBUG):
            async with telegram.typing_action(message, enabled=True, interval=60.0):
                pass
        message.bot.send_chat_action.assert_awaited()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
