from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.skills.base import SkillContext
from bot.services.skills.send_sticker import SendStickerSkill
from bot.services.sticker_library import sticker_library


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_args: object) -> bool:
        return False


class SendStickerTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_is_committed_before_send_and_mark_uses_fresh_session(self) -> None:
        events: list[str] = []
        tool_session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("snapshot_commit")),
        )
        mark_session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("mark_commit")),
        )

        async def send_after_release(*_args, **_kwargs) -> None:
            events.append("send")
            self.assertEqual(events, ["snapshot_commit", "send"])

        async def mark_in_fresh_session(session, *_args, **_kwargs) -> None:
            self.assertIs(session, mark_session)
            events.append("mark")

        context = SkillContext(
            session=tool_session,
            session_factory=lambda: _SessionContext(mark_session),
            message=SimpleNamespace(chat=SimpleNamespace(id=-100)),
            current_user_text="无语",
        )
        with (
            patch(
                "bot.services.skills.send_sticker.sticker_library.pick_sticker",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        file_id="sticker-id",
                        source="library_match",
                        description="测试贴纸",
                    )
                ),
            ),
            patch(
                "bot.services.skills.send_sticker.send_sticker_with_auto_delete",
                new=AsyncMock(side_effect=send_after_release),
            ),
            patch(
                "bot.services.skills.send_sticker.sticker_library.mark_sent",
                new=AsyncMock(side_effect=mark_in_fresh_session),
            ),
        ):
            result = await SendStickerSkill().run({"query": "无语"}, context)

        self.assertTrue(result.ok)
        self.assertEqual(
            events,
            ["snapshot_commit", "send", "mark", "mark_commit"],
        )
        self.assertTrue(context.handled)
        self.assertTrue(context.sticker_sent)

    async def test_bookkeeping_failure_does_not_retry_successful_delivery(self) -> None:
        tool_session = SimpleNamespace(commit=AsyncMock())
        mark_session = SimpleNamespace(commit=AsyncMock())
        context = SkillContext(
            session=tool_session,
            session_factory=lambda: _SessionContext(mark_session),
            message=SimpleNamespace(chat=SimpleNamespace(id=-100)),
        )
        send = AsyncMock()
        with (
            patch(
                "bot.services.skills.send_sticker.send_sticker_with_auto_delete",
                new=send,
            ),
            patch(
                "bot.services.skills.send_sticker.sticker_library.mark_sent",
                new=AsyncMock(side_effect=RuntimeError("db unavailable")),
            ),
        ):
            result = await SendStickerSkill().run(
                {"sticker_file_id": "sticker-id"},
                context,
            )

        self.assertTrue(result.ok)
        send.assert_awaited_once()
        self.assertTrue(context.handled)
        self.assertTrue(context.suppress_followup_text)

    async def test_semantic_miss_sends_nothing_and_returns_no_sticker(self) -> None:
        tool_session = SimpleNamespace(commit=AsyncMock())
        context = SkillContext(
            session=tool_session,
            message=SimpleNamespace(chat=SimpleNamespace(id=-100)),
            current_user_text="请检查数据库连接失败的原因并给出修复方案",
        )
        send = AsyncMock()
        with (
            patch(
                "bot.services.skills.send_sticker.send_sticker_with_auto_delete",
                new=send,
            ),
            patch.object(
                sticker_library,
                "pick_sticker",
                new=AsyncMock(return_value=SimpleNamespace(file_id="", source="no_semantic_match")),
            ),
        ):
            result = await SendStickerSkill().run({"query": "庆祝"}, context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_sticker")
        send.assert_not_awaited()
        self.assertFalse(context.handled)
        self.assertFalse(context.sticker_sent)

    async def test_successful_send_marks_bookkeeping_exactly_once(self) -> None:
        tool_session = SimpleNamespace(commit=AsyncMock())
        mark_session = SimpleNamespace(commit=AsyncMock())
        context = SkillContext(
            session=tool_session,
            session_factory=lambda: _SessionContext(mark_session),
            message=SimpleNamespace(chat=SimpleNamespace(id=-100)),
        )
        send = AsyncMock()
        mark = AsyncMock()
        with (
            patch(
                "bot.services.skills.send_sticker.send_sticker_with_auto_delete",
                new=send,
            ),
            patch(
                "bot.services.skills.send_sticker.sticker_library.mark_sent",
                new=mark,
            ),
        ):
            result = await SendStickerSkill().run(
                {"sticker_file_id": "sticker-id"}, context
            )

        self.assertTrue(result.ok)
        send.assert_awaited_once()
        mark.assert_awaited_once()
        self.assertEqual(mark.await_args.args[1], -100)
        self.assertEqual(mark.await_args.args[2], "sticker-id")


if __name__ == "__main__":
    unittest.main()
