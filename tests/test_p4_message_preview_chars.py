"""P4-2（A-16）：入口日志的消息正文预览字数变成可配的 ``logging.message_preview_chars``。

现象：``bot/middlewares/logging_mw.py`` 把消息正文前 100 字**明文**写进 INFO 日志，
这 100 字是硬编码的，运维既不能调小也不能关掉——生产日志里因此长期留着一份
用户原文（合规与隐私角度都不该由日志默认承担）。

要求（任务书）：**默认行为一字不变**。所以本文件的第一组断言钉的是
「默认 100 = 改之前的硬编码值」，而不是新值。

被测对象：

* ``LoggingSettingsConfig.message_preview_chars``（schema，默认 100）
* ``build_legacy_runtime_config`` 的 env 引导（``LOG_MESSAGE_PREVIEW_CHARS``）
* ``logging_setup.message_preview_chars()`` / ``redacted_message_preview()``
* ``LoggingMiddleware`` 按该值决定预览长度；0 = 不落盘正文
"""

from __future__ import annotations

import logging
import os
import unittest
from types import SimpleNamespace

from bot.middlewares.logging_mw import LoggingMiddleware
from bot.services.runtime_config import (
    LoggingSettingsConfig,
    build_legacy_runtime_config,
)
from bot.utils.logging_setup import (
    configure_logging,
    message_preview_chars,
    redacted_message_preview,
    reset_log_context,
    shutdown_logging,
)

_LONG_TEXT = "0123456789" * 30  # 300 字


def _event(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=11,
        chat=SimpleNamespace(id=-100123, type="supergroup", title="群"),
        from_user=SimpleNamespace(
            id=555, is_bot=False, full_name="张三", username="zhangsan"
        ),
        sender_chat=None,
        text=text,
        caption=None,
        content_type="text",
        bot=SimpleNamespace(token="42:TEST"),
    )


