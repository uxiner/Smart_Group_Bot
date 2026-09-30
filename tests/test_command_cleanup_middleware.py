from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.middlewares.command_cleanup import ManagementCommandCleanupMiddleware
from bot.utils.command_catalog import bare_command, management_command_names


def _message(
    text: str,
    *,
    chat_type: str = "supergroup",
    message_id: int = 555,
    user_id: int = 42,
    chat_id: int = -10001,
    is_bot: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id, is_bot=is_bot),
    )


class ManagementCommandCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, message: SimpleNamespace, schedule: AsyncMock | None = None):
        middleware = ManagementCommandCleanupMiddleware()
        handler = AsyncMock(return_value="handled")
        spy = schedule or AsyncMock(return_value=True)
        with patch(
            "bot.middlewares.command_cleanup.schedule_message_auto_delete_durable",
            new=spy,
        ):
            result = await middleware(handler, message, {})
        return result, spy, handler

    async def test_group_operator_command_is_scheduled_for_cleanup(self) -> None:
        message = _message("/warnings")
        result, schedule, handler = await self._run(message)

        # The command still runs; only its own message is queued for removal.
        self.assertEqual(result, "handled")
        handler.assert_awaited_once()
        schedule.assert_awaited_once()
        self.assertIs(schedule.await_args.args[0], message)
        self.assertEqual(
            schedule.await_args.args[1],
            ManagementCommandCleanupMiddleware()._seconds,
        )

    async def test_bot_suffixed_and_argument_forms_are_recognized(self) -> None:
        for text in ("/ban@xatongxue_bot 123 广告", "/mute all", "/exemptlist", "/modlist"):
            with self.subTest(text=text):
                _, schedule, _ = await self._run(_message(text))
                self.assertEqual(schedule.await_count, 1, text)

    async def test_member_facing_commands_are_left_alone(self) -> None:
        for text in ("/help", "/av 某个关键词", "/voteban", "/rules", "warnings"):
            with self.subTest(text=text):
                _, schedule, _ = await self._run(_message(text))
                schedule.assert_not_awaited()

    async def test_argument_form_of_a_member_command_is_left_alone(self) -> None:
        # "/av enable" sits in the operator section, but "/av" itself belongs to
        # members: a plain /av search must never be deleted.
        for text in ("/av enable", "/av <番号>", "/lm add 记住这条"):
            with self.subTest(text=text):
                _, schedule, _ = await self._run(_message(text))
                schedule.assert_not_awaited()

    async def test_private_chat_is_not_touched(self) -> None:
        _, schedule, handler = await self._run(_message("/warnings", chat_type="private", chat_id=42))
        schedule.assert_not_awaited()
        handler.assert_awaited_once()

    async def test_another_bots_command_is_not_touched(self) -> None:
        _, schedule, _ = await self._run(_message("/warnings", is_bot=True, user_id=999))
        schedule.assert_not_awaited()

    async def test_scheduling_failure_never_blocks_the_command(self) -> None:
        message = _message("/warnings")
        result, _, handler = await self._run(message, schedule=AsyncMock(side_effect=RuntimeError("db down")))
        self.assertEqual(result, "handled")
        handler.assert_awaited_once()

    def test_management_set_covers_operator_commands_only(self) -> None:
        names = management_command_names()
        for expected in ("ban", "unban", "mute", "unmute", "warnings", "exemptlist", "modlist"):
            self.assertIn(expected, names)
        for member_facing in ("help", "av", "voteban", "rules", "settings", "lm"):
            self.assertNotIn(member_facing, names)

    def test_bare_command_parsing(self) -> None:
        self.assertEqual(bare_command("/mute all"), "mute")
        self.assertEqual(bare_command("/mute@xatongxue_bot"), "mute")
        self.assertEqual(bare_command("/exemptlist（别名 /modlist）"), "exemptlist")
        self.assertEqual(bare_command("/lm replace <#ID> => <新内容>"), "lm")
        self.assertEqual(bare_command("普通消息"), "")
        self.assertEqual(bare_command(""), "")


if __name__ == "__main__":
    unittest.main()


class OwnerSalutationGuardTests(unittest.TestCase):
    """The retired `主人` and the new 亲爱的 are both stripped for non-owners."""

    def test_new_salutation_is_removed_for_group_members(self) -> None:
        from bot.handlers.group import _normalize_owner_address

        self.assertEqual(_normalize_owner_address("亲爱的，我在呢~", False), "我在呢~")
        self.assertEqual(_normalize_owner_address("好的主人，马上办", False), "马上办")

    def test_owner_keeps_the_salutation(self) -> None:
        from bot.handlers.group import _normalize_owner_address

        self.assertEqual(_normalize_owner_address("亲爱的，我在呢~", True), "亲爱的，我在呢~")

    def test_unrelated_text_is_untouched(self) -> None:
        from bot.handlers.group import _normalize_owner_address

        text = "他刚说主人不在，我记下了"
        self.assertEqual(_normalize_owner_address(text, False), text)
