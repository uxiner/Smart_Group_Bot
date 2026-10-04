"""修复批 P1-3 / D3-51：归档阶段借走 delivery evidence 时要连带借走它的 plan。

``_process_pending_reply_batch`` 的归档阶段用「文本」把 ``stored_reply`` 匹配回
``delivery_plans``，文本对不上时就**无条件取第一条**剩余 evidence::

    # bot/handlers/group.py:7047-7051（修前）
    elif unmatched_delivery_evidence:
        # Partial TTS delivery stores only its delivered prefix,
        # so it cannot text-match the original full plan.
        matched_evidence = unmatched_delivery_evidence.pop(0)
        matched_plan = matched_evidence.plan

``elif`` 只要求「本条 stored_reply 没匹配到任何 plan」，**并不校验 pop(0) 拿到的
evidence 是否真的对应这条文本**；更关键的是借走 evidence 之后，**对应的 plan 仍留在
``unmatched_plans`` 里**。于是同一个 plan 可能被匹配两遍：第二遍自然再也找不到它的
evidence，写进 ``archive_metadata`` 的 ``telegram_message_ids`` / ``sent_at`` /
``reply_to_message_id`` 就指错了消息（后续删除/编辑/溯源会指错对象）。

修法：``pop(0)`` 之后把对应 plan 从 ``unmatched_plans`` 一并移除，两边同进同出。

本文件走真实 ``_process_pending_reply_batch``（模型输出经真实
``parse_reply_output`` 解析出两个 plan），只把 ``_deliver_reply_plans`` 换成可控替身。
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.handlers import group
from bot.services.reply_output import REPLY_OUTPUT_SCHEMA


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


def _two_plan_reply() -> str:
    return json.dumps(
        {
            "schema": REPLY_OUTPUT_SCHEMA,
            "should_reply": True,
            "messages": [
                {"text": "AAA", "delivery_mode": "message"},
                {"text": "BBB", "delivery_mode": "message"},
            ],
        },
        ensure_ascii=False,
    )


class BorrowedEvidenceKeepsBothSidesInStepTests(unittest.IsolatedAsyncioTestCase):
    async def _archive(self) -> list[dict]:
        """Drive one batch whose first stored reply cannot text-match its plan."""

        item = _pending_item()
        session = _PendingSession()
        archived: list[dict] = []

        async def add_message(_group_id, role, text, **kwargs):
            archived.append({"role": role, "text": text, **kwargs})
            return None

        memory = SimpleNamespace(
            session_factory=lambda: session,
            get_history=Mock(return_value=[]),
            load_group_history_by_budget=AsyncMock(return_value=[]),
            get_history_for_llm=AsyncMock(return_value=[]),
            add_message=AsyncMock(side_effect=add_message),
        )
        fake_skill = SimpleNamespace(
            tts_service=SimpleNamespace(available=False),
            build_answer_prompt_payload=Mock(return_value={"messages": [], "tools": []}),
            available_skill_names=Mock(return_value=[]),
            content_boundaries_context="",
            answer_with_skill=AsyncMock(
                return_value=SimpleNamespace(
                    text=_two_plan_reply(),
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

        async def fake_deliver(**kwargs):
            plans = list(kwargs.get("delivery_plans") or [])
            evidence = kwargs.get("delivery_evidence")
            if evidence is not None:
                for index, plan in enumerate(plans):
                    evidence.append(
                        group._ReplyDeliveryEvidence(
                            plan=plan,
                            telegram_message_ids=(900 + index,),
                        )
                    )
            # 第一条存的是「部分 TTS 投递的前缀」，与 plan.text 对不上 —— 正是报告
            # 点名的那条 elif 借证据分支；随后同一 plan 的完整文本又被存了一次
            # （TTS 工具已投递 + 文本兜底两条路径都会往 stored_reply_messages 里
            # append），这才是"同一 plan 被匹配两遍"的现场。
            stored = [f"prefix::{plans[0].text}", plans[0].text]
            return True, True, stored

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
                new=AsyncMock(side_effect=fake_deliver),
            ),
        ):
            await group._process_pending_reply_batch(
                [item], _processing_settings()
            )
        return archived

    async def test_borrowed_plan_is_not_matched_a_second_time(self) -> None:
        archived = await self._archive()
        assistant = [
            row for row in archived if row.get("role") == "assistant"
        ]
        self.assertEqual(len(assistant), 2, archived)

        first, second = assistant
        # 第一条文本对不上任何 plan，只能借 evidence，于是借到 plan[0] 的（900）。
        self.assertEqual(
            first["archive_metadata"]["extra_metadata"]["telegram_message_ids"],
            [900],
        )
        # 修前：plan[0] 被借走后**仍留在 unmatched_plans 里**，第二条的完整文本
        # "AAA" 于是又文本命中它一次；而它的 evidence 已经在第一轮被 pop 走，
        # 第二轮找不到 → 这条归档拿不到任何消息 id，只剩
        # telegram_message_id_unavailable，后续删除/编辑/溯源指不到消息。
        # 修后：借走 evidence 的同时把 plan 一并移出，两边同进同出。
        self.assertNotIn(
            "telegram_message_id_unavailable", second["archive_metadata"]
        )
        self.assertIn(
            "telegram_message_ids", second["archive_metadata"]["extra_metadata"]
        )

    async def test_every_archived_reply_keeps_its_own_delivery_metadata(self) -> None:
        archived = await self._archive()
        assistant = [row for row in archived if row.get("role") == "assistant"]
        self.assertEqual(len(assistant), 2, archived)
        for row in assistant:
            metadata = row["archive_metadata"]
            self.assertFalse(
                metadata.get("telegram_message_id_unavailable"),
                metadata,
            )
            self.assertIn("telegram_message_id", metadata)

    def test_borrowing_keeps_plans_and_evidence_the_same_length(self) -> None:
        """静态钉住「同进同出」这条不变式。"""

        import inspect

        source = inspect.getsource(group._process_pending_reply_batch)
        borrow = source.split("elif unmatched_delivery_evidence:", 1)[1]
        borrow = borrow.split("resolved_delivery_mode", 1)[0]
        self.assertIn("matched_plan = matched_evidence.plan", borrow)
        self.assertIn("unmatched_plans.pop(borrowed_index)", borrow)
