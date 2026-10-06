from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.utils import telegram




@contextmanager
def _bind_telegram_send(**overrides):
    """把 ``telegram_send`` 字段绑进运行时配置（真实读路径）。

    以前这里 patch 的是模块常量；那些常量现在是"未绑定时的默认值"，patch 它们
    只会让测试"看起来在调、其实没调"。
    """

    from bot.config import Settings
    from bot.services import policy_runtime

    previous = policy_runtime.bound_settings()
    settings = previous or Settings(_env_file=None)
    settings = settings.model_copy(deep=True)
    holder = settings.telegram_send
    unknown = set(overrides) - set(holder.model_fields)
    if unknown:
        raise AssertionError(f"不是 telegram_send 的字段名：{sorted(unknown)}")
    setattr(settings, "telegram_send", holder.model_copy(update=overrides))
    policy_runtime.bind(settings)
    try:
        yield
    finally:
        if previous is None:
            policy_runtime.unbind()
        else:
            policy_runtime.bind(previous)


class TelegramTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await telegram.flush_telegram_background_tasks(timeout_seconds=0.2)

    async def test_typing_action_has_hard_deadline_for_cancel_resistant_send(self) -> None:
        release = asyncio.Event()

        async def stubborn_send(**_kwargs: object) -> None:
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()

        message = SimpleNamespace(
            chat=SimpleNamespace(id=-1001),
            bot=SimpleNamespace(send_chat_action=AsyncMock(side_effect=stubborn_send)),
        )

        started = time.monotonic()
        with (
            _bind_telegram_send(typing_send_timeout_seconds=0.01),
            patch.object(telegram, "_TELEGRAM_CANCEL_GRACE_SECONDS", 0.01),
            patch.object(telegram, "_TYPING_WORKER_CANCEL_GRACE_SECONDS", 0.05),
        ):
            async with telegram.typing_action(message, enabled=True, interval=60.0):
                pass

        self.assertLess(time.monotonic() - started, 0.25)
        self.assertTrue(telegram._TELEGRAM_BACKGROUND_TASKS)
        release.set()
        await telegram.flush_telegram_background_tasks(timeout_seconds=0.2)


class EntrypointShutdownTests(unittest.TestCase):
    def test_custom_runner_does_not_wait_forever_for_cancel_resistant_task(self) -> None:
        root = Path(__file__).resolve().parents[1]
        code = """
import asyncio
import bot.__main__ as entry

entry._FINAL_LOOP_DRAIN_SECONDS = 0.02

async def stubborn():
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            continue

async def main():
    asyncio.create_task(stubborn(), name="cancel-resistant-regression")

entry.run_async_entrypoint(main())
print("runner-finished", flush=True)
"""
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=root,
            capture_output=True,
            text=True,
            # Importing the full bot graph performs Pydantic schema generation
            # and can take ~15 seconds on a cold CI worker.  The runner itself
            # still has only a 20ms cancellation drain inside the child.
            timeout=25.0,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("runner-finished", completed.stdout)


if __name__ == "__main__":
    unittest.main()
