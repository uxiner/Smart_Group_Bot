"""修复批 P1-3 / D3-50：投递循环要有条数上限、逐条超时和投递间隔。

两个投递循环（always-tts 与纯文本）过去都是**串行、无 sleep、无条数上限、无逐条
超时**::

    # bot/handlers/group.py（修前）
    for plan in delivery_plans:            # ← reply_specs 完全来自模型输出
        delivery = await detailed_sender(message, plan.text, ...)   # 无 asyncio.timeout

而 ``reply_specs`` 来自模型输出、代码里没有任何 ``len(reply_specs)`` 截断。TTS 分支
每条至少 2 次 Bot API 调用（合成 + 上传，``_TTS_MAX_HTTP_TIMEOUT_SECONDS = 60.0``）；
plan 稍多或第一条遇 flood-wait，后面的 plan 在整段 45s 硬 deadline 到期时被整段取消
→ **剩余 plan 静默丢失**，而进度 overlay 已在第一次 fallback 时被
``_claim_progress_overlay`` 消耗掉，群内不会有任何提示。

修法：``delivery_plans`` 截到 3 条 + 逐条 ``asyncio.timeout`` + 两条之间 sleep 0.3s。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.handlers import group
from bot.services.reply_output import REPLY_OUTPUT_SCHEMA

CAP = 3  # 与 _PENDING_REPLY_MAX_DELIVERY_PLANS 对应


def _plans(count: int) -> list["group._ReplyDeliveryPlan"]:
    return [
        group._ReplyDeliveryPlan(
            text=f"第{i + 1}段",
            delivery_mode="message",
            reply_to_message_id=None,
        )
        for i in range(count)
    ]


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(
            enable_rich_messages=False,
            enable_streaming=False,
            stream_chunk_size=100,
            stream_edit_interval_sec=0.0,
            disable_link_preview=True,
        )
    )


class DeliveryLoopBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def _deliver(self, plans: list["group._ReplyDeliveryPlan"], send_reply):
        return await group._deliver_reply_plans(
            message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=1),
            delivery_plans=plans,
            settings=_settings(),
            tts_mode="off",
            tts_service=None,
            user_id=7,
            group_id=-100,
            tts_already_sent=False,
        )

    async def test_a_hanging_plan_does_not_swallow_the_others(self) -> None:
        """修前：第一条挂住 → 整段被硬 deadline 取消，后面全部静默丢失。"""

        delivered: list[str] = []
        gate = asyncio.Event()

        async def flaky_send(_message, text, **_kwargs):
            if text == "第1段":
                # 永远不返回：模拟 flood-wait / 网络挂死。
                await gate.wait()
            delivered.append(text)
            return True

        send_reply = AsyncMock(side_effect=flaky_send)
        with patch.object(group, "send_reply", send_reply), patch(
            "bot.handlers.group._PENDING_REPLY_PLAN_TIMEOUT_SECONDS", 0.05
        ):
            sent_ok, _tts_ok, messages = await self._deliver(_plans(3), send_reply)
            gate.set()

        self.assertTrue(sent_ok)
        # 第 1 条超时后被按"没发出去"处理，第 2、3 条仍然发得出去。
        self.assertEqual(delivered, ["第2段", "第3段"])
        self.assertEqual(messages, ["第2段", "第3段"])

    async def test_consecutive_plans_are_spaced(self) -> None:
        send_reply = AsyncMock(return_value=True)
        loop = asyncio.get_running_loop()
        with patch(
            "bot.handlers.group._PENDING_REPLY_DELIVERY_GAP_SECONDS", 0.05
        ), patch.object(group, "send_reply", send_reply):
            started = loop.time()
            await self._deliver(_plans(3), send_reply)
            elapsed = loop.time() - started
        # 3 条 = 2 个间隔。
        self.assertGreaterEqual(elapsed, 0.08)

    async def test_a_single_plan_is_never_spaced(self) -> None:
        send_reply = AsyncMock(return_value=True)
        loop = asyncio.get_running_loop()
        with patch(
            "bot.handlers.group._PENDING_REPLY_DELIVERY_GAP_SECONDS", 5.0
        ), patch.object(group, "send_reply", send_reply):
            started = loop.time()
            await self._deliver(_plans(1), send_reply)
            self.assertLess(loop.time() - started, 1.0)

    async def test_always_tts_branch_gets_the_same_per_item_timeout(self) -> None:
        gate = asyncio.Event()
        calls: list[str] = []

        async def slow_tts(_message, text, **_kwargs):
            calls.append(text)
            if text == "第1段":
                await gate.wait()
            return group.TTSDeliveryResult(
                requested_segments=(text,),
                sent_segment_count=1,
            )

        tts = SimpleNamespace(available=True, send_message_tts_result=slow_tts)
        send_reply = AsyncMock(return_value=True)
        with patch.object(group, "send_reply", send_reply), patch(
            "bot.handlers.group._PENDING_REPLY_PLAN_TIMEOUT_SECONDS", 0.05
        ):
            sent_ok, tts_ok, _messages = await group._deliver_reply_plans(
                message=SimpleNamespace(chat=SimpleNamespace(id=-100), message_id=1),
                delivery_plans=_plans(2),
                settings=_settings(),
                tts_mode="always",
                tts_service=tts,
                user_id=7,
                group_id=-100,
                tts_already_sent=False,
            )
            gate.set()

        self.assertEqual(calls, ["第1段", "第2段"])
        self.assertTrue(sent_ok)
        self.assertTrue(tts_ok)


def _pending_item() -> "group._PendingReplyItem":
    message = SimpleNamespace(
        message_id=99,
        text="在吗",
        caption=None,
        from_user=SimpleNamespace(
            id=123, is_bot=False, username="tester", full_name="Tester"
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
        return None


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


def _many_segment_reply(count: int) -> str:
    return json.dumps(
        {
            "schema": REPLY_OUTPUT_SCHEMA,
            "should_reply": True,
            "messages": [
                {"text": f"第{i + 1}段", "delivery_mode": "message"}
                for i in range(count)
            ],
        },
        ensure_ascii=False,
    )


class DeliveryPlanCapTests(unittest.IsolatedAsyncioTestCase):
    async def _plans_seen(self, reply_text: str) -> list[str]:
        seen: list[str] = []
        session = _PendingSession()
        memory = SimpleNamespace(
            session_factory=lambda: session,
            get_history=Mock(return_value=[]),
            load_group_history_by_budget=AsyncMock(return_value=[]),
            get_history_for_llm=AsyncMock(return_value=[]),
            add_message=AsyncMock(),
            compact_if_needed=AsyncMock(),
        )
        fake_skill = SimpleNamespace(
            tts_service=SimpleNamespace(available=False),
            build_answer_prompt_payload=Mock(
                return_value={"messages": [], "tools": []}
            ),
            available_skill_names=Mock(return_value=[]),
            content_boundaries_context="",
            answer_with_skill=AsyncMock(
                return_value=SimpleNamespace(
                    text=reply_text,
                    handled=True,
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

        async def capture(**kwargs):
            plans = list(kwargs.get("delivery_plans") or [])
            seen.extend(plan.text for plan in plans)
            return True, True, [plan.text for plan in plans]

        with (
            patch("bot.handlers.group.memory_holder.get", return_value=memory),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=fake_skill),
            patch(
                "bot.handlers.group.CasualService",
                return_value=SimpleNamespace(reply=AsyncMock(return_value="")),
            ),
            patch(
                "bot.handlers.group.ReplyProgressTracker", return_value=fake_progress
            ),
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch(
                "bot.handlers.group._deliver_reply_plans",
                new=AsyncMock(side_effect=capture),
            ),
        ):
            await group._process_pending_reply_batch(
                [_pending_item()], _processing_settings()
            )
        return seen

    async def test_model_cannot_force_an_unbounded_reply(self) -> None:
        seen = await self._plans_seen(_many_segment_reply(9))
        self.assertEqual(seen, ["第1段", "第2段", "第3段"])

    async def test_short_replies_are_untouched(self) -> None:
        seen = await self._plans_seen(_many_segment_reply(2))
        self.assertEqual(seen, ["第1段", "第2段"])

    def test_the_cap_is_a_named_constant_next_to_the_other_budgets(self) -> None:
        self.assertEqual(group._PENDING_REPLY_MAX_DELIVERY_PLANS, CAP)
        self.assertGreater(group._PENDING_REPLY_PLAN_TIMEOUT_SECONDS, 0)
        self.assertGreaterEqual(group._PENDING_REPLY_DELIVERY_GAP_SECONDS, 0.3)
