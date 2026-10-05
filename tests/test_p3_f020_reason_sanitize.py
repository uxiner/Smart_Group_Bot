"""P3-6 / F-020：模型生成的 ``判定理由`` 原样进审核/交接卡片。

复现的原缺陷
------------
``bot/handlers/group.py`` 的交接卡把模型自由文本 ``reason`` 直接拼进
``f"判定理由：{reason}"``。这条消息是发给管理员的纯文本卡（``disable_web_page_
preview=True``），于是：

* 模型输出里的**裸 URL 会被 Telegram 自动变成可点链接**——证据卡里冒出一个
  入口，来源是模型自由文本；
* **换行**会打乱"一行一个字段"的版式（后面还有消息回链、@机器人 提示）；
* 长度不可控。

修复（最小改动）：展示前按固定格式净化——剥 URL、去换行、限长。
**不改变审核判定与动作**，也不动卡片其它字段。
"""

from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.config import Settings
from bot.handlers import group

#: 期望的固定格式：链接一律换成这个占位符（独立写一遍，不从实现里导入）。
URL_PLACEHOLDER = "[链接]"

MALICIOUS_REASON = (
    "命中规则 https://t.me/+abcdef\n第二行再来一次 www.example.com/x?a=1"
)
CLEAN_REASON = "命中正则规则"


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.moderation.log_channel_id = -1000000000001
    return settings


def _callback(card: str = ""):
    return SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7))),
        message=SimpleNamespace(html_text=card),
    )


def _violation(reason: str):
    return SimpleNamespace(
        id=99,
        group_id=-100,
        user_id=12345,
        action_taken="ban",
        confidence=0.97,
        verdict_reason=reason,
        message_text="加微信 abc123",
        source_message_id=555,
    )


class SanitizeModelReasonTests(unittest.TestCase):
    """① 含 URL/换行的 reason 被净化；② 干净 reason 原样保留。"""

    def test_urls_and_newlines_are_stripped(self) -> None:
        cleaned = group._sanitize_model_reason(MALICIOUS_REASON)

        self.assertNotIn("https://", cleaned)
        self.assertNotIn("www.example.com", cleaned)
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("\r", cleaned)
        self.assertIn(URL_PLACEHOLDER, cleaned)
        # 正文本身不能被悄悄删掉。
        self.assertIn("命中规则", cleaned)
        self.assertIn("第二行再来一次", cleaned)

    def test_telegram_autolink_trigger_forms_are_covered(self) -> None:
        for raw in (
            "详见 https://example.com/a",
            "详见 http://example.com/a",
            "详见 www.example.com/a",
            "详见 HTTPS://EXAMPLE.COM/a",
        ):
            with self.subTest(raw=raw):
                cleaned = group._sanitize_model_reason(raw)
                self.assertNotIn("://", cleaned)
                self.assertNotIn("www.", cleaned)
                self.assertIn(URL_PLACEHOLDER, cleaned)

    def test_clean_reason_is_kept_verbatim(self) -> None:
        self.assertEqual(group._sanitize_model_reason(CLEAN_REASON), CLEAN_REASON)
        self.assertEqual(
            group._sanitize_model_reason("命中关键词规则：加微信"),
            "命中关键词规则：加微信",
        )

    def test_length_is_bounded(self) -> None:
        cleaned = group._sanitize_model_reason("长" * 500)

        self.assertLessEqual(len(cleaned), 121)
        self.assertTrue(cleaned.endswith("…"))

    def test_empty_and_non_string_inputs_are_safe(self) -> None:
        self.assertEqual(group._sanitize_model_reason(""), "")
        self.assertEqual(group._sanitize_model_reason(None), "")
        self.assertEqual(group._sanitize_model_reason(123), "123")


class HandoverCardReasonTests(unittest.IsolatedAsyncioTestCase):
    """端到端：交接卡上的「判定理由」那一行是净化过的，别的字段一字不动。"""

    async def asyncSetUp(self) -> None:
        from bot.config import Settings
        from bot.services import policy_runtime

        # 交接对象默认是空的（公开树不 @ 任何人）；这个文件专门验 mention 实体的
        # UTF-16 偏移，所以显式配一个合成账号再跑。
        settings = Settings(_env_file=None)
        settings.moderation.review_handover_mention = "@your_bot"
        policy_runtime.bind(settings)
        self.addCleanup(policy_runtime.unbind)

    async def _send(self, reason: str, card: str = ""):
        callback = _callback(card)
        await group._send_review_handover(
            callback=callback,
            settings=_settings(),
            violation=_violation(reason),
            header="🟢 人工放行 · 待调整规则",
        )
        self.assertTrue(callback.bot.send_message.await_args, "交接消息没有发出去")
        return callback.bot.send_message.await_args.kwargs

    async def test_card_reason_line_is_sanitized(self) -> None:
        text = (await self._send(MALICIOUS_REASON))["text"]

        reason_lines = [line for line in text.splitlines() if line.startswith("判定理由：")]
        self.assertEqual(len(reason_lines), 1, text)
        self.assertNotIn("://", reason_lines[0])
        self.assertNotIn("www.", reason_lines[0])
        self.assertIn(URL_PLACEHOLDER, reason_lines[0])

    async def test_clean_reason_reaches_the_card_unchanged(self) -> None:
        text = (await self._send(CLEAN_REASON))["text"]

        self.assertIn(f"判定理由：{CLEAN_REASON}", text)

    async def test_card_layout_and_other_fields_are_untouched(self) -> None:
        kwargs = await self._send(MALICIOUS_REASON)
        text = kwargs["text"]
        lines = text.splitlines()

        self.assertEqual(lines[0], "🟢 人工放行 · 待调整规则")
        for label in ("case：", "群组：", "发送者：", "命中规则：", "动作：", "置信度："):
            self.assertTrue(
                any(line.startswith(label) for line in lines), f"缺字段 {label}: {lines}"
            )
        # 净化不得改判定与动作本身。
        self.assertIn("动作：ban", text)
        self.assertIn("置信度：0.97", text)
        # mention 实体偏移仍然指向 @your_bot 本身（净化换行不能把它带偏）。
        entities = kwargs["entities"]
        self.assertEqual(len(entities), 1)
        mention = group._review_handover_mention()
        self.assertTrue(mention, "本用例需要显式配置交接对象")
        self.assertEqual(entities[0].type, "mention")
        self.assertEqual(entities[0].length, len(mention))
        utf16 = text.encode("utf-16-le")
        start = entities[0].offset * 2
        self.assertEqual(
            utf16[start : start + entities[0].length * 2].decode("utf-16-le"), mention
        )

    async def test_reason_taken_from_the_card_is_sanitized_too(self) -> None:
        """判定理由也可能来自证据卡本身（那条路径同样要净化）。"""

        card = (
            "<b>判定理由</b> 详见 https://t.me/spam\n并附一行说明"
        )
        kwargs = await self._send("ignored", card=card)
        text = kwargs["text"]

        self.assertNotIn("https://t.me/spam", text)
        self.assertIn(URL_PLACEHOLDER, text)
