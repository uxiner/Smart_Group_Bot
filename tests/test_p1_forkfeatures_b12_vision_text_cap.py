"""修复批 P1-2 / B-12：``vision_text`` 必须在代码侧有长度上限。

复现的原缺陷（``AUDIT-B`` B-12）：

``_VISION_DESCRIBE_PROMPT`` 里「Respond briefly in Chinese within 30 words」只是
**软约束**，代码侧没有截断：``vision_text`` 随后被拼进 ``input_text``，再进入关键词/
正则扫描、决策上下文、以及 ``group_message_archive`` 归档。一张文字密集的截图可以让
OCR 描述膨胀到几万个字符。（正则扫描侧有 ``safe_regex`` + 20ms/整体 deadline 兜底，
所以不存在 ReDoS 放大；问题在 token 预算与归档行。）

修法：``_cap_vision_text`` 在**两个**产出点（``_append_image_context`` 与
``_nsfw_video_thumbnail_vision_text``）做硬截断，日志之外的所有下游只喂截断版。
截断**保留最后一行**的 NSFW 判定 JSON——一刀切在末尾会把判定砍成半行。
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from bot.handlers import group as group_handler


class VisionTextCapTests(unittest.TestCase):
    def test_short_text_is_untouched(self) -> None:
        self.assertEqual(group_handler._cap_vision_text("一张显卡"), "一张显卡")

    def test_long_text_is_truncated_to_the_cap(self) -> None:
        body = "很长的OCR文字" * 5000
        capped = group_handler._cap_vision_text(body)
        self.assertLessEqual(len(capped), group_handler.VISION_TEXT_MAX_CHARS + 16)
        self.assertTrue(capped.startswith("很长的OCR文字"))
        self.assertTrue(capped.endswith("..."))

    def test_the_nsfw_decision_line_is_preserved(self) -> None:
        """末尾判定行必须完整保留——砍成半行会让 NSFW 守卫静默失效。"""

        body = "画面里是海边\n" + "细节" * 3000
        raw = f"{body}\nNSFW_DECISION: {{\"nsfw\": \"yes\"}}"
        self.assertEqual(
            group_handler._parse_nsfw_marker(raw),
            "NSFW_YES",
            "未截断的原文本来就应能解析出判定",
        )
        capped = group_handler._cap_vision_text(raw)
        self.assertLessEqual(
            len(capped), group_handler.VISION_TEXT_MAX_CHARS + len('NSFW_DECISION: {"nsfw": "yes"}') + 1
        )
        self.assertTrue(
            capped.splitlines()[-1] == 'NSFW_DECISION: {"nsfw": "yes"}',
            f"末尾判定行必须一字不差，实际={capped.splitlines()[-1:]!r}",
        )
        self.assertEqual(
            group_handler._parse_nsfw_marker(capped),
            "NSFW_YES",
            "截断后判定仍必须可解析",
        )

    def test_the_cap_is_a_positive_budget(self) -> None:
        self.assertGreater(group_handler.VISION_TEXT_MAX_CHARS, 0)
        self.assertLessEqual(group_handler.VISION_TEXT_MAX_CHARS, 2000)


class AppendImageContextCapTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_downstream_receives_the_capped_text(self) -> None:
        """``input_text``（扫描/决策/归档的入口）与原始返回值都必须是截断版。"""

        huge = "文字" * 20000
        message = AsyncMock()
        message.photo = [object()]
        message.caption = None
        message.document = None
        message.animation = None
        message.sticker = None
        message.text = None
        llm = AsyncMock()
        llm.vision_describe = AsyncMock(return_value=huge)

        with patch.object(
            group_handler, "_build_telegram_image_data_uri", new=AsyncMock(return_value="data:")
        ), patch.object(group_handler, "_await_hard_deadline", new=_immediate):
            enriched, raw = await group_handler._append_image_context(message, llm, "原文", "photo")

        self.assertIn("[image-vision]", enriched)
        self.assertNotIn(huge, enriched, "拼进 input_text 的必须是截断版")
        self.assertLessEqual(
            len(raw), group_handler.VISION_TEXT_MAX_CHARS + 16, "返回值也必须是截断版"
        )
        self.assertLess(len(enriched), 2 * group_handler.VISION_TEXT_MAX_CHARS)


async def _immediate(awaitable, *, timeout_seconds):  # noqa: ANN001
    return await awaitable


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
