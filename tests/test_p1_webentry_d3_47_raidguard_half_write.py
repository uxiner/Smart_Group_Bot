"""修复批 P1-3 / D3-47：``/raidguard on`` 的「先落库后发消息」半写要说实话。

``RaidGuardService.enable_manual_lockdown`` 是**先落库/武装、后发 Telegram**::

    # bot/services/raid_guard.py:2103-2122
    saved = await self._update_persisted_manual_state(group_id, value=persisted)  # 落库
    ...
    self._arm_lockdown_until(...)                                                # 武装
    ...
    status_message_id = await self._publish_lockdown_status(...)                  # 发消息

而 handler 把两者混成一句"保存失败"::

    # bot/handlers/admin.py:3271-3273（修前）
    except Exception as exc:
        log.exception("manual raid enable failed | group=%s", group_id)
        detail = str(exc) if isinstance(exc, RuntimeError) else "手动爆破防护状态保存失败，请稍后重试"

flood-wait / 网络错属于 ``TelegramRetryAfter`` / ``TelegramNetworkError``，**不是
``RuntimeError``** → 一律落到那句"保存失败"。此时锁定**已经生效**（内存 + DB 都写
了），只是状态消息没发出去；管理员以为没生效、实际进群全被拒，重试还会把截止时间
推后。

本文件验证 handler 对两类异常给出**不同**的答复。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

from bot.handlers import admin


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        text="/raidguard on",
        message_id=1,
        chat=SimpleNamespace(id=-100123, type="supergroup", title="group"),
        from_user=SimpleNamespace(id=1001, is_bot=False),
        bot=SimpleNamespace(token="42:TEST"),
    )


def _settings():
    from bot.config import Settings

    return Settings(_env_file=None)


class RaidGuardEnableHalfWriteTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, failure: Exception) -> list[str]:
        answers: list[str] = []
        service = SimpleNamespace(enable_manual_lockdown=AsyncMock(side_effect=failure))
        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.admin._ensure_ban_command_admin",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.admin._commit_if_supported", new=AsyncMock()),
            patch("bot.handlers.admin.get_raid_guard_service", return_value=service),
            patch(
                "bot.handlers.admin._answer",
                new=AsyncMock(side_effect=lambda _m, _s, text, **_: answers.append(text)),
            ),
        ):
            await admin.cmd_raidguard(
                _message(), AsyncMock(), _settings(), session_factory=None
            )
        return answers

    async def test_persistence_failure_still_says_save_failed(self) -> None:
        answers = await self._run(RuntimeError("手动爆破防护状态保存失败，请稍后重试"))
        self.assertEqual(len(answers), 1)
        self.assertIn("保存失败", answers[0])

    async def test_telegram_failure_says_the_lockdown_is_already_on(self) -> None:
        answers = await self._run(
            TelegramRetryAfter(
                method=None,
                message="Too Many Requests: retry after 42",
                retry_after=42,
            )
        )
        self.assertEqual(len(answers), 1)
        self.assertIn("已开启", answers[0])
        self.assertNotIn("保存失败", answers[0])
        self.assertIn("/raidguard off", answers[0])

    async def test_network_failure_says_the_lockdown_is_already_on(self) -> None:
        answers = await self._run(
            TelegramNetworkError(method=None, message="connect timeout")
        )
        self.assertEqual(len(answers), 1)
        self.assertIn("已开启", answers[0])
        self.assertNotIn("保存失败", answers[0])