class MessagePreviewCharsSchemaTests(unittest.TestCase):
    def test_schema_default_is_the_preexisting_hardcoded_value(self) -> None:
        self.assertEqual(LoggingSettingsConfig().message_preview_chars, 100)

    def test_env_bootstrap_defaults_to_100_and_honours_the_env_override(self) -> None:
        default_import = build_legacy_runtime_config(
            "config.toml", raw_env={"BOT_TOKEN": "1:TEST"}
        )
        self.assertEqual(default_import.logging.message_preview_chars, 100)

        overridden = build_legacy_runtime_config(
            "config.toml",
            raw_env={"BOT_TOKEN": "1:TEST", "LOG_MESSAGE_PREVIEW_CHARS": "12"},
        )
        self.assertEqual(overridden.logging.message_preview_chars, 12)

    def test_mini_app_exposes_the_knob_with_the_same_bounds(self) -> None:
        from pathlib import Path

        app_js = (
            Path(__file__).resolve().parents[1]
            / "bot"
            / "web"
            / "static"
            / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('field("logging.message_preview_chars"', app_js)
        field = LoggingSettingsConfig.model_fields["message_preview_chars"]
        self.assertEqual(getattr(field.metadata[0], "ge", None), 0)
        self.assertEqual(getattr(field.metadata[1], "le", None), 1000)


class MessagePreviewCharsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """``configure_logging`` 发布当前值，中间件按它决定预览长度。"""

    def setUp(self) -> None:
        root = logging.getLogger()
        self._old_handlers = list(root.handlers)
        self._old_level = root.level
        self._old_env = os.environ.get("LOG_MESSAGE_PREVIEW_CHARS")
        self._old_chars = message_preview_chars()

    def tearDown(self) -> None:
        root = logging.getLogger()
        shutdown_logging(timeout=2.0)
        root.handlers = self._old_handlers
        root.setLevel(self._old_level)
        if self._old_env is None:
            os.environ.pop("LOG_MESSAGE_PREVIEW_CHARS", None)
        else:
            os.environ["LOG_MESSAGE_PREVIEW_CHARS"] = self._old_env
        import bot.utils.logging_setup as logging_setup

        logging_setup._MESSAGE_PREVIEW_CHARS = self._old_chars

    async def _entry_line(self, text: str) -> str:
        async def handler(_event, _data):
            return "handled"

        with self.assertLogs("bot.middlewares.logging_mw", level=logging.INFO) as logs:
            await LoggingMiddleware()(
                handler, _event(text), {"event_update": SimpleNamespace(update_id=9)}
            )
        return logs.output[0]

    @staticmethod
    def _content_field(line: str) -> str:
        """从入口日志行里取出「内容=」后面的正文预览。"""

        parts = line.split("内容=", 1)
        assert len(parts) == 2, f"入口日志行没有内容字段：{line!r}"
        return parts[1]

    async def test_default_keeps_the_first_100_characters(self) -> None:
        os.environ.pop("LOG_MESSAGE_PREVIEW_CHARS", None)
        configure_logging(force=True)
        self.assertEqual(message_preview_chars(), 100)
        content = self._content_field(await self._entry_line(_LONG_TEXT))
        self.assertEqual(content, _LONG_TEXT[:100])
        self.assertEqual(len(content), 100)

    async def test_a_smaller_budget_truncates_the_preview(self) -> None:
        os.environ.pop("LOG_MESSAGE_PREVIEW_CHARS", None)
        configure_logging(
            force=True,
            config=LoggingSettingsConfig(message_preview_chars=10),
        )
        self.assertEqual(message_preview_chars(), 10)
        content = self._content_field(await self._entry_line(_LONG_TEXT))
        self.assertEqual(content, _LONG_TEXT[:10])
        self.assertEqual(len(content), 10)

    async def test_zero_records_no_body_just_length_and_digest(self) -> None:
        os.environ["LOG_MESSAGE_PREVIEW_CHARS"] = "0"
        configure_logging(force=True)
        self.assertEqual(message_preview_chars(), 0)
        line = await self._entry_line(_LONG_TEXT)
        content = self._content_field(line)
        self.assertNotIn("0123456789", line)
        self.assertEqual(content, redacted_message_preview(_LONG_TEXT))
        self.assertIn(f"{len(_LONG_TEXT)}字", content)

    async def test_empty_body_still_logs_the_placeholder_dash(self) -> None:
        os.environ["LOG_MESSAGE_PREVIEW_CHARS"] = "0"
        configure_logging(force=True)
        content = self._content_field(await self._entry_line("   "))
        self.assertEqual(content, "-")

    async def test_env_override_is_published_by_the_env_only_startup_path(self) -> None:
        os.environ["LOG_MESSAGE_PREVIEW_CHARS"] = "7"
        configure_logging(force=True)
        self.assertEqual(message_preview_chars(), 7)
        content = self._content_field(await self._entry_line(_LONG_TEXT))
        self.assertEqual(content, _LONG_TEXT[:7])
        self.assertEqual(len(content), 7)


class RedactedMessagePreviewTests(unittest.TestCase):
    def test_empty_text_keeps_the_dash_placeholder(self) -> None:
        self.assertEqual(redacted_message_preview(""), "-")

    def test_two_different_bodies_get_different_digests(self) -> None:
        first = redacted_message_preview("内容甲")
        second = redacted_message_preview("内容乙")
        self.assertNotEqual(first, second)
        self.assertNotIn("内容甲", first)
        self.assertNotIn("内容乙", second)
        self.assertTrue(first.startswith("<3字 #"))

    def test_the_same_body_is_stable_across_calls(self) -> None:
        self.assertEqual(
            redacted_message_preview("同样的话"), redacted_message_preview("同样的话")
        )

    def test_it_carries_no_forgeable_control_characters(self) -> None:
        rendered = redacted_message_preview("a\r\nb\x00c")
        self.assertNotIn("\r", rendered)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\x00", rendered)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
