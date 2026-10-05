"""P4-1（A-17）：日志字段净化 —— 控制字符不能再伪造出「多条/多条记录」。

被测对象：

* ``bot/utils/logging_setup.py::sanitize_log_field``（新增的净化原语）
* ``bot/middlewares/logging_mw.py::LoggingMiddleware``（入口日志的三个用户可控字段：
  群名 ``chat``、用户名/昵称 ``name``、消息正文预览 ``preview``）

现象（修前）：三个字段原样写进 ``log.info("... | 聊天=%s | 用户=%s | 类型=%s | 内容=%s")``，
消息正文/用户名/群名里的 ``\\r`` / ``\\n`` / ``\\x00`` 会让**一条**记录看起来像**多条**，
ESC（``\\x1b``）能让终端按 ANSI 序列重绘，字段里的 ``|`` 能伪造出额外一列。对排障与
审计都是硬伤。

本文件在旧代码上会红：``sanitize_log_field`` 根本不存在（旧代码没有这个函数），
且旧中间件的 ``assertLogs`` 输出里会出现伪造出来的额外行。
"""

from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace

from bot.middlewares.logging_mw import LoggingMiddleware
from bot.utils.logging_setup import sanitize_log_field


def _event(
    *,
    text: str | None = "在吗",
    caption: str | None = None,
    username: str | None = "zhangsan",
    full_name: str = "张三",
    title: str = "群",
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=11,
        chat=SimpleNamespace(id=-100123, type="supergroup", title=title),
        from_user=SimpleNamespace(
            id=555, is_bot=False, full_name=full_name, username=username
        ),
        sender_chat=None,
        text=text,
        caption=caption,
        content_type="text",
    )


class SanitizeLogFieldTests(unittest.TestCase):
    def test_newlines_carriage_returns_and_nul_are_escaped_visibly(self) -> None:
        self.assertEqual(sanitize_log_field("a\nb"), "a\\nb")
        self.assertEqual(sanitize_log_field("a\rb"), "a\\rb")
        self.assertEqual(sanitize_log_field("a\x00b"), "a\\x00b")
        self.assertEqual(sanitize_log_field("a\tb"), "a\\tb")
        # 保留原文字符形态：不是占位符堆，转义后仍能读出原文。
        self.assertIn("hello", sanitize_log_field("hello\nworld"))

    def test_ansi_escape_sequences_lose_their_escape_byte(self) -> None:
        # ESC 被转义成 \x1b，剩下的是普通字面量：终端不会再按 ANSI 序列重绘。
        cleaned = sanitize_log_field("\x1b[31m红\x1b[0m")
        self.assertNotIn("\x1b", cleaned)
        self.assertIn("\\x1b[31m", cleaned)
        self.assertIn("\\x1b[0m", cleaned)

    def test_other_control_and_c1_characters_are_escaped(self) -> None:
        self.assertEqual(sanitize_log_field("a\x07b"), "a\\x07b")
        self.assertEqual(sanitize_log_field("a\x1bb"), "a\\x1bb")
        self.assertEqual(sanitize_log_field("a\x85b"), "a\\x85b")  # C1 NEL
        self.assertEqual(sanitize_log_field("a\x7fb"), "a\\x7fb")  # DEL

    def test_column_separator_inside_a_field_cannot_forge_a_column(self) -> None:
        self.assertEqual(sanitize_log_field("a|b"), "a\\|b")
        self.assertEqual(sanitize_log_field(" | 伪列=1"), " \\| 伪列=1")

    def test_clean_text_is_returned_unchanged(self) -> None:
        for value in ("在吗", "normal text 123", "带|的普通文本".replace("|", "／")):
            self.assertEqual(sanitize_log_field(value), value)
        self.assertEqual(sanitize_log_field(""), "")

    def test_non_string_values_use_str_semantics(self) -> None:
        self.assertEqual(sanitize_log_field(-100123), "-100123")
        self.assertEqual(sanitize_log_field(None), "None")


class LoggingMiddlewareSanitizationTests(unittest.IsolatedAsyncioTestCase):
    """入口日志不能再被消息内容伪造成多条记录。"""

    async def _run(self, event: SimpleNamespace) -> list[str]:
        async def handler(_event, _data):
            return "handled"

        event.bot = SimpleNamespace(token="42:TEST")
        with self.assertLogs("bot.middlewares.logging_mw", level=logging.INFO) as logs:
            await LoggingMiddleware()(
                handler, event, {"event_update": SimpleNamespace(update_id=9)}
            )
        return logs.output

    async def test_message_body_newlines_do_not_forge_extra_log_lines(self) -> None:
        # 消息正文这条路径本来就 replace("\\n")；真正没被处理的是**用户名/群名**，
        # 伪造行也正是从这两个字段来的。
        lines = await self._run(
            _event(username="evil\n2026-01-01 00:00:00 | ERROR | db | 流=x | 群=-1")
        )
        self.assertEqual(len(lines), 2, f"一条消息只应产生入口/出口两行：{lines}")
        self.assertIn("evil\\n2026-01-01 00:00:00", lines[0])
        # 伪造的记录会以「时间戳」开头；真实的两行都以 logging 自己的格式开头。
        for line in lines:
            self.assertFalse(
                line.startswith("2026-01-01"),
                f"用户名伪造出了一条独立记录：{line!r}",
            )

    async def test_group_title_control_characters_cannot_forge_a_record(self) -> None:
        lines = await self._run(_event(title="群\r\n2026-01-01 00:00:00 | 严重 | db | 流=x"))
        self.assertEqual(len(lines), 2, f"一条消息只应产生入口/出口两行：{lines}")
        self.assertIn("群\\r\\n2026-01-01", lines[0])

    async def test_caption_control_characters_are_escaped(self) -> None:
        lines = await self._run(
            _event(text=None, caption="首行\r\x00\x1b[2K伪造\x1b[0m")
        )
        self.assertEqual(len(lines), 2, f"一条消息只应产生入口/出口两行：{lines}")
        entry = lines[0]
        self.assertIn("\\r", entry)
        self.assertIn("\\x00", entry)
        self.assertIn("\\x1b[2K", entry)
        self.assertNotIn("\x1b", entry)
        self.assertNotIn("\x00", entry)
        self.assertNotIn("\r", entry)

    async def test_no_record_contains_a_raw_escape_or_nul_byte(self) -> None:
        lines = await self._run(
            _event(
                text="正常正文",
                title="群\x1b[31m",
                username="user\x00name\r",
            )
        )
        for line in lines:
            self.assertNotIn("\x1b", line)
            self.assertNotIn("\x00", line)
            self.assertNotIn("\r", line)

    async def test_pipe_inside_a_title_cannot_forge_a_column(self) -> None:
        lines = await self._run(_event(title="群 | 用户=root | 类型=system"))
        self.assertEqual(len(lines), 2)
        self.assertIn("群 \\| 用户=root \\| 类型=system", lines[0])

    async def test_ordinary_message_log_is_unchanged_by_the_sanitizer(self) -> None:
        """默认路径下净化必须是**恒等**的：老代码打什么，现在就打什么。"""

        lines = await self._run(_event(text="在吗", title="群", username="zhangsan"))
        self.assertEqual(len(lines), 2)
        self.assertIn("收到消息", lines[0])
        self.assertIn("聊天=群 | 用户=zhangsan | 类型=text | 内容=在吗", lines[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
