"""「用图找番号」只能私聊：群内一次外发都不做（用户口径 2026-10-03）。

用户原话：*不可以……只能设置私聊发图反查，群聊不允许发 NSFW 图和视频。*

锁定的两条：
- **私聊**：反查照旧执行（命中即短路，省掉视觉调用）；
- **群聊**：`try_reverse_image_lookup` **一次都不能被调用**（群成员的图不外发第三方），
  但仍走「读图里的文字」那条老路，群内行为不变（先删图在调用方完成）。

Telegram 与模型调用全是 mock，不触网。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import ModelConfig, Settings
from bot.handlers import commands

USER_ID = 900123
KB = 1024
PRIVATE_CHAT_ID = 900123
GROUP_CHAT_ID = -100987


class _AsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 0
    settings.bot.main_model = ModelConfig(model="main-model-for-test")
    settings.bot.vision_model = ModelConfig(model="vision-model-for-test")
    return settings


def _image_message(chat_type: str) -> SimpleNamespace:
    photo = SimpleNamespace(file_id="f-40k", file_size=40 * KB, width=100, height=100)
    chat_id = PRIVATE_CHAT_ID if chat_type == "private" else GROUP_CHAT_ID
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="群" if chat_type != "private" else ""),
        from_user=SimpleNamespace(id=USER_ID, full_name="张三", username="zhangsan"),
        photo=[photo],
        document=None,
        animation=None,
        message_id=11,
        caption="/av",
    )


class ReverseLookupPrivateOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, chat_type: str, reverse_result: str):
        reverse = AsyncMock(return_value=reverse_result)
        vision = AsyncMock(return_value="编号 SONE-342 清原みゆう")
        with (
            patch.object(commands, "try_reverse_image_lookup", new=reverse),
            patch.object(
                commands,
                "build_av_image_data_uri_for",
                new=AsyncMock(return_value="data:image/jpeg;base64,QUJD"),
            ),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_av_vision_text", new=vision),
        ):
            outcome = await commands._av_image_vision_with_escalation(
                _image_message(chat_type), _settings(), user_id=USER_ID
            )
        return reverse, vision, outcome

    async def test_private_chat_still_uses_reverse_lookup(self) -> None:
        reverse, vision, outcome = await self._run("private", "SONE-342")

        reverse.assert_awaited_once()
        # 命中即短路，不浪费视觉调用。
        vision.assert_not_awaited()
        self.assertEqual(outcome.vision_text, "SONE-342")

    async def test_group_chat_never_calls_reverse_lookup(self) -> None:
        reverse, vision, outcome = await self._run("supergroup", "SONE-342")

        reverse.assert_not_awaited()
        # 群内仍走「读图里的文字」那条老路（行为不变，只是不外发）。
        vision.assert_awaited()
        self.assertIn("SONE-342", outcome.vision_text)

    async def test_group_skip_is_logged_not_silent(self) -> None:
        with self.assertLogs("bot.handlers.commands", level="INFO") as logs:
            await self._run("group", "")

        joined = "\n".join(logs.output)
        self.assertIn("群内跳过用图反查", joined)


if __name__ == "__main__":
    unittest.main()
