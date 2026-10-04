"""无法回答时的**诚实**提示（2026-10-04 生产事故的第 5 项修复）。

事故里主模型与备用都因为"超限"被 skip、HTTP 一次都没发出去，但 ``force_reply`` 分支
硬编码回「我在，直接说就好~」——听起来像听懂了，实际上什么都没生成，而且这句话术
**什么都可以被误读成"已经处理了"**。

这里锁三件事：

1. 文案本身是诚实的：不声称已签到 / 已排程 / 已完成任何副作用；
2. 文案不会被静默规则吃掉（否则"诚实失败"根本发不出去）；
3. 真正的回复链路上，模型返回空 + 被直接点名时，发出去的是这条诚实提示，
   **不再是**那句固定话术。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.handlers import group

#: 绝不允许出现的"已完成"口径（不虚称签到/排程/执行过任何副作用）。
_FORBIDDEN_CLAIMS = (
    "已签到",
    "已排程",
    "已安排",
    "已完成",
    "已处理",
    "好了",
    "直接说就好",
)


class UnavailableNoticeTests(unittest.TestCase):
    def test_notice_is_honest_about_side_effects(self) -> None:
        notice = group.REPLY_UNAVAILABLE_NOTICE

        self.assertTrue(notice.strip())
        for claim in _FORBIDDEN_CLAIMS:
            self.assertNotIn(claim, notice)

    def test_notice_is_not_swallowed_by_the_silence_rules(self) -> None:
        """诚实提示必须真的发得出去：不能被静默标记 / 简短回避规则吃掉。"""

        silenced, reason = group._should_silence_generated_reply(
            group.REPLY_UNAVAILABLE_NOTICE
        )

        self.assertFalse(silenced, reason)


def _pending_item() -> "group._PendingReplyItem":
    message = SimpleNamespace(
        message_id=99,
        text="在吗",
        caption=None,
        from_user=SimpleNamespace(
            id=123,
            is_bot=False,
            username="tester",
            full_name="Tester",
        ),
        sender_chat=None,
        reply_to_message=None,
        chat=SimpleNamespace(id=-10001, type="supergroup"),
    )
    return group._PendingReplyItem(
        message=message,
        group_id=-10001,
        user_id=123,
        input_text="在吗",
        msg_type="text",
        sender_username="tester",
        sender_is_owner=False,
        sender_is_tg_admin=False,
        user_tag="id:123",
        explicit_mention=True,
        mentioned=True,
        is_reply=False,
        reply_to_bot=False,
        reply_to_other=False,
        mention_other=False,
    )


class _PendingSession:
    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> "_PendingSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(self, model: object, key: int) -> SimpleNamespace:
        return SimpleNamespace(settings={})

    async def execute(self, statement: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def _processing_settings() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(
            inbound_debounce_seconds=1.0,
            main_model="",
            decision_model="",
            compress_model="",
            moderation_model="",
            vision_model="",
            embed_model="",
            max_context_tokens=0,
            context_window_mode="auto",
            decision_context_items=0,
            enable_typing=False,
            enable_streaming=False,
            stream_chunk_size=100,
            stream_edit_interval_sec=0.0,
        ),
        skill_sticker_file_ids="",
    )


class ForcedReplyFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_model_reply_sends_the_honest_notice(self) -> None:
        """模型/工具链路什么都没产出 + 被直接点名 → 诚实失败，不装作听懂。"""

        item = _pending_item()
        session = _PendingSession()
        memory = SimpleNamespace(
            session_factory=lambda: session,
            get_history=Mock(return_value=[]),
            load_group_history_by_budget=AsyncMock(return_value=[]),
            get_history_for_llm=AsyncMock(return_value=[]),
            add_message=AsyncMock(),
        )
        # 工具链路整条失败：handled=False、text=""（正是事故现场的形状）。
        fake_skill = SimpleNamespace(
            tts_service=SimpleNamespace(available=False),
            build_answer_prompt_payload=Mock(return_value={"messages": [], "tools": []}),
            available_skill_names=Mock(return_value=[]),
            content_boundaries_context="",
            answer_with_skill=AsyncMock(
                return_value=SimpleNamespace(
                    text="",
                    handled=False,
                    must_deliver_text=False,
                    sticker_sent=False,
                    tts_sent=False,
                    sticker_file_id="",
                    tts_text="",
                    tts_telegram_message_ids=(),
                    delivery_confirmed=False,
                    embedded_reply_sent=False,
                    embedded_reply_text="",
                )
            ),
        )
        fake_casual = SimpleNamespace(reply=AsyncMock(return_value=""))
        fake_progress = SimpleNamespace(
            visible=False,
            start=AsyncMock(),
            report=AsyncMock(),
            composing=AsyncMock(),
            handoff=AsyncMock(return_value=None),
            finish=AsyncMock(),
            fail=AsyncMock(),
            dismiss=AsyncMock(),
            close=AsyncMock(),
        )
        delivered: list[list[object]] = []

        async def _capture_delivery(**kwargs: object):
            plans = list(kwargs.get("delivery_plans") or [])
            delivered.append(plans)
            return True, False, [plan.text for plan in plans]

        with (
            patch("bot.handlers.group.memory_holder.get", return_value=memory),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=fake_skill),
            patch("bot.handlers.group.CasualService", return_value=fake_casual),
            patch("bot.handlers.group.ReplyProgressTracker", return_value=fake_progress),
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch(
                "bot.handlers.group._deliver_reply_plans",
                new=AsyncMock(side_effect=_capture_delivery),
            ),
        ):
            await group._process_pending_reply_batch([item], _processing_settings())

        texts = [plan.text for plans in delivered for plan in plans]
        self.assertEqual(texts, [group.REPLY_UNAVAILABLE_NOTICE])
        self.assertNotIn("我在，直接说就好~", texts)
        # 诚实提示被静默规则放行（否则这里会是空列表）。
        silenced, reason = group._should_silence_generated_reply(texts[0])
        self.assertFalse(silenced, reason)


if __name__ == "__main__":
    unittest.main()
