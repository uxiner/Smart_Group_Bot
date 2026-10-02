from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.middlewares.command_cleanup import ManagementCommandCleanupMiddleware
from bot.utils.command_catalog import bare_command, management_command_names


def _settings(super_admin_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(super_admin_id=super_admin_id)


def _session_factory() -> object:
    """最小可用的 session_factory：授权查询本身在用例里被 patch。"""

    class _SessionContext:
        async def __aenter__(self) -> SimpleNamespace:
            return SimpleNamespace(commit=AsyncMock())

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    return lambda: _SessionContext()


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


_UNSET = object()


class ManagementCommandCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        message: SimpleNamespace,
        schedule: AsyncMock | None = None,
        *,
        settings: SimpleNamespace | None = None,
        session_factory: object = _UNSET,
        delegated_admin: bool = True,
        delegated_admin_error: bool = False,
    ):
        """默认发送者是"已确证的委派管理员"（会话查询在用例里被 patch）。

        权限收窄后（F-048）只有两种身份会被清理：超管、以及数据库里被委派的
        群管理员。`delegated_admin=False` 表示普通成员 —— 命令行必须留着。
        """

        factory = _session_factory() if session_factory is _UNSET else session_factory
        middleware = ManagementCommandCleanupMiddleware(factory)
        handler = AsyncMock(return_value="handled")
        spy = schedule or AsyncMock(return_value=True)
        delegated_check = AsyncMock(
            return_value=delegated_admin,
            side_effect=(
                RuntimeError("db down") if delegated_admin_error else None
            ),
        )
        with (
            patch(
                "bot.middlewares.command_cleanup.schedule_message_auto_delete_durable",
                new=spy,
            ),
            patch(
                "bot.middlewares.command_cleanup.is_group_admin_authorized",
                new=delegated_check,
            ),
        ):
            result = await middleware(
                handler,
                message,
                {"settings": settings if settings is not None else _settings()},
            )
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

    # --- F-048：消息像管理命令 ≠ 发送者是操作者 ---------------------------

    async def test_member_lookalike_command_is_not_deleted(self) -> None:
        """负例：普通成员把 /ban 当文本发出来时，不替他删掉这条证据。"""

        message = _message("/ban 555 广告", user_id=777)
        result, schedule, handler = await self._run(
            message,
            delegated_admin=False,
        )

        self.assertEqual(result, "handled")
        handler.assert_awaited_once()
        schedule.assert_not_awaited()

    async def test_delegated_group_admin_command_is_cleaned(self) -> None:
        """数据库里被委派的群管理员：命令确实由机器人服务，照旧清理。"""

        message = _message("/warnings", user_id=778)
        _, schedule, _ = await self._run(
            message,
            session_factory=_session_factory(),
            delegated_admin=True,
        )

        schedule.assert_awaited_once()

    async def test_super_admin_command_is_cleaned(self) -> None:
        message = _message("/warnings", user_id=1)
        _, schedule, _ = await self._run(
            message,
            settings=_settings(super_admin_id=1),
            session_factory=None,
            delegated_admin=False,
        )

        schedule.assert_awaited_once()

    async def test_telegram_admin_status_is_never_consulted(self) -> None:
        """F-048：只看"已确证的操作者"，不再去问 Telegram 管理员身份。

        未委派的 Telegram 管理员跑管理命令只会得到"权限不足"，那条命令没有被
        机器人服务，因此也不该被清理（宁可漏删，不可误删）。
        """

        message = _message("/ban 555", user_id=779)
        with patch(
            "bot.middlewares.command_cleanup.is_user_admin_cached",
            create=True,
        ) as telegram_admin:
            _, schedule, handler = await self._run(
                message,
                delegated_admin=False,
            )

        schedule.assert_not_awaited()
        handler.assert_awaited_once()
        telegram_admin.assert_not_called()

    async def test_quoted_management_command_text_is_not_deleted(self) -> None:
        """负例：成员把管理命令当引用/转述文本时，不以 "/" 开头，不清理。"""

        for text in ("> /ban 555", "转发：/mute all", "他说 /warnings 能看名单"):
            with self.subTest(text=text):
                _, schedule, handler = await self._run(
                    _message(text, user_id=780),
                    delegated_admin=False,
                )
                schedule.assert_not_awaited()
                handler.assert_awaited_once()

    async def test_permission_lookup_failure_keeps_the_message(self) -> None:
        """权限查不出来时宁可留着消息，也不能删错人的证据。"""

        _, schedule, handler = await self._run(
            _message("/ban 555"),
            session_factory=_session_factory(),
            delegated_admin_error=True,
        )

        schedule.assert_not_awaited()
        handler.assert_awaited_once()

    async def test_missing_session_factory_cannot_authorize_a_member(self) -> None:
        """没有 DB 会话时只剩超管一条路径，成员一律放过（只记日志）。"""

        _, schedule, _ = await self._run(
            _message("/ban 555", user_id=781),
            session_factory=None,
            delegated_admin=False,
        )

        schedule.assert_not_awaited()

    async def test_missing_session_factory_still_cleans_the_super_admin(self) -> None:
        """负例的反面：超管不依赖 DB 会话，仍然照旧清理。"""

        _, schedule, _ = await self._run(
            _message("/ban 555", user_id=1),
            settings=_settings(super_admin_id=1),
            session_factory=None,
            delegated_admin=False,
        )

        schedule.assert_awaited_once()

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
