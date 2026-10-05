"""私聊自主文字/语音（TTS）回归测试。

产品口径（2026-10-06）：私聊和群聊一样，由机器人**自己**决定这一条发文字还是发语音——
不是关键词命中才准发语音，也不是每条都强制语音，更没有固定随机概率。最高管理员本轮的
明确指示按**真实鉴权结果**优先；正文里自称超管不产生任何权限。

测的是会影响真实体验和钱的那些事：

- 信封（``[[DM_DELIVERY: text|voice]]``）解析：畸形也**不丢正文、不发空消息**，且
  **绝不显示、绝不落库**；
- 模型自主选文字/选语音；最高管理员指示优先；普通成员自称超管**不升级**；
- 引用/转述（「他说别发语音」「「用文字」」）**不产生规则**；
- 全局 TTS 不可用 → 自然文字回复，一个字节的媒介提示都不注入模型；
- 语音条被 Telegram 明确因语音隐私拒收 → 改投**真 MP3** 音频文件（不是改后缀名的
  OGG）；限流/Forbidden/其它 BadRequest **不冒充**隐私拒收；
- 合成/投递失败回正文文字，不谎称语音成功；多段只补发没发出去的段（不重复播出）；
- 发生可见回复才计配额，一段都没发出去照原语义退款；``CancelledError`` 不被吞掉；
- 私聊正文/语音**不流入**群档案或群记忆；群聊路由与 TTS 行为一个字都没改。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.db.engine import init_db
from bot.db.models import PrivateChatMessage
from bot.handlers import private_chat as dm_handler
from bot.services import private_chat as dm
from bot.services import private_tts

SUPER_ADMIN = 601298409
GROUP_ID = -1001364206062
MEDIA_ATTRS = (
    "photo",
    "document",
    "sticker",
    "animation",
    "video",
    "video_note",
    "voice",
    "audio",
)

#: 假的音频字节：语音载体走 ogg，音频载体走 mp3，测试要能一眼分清「有没有换载体」。
OGG_BYTES = b"OGG-OPUS-BYTES"
MP3_BYTES = b"ID3-MP3-BYTES"


def _settings(super_admin_id: int = SUPER_ADMIN) -> SimpleNamespace:
    return SimpleNamespace(
        super_admin_id=super_admin_id,
        bot=SimpleNamespace(),
        firecrawl_api_key="",
    )


def _verdict(tier: str = dm.TIER_MEMBER) -> dm.AccessVerdict:
    if tier == dm.TIER_NONE:
        return dm.AccessVerdict(False, tier)
    if tier == dm.TIER_UNKNOWN:
        return dm.AccessVerdict(None, tier)
    return dm.AccessVerdict(True, tier)


def _message(*, text=None, caption=None, chat_id: int = 4242, message_id: int = 9001) -> MagicMock:
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=chat_id, type="private")
    msg.from_user = SimpleNamespace(id=777, username="member", is_bot=False, first_name="M")
    msg.text = text
    msg.caption = caption
    for attr in MEDIA_ATTRS:
        setattr(msg, attr, None)
    msg.message_id = message_id
    msg.answer = AsyncMock()
    msg.answer_voice = AsyncMock(return_value=_sent(chat_id))
    msg.answer_audio = AsyncMock(return_value=_sent(chat_id))
    msg.bot = MagicMock()
    msg.bot.get_chat_member = AsyncMock()
    msg.bot.send_chat_action = AsyncMock()
    msg.bot.get_chat = AsyncMock(return_value=_chat())
    return msg


def _sent(chat_id: int = 4242, message_id: int = 77) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)


def _chat(*, restricted_voice: bool | None = False) -> SimpleNamespace:
    """``Chat`` 的替身。真机上最高管理员私聊就是 ``has_restricted_voice_and_video_messages=True``。"""

    return SimpleNamespace(id=4242, type="private", has_restricted_voice_and_video_messages=restricted_voice)


def _bad_request(description: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=f"Bad Request: {description}")


def _forbidden(description: str) -> TelegramForbiddenError:
    return TelegramForbiddenError(method=SimpleNamespace(), message=f"Forbidden: {description}")


def _quota_ok() -> dm.QuotaOutcome:
    return dm.QuotaOutcome(
        allowed=True, reason="ok", user_used=1, per_user_limit=20, global_used=1, global_limit=200
    )


def _answer(text: str, *, searches: int = 0) -> SimpleNamespace:
    return SimpleNamespace(text=text, searches=searches, exhausted=False)


class _FakeTTSService:
    """语音服务替身：只实现私聊这条路径真正用到的那几个方法。"""

    def __init__(
        self,
        *,
        available: bool = True,
        segments: list[str] | None = None,
        voice_ok: bool = True,
        audio_ok: bool = True,
        synth_error: str = "",
        fail_from: int = 0,
    ) -> None:
        self.available = available
        self._segments = segments
        self._voice_ok = voice_ok
        self._audio_ok = audio_ok
        self._synth_error = synth_error
        #: 1-based：第 N 段起合成失败（0 = 全段成功）
        self.fail_from = int(fail_from)
        self.voice_calls: list[str] = []
        self.audio_calls: list[str] = []
        self.audio_formats: list[str] = []

    def split_text(self, text: str) -> list[str]:
        if self._segments is not None:
            return list(self._segments)
        body = str(text or "").strip()
        return [body] if body else []

    async def synthesize_voice_payload(self, text: str, *, uid: str = "", **_kwargs):
        self.voice_calls.append(text)
        if self.fail_from and len(self.voice_calls) >= self.fail_from:
            return SimpleNamespace(ok=False, audio_bytes=b"", error="synthesis_failed", text=text)
        if self._synth_error:
            return SimpleNamespace(ok=False, audio_bytes=b"", error=self._synth_error, text=text)
        return SimpleNamespace(ok=True, audio_bytes=OGG_BYTES, error="", text=text)

    async def synthesize(self, text: str, *, audio_format: str = "", uid: str = "", **_kwargs):
        self.audio_calls.append(text)
        self.audio_formats.append(audio_format)
        if self._synth_error:
            return SimpleNamespace(ok=False, audio_bytes=b"", error=self._synth_error, text=text)
        # 真 MP3：音频载体必须是重新合成的 mp3，绝不是把 ogg 改个后缀名。
        return SimpleNamespace(ok=True, audio_bytes=MP3_BYTES, error="", audio_format=audio_format, text=text)


# ---------------------------------------------------------------------------
# 投递信封解析
# ---------------------------------------------------------------------------


class DeliveryEnvelopeTests(unittest.TestCase):
    def test_strict_voice_marker_selects_voice_and_is_stripped(self) -> None:
        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY: voice]]\n诶--我在呢")
        self.assertEqual(plan.delivery, private_tts.DELIVERY_VOICE)
        self.assertEqual(plan.text, "诶--我在呢", "信封是传输标记，正文才是给用户看的")
        self.assertFalse(plan.malformed)

    def test_strict_text_marker_selects_text_and_is_stripped(self) -> None:
        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY: text]]\n```python\nprint(1)\n```")
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)
        self.assertIn("print(1)", plan.text)
        self.assertNotIn("DM_DELIVERY", plan.text, "信封一个字都不许露出去")

    def test_marker_case_and_spacing_tolerated(self) -> None:
        for raw in ("[[dm_delivery:VOICE]]\n在的", "  [[ DM_DELIVERY : voice ]]\n在的"):
            with self.subTest(raw=raw):
                plan = private_tts.parse_dm_delivery(raw)
                self.assertEqual(plan.delivery, private_tts.DELIVERY_VOICE)
                self.assertEqual(plan.text, "在的")

    def test_no_marker_is_the_normal_text_case(self) -> None:
        plan = private_tts.parse_dm_delivery("  在的，怎么了？  ")
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)
        self.assertEqual(plan.text, "在的，怎么了？")
        self.assertFalse(plan.malformed)

    def test_marker_inside_the_body_is_ordinary_text(self) -> None:
        """只有独占开头那一行才是信封；正文里提到它就是正文。"""

        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY: voice]] 是啥意思？")
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)
        self.assertEqual(plan.text, "[[DM_DELIVERY: voice]] 是啥意思？")

    def test_malformed_marker_keeps_the_body_and_falls_back_to_text(self) -> None:
        for raw in (
            "[[DM_DELIVERY: 语音]]\n诶--我在呢",
            "[[DM_DELIVERY voice]]\n诶--我在呢",
            "[[DM_DELIVERY: video]]\n诶--我在呢",
        ):
            with self.subTest(raw=raw):
                plan = private_tts.parse_dm_delivery(raw)
                self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)
                self.assertEqual(plan.text, "诶--我在呢", "畸形也**不许丢正文**")
                self.assertTrue(plan.malformed)

    def test_marker_without_a_body_never_becomes_a_visible_empty_reply(self) -> None:
        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY: voice]]")
        self.assertEqual(plan.text, "", "只有信封没有正文时不能把信封当正文发出去")
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)

    def test_empty_input(self) -> None:
        plan = private_tts.parse_dm_delivery("")
        self.assertEqual(plan.text, "")
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)


# ---------------------------------------------------------------------------
# 最高管理员的媒介指示（纯解析）
# ---------------------------------------------------------------------------


class OwnerDirectiveTests(unittest.TestCase):
    def test_explicit_text_instruction(self) -> None:
        for raw in ("这次用文字", "改成文字吧", "文字就行", "先发文字，别的不急"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    private_tts.parse_owner_delivery_instruction(raw), private_tts.DELIVERY_TEXT
                )

    def test_explicit_voice_instruction(self) -> None:
        for raw in ("改为语音", "用语音说给我听", "发个语音听听", "以后都用语音"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    private_tts.parse_owner_delivery_instruction(raw), private_tts.DELIVERY_VOICE
                )

    def test_negation_beats_the_affirmative_form(self) -> None:
        """「不要发文字」不是要文字——那是不要文字，也就是要语音。"""

        self.assertEqual(
            private_tts.parse_owner_delivery_instruction("不要发文字"), private_tts.DELIVERY_VOICE
        )
        self.assertEqual(
            private_tts.parse_owner_delivery_instruction("别发语音，用文字"), private_tts.DELIVERY_TEXT
        )

    def test_voice_shutdown_wins_over_a_trailing_voice_mention(self) -> None:
        self.assertEqual(
            private_tts.parse_owner_delivery_instruction("语音别发了，用文字就行"),
            private_tts.DELIVERY_TEXT,
        )

    def test_delegating_back_yields_no_rule(self) -> None:
        for raw in ("随你选", "你决定就好", "都行", "看着办"):
            with self.subTest(raw=raw):
                self.assertIsNone(private_tts.parse_owner_delivery_instruction(raw))

    def test_quoted_or_reported_speech_is_not_an_instruction(self) -> None:
        """引用/转述里出现「用语音」不产生规则——上下文资料、群记录里的原话同理。"""

        for raw in (
            "「这次用文字」",
            "他说别发语音",
            "你之前说过用语音",
            "听说用语音更好",
            "资料里写着用语音",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(private_tts.parse_owner_delivery_instruction(raw))

    def test_ordinary_questions_produce_no_rule(self) -> None:
        for raw in ("今天天气怎么样", "把这段代码发我", "语音包怎么下载", "文字排版好看吗"):
            with self.subTest(raw=raw):
                self.assertIsNone(private_tts.parse_owner_delivery_instruction(raw))


# ---------------------------------------------------------------------------
# 提示词块：能选就别装死，且不泄露控制格式
# ---------------------------------------------------------------------------


class PreferenceBlockTests(unittest.TestCase):
    def test_block_is_empty_when_tts_is_off(self) -> None:
        """全局 TTS 不可用时一个字节都不注入——模型不会因此说「只能打字」。"""

        self.assertEqual(private_tts.build_private_tts_preference(service_ready=False), "")

    def test_block_teaches_the_envelope_and_the_choice(self) -> None:
        block = private_tts.build_private_tts_preference(service_ready=True)
        self.assertIn(private_tts.PRIVATE_TTS_PREFERENCE_HEADER, block)
        self.assertIn("[[DM_DELIVERY: voice]]", block)
        self.assertIn("[[DM_DELIVERY: text]]", block)
        self.assertIn("EITHER text or voice", block)

    def test_block_does_not_use_group_wording(self) -> None:
        """群聊那块是 ``[GROUP_TTS_PREFERENCE]``；私聊不能复用，否则模型以为在群里。"""

        block = private_tts.build_private_tts_preference(service_ready=True)
        self.assertNotIn("[GROUP_TTS_PREFERENCE]", block)
        self.assertNotIn("This group", block)

    def test_owner_directive_is_only_injected_for_a_real_owner_request(self) -> None:
        with_directive = private_tts.build_private_tts_preference(
            service_ready=True, owner_directive=private_tts.DELIVERY_VOICE
        )
        self.assertIn(private_tts.PRIVATE_TTS_DIRECTIVE_HEADER, with_directive)
        self.assertIn(private_tts.DELIVERY_VOICE, with_directive)

        plain = private_tts.build_private_tts_preference(service_ready=True)
        self.assertNotIn(private_tts.PRIVATE_TTS_DIRECTIVE_HEADER, plain)

    def test_directive_is_dropped_when_tts_is_off(self) -> None:
        block = private_tts.build_private_tts_preference(
            service_ready=False, owner_directive=private_tts.DELIVERY_VOICE
        )
        self.assertEqual(block, "")

    def test_build_messages_only_injects_when_asked(self) -> None:
        without = dm.build_private_chat_messages("在吗", sender_user_id=777)
        self.assertFalse(
            any(
                private_tts.PRIVATE_TTS_PREFERENCE_HEADER in str(m.get("content"))
                for m in without
            ),
            "没给 tts_preference 就必须一个字都不注入",
        )
        block = private_tts.build_private_tts_preference(service_ready=True)
        with_block = dm.build_private_chat_messages("在吗", sender_user_id=777, tts_preference=block)
        self.assertTrue(
            any(
                private_tts.PRIVATE_TTS_PREFERENCE_HEADER in str(m.get("content"))
                for m in with_block
            )
        )

    def test_member_text_never_reaches_the_system_role(self) -> None:
        """F-003 私聊版：成员可控正文一个字都不许出现在 system 消息里。"""

        marker = "INJECTABLE-TEXT-9c3f"
        block = private_tts.build_private_tts_preference(service_ready=True)
        messages = dm.build_private_chat_messages(
            marker, sender_user_id=777, tts_preference=block
        )
        systems = [str(m["content"]) for m in messages if m["role"] == "system"]
        self.assertFalse(any(marker in s for s in systems))


# ---------------------------------------------------------------------------
# 语音隐私判定
# ---------------------------------------------------------------------------


class VoicePrivacyDetectionTests(unittest.TestCase):
    def test_real_voice_privacy_rejections_are_recognized(self) -> None:
        for detail in (
            "Bad Request: not allowed to send voice messages",
            "Bad Request: voice messages are restricted",
            "VOICE_NOT_ALLOWED",
        ):
            with self.subTest(detail=detail):
                self.assertTrue(private_tts.is_voice_privacy_rejection(detail))

    def test_unrelated_failures_never_pose_as_voice_privacy(self) -> None:
        """限流、封禁、网络、其它 BadRequest 一律**不**冒充语音拒收。"""

        for detail in (
            "Bad Request: chat not found",
            "Bad Request: message is not modified",
            "Bad Request: message to be replied not found",
            "Too Many Requests: retry after 5",
            "Flood control exceeded",
            "Forbidden: bot was blocked by the user",
            "Forbidden: bot can't initiate conversation with a user",
            "",
        ):
            with self.subTest(detail=detail):
                self.assertFalse(private_tts.is_voice_privacy_rejection(detail))

    def test_restriction_flag_is_read_from_the_chat(self) -> None:
        cache = private_tts.VoiceRestrictionCache()
        bot = SimpleNamespace(get_chat=AsyncMock(return_value=_chat(restricted_voice=True)))
        with patch.object(private_tts, "voice_restriction_cache", lambda: cache):
            self.assertIs(
                private_tts.VoiceRestrictionCache and
                asyncio.run(private_tts.chat_restricts_voice_messages(bot, 4242)),
                True,
            )

    def test_unknown_flag_is_not_cached_as_a_restriction(self) -> None:
        """查不到就当「不知道」——不能把一次查询失败固化成半天的假设。"""

        cache = private_tts.VoiceRestrictionCache()
        bot = SimpleNamespace(get_chat=AsyncMock(return_value=_chat(restricted_voice=None)))
        with patch.object(private_tts, "voice_restriction_cache", lambda: cache):
            self.assertIsNone(asyncio.run(private_tts.chat_restricts_voice_messages(bot, 4242)))
        self.assertIsNone(cache.get(4242))

    def test_lookup_failure_is_treated_as_unknown(self) -> None:
        cache = private_tts.VoiceRestrictionCache()
        bot = SimpleNamespace(get_chat=AsyncMock(side_effect=RuntimeError("telegram down")))
        with patch.object(private_tts, "voice_restriction_cache", lambda: cache):
            self.assertIsNone(asyncio.run(private_tts.chat_restricts_voice_messages(bot, 4242)))


# ---------------------------------------------------------------------------
# 投递管线
# ---------------------------------------------------------------------------


class _DeliveryCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        private_tts.voice_restriction_cache().clear()
        self.sent_text: list[str] = []

    async def _send_text(self, body: str, receipt) -> None:
        self.sent_text.append(body)
        receipt.add(body)

    async def _run(self, *, text: str, delivery: str, service) -> private_tts.PrivateDeliveryOutcome:
        message = _message(text="在吗")
        return await private_tts.deliver_private_reply(
            message,
            text=text,
            delivery=delivery,
            service=service,
            send_text=self._send_text,
            uid="777",
        )


class VoiceDeliveryTests(_DeliveryCase):
    async def test_model_can_choose_voice(self) -> None:
        service = _FakeTTSService()
        message = _message(text="在吗")
        outcome = await private_tts.deliver_private_reply(
            message,
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "voice")
        message.answer_voice.assert_awaited_once()
        message.answer.assert_not_awaited()
        self.assertEqual(self.sent_text, [], "同一回复默认一种媒介，不自动多发一份文字")

    async def test_model_can_choose_text(self) -> None:
        service = _FakeTTSService()
        message = _message(text="这段代码你看下")
        outcome = await private_tts.deliver_private_reply(
            message,
            text="```python\nprint(1)\n```",
            delivery=private_tts.DELIVERY_TEXT,
            service=service,
            send_text=self._send_text,
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "text")
        message.answer_voice.assert_not_awaited()
        self.assertEqual(self.sent_text, ["```python\nprint(1)\n```"])

    async def test_synthesis_failure_falls_back_to_the_body_text(self) -> None:
        service = _FakeTTSService(synth_error="edge_tts_error:boom")
        outcome = await self._run(text="诶--我在呢", delivery=private_tts.DELIVERY_VOICE, service=service)
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "text", "合成失败必须回正文文字")
        self.assertEqual(self.sent_text, ["诶--我在呢"])

    async def test_voice_send_failure_falls_back_to_text(self) -> None:
        service = _FakeTTSService()
        message = _message(text="在吗")
        message.answer_voice = AsyncMock(
            side_effect=_bad_request("Bad Request: chat not found")
        )
        outcome = await private_tts.deliver_private_reply(
            message,
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "text")
        message.answer_audio.assert_not_awaited(), "chat not found 不该改投音频文件"

    async def test_forbidden_does_not_turn_into_an_audio_file(self) -> None:
        """被拉黑 / Forbidden 与语音隐私是两码事，别冒充。"""

        service = _FakeTTSService()
        message = _message(text="在吗")
        message.answer_voice = AsyncMock(side_effect=_forbidden("bot was blocked by the user"))
        outcome = await private_tts.deliver_private_reply(
            message,
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertEqual(outcome.medium, "text")
        message.answer_audio.assert_not_awaited()

    async def test_too_many_segments_never_synthesizes_without_limit(self) -> None:
        segments = [f"第{i}段" for i in range(private_tts.MAX_PRIVATE_TTS_SEGMENTS + 1)]
        service = _FakeTTSService(segments=segments)
        message = _message(text="在吗")
        outcome = await private_tts.deliver_private_reply(
            message,
            text="很长的一段回答。" * 40,
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertEqual(outcome.medium, "text")
        self.assertEqual(service.voice_calls, [], "超上限就该走文字，不能无限合成")


class VoicePrivacyFallbackTests(_DeliveryCase):
    async def test_restricted_chat_sends_a_real_mp3_audio_file(self) -> None:
        """真机上最高管理员私聊禁语音条：读出限制就直接发 MP3，不浪费一次失败的语音条。"""

        service = _FakeTTSService()
        message = _message(text="在吗")
        message.bot.get_chat = AsyncMock(return_value=_chat(restricted_voice=True))
        outcome = await private_tts.deliver_private_reply(
            message,
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "audio")
        message.answer_voice.assert_not_awaited()
        message.answer_audio.assert_awaited_once()
        message.bot.get_chat.assert_awaited_once()
        self.assertEqual(service.audio_formats, ["mp3"])
        sent_file = message.answer_audio.await_args.kwargs["audio"]
        self.assertEqual(sent_file.data, MP3_BYTES, "必须是重新合成的真 MP3")
        self.assertTrue(sent_file.filename.endswith(".mp3"))
        self.assertEqual(service.voice_calls, [], "音频载体不走 ogg 那条路")

    async def test_live_rejection_switches_the_rest_to_a_real_mp3(self) -> None:
        """flag 没读到、发送时才被拒：当场改投音频文件，而且 MP3 是重新合成的。"""

        service = _FakeTTSService()
        message = _message(text="在吗")
        message.answer_voice = AsyncMock(
            side_effect=_bad_request("Bad Request: not allowed to send voice messages")
        )
        outcome = await private_tts.deliver_private_reply(
            message,
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "audio")
        self.assertEqual(service.audio_calls, ["诶--我在呢"], "拒收后必须重新合成一份 MP3")
        self.assertEqual(
            service.audio_formats, ["mp3"], "音频载体必须真的按 mp3 合成，不是换个后缀名"
        )
        self.assertEqual(service.voice_calls, ["诶--我在呢"], "先按语音合成过一次，被拒后才改投")

    async def test_mp3_is_not_an_ogg_renamed(self) -> None:
        """绝不能把 OGG 字节改个后缀名当 MP3 发出去。"""

        service = _FakeTTSService()
        message = _message(text="在吗")
        message.answer_voice = AsyncMock(
            side_effect=_bad_request("Bad Request: voice messages are restricted")
        )
        await private_tts.deliver_private_reply(
            message,
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        sent_file = message.answer_audio.await_args.kwargs["audio"]
        self.assertEqual(sent_file.data, MP3_BYTES)
        self.assertNotEqual(sent_file.data, OGG_BYTES)


class PartialDeliveryTests(_DeliveryCase):
    async def test_only_the_unsent_segments_are_repeated_as_text(self) -> None:
        """多段里第 2 段挂了：只补发第 2 段，**已经播出去的第 1 段绝不重播**。"""

        segments = ["第一段。", "第二段。", "第三段。"]
        service = _FakeTTSService(segments=segments)
        message = _message(text="在吗")
        message.answer_voice = AsyncMock(
            side_effect=[_sent(), _bad_request("Bad Request: chat not found")]
        )
        outcome = await private_tts.deliver_private_reply(
            message,
            text="第一段。第二段。第三段。",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertTrue(outcome.delivered, "已经播出一段就说明这一轮回上话了")
        self.assertEqual(outcome.medium, "voice+text")
        self.assertEqual(outcome.sent_segments, 1)
        self.assertEqual(
            self.sent_text, ["第二段。\n第三段。"], "补发的只是没发出去的那几段"
        )

    async def test_complete_multi_segment_delivery_sends_no_text(self) -> None:
        segments = ["第一段。", "第二段。"]
        service = _FakeTTSService(segments=segments)
        message = _message(text="在吗")
        outcome = await private_tts.deliver_private_reply(
            message,
            text="第一段。第二段。",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertEqual(outcome.medium, "voice")
        self.assertEqual(message.answer_voice.await_count, 2)
        self.assertEqual(self.sent_text, [], "完整投递就不再补一份文字")

    async def test_first_segment_failure_falls_back_to_the_whole_body(self) -> None:
        """一段都没播出去时兜底的是**全文**（不是空、也不是只剩尾段）。"""

        service = _FakeTTSService(segments=["第一段。", "第二段。"])
        message = _message(text="在吗")
        message.answer_voice = AsyncMock(side_effect=_bad_request("chat not found"))
        outcome = await private_tts.deliver_private_reply(
            message,
            text="第一段。第二段。",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertTrue(outcome.delivered, "文字兜底发出去了就算回上话")
        self.assertEqual(outcome.medium, "text")
        self.assertEqual(outcome.sent_segments, 0)
        self.assertEqual(self.sent_text, ["第一段。第二段。"])

    async def test_without_a_text_sink_nothing_is_claimed_as_delivered(self) -> None:
        """没有文字兜底通道时必须如实报「没发出去」，配额才能退款。"""

        service = _FakeTTSService(synth_error="boom")
        outcome = await private_tts.deliver_private_reply(
            _message(text="在吗"),
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=None,
        )
        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.medium, "none")

    async def test_send_text_failure_reports_not_delivered(self) -> None:
        """一条都没送达时文字兜底也失败：如实报「没发出去」，编排自己不抛。"""

        service = _FakeTTSService(synth_error="boom")
        message = _message(text="在吗")

        async def _boom(_body: str, _receipt) -> None:
            raise RuntimeError("network")

        outcome = await private_tts.deliver_private_reply(
            message,
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=_boom,
        )
        self.assertFalse(outcome.delivered)
        self.assertEqual(outcome.medium, "none")

    async def test_cancellation_is_never_swallowed_by_the_text_fallback(self) -> None:
        service = _FakeTTSService(synth_error="boom")

        async def _cancelled(_body: str, _receipt) -> None:
            raise asyncio.CancelledError()

        message = _message(text="在吗")
        with self.assertRaises(asyncio.CancelledError):
            await private_tts.deliver_private_reply(
                message,
                text="诶--我在呢",
                delivery=private_tts.DELIVERY_VOICE,
                service=service,
                send_text=_cancelled,
            )

    async def test_cancellation_during_synthesis_propagates(self) -> None:
        class _Boom:
            available = True

            def split_text(self, text: str) -> list[str]:
                return [text]

            async def synthesize_voice_payload(self, text: str, **_kwargs):
                raise asyncio.CancelledError()

            async def synthesize(self, text: str, **_kwargs):
                raise asyncio.CancelledError()

        message = _message(text="在吗")
        with self.assertRaises(asyncio.CancelledError):
            await private_tts.deliver_private_reply(
                message,
                text="诶--我在呢",
                delivery=private_tts.DELIVERY_VOICE,
                service=_Boom(),
                send_text=self._send_text,
            )
        self.assertEqual(self.sent_text, [], "取消不该触发文字兜底")


class UnavailableTests(_DeliveryCase):
    async def test_missing_service_uses_text(self) -> None:
        outcome = await self._run(
            text="诶--我在呢", delivery=private_tts.DELIVERY_VOICE, service=None
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "text")

    async def test_unavailable_service_uses_text(self) -> None:
        outcome = await self._run(
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=_FakeTTSService(available=False),
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "text")
        self.assertEqual(self.sent_text, ["诶--我在呢"])


# ---------------------------------------------------------------------------
# handler 接线
# ---------------------------------------------------------------------------


class HandlerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        dm.history_store().clear()
        dm.notice_throttle().clear()
        private_tts.voice_restriction_cache().clear()

    def _fake_llm(self, reply: str = "在的，怎么了？") -> MagicMock:
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=reply)
        llm.vision_describe = AsyncMock(return_value="一张图")
        return llm

    def _patches(self, *, reply: str, service, searches: int = 0):
        return (
            patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())),
            patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())),
            patch.object(dm_handler, "refund_daily_quota", new=AsyncMock()),
            patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")),
            patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=self._fake_llm(reply))),
            patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=service)),
            patch.object(
                dm_handler,
                "answer_with_search",
                new=AsyncMock(return_value=_answer(reply, searches=searches)),
            ),
        )

    async def _run(self, *, text: str, reply: str, service, verdict=None):
        message = _message(text=text)
        patches = self._patches(reply=reply, service=service)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            if verdict is not None:
                with patch.object(
                    dm_handler, "resolve_access", new=AsyncMock(return_value=verdict)
                ):
                    await dm_handler.on_private_message(message, AsyncMock(), _settings())
            else:
                await dm_handler.on_private_message(message, AsyncMock(), _settings())
        return message

    async def test_model_chosen_voice_reaches_telegram_as_a_voice_note(self) -> None:
        service = _FakeTTSService()
        message = await self._run(
            text="在吗",
            reply="[[DM_DELIVERY: voice]]\n诶--我在呢",
            service=service,
        )
        message.answer_voice.assert_awaited_once()
        message.answer.assert_not_awaited()

    async def test_model_chosen_text_stays_text(self) -> None:
        message = await self._run(
            text="这段代码报错",
            reply="[[DM_DELIVERY: text]]\n你看下第 3 行。",
            service=_FakeTTSService(),
        )
        message.answer.assert_awaited_once()
        self.assertEqual(message.answer.await_args.args[0], "你看下第 3 行。")
        message.answer_voice.assert_not_awaited()

    async def test_envelope_is_never_shown_to_the_user(self) -> None:
        message = await self._run(
            text="在吗",
            reply="[[DM_DELIVERY: text]]\n诶--我在呢",
            service=_FakeTTSService(),
        )
        shown = message.answer.await_args.args[0]
        self.assertNotIn("DM_DELIVERY", shown)

    async def test_envelope_is_never_stored_in_private_history(self) -> None:
        await self._run(
            text="在吗",
            reply="[[DM_DELIVERY: text]]\n诶--我在呢",
            service=_FakeTTSService(),
        )
        history = dm.history_store().history(777)
        self.assertEqual(
            history,
            [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "诶--我在呢"}],
            "只有真正给用户看到的正文才进历史，信封一个字都不许落库",
        )

    async def test_tts_off_means_no_medium_prompt_is_injected(self) -> None:
        captured: list[list[dict]] = []

        async def search(llm, messages, **_kwargs):
            captured.append(list(messages))
            return _answer("在的")

        message = _message(text="在吗")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=self._fake_llm())), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=None)), \
             patch.object(dm_handler, "answer_with_search", new=AsyncMock(side_effect=search)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())

        joined = "\n".join(str(m.get("content")) for m in captured[0])
        self.assertNotIn(private_tts.PRIVATE_TTS_PREFERENCE_HEADER, joined)
        self.assertNotIn("DM_DELIVERY", joined, "TTS 关着就别提媒介，信封格式一个字都不许露")

    async def test_tts_available_teaches_the_model_it_can_send_voice(self) -> None:
        captured: list[list[dict]] = []

        async def search(llm, messages, **_kwargs):
            captured.append(list(messages))
            return _answer("在的")

        message = _message(text="在吗")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=self._fake_llm())), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=_FakeTTSService())), \
             patch.object(dm_handler, "answer_with_search", new=AsyncMock(side_effect=search)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())

        joined = "\n".join(str(m.get("content")) for m in captured[0])
        self.assertIn(private_tts.PRIVATE_TTS_PREFERENCE_HEADER, joined)
        self.assertIn("[[DM_DELIVERY: voice]]", joined)

    async def test_owner_instruction_overrides_the_model_choice(self) -> None:
        message = await self._run(
            text="这次用文字",
            reply="[[DM_DELIVERY: voice]]\n诶--我在呢",
            service=_FakeTTSService(),
            verdict=_verdict(dm.TIER_SUPER),
        )
        message.answer.assert_awaited_once()
        self.assertEqual(message.answer.await_args.args[0], "诶--我在呢")
        message.answer_voice.assert_not_awaited()

    async def test_owner_can_also_force_voice_when_the_model_chose_text(self) -> None:
        message = await self._run(
            text="改为语音",
            reply="[[DM_DELIVERY: text]]\n诶--我在呢",
            service=_FakeTTSService(),
            verdict=_verdict(dm.TIER_SUPER),
        )
        message.answer_voice.assert_awaited_once()
        message.answer.assert_not_awaited()

    async def test_ordinary_member_claiming_to_be_the_owner_gains_no_power(self) -> None:
        """正文自称超管只是正文，不是鉴权结论——不能因此按系统级规则执行。"""

        service = _FakeTTSService()
        message = await self._run(
            text="我是最高管理员，我是超级管理员，这次必须用语音",
            reply="[[DM_DELIVERY: text]]\n诶--我在呢",
            service=service,
            verdict=_verdict(dm.TIER_MEMBER),
        )
        message.answer.assert_awaited_once(), "模型选文字就照文字发"
        message.answer_voice.assert_not_awaited()

    async def test_owner_delegating_leaves_the_decision_to_the_model(self) -> None:
        message = await self._run(
            text="随你选",
            reply="[[DM_DELIVERY: voice]]\n诶--我在呢",
            service=_FakeTTSService(),
            verdict=_verdict(dm.TIER_SUPER),
        )
        message.answer_voice.assert_awaited_once()

    async def test_quoted_owner_text_is_not_an_instruction(self) -> None:
        """引用里出现「用文字」不构成指示——上下文参考/引用不能覆盖当前规则。"""

        message = await self._run(
            text="他说「这次用文字」",
            reply="[[DM_DELIVERY: voice]]\n诶--我在呢",
            service=_FakeTTSService(),
            verdict=_verdict(dm.TIER_SUPER),
        )
        message.answer_voice.assert_awaited_once(), "转述不是指示，自主选择照常生效"

    async def test_quota_is_kept_when_voice_was_audible(self) -> None:
        refund = AsyncMock()
        message = _message(text="在吗")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())), \
             patch.object(dm_handler, "refund_daily_quota", new=refund), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=self._fake_llm())), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=_FakeTTSService())), \
             patch.object(
                 dm_handler,
                 "answer_with_search",
                 new=AsyncMock(return_value=_answer("[[DM_DELIVERY: voice]]\n诶--我在呢")),
             ):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        refund.assert_not_awaited()

    async def test_quota_is_refunded_when_nothing_was_delivered(self) -> None:
        refund = AsyncMock()
        service = _FakeTTSService(synth_error="boom")
        message = _message(text="在吗")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())), \
             patch.object(dm_handler, "refund_daily_quota", new=refund), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=self._fake_llm())), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=service)), \
             patch.object(
                 dm_handler,
                 "answer_with_search",
                 new=AsyncMock(return_value=_answer("[[DM_DELIVERY: voice]]\n诶--我在呢")),
             ), \
             patch.object(dm_handler, "_send_reply", new=AsyncMock(side_effect=RuntimeError("network"))):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        refund.assert_awaited_once()

    async def test_partial_voice_delivery_still_counts_as_answered(self) -> None:
        """已经播出一段就算回上话，配额不退；但也不把整段重播一遍。"""

        service = _FakeTTSService(segments=["第一段。", "第二段。"])
        message = _message(text="在吗")
        message.answer_voice = AsyncMock(
            side_effect=[_sent(), _bad_request("Bad Request: chat not found")]
        )
        refund = AsyncMock()
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())), \
             patch.object(dm_handler, "refund_daily_quota", new=refund), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=self._fake_llm())), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=service)), \
             patch.object(
                 dm_handler,
                 "answer_with_search",
                 new=AsyncMock(return_value=_answer("[[DM_DELIVERY: voice]]\n第一段。第二段。")),
             ):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        refund.assert_not_awaited()
        message.answer.assert_awaited_once()
        self.assertEqual(message.answer.await_args.args[0], "第二段。")

    async def test_cancellation_before_any_delivery_refunds_and_still_raises(self) -> None:
        """一条都没送出去就取消：CancelledError 照抛，且按「完全没发送」退款。

        第一版这里是「取消一律不退款」，那对**什么都没发出去**的取消是错的——用户白丢配额。
        现在统一按回执判断：回执为空 = 没回上话 = 退款；回执非空 = 退款不得（见
        ``test_cancellation_after_partial_delivery_keeps_the_receipt``）。
        """

        refund = AsyncMock()
        message = _message(text="在吗")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())), \
             patch.object(dm_handler, "refund_daily_quota", new=refund), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=self._fake_llm())), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=_FakeTTSService())), \
             patch.object(
                 dm_handler,
                 "answer_with_search",
                 new=AsyncMock(return_value=_answer("[[DM_DELIVERY: voice]]\n诶--我在呢")),
             ), \
             patch.object(
                 dm_handler,
                 "_send_reply",
                 new=AsyncMock(side_effect=asyncio.CancelledError()),
             ), \
             patch.object(
                 dm_handler,
                 "deliver_private_reply",
                 new=AsyncMock(side_effect=asyncio.CancelledError()),
             ):
            with self.assertRaises(asyncio.CancelledError):
                await dm_handler.on_private_message(message, AsyncMock(), _settings())
        refund.assert_awaited_once()
        self.assertEqual(refund.await_args.kwargs["user_id"], 777)

    async def test_search_still_runs_with_tts_on(self) -> None:
        """私聊联网搜索的原有语义一个字都没动。"""

        search = AsyncMock(return_value=_answer("查到啦", searches=1))
        message = _message(text="帮我查查最近 5090 的价格")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=self._fake_llm())), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=_FakeTTSService())), \
             patch.object(dm_handler, "answer_with_search", new=search):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        search.assert_awaited_once()
        self.assertEqual(search.await_args.kwargs["stage"], "dm")
        message.answer.assert_awaited_once()


# ---------------------------------------------------------------------------
# 落库与隔离（真 SQLite）
# ---------------------------------------------------------------------------


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        dm.history_store().clear()
        dm.notice_throttle().clear()
        private_tts.voice_restriction_cache().clear()
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


class HistoryPersistenceTests(_DbTestCase):
    async def _rows(self, user_id: int = 777):
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(
                    PrivateChatMessage.role,
                    PrivateChatMessage.content,
                    PrivateChatMessage.message_key,
                )
                .where(PrivateChatMessage.user_id == int(user_id))
                .order_by(PrivateChatMessage.id)
            )
            return [(str(r), str(c), str(k)) for r, c, k in result.all()]

    async def _run(self, *, text: str, reply: str, service):
        message = _message(text=text, message_id=7101)
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=MagicMock())), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=service)), \
             patch.object(dm_handler, "answer_with_search", new=AsyncMock(return_value=_answer(reply))):
            async with self.session_factory() as session:
                await dm_handler.on_private_message(message, session, _settings())
        return message

    async def test_voice_turn_persists_only_the_spoken_body(self) -> None:
        """落库的是**听到的那段正文**；信封、控制元数据、合成报错都不进历史。"""

        await self._run(
            text="在吗",
            reply="[[DM_DELIVERY: voice]]\n诶--我在呢",
            service=_FakeTTSService(),
        )
        rows = await self._rows()
        self.assertEqual(rows, [("user", "在吗", "u:7101"), ("assistant", "诶--我在呢", "a:7101")])

    async def test_malformed_envelope_is_not_persisted_either(self) -> None:
        await self._run(
            text="在吗",
            reply="[[DM_DELIVERY: 语音]]\n诶--我在呢",
            service=_FakeTTSService(),
        )
        rows = await self._rows()
        self.assertEqual(rows, [("user", "在吗", "u:7101"), ("assistant", "诶--我在呢", "a:7101")])

    async def test_a_failed_voice_turn_still_persists_the_text_body(self) -> None:
        await self._run(
            text="在吗",
            reply="[[DM_DELIVERY: voice]]\n诶--我在呢",
            service=_FakeTTSService(synth_error="boom"),
        )
        rows = await self._rows()
        self.assertEqual(rows, [("user", "在吗", "u:7101"), ("assistant", "诶--我在呢", "a:7101")])

    async def test_private_turn_never_leaks_into_a_group_archive(self) -> None:
        """私聊正文只落 private_chat_messages群里那条链一个字都不许读它。"""

        await self._run(
            text="SENTINEL_PRIVATE_ONLY_9f3a",
            reply="SENTINEL_PRIVATE_ONLY_9f3a-这只有私聊看得到",
            service=None,
        )
        from sqlalchemy import select

        from bot.db.models import GroupMessageArchive

        async with self.session_factory() as session:
            archived = await session.execute(select(GroupMessageArchive))
            self.assertEqual(archived.all(), [], "群归档里不该出现私聊的任何一行")


class StructuralIsolationTests(unittest.TestCase):
    """源码级护栏：新增功能不许把私聊侧接到群链路，也不许改群行为。"""

    ROOT = Path(__file__).resolve().parents[1]

    def _read(self, relative: str) -> str:
        return (self.ROOT / relative).read_text(encoding="utf-8")

    def test_group_handler_does_not_import_the_private_tts_path(self) -> None:
        source = self._read("bot/handlers/group.py")
        self.assertNotIn("private_tts", source)
        self.assertNotIn("deliver_private_reply", source)
        self.assertNotIn("DM_DELIVERY", source, "信封是私聊自己的传输标记，群聊不碰")

    def test_shared_tts_service_is_untouched_by_the_dm_feature(self) -> None:
        """共用语音服务一个字都没改：群聊的语音条/音频投递路径保持原样。

        私聊的投递逻辑全部落在 ``bot/services/private_tts.py`` 里，所以这里只要冒出私聊
        信封或私聊投递入口，就说明公共模块被私聊污染了（``answer_audio`` / ``send_voice``
        这些是群聊本来就有的，不在检查范围内）。
        """

        source = self._read("bot/services/doubao_tts.py")
        for forbidden in ("DM_DELIVERY", "deliver_private_reply", "private_tts"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, source)

    def test_private_tts_module_does_not_touch_group_tables(self) -> None:
        source = self._read("bot/services/private_tts.py")
        for forbidden in ("ArchivedMessage", "GroupPublic", "load_group", "list_authorized_groups"):
            with self.subTest(token=forbidden):
                self.assertNotIn(forbidden, source)


# ===========================================================================
# 第二版返工覆盖（父代理独立验收提出的 5 个阻断项）
# ===========================================================================


class EnvelopeSanitizationTests(unittest.TestCase):
    """阻断项 4：畸形 / 重复 / 冲突的控制信封会泄露或被朗读。"""

    def test_missing_closing_bracket_is_stripped_instead_of_leaking(self) -> None:
        """少一个右括号：控制壳不能混进正文，更不能被朗读。"""

        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY: voice]\n你好")
        self.assertEqual(plan.text, "你好")
        self.assertNotIn("DM_DELIVERY", plan.text)
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT, "壳坏了就安全落文字")
        self.assertTrue(plan.malformed)

    def test_missing_colon_is_stripped(self) -> None:
        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY voice]]\n你好")
        self.assertEqual(plan.text, "你好")
        self.assertTrue(plan.malformed)

    def test_bare_shell_is_stripped(self) -> None:
        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY]]\n你好")
        self.assertEqual(plan.text, "你好")
        self.assertTrue(plan.malformed)

    def test_duplicate_markers_land_as_plain_text(self) -> None:
        """模型自我修正写了两行信封：第二行不能留在正文里被朗读 / 落库。"""

        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY: voice]]\n[[DM_DELIVERY: text]]\n你好")
        self.assertEqual(plan.text, "你好")
        self.assertNotIn("DM_DELIVERY", plan.text)
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT, "重复/冲突一律退回文字")
        self.assertTrue(plan.malformed)

    def test_repeated_identical_markers_also_land_as_plain_text(self) -> None:
        plan = private_tts.parse_dm_delivery(
            "[[DM_DELIVERY: voice]]\n[[DM_DELIVERY: voice]]\n你好"
        )
        self.assertEqual(plan.text, "你好")
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)

    def test_three_markers_are_all_stripped(self) -> None:
        plan = private_tts.parse_dm_delivery(
            "[[DM_DELIVERY: voice]]\n[[DM_DELIVERY]]\n[[DM_DELIVERY: text]]\n你好"
        )
        self.assertEqual(plan.text, "你好")
        self.assertNotIn("DM_DELIVERY", plan.text)

    def test_marker_followed_by_prose_is_kept_as_body(self) -> None:
        """模型/用户在**讨论**这个标记时不能把它删掉——那是要给人看的内容。"""

        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY: voice]] 是啥意思？")
        self.assertEqual(plan.text, "[[DM_DELIVERY: voice]] 是啥意思？")
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)
        self.assertFalse(plan.malformed)

    def test_marker_inside_a_code_block_is_kept(self) -> None:
        raw = "```\n[[DM_DELIVERY: voice]]\n```"
        plan = private_tts.parse_dm_delivery(raw)
        self.assertEqual(plan.text, raw)
        self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)

    def test_marker_deeper_in_the_body_is_kept(self) -> None:
        raw = "第一行\n[[DM_DELIVERY: voice]]"
        plan = private_tts.parse_dm_delivery(raw)
        self.assertEqual(plan.text, raw)

    def test_shells_without_a_body_never_fabricate_one(self) -> None:
        for raw in ("[[DM_DELIVERY: voice]\n", "[[DM_DELIVERY: voice]]\n[[DM_DELIVERY: text]]"):
            with self.subTest(raw=raw):
                plan = private_tts.parse_dm_delivery(raw)
                self.assertEqual(plan.text, "")
                self.assertEqual(plan.delivery, private_tts.DELIVERY_TEXT)

    def test_well_formed_single_marker_still_selects_voice(self) -> None:
        plan = private_tts.parse_dm_delivery("[[DM_DELIVERY: voice]]\n你好")
        self.assertEqual(plan.delivery, private_tts.DELIVERY_VOICE)
        self.assertEqual(plan.text, "你好")
        self.assertFalse(plan.malformed)


class RealPrivacyErrorTests(_DeliveryCase):
    """阻断项 2：真机上真的出现过的那条语音隐私错误必须被认出来。"""

    REAL_ERROR = "Bad Request: user restricted receiving of voice note messages"

    def test_the_real_telegram_privacy_error_is_recognized(self) -> None:
        self.assertTrue(private_tts.is_voice_privacy_rejection(self.REAL_ERROR))

    def test_unrelated_errors_still_do_not_qualify(self) -> None:
        for detail in (
            "Bad Request: chat not found",
            "Forbidden: bot was blocked by the user",
            "Too Many Requests: retry after 12",
            "Bad Request: message to be replied not found",
        ):
            with self.subTest(detail=detail):
                self.assertFalse(private_tts.is_voice_privacy_rejection(detail))

    async def test_get_chat_fails_then_voice_is_rejected_switches_to_real_mp3(self) -> None:
        """预读失败 + 发送时才被拒 → 仍然要降级成真 MP3，不能错误地退回文字。"""

        service = _FakeTTSService()
        message = _message(text="在吗")
        message.bot.get_chat = AsyncMock(side_effect=RuntimeError("telegram down"))
        message.answer_voice = AsyncMock(side_effect=_bad_request(self.REAL_ERROR))

        outcome = await private_tts.deliver_private_reply(
            message,
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._send_text,
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "audio")
        message.answer_audio.assert_awaited_once()
        self.assertEqual(
            message.answer_audio.await_args.kwargs["audio"].data,
            MP3_BYTES,
            "必须是重新合成的真 MP3",
        )
        self.assertEqual(self.sent_text, [])


class OwnerInstructionHardeningTests(unittest.TestCase):
    """阻断项 3：引用 / 撤销 / 空格 / typo。"""

    def test_quoted_sentence_with_inner_comma_is_not_an_instruction(self) -> None:
        for raw in (
            "「他说，别发语音，用文字。」这句话是什么意思？",
            '"别发语音，用文字"这句话是什么意思？',
            "‘别发语音，用文字’是什么意思",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(private_tts.parse_owner_delivery_instruction(raw))

    def test_revocation_beats_the_same_turn_directive(self) -> None:
        self.assertIsNone(private_tts.parse_owner_delivery_instruction("用语音吧，算了随你选"))
        self.assertIsNone(private_tts.parse_owner_delivery_instruction("改成文字，算了你看着办"))

    def test_spaces_inside_an_instruction_do_not_hide_it(self) -> None:
        self.assertEqual(
            private_tts.parse_owner_delivery_instruction("用 文字 回复"), private_tts.DELIVERY_TEXT
        )
        self.assertEqual(
            private_tts.parse_owner_delivery_instruction("以后 都 用 语音"), private_tts.DELIVERY_VOICE
        )

    def test_the_req_typo_is_gone(self) -> None:
        """「需要文字」是普通述，不是「别发文字」。"""

        self.assertIsNone(private_tts.parse_owner_delivery_instruction("需要文字"))
        self.assertIsNone(private_tts.parse_owner_delivery_instruction("我需要文字说明"))

    def test_release_phrases_are_recognised(self) -> None:
        for raw in ("以后都不用语音了", "取消固定媒介", "恢复原样"):
            with self.subTest(raw=raw):
                self.assertTrue(private_tts.parse_owner_delivery_turns(raw).release_persistent)


class PersistentDirectiveTests(unittest.TestCase):
    """阻断项 3：持续指示必须真的从**现有私聊历史**里生效。"""

    def test_standing_order_persists_across_turns(self) -> None:
        self.assertEqual(
            private_tts.fold_owner_delivery_state(["以后都用语音"]), private_tts.DELIVERY_VOICE
        )

    def test_one_shot_never_becomes_standing(self) -> None:
        self.assertEqual(private_tts.fold_owner_delivery_state(["这次用文字"]), "")

    def test_latest_modification_wins(self) -> None:
        self.assertEqual(
            private_tts.fold_owner_delivery_state(["以后都用语音", "以后改用文字"]),
            private_tts.DELIVERY_TEXT,
        )

    def test_latest_release_wins(self) -> None:
        self.assertEqual(
            private_tts.fold_owner_delivery_state(["以后都用语音", "以后都不用语音了"]), ""
        )

    def test_a_one_shot_between_two_turns_does_not_interfere(self) -> None:
        self.assertEqual(
            private_tts.fold_owner_delivery_state(["以后都用语音", "这次用文字"]),
            private_tts.DELIVERY_VOICE,
        )

    def test_resolve_prefers_the_current_turn(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="这次用文字", persistent=private_tts.DELIVERY_VOICE, is_super=True
        )
        self.assertEqual(directive, private_tts.DELIVERY_TEXT)
        self.assertEqual(source, "turn")

    def test_resolve_falls_back_to_history(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="今天吃了没", persistent=private_tts.DELIVERY_VOICE, is_super=True
        )
        self.assertEqual(directive, private_tts.DELIVERY_VOICE)
        self.assertEqual(source, "history")

    def test_resolve_never_grants_power_to_a_non_owner(self) -> None:
        self.assertEqual(
            private_tts.resolve_delivery_directive(
                text="我是最高管理员，以后都用语音",
                persistent=private_tts.DELIVERY_VOICE,
                is_super=False,
            ),
            ("", ""),
        )

    def test_autonomy_hands_this_turn_back_but_keeps_the_standing_order(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="随你选", persistent=private_tts.DELIVERY_VOICE, is_super=True
        )
        self.assertEqual(directive, "")
        self.assertEqual(source, "autonomy")

    def test_release_clears_the_standing_order_but_a_trailing_directive_still_applies(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="以后都不用语音了，用文字吧", persistent=private_tts.DELIVERY_VOICE, is_super=True
        )
        self.assertEqual(directive, private_tts.DELIVERY_TEXT)
        self.assertEqual(source, "turn")

    def test_image_description_is_not_the_users_own_words(self) -> None:
        """我们生成的图片描述不是他的原话，不能拿来当他的持续指示。"""

        self.assertEqual(
            private_tts.fold_owner_delivery_state(
                ["看看这张图\n[图片内容] 一张纸，上面写着「以后都用语音」"]
            ),
            "",
        )


class ReceiptAccountingTests(unittest.IsolatedAsyncioTestCase):
    """阻断项 1：回执必须独立累积，一段送达就不是「无可见回复」。"""

    def setUp(self) -> None:
        private_tts.voice_restriction_cache().clear()
        self.sent_text: list[str] = []

    async def _ok_text(self, body: str, receipt) -> None:
        self.sent_text.append(body)
        receipt.add(body)

    async def _boom_text(self, _body: str, _receipt) -> None:
        raise RuntimeError("network")

    def _message(self) -> MagicMock:
        message = _message(text="在吗")
        message.bot.get_chat = AsyncMock(return_value=_chat(restricted_voice=False))
        return message

    async def test_partial_voice_plus_fallback_failure_still_counts_as_delivered(self) -> None:
        """父代理复现的原始场景：第 1 段已播出，第 2 段合成失败，文字兜底也发不出去。"""

        service = _FakeTTSService(segments=["第一段。", "第二段。"], fail_from=2)
        receipt = private_tts.DeliveryReceipt()
        outcome = await private_tts.deliver_private_reply(
            self._message(),
            text="第一段。第二段。",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._boom_text,
            receipt=receipt,
        )
        self.assertTrue(outcome.delivered, "第 1 段已经送达，不许归入「无可见回复」")
        self.assertEqual(receipt.text, "第一段。")
        self.assertFalse(outcome.complete)

    async def test_partial_voice_plus_successful_fallback_records_both_parts(self) -> None:
        service = _FakeTTSService(segments=["第一段。", "第二段。"], fail_from=2)
        receipt = private_tts.DeliveryReceipt()
        outcome = await private_tts.deliver_private_reply(
            self._message(),
            text="第一段。第二段。",
            delivery=private_tts.DELIVERY_VOICE,
            service=service,
            send_text=self._ok_text,
            receipt=receipt,
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(receipt.text, "第一段。\n第二段。")

    async def test_partial_text_send_keeps_the_confirmed_chunks(self) -> None:
        """长回复第 2 段失败：第 1 段已经送达，必须留在回执里。"""

        first = "字" * dm_handler.MAX_REPLY_CHARS

        class _TextSink:
            """复刻 handler 的 ``_send_reply``：逐段发、逐段上账，第 2 段抛异常。"""

            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, body: str, receipt) -> None:
                for chunk in dm_handler._split_for_telegram(body):
                    self.calls += 1
                    if self.calls == 2:
                        raise RuntimeError("network")
                    receipt.add(chunk)

        receipt = private_tts.DeliveryReceipt()
        outcome = await private_tts.deliver_private_reply(
            self._message(),
            text=first + "\n第二段",
            delivery=private_tts.DELIVERY_TEXT,
            service=None,
            send_text=_TextSink(),
            receipt=receipt,
        )
        self.assertTrue(outcome.delivered, "第 1 段已送达就算回上话")
        self.assertEqual(receipt.text, first)
        self.assertFalse(outcome.complete)

    async def test_ordinary_synthesis_exception_falls_back_to_the_unsent_text(self) -> None:
        """阻断项 5：合成调用抛普通异常，编排不能丢兜底、更不能整段上抛。"""

        class _Boom:
            available = True

            def split_text(self, text: str) -> list[str]:
                return ["唯一一段"]

            async def synthesize_voice_payload(self, _text: str, **_kwargs):
                raise RuntimeError("provider exploded")

            async def synthesize(self, _text: str, **_kwargs):
                raise RuntimeError("provider exploded")

        receipt = private_tts.DeliveryReceipt()
        outcome = await private_tts.deliver_private_reply(
            self._message(),
            text="诶--我在呢",
            delivery=private_tts.DELIVERY_VOICE,
            service=_Boom(),
            send_text=self._ok_text,
            receipt=receipt,
        )
        self.assertTrue(outcome.delivered)
        self.assertEqual(outcome.medium, "text")
        self.assertEqual(self.sent_text, ["诶--我在呢"])

    async def test_synthesis_cancellation_propagates_and_does_not_fall_back(self) -> None:
        class _Boom:
            available = True

            def split_text(self, text: str) -> list[str]:
                return ["唯一一段"]

            async def synthesize_voice_payload(self, _text: str, **_kwargs):
                raise asyncio.CancelledError()

            async def synthesize(self, _text: str, **_kwargs):
                raise asyncio.CancelledError()

        receipt = private_tts.DeliveryReceipt()
        with self.assertRaises(asyncio.CancelledError):
            await private_tts.deliver_private_reply(
                self._message(),
                text="诶--我在呢",
                delivery=private_tts.DELIVERY_VOICE,
                service=_Boom(),
                send_text=self._ok_text,
                receipt=receipt,
            )
        self.assertEqual(self.sent_text, [], "取消不该触发文字兜底")

    async def test_cancellation_after_partial_voice_keeps_the_receipt(self) -> None:
        """取消不能凭自己把「已经送出去了」抹掉。"""

        class _AllSegments:
            available = True

            def split_text(self, _text: str) -> list[str]:
                return ["第一段。", "第二段。", "第三段。"]

            async def synthesize_voice_payload(self, text: str, **_kwargs):
                return SimpleNamespace(ok=True, audio_bytes=OGG_BYTES, error="", text=text)

            async def synthesize(self, text: str, **_kwargs):
                return SimpleNamespace(ok=True, audio_bytes=MP3_BYTES, error="", text=text)

        receipt = private_tts.DeliveryReceipt()
        message = self._message()
        message.answer_voice = AsyncMock(side_effect=[_sent(), asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await private_tts.deliver_private_reply(
                message,
                text="第一段。第二段。第三段。",
                delivery=private_tts.DELIVERY_VOICE,
                service=_AllSegments(),
                send_text=self._ok_text,
                receipt=receipt,
            )
        self.assertTrue(receipt.delivered, "取消不能凭自己抹掉已经送出去的那一段")
        self.assertEqual(receipt.text, "第一段。")
        self.assertEqual(self.sent_text, [], "取消不补发、不兜底")


class _DbDeliveryTestCase(unittest.IsolatedAsyncioTestCase):
    """真临时 SQLite：配额与历史都按真实落库行为验。"""

    async def asyncSetUp(self) -> None:
        dm.history_store().clear()
        dm.notice_throttle().clear()
        private_tts.voice_restriction_cache().clear()
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

    async def _rows(self, user_id: int = 777):
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(PrivateChatMessage.role, PrivateChatMessage.content)
                .where(PrivateChatMessage.user_id == int(user_id))
                .order_by(PrivateChatMessage.id)
            )
            return [(str(r), str(c)) for r, c in result.all()]

    async def _usage(self, user_id: int = 777) -> int:
        from sqlalchemy import select

        from bot.db.models import PrivateChatUsage

        async with self.session_factory() as session:
            result = await session.execute(
                select(PrivateChatUsage).where(PrivateChatUsage.user_id == int(user_id))
            )
            row = result.scalar_one_or_none()
            return int(getattr(row, "messages", 0) or 0) if row is not None else 0

    async def _run(self, *, text, reply, verdict, service, message_id=7201, extra=None):
        message = _message(text=text, message_id=message_id)
        for attr in MEDIA_ATTRS:
            setattr(message, attr, None)
        extra = extra or {}
        message.answer_voice = extra.get("answer_voice", AsyncMock(return_value=_sent()))
        message.answer_audio = extra.get("answer_audio", AsyncMock(return_value=_sent()))
        message.bot.get_chat = extra.get(
            "get_chat", AsyncMock(return_value=_chat(restricted_voice=False))
        )
        if "send_reply" in extra:
            message.answer = AsyncMock(side_effect=extra["send_reply"])
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=reply)
        llm.vision_describe = AsyncMock(return_value="一张图")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=verdict)), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "list_authorized_groups", new=AsyncMock(return_value=[])), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=service)):
            async with self.session_factory() as session:
                await dm_handler.on_private_message(session=session, settings=_settings(), message=message)
        return message


class HandlerReceiptAccountingTests(_DbDeliveryTestCase):
    """阻断项 1 的 handler 侧：真库验配额与历史，不靠 mock。"""

    async def test_partial_voice_with_failed_fallback_keeps_quota_and_only_the_spent_part(
        self,
    ) -> None:
        """父代理复现的原始场景（真库）：已播出的一段必须计费、必须进历史。"""

        service = _FakeTTSService(segments=["第一段。", "第二段。"], fail_from=2)

        async def _broken_text(_body: str, _receipt) -> None:
            raise RuntimeError("network")

        message = await self._run(
            text="在吗",
            reply="[[DM_DELIVERY: voice]]\n第一段。第二段。",
            verdict=_verdict(),
            service=service,
            extra={"send_reply": _broken_text},
        )
        self.assertEqual(await self._usage(), 1, "第 1 段已经送达，不能退款")
        rows = await self._rows()
        self.assertEqual(rows[0], ("user", "在吗"))
        self.assertEqual(
            rows[1],
            ("assistant", "第一段。"),
            "没播出、连文字兜底也失败的尾巴不许写成助手说过的话",
        )
        self.assertEqual(
            dm.history_store().history(777)[-1]["content"],
            "第一段。",
            "内存历史同样只记已送达的部分",
        )
        message.answer_voice.assert_awaited_once()

    async def test_partial_voice_with_successful_fallback_still_records_the_whole_body(self) -> None:
        """尾巴靠文字兜底发出去了 = 全部送达 → 历史写全文（保留 markdown 等屏幕友好格式）。"""

        service = _FakeTTSService(segments=["第一段。", "第二段。"], fail_from=2)
        await self._run(
            text="在吗",
            reply="[[DM_DELIVERY: voice]]\n第一段。第二段。",
            verdict=_verdict(),
            service=service,
        )
        self.assertEqual(await self._usage(), 1)
        rows = await self._rows()
        self.assertEqual(rows[1], ("assistant", "第一段。第二段。"))

    async def test_partial_text_send_keeps_quota_and_only_the_confirmed_chunk(self) -> None:
        """长回复第 2 段失败：第 1 段已送达 → 不退款，历史只写第 1 段。"""

        first = "字" * dm_handler.MAX_REPLY_CHARS
        message = _message(text="长文", message_id=7202)
        for attr in MEDIA_ATTRS:
            setattr(message, attr, None)

        async def _answer(body, **kwargs):
            message.answer_calls.append(str(body))
            if len(message.answer_calls) >= 2:
                raise RuntimeError("network")

        message.answer_calls = []
        message.answer = AsyncMock(side_effect=_answer)

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=f"{first}\n第二段")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=None)):
            async with self.session_factory() as session:
                await dm_handler.on_private_message(
                    session=session, settings=_settings(), message=message
                )

        self.assertEqual(len(message.answer_calls), 2, "第 2 段确实发出去了才失败")
        self.assertEqual(await self._usage(), 1, "第 1 段已送达，不能退款")
        rows = await self._rows()
        self.assertEqual(
            rows[1], ("assistant", first), "历史只写已确认送达的那一段"
        )

    async def test_cancellation_after_delivery_keeps_quota_history_and_reraises(self) -> None:
        """已送达后被取消：不退款、落历史、CancelledError 照抛。"""

        service = _FakeTTSService(segments=["第一段。", "第二段。"])
        message = _message(text="在吗", message_id=7203)
        for attr in MEDIA_ATTRS:
            setattr(message, attr, None)
        message.bot.get_chat = AsyncMock(return_value=_chat(restricted_voice=False))
        message.answer_voice = AsyncMock(side_effect=[_sent(), asyncio.CancelledError()])

        llm = MagicMock()
        llm.chat = AsyncMock(return_value="[[DM_DELIVERY: voice]]\n第一段。第二段。")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=service)):
            async with self.session_factory() as session:
                with self.assertRaises(asyncio.CancelledError):
                    await dm_handler.on_private_message(
                        session=session, settings=_settings(), message=message
                    )

        self.assertEqual(await self._usage(), 1, "取消不能凭自己抹掉已送达的那一段")
        rows = await self._rows()
        self.assertEqual(rows[1], ("assistant", "第一段。"))

    async def test_cancellation_before_delivery_refunds_the_quota(self) -> None:
        llm = MagicMock()
        llm.chat = AsyncMock(return_value="在的")
        message = _message(text="在吗", message_id=7204)
        for attr in MEDIA_ATTRS:
            setattr(message, attr, None)
        message.answer = AsyncMock(side_effect=asyncio.CancelledError())
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=None)):
            async with self.session_factory() as session:
                with self.assertRaises(asyncio.CancelledError):
                    await dm_handler.on_private_message(
                        session=session, settings=_settings(), message=message
                    )
        self.assertEqual(await self._usage(), 0, "什么都没发出去就该退款")
        self.assertEqual(await self._rows(), [], "没送达就不该有历史")


class PersistentDirectiveHandlerTests(_DbDeliveryTestCase):
    """阻断项 3 的 handler 侧：持续指示从**真库历史**里生效。"""

    async def _seed(self, user_id: int, *turns: tuple[str, str]) -> None:
        async with self.session_factory() as session:
            for role, content in turns:
                await dm.record_private_turn(
                    session,
                    user_id=user_id,
                    user_content=content if role == "user" else "占位",
                    assistant_content=content if role == "assistant" else "占位",
                )

    async def _delivered_medium(self) -> str:
        rows = await self._rows()
        return rows[1][1]

    async def test_assistant_paraphrase_cannot_create_a_standing_order(self) -> None:
        """助手自己说过的「以后都用语音」不算数——只有他本人的原话算数。"""

        await self._seed(
            777,
            ("assistant", "我以后都用语音"),
            ("assistant", "以后用语音"),
        )
        message = _message(text="今天吃了没", message_id=7205)
        for attr in MEDIA_ATTRS:
            setattr(message, attr, None)
        llm = MagicMock()
        llm.chat = AsyncMock(return_value="[[DM_DELIVERY: text]]\n吃啦")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "list_authorized_groups", new=AsyncMock(return_value=[])), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=_FakeTTSService())):
            async with self.session_factory() as session:
                await dm_handler.on_private_message(
                    session=session, settings=_settings(), message=message
                )
        message.answer_voice.assert_not_awaited(), "助手转述不构成超管规则"

    async def test_standing_order_applies_to_a_later_turn(self) -> None:
        """真链路：历史里有持续指示 → 本轮没有指示也要按它走。"""

        await self._seed(777, ("user", "以后都用语音"), ("assistant", "好"))
        message = _message(text="今天吃了没", message_id=7206)
        for attr in MEDIA_ATTRS:
            setattr(message, attr, None)
        message.bot.get_chat = AsyncMock(return_value=_chat(restricted_voice=False))
        llm = MagicMock()
        llm.chat = AsyncMock(return_value="[[DM_DELIVERY: text]]\n吃啦")
        with patch.object(
            dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict(dm.TIER_SUPER))
        ), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "list_authorized_groups", new=AsyncMock(return_value=[])), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=_FakeTTSService())):
            async with self.session_factory() as session:
                await dm_handler.on_private_message(
                    session=session, settings=_settings(), message=message
                )
        message.answer_voice.assert_awaited_once(), "历史里的持续指示必须真的生效"

    async def test_release_in_history_clears_the_standing_order(self) -> None:
        await self._seed(
            777,
            ("user", "以后都用语音"),
            ("assistant", "好"),
            ("user", "以后都不用语音了"),
            ("assistant", "好"),
        )
        message = _message(text="今天吃了没", message_id=7207)
        for attr in MEDIA_ATTRS:
            setattr(message, attr, None)
        llm = MagicMock()
        llm.chat = AsyncMock(return_value="[[DM_DELIVERY: text]]\n吃啦")
        with patch.object(
            dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict(dm.TIER_SUPER))
        ), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "list_authorized_groups", new=AsyncMock(return_value=[])), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=_FakeTTSService())):
            async with self.session_factory() as session:
                await dm_handler.on_private_message(
                    session=session, settings=_settings(), message=message
                )
        message.answer_voice.assert_not_awaited()

    async def test_a_member_never_borrows_the_owner_standing_order(self) -> None:
        await self._seed(777, ("user", "以后都用语音"), ("assistant", "好"))
        message = _message(text="今天吃了没", message_id=7208)
        for attr in MEDIA_ATTRS:
            setattr(message, attr, None)
        llm = MagicMock()
        llm.chat = AsyncMock(return_value="[[DM_DELIVERY: text]]\n吃啦")
        with patch.object(
            dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict(dm.TIER_MEMBER))
        ), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "list_authorized_groups", new=AsyncMock(return_value=[])), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=_FakeTTSService())):
            async with self.session_factory() as session:
                await dm_handler.on_private_message(
                    session=session, settings=_settings(), message=message
                )
        message.answer_voice.assert_not_awaited(), "普通成员不共享超管的持续指示"


class LatestStandingOrderTests(unittest.TestCase):
    """阻断项 A：最高管理员刚下的持续指示，本轮就该生效，不能被旧历史压过去。"""

    def test_the_probe_that_caught_it(self) -> None:
        """父代理探针：刚说「以后都用文字」，旧的「以后都用语音」不能压过它。"""

        directive, source = private_tts.resolve_delivery_directive(
            text="以后都用文字回复", persistent=private_tts.DELIVERY_VOICE, is_super=True
        )
        self.assertEqual(directive, private_tts.DELIVERY_TEXT)
        self.assertEqual(source, "turn", "来源必须是本轮，不是 history")

    def test_without_any_history_a_standing_order_still_applies_this_turn(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="以后都用文字回复", persistent="", is_super=True
        )
        self.assertEqual(directive, private_tts.DELIVERY_TEXT)
        self.assertEqual(source, "turn")

    def test_the_reverse_direction_is_covered_too(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="以后都用语音", persistent=private_tts.DELIVERY_TEXT, is_super=True
        )
        self.assertEqual(directive, private_tts.DELIVERY_VOICE)
        self.assertEqual(source, "turn")

    def test_one_shot_still_wins_over_a_fresh_standing_order_in_the_same_turn(self) -> None:
        """「以后都用语音，这次用文字」：这一轮是文字，往后才是语音。"""

        directive, source = private_tts.resolve_delivery_directive(
            text="以后都用语音，这次用文字", persistent=private_tts.DELIVERY_TEXT, is_super=True
        )
        self.assertEqual(directive, private_tts.DELIVERY_TEXT)
        self.assertEqual(source, "turn")

    def test_history_is_only_used_when_this_turn_says_nothing(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="今天吃了没", persistent=private_tts.DELIVERY_VOICE, is_super=True
        )
        self.assertEqual(directive, private_tts.DELIVERY_VOICE)
        self.assertEqual(source, "history")

    def test_the_newest_revocation_still_clears_the_standing_order(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="以后都不用语音了", persistent=private_tts.DELIVERY_VOICE, is_super=True
        )
        self.assertEqual(directive, "")
        self.assertEqual(source, "released")

    def test_autonomy_still_hands_the_turn_back_without_touching_the_standing_order(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="用语音吧，算了随你选", persistent=private_tts.DELIVERY_TEXT, is_super=True
        )
        self.assertEqual(directive, "")
        self.assertEqual(source, "autonomy")

    def test_a_non_owner_still_gets_nothing(self) -> None:
        self.assertEqual(
            private_tts.resolve_delivery_directive(
                text="以后都用语音", persistent=private_tts.DELIVERY_VOICE, is_super=False
            ),
            ("", ""),
        )


class RevokedOrderFoldingTests(unittest.TestCase):
    """同句里被自己撤销掉的新指示，不许写进持续态。"""

    def test_an_order_revoked_later_in_the_same_message_is_not_written(self) -> None:
        self.assertEqual(
            private_tts.fold_owner_delivery_state(["以后都用语音，算了随你选"]), ""
        )

    def test_a_clean_standing_order_is_still_written(self) -> None:
        self.assertEqual(
            private_tts.fold_owner_delivery_state(["以后都用语音"]), private_tts.DELIVERY_VOICE
        )

    def test_autonomy_keeps_the_previous_standing_order_intact(self) -> None:
        """「随你选」只让那一轮不写；它不是解除，既有持续态原样留着。"""

        self.assertEqual(
            private_tts.fold_owner_delivery_state(["以后都用语音", "算了随你选"]),
            private_tts.DELIVERY_VOICE,
        )

    def test_release_still_clears_the_previous_standing_order(self) -> None:
        self.assertEqual(
            private_tts.fold_owner_delivery_state(["以后都用语音", "以后都不用语音了"]), ""
        )

    def test_autonomy_before_a_later_real_order_does_not_block_it(self) -> None:
        self.assertEqual(
            private_tts.fold_owner_delivery_state(["随你选", "以后都用语音"]),
            private_tts.DELIVERY_VOICE,
        )


class QuestionIsNotAnInstructionTests(unittest.TestCase):
    """阻断项 B：提问 / 条件 / 转述不得升级成硬规则；礼貌确认仍然是真指示。"""

    def test_the_probe_that_caught_it(self) -> None:
        self.assertIsNone(private_tts.parse_owner_delivery_instruction("用语音吗？"))
        self.assertEqual(
            private_tts.resolve_delivery_directive(text="用语音吗？", is_super=True), ("", "")
        )

    def test_questions_about_the_medium_are_not_orders(self) -> None:
        for raw in (
            "用语音吗？",
            "改成语音行吗？",
            "这次用文字好吗？",
            "可以发语音吗？",
            "语音好不好",
            "文字行不行",
            "用语音吧？",
            "改成文字好吗",
            "这次改成文字好不好呢",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(private_tts.parse_owner_delivery_instruction(raw))

    def test_conditional_and_hypothetical_sentences_are_not_orders(self) -> None:
        for raw in (
            "如果以后都用语音会怎么样？",
            "假如都用语音会不会很奇怪",
            "要是以后都用文字行吗",
            "万一都用语音呢",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(private_tts.parse_owner_delivery_instruction(raw))

    def test_a_conditional_never_becomes_a_standing_order(self) -> None:
        self.assertEqual(
            private_tts.fold_owner_delivery_state(["如果以后都用语音会怎么样？"]), ""
        )

    def test_a_question_never_becomes_a_standing_order(self) -> None:
        self.assertEqual(private_tts.fold_owner_delivery_state(["以后都用语音好吗？"]), "")

    def test_polite_confirmation_is_still_a_real_instruction(self) -> None:
        """疑问词不贴媒介词时，它是礼貌地确认一个真指示，不是提问。"""

        for raw, expected in (
            ("这次用语音说给我听好吗", private_tts.DELIVERY_VOICE),
            ("以后都用语音，麻烦你了，好吗", private_tts.DELIVERY_VOICE),
            ("下次改成文字，拜托拜托", private_tts.DELIVERY_TEXT),
            ("用语音吧，拜托了", private_tts.DELIVERY_VOICE),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(private_tts.parse_owner_delivery_instruction(raw), expected)

    def test_a_question_in_one_clause_does_not_cancel_a_real_order_in_another(self) -> None:
        self.assertEqual(
            private_tts.parse_owner_delivery_instruction("今天用语音吗？算了，改成文字"),
            private_tts.DELIVERY_TEXT,
        )

    def test_a_question_never_overrides_an_existing_standing_order(self) -> None:
        directive, source = private_tts.resolve_delivery_directive(
            text="用语音吗？", persistent=private_tts.DELIVERY_VOICE, is_super=True
        )
        self.assertEqual(directive, private_tts.DELIVERY_VOICE)
        self.assertEqual(source, "history", "问句不解除、不改写既有持续态")

    def test_a_non_owner_question_grants_nothing(self) -> None:
        self.assertEqual(
            private_tts.resolve_delivery_directive(text="用语音吗？", is_super=False), ("", "")
        )


class LatestStandingOrderHandlerTests(_DbDeliveryTestCase):
    """跨真实 handler + 真临时库：新的持续指示要立刻改变投递方式。"""

    async def _seed(self, user_id: int, *contents: str) -> None:
        async with self.session_factory() as session:
            for content in contents:
                await dm.record_private_turn(
                    session,
                    user_id=user_id,
                    user_content=content,
                    assistant_content="好",
                )

    async def _dispatch(self, *, text, reply, verdict, service, message_id, patches):
        message = _message(text=text, message_id=message_id)
        for attr in MEDIA_ATTRS:
            setattr(message, attr, None)
        message.bot.get_chat = AsyncMock(return_value=_chat(restricted_voice=False))
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=reply)
        llm.vision_describe = AsyncMock(return_value="一张图")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=verdict)), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "list_authorized_groups", new=AsyncMock(return_value=[])), \
             patch.object(dm_handler, "_tts_service", new=MagicMock(return_value=service)), \
             patches:
            async with self.session_factory() as session:
                await dm_handler.on_private_message(
                    session=session, settings=_settings(), message=message
                )
        return message

    async def test_a_fresh_standing_order_beats_old_history_in_this_same_turn(self) -> None:
        """历史里是「以后都用语音」，本轮说「以后都用文字回复」→ 本轮就该走文字。"""

        await self._seed(777, "以后都用语音", "今天天气不错")
        message = await self._dispatch(
            text="以后都用文字回复",
            reply="[[DM_DELIVERY: voice]]\n诶--我在呢",
            verdict=_verdict(dm.TIER_SUPER),
            service=_FakeTTSService(),
            message_id=7301,
            patches=contextlib.nullcontext(),
        )
        message.answer.assert_awaited_once(), "本轮必须按文字走，不能等下一轮"
        self.assertEqual(message.answer.await_args.args[0], "诶--我在呢")
        message.answer_voice.assert_not_awaited()

    async def test_the_new_standing_order_is_persisted_for_the_next_turn(self) -> None:
        """并且要真的写进历史，下一轮才不必重复说。"""

        await self._seed(777, "以后都用语音", "今天天气不错")
        message = await self._dispatch(
            text="以后都用文字回复",
            reply="[[DM_DELIVERY: voice]]\n诶--我在呢",
            verdict=_verdict(dm.TIER_SUPER),
            service=_FakeTTSService(),
            message_id=7302,
            patches=contextlib.nullcontext(),
        )
        self.assertEqual(message.answer.await_args.args[0], "诶--我在呢")

        # 第二轮：同一句「今天吃没」，不再重复指示，也必须走文字。
        second = await self._dispatch(
            text="今天吃了没",
            reply="[[DM_DELIVERY: voice]]\n吃啦",
            verdict=_verdict(dm.TIER_SUPER),
            service=_FakeTTSService(),
            message_id=7303,
            patches=contextlib.nullcontext(),
        )
        second.answer.assert_awaited_once()
        second.answer_voice.assert_not_awaited()

    async def test_history_only_applies_when_the_turn_says_nothing(self) -> None:
        await self._seed(777, "以后都用语音", "今天天气不错")
        message = await self._dispatch(
            text="今天吃了没",
            reply="[[DM_DELIVERY: text]]\n吃啦",
            verdict=_verdict(dm.TIER_SUPER),
            service=_FakeTTSService(),
            message_id=7304,
            patches=contextlib.nullcontext(),
        )
        message.answer_voice.assert_awaited_once(), "历史里的持续指示照常生效"

    async def test_a_question_does_not_force_voice_in_a_real_turn(self) -> None:
        message = await self._dispatch(
            text="用语音吗？",
            reply="[[DM_DELIVERY: text]]\n嗯嗯",
            verdict=_verdict(dm.TIER_SUPER),
            service=_FakeTTSService(),
            message_id=7305,
            patches=contextlib.nullcontext(),
        )
        message.answer.assert_awaited_once(), "问句不能强制语音"
        message.answer_voice.assert_not_awaited()

    async def test_a_revoked_order_is_not_persisted_for_the_next_turn(self) -> None:
        """「以后都用语音，算了随你选」这一轮不得给历史留下新持续态。"""

        await self._seed(777, "今天天气不错")
        first = await self._dispatch(
            text="以后都用语音，算了随你选",
            reply="[[DM_DELIVERY: voice]]\n诶--我在呢",
            verdict=_verdict(dm.TIER_SUPER),
            service=_FakeTTSService(),
            message_id=7306,
            patches=contextlib.nullcontext(),
        )
        # 本轮撤销 = 交回自主：模型选什么就发什么（这里模型选了语音，就发语音）
        first.answer_voice.assert_awaited_once()
        first.answer.assert_not_awaited()

        # 关键：被撤销的持续态不得留给下一轮——模型说文字，就该发文字。
        second = await self._dispatch(
            text="今天吃了没",
            reply="[[DM_DELIVERY: text]]\n吃啦",
            verdict=_verdict(dm.TIER_SUPER),
            service=_FakeTTSService(),
            message_id=7307,
            patches=contextlib.nullcontext(),
        )
        second.answer.assert_awaited_once()
        second.answer_voice.assert_not_awaited(), "被撤销的持续态不得留给下一轮"


if __name__ == "__main__":
    unittest.main()
