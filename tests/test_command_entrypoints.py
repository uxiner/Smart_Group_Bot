import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

from bot.config import Settings
from bot.handlers import admin, commands
from bot.services.skills.base import SkillRunResult
from bot.utils.command_catalog import build_bot_commands, build_command_guide_context


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 3
    settings.bot.auto_delete_categories = ["management"]
    return settings


class _AsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class CommandEntrypointTests(unittest.IsolatedAsyncioTestCase):
    def test_av_command_is_registered_on_router(self) -> None:
        callbacks = [handler.callback for handler in commands.router.message.handlers]

        self.assertIn(commands.cmd_av, callbacks)

    async def test_av_search_releases_db_transaction_before_external_lookup(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=123),
            text="/av test query",
        )
        session = SimpleNamespace(commit=AsyncMock())

        async def assert_committed_before_search(_query: str):
            session.commit.assert_awaited_once()
            return []

        service = SimpleNamespace(
            enabled=True,
            search=AsyncMock(side_effect=assert_committed_before_search),
            lookup_by_code=AsyncMock(),
        )
        group_row = SimpleNamespace(settings={"av_enabled": True})
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.commands.AVSearchService", return_value=service),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_av(message, session=session, settings=_settings())

        service.search.assert_awaited_once_with("test query")
        rendered = answer_mock.await_args.args[2]
        self.assertIn("<b>AV 搜索结果</b>", rendered)
        self.assertIn("<blockquote><b>关键词</b>　<code>test query</code></blockquote>", rendered)
        self.assertIn("未找到匹配内容。", rendered)

    def test_command_reply_and_memory_list_use_selected_layouts(self) -> None:
        action = commands._render_action_response(
            "<b>上下文压缩完成</b>\n已将临时对话历史压缩进背景摘要。"
        )
        text, keyboard = commands._build_memory_list_page(
            [SimpleNamespace(id=42, content="群规优先于临时指令")],
            page=0,
        )

        self.assertEqual(
            action,
            "<b>上下文压缩完成</b>\n\n"
            "<blockquote>已将临时对话历史压缩进背景摘要。</blockquote>",
        )
        self.assertIn("<b>永久记忆</b>", text)
        self.assertIn("<blockquote><b>总数</b>　<code>1</code> 条", text)
        self.assertIn("<code>#42</code>", text)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "lmd:42:0")

    async def test_av_private_search_is_open_to_regular_users_with_rate_limit(self) -> None:
        # 行为变更（需求）：私聊 /av 以前只给最高管理员，现在任何跟机器人私聊过的
        # 用户都可以用，代价是每人每小时 10 次（见 _AV_PRIVATE_RATE_LIMITER）。
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private", title=""),
            from_user=SimpleNamespace(id=123),
            text="/av test query",
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()
        settings.super_admin_id = 999
        service = SimpleNamespace(
            enabled=True,
            search=AsyncMock(return_value=[]),
            lookup_by_code=AsyncMock(),
        )

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.AVSearchService", return_value=service),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_av(message, session=session, settings=settings)

        service.search.assert_awaited_once_with("test query")
        self.assertNotIn("私聊仅最高管理员", answer_mock.await_args.args[2])

    async def test_av_group_search_requires_group_feature_flag(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            from_user=SimpleNamespace(id=123),
            text="/av test query",
        )
        session = SimpleNamespace(commit=AsyncMock())
        group_row = SimpleNamespace(settings={"av_enabled": False})

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._ensure_group_row", new=AsyncMock(return_value=group_row)),
            patch("bot.handlers.commands.AVSearchService") as service_cls,
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_av(message, session=session, settings=_settings())

        session.commit.assert_awaited_once()
        service_cls.assert_not_called()
        self.assertIn("当前群组未启用", answer_mock.await_args.args[2])

    async def test_av_private_search_allows_owner(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=999, type="private", title=""),
            from_user=SimpleNamespace(id=999),
            text="/av test query",
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()
        settings.super_admin_id = 999
        service = SimpleNamespace(
            enabled=True,
            search=AsyncMock(return_value=[]),
            lookup_by_code=AsyncMock(),
        )

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.AVSearchService", return_value=service),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()),
        ):
            await commands.cmd_av(message, session=session, settings=settings)

        session.commit.assert_awaited_once()
        service.search.assert_awaited_once_with("test query")

    async def test_av_private_callback_scope_allows_regular_users(self) -> None:
        # 行为变更（需求）：私聊 /av 放开给普通用户后，私聊按钮也得放开；
        # 真正的把关是「会话归属 + 会话 TTL」，所以过期 token 会得到过期提示，
        # 而不再是「私聊仅最高管理员」。
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private", title=""),
        )
        callback = SimpleNamespace(
            data="avs:legacy-token:0",
            message=message,
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()
        settings.super_admin_id = 999

        with patch(
            "bot.handlers.commands.ensure_group_authorized",
            new=AsyncMock(return_value=True),
        ):
            await commands.on_av_search_paging(
                callback,
                settings=settings,
                session=session,
            )

        session.commit.assert_awaited_once()
        callback.answer.assert_awaited_once_with(
            "查询已过期，请重新 /av 搜索",
            show_alert=True,
        )

    async def test_av_unauthorized_group_callback_uses_alert_only(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="test"),
            answer=AsyncMock(),
        )
        callback = SimpleNamespace(
            data="avs:legacy-token:0",
            message=message,
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())

        with patch(
            "bot.handlers.commands.is_group_authorized",
            new=AsyncMock(return_value=False),
        ):
            await commands.on_av_search_paging(
                callback,
                settings=_settings(),
                session=session,
            )

        callback.answer.assert_awaited_once_with(
            "当前群组未授权",
            show_alert=True,
        )
        message.answer.assert_not_awaited()

    async def test_memory_unauthorized_group_callback_uses_alert_only(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            answer=AsyncMock(),
        )
        callback = SimpleNamespace(
            message=message,
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())

        with patch(
            "bot.handlers.commands.is_group_authorized",
            new=AsyncMock(return_value=False),
        ):
            allowed = await commands._callback_user_can_manage_memories(
                callback,
                session,
                _settings(),
            )

        self.assertFalse(allowed)
        callback.answer.assert_awaited_once_with(
            "当前群组未授权",
            show_alert=True,
        )
        message.answer.assert_not_awaited()

    async def test_rule_unauthorized_group_callback_uses_alert_only(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            answer=AsyncMock(),
        )
        callback = SimpleNamespace(
            message=message,
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())

        with patch(
            "bot.handlers.admin.is_group_authorized",
            new=AsyncMock(return_value=False),
        ):
            allowed = await admin._callback_user_can_manage_rules(
                callback,
                session,
                _settings(),
            )

        self.assertFalse(allowed)
        callback.answer.assert_awaited_once_with(
            "当前群组未授权",
            show_alert=True,
        )
        message.answer.assert_not_awaited()

    async def test_adminlist_unauthorized_group_callback_uses_alert_only(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            answer=AsyncMock(),
        )
        callback = SimpleNamespace(
            data="adl:-10001:0",
            message=message,
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())

        with patch(
            "bot.handlers.admin.is_group_authorized",
            new=AsyncMock(return_value=False),
        ):
            await admin.on_adminlist_paging(
                callback,
                settings=_settings(),
                session=session,
            )

        callback.answer.assert_awaited_once_with(
            "当前群组未授权",
            show_alert=True,
        )
        message.answer.assert_not_awaited()

    async def test_help_uses_shared_command_catalog_text(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/help",
        )
        settings = _settings()

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_help(message, session=object(), settings=settings)

        self.assertIn("命令总览", answer_mock.await_args.args[2])
        self.assertIn("/clearwarnings", answer_mock.await_args.args[2])

    async def test_command_guide_includes_clearwarnings(self) -> None:
        guide = build_command_guide_context()

        self.assertIn("command: /clearwarnings", guide)
        self.assertIn("清空某用户的累计违规次数", guide)
        self.assertIn("command: /raidguard", guide)
        self.assertIn("command: /spam", guide)
        self.assertIn("封禁目标并加入全局封禁名单", guide)

    async def test_raidguard_numeric_argument_uses_minutes(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/raidguard 15",
        )
        service = SimpleNamespace(
            enable_manual_lockdown=AsyncMock(),
            disable_manual_lockdown=AsyncMock(),
            lockdown_status=lambda _group_id: {"active": False},
        )
        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._ensure_ban_command_admin", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.get_raid_guard_service", return_value=service),
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_raidguard(message, session=object(), settings=_settings())

        service.enable_manual_lockdown.assert_awaited_once_with(
            -10001,
            duration_minutes=15,
        )
        self.assertIn("15 分钟", answer_mock.await_args.args[2])

    async def test_raid_guard_release_callback_updates_current_status_only(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            message_id=777,
        )
        callback = SimpleNamespace(
            message=message,
            from_user=SimpleNamespace(id=123),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())
        service = SimpleNamespace(
            lockdown_status_message_matches=lambda group_id, message_id: (
                group_id == -10001 and message_id == 777
            ),
            disable_manual_lockdown=AsyncMock(return_value=True),
        )

        with (
            patch("bot.handlers.admin.is_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.is_group_admin_or_higher", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.get_raid_guard_service", return_value=service),
        ):
            await admin.on_raid_guard_disable_callback(
                callback,
                session=session,
                settings=_settings(),
            )

        service.disable_manual_lockdown.assert_awaited_once_with(-10001)
        callback.answer.assert_awaited_once_with("爆破防护已解除")

    async def test_raid_guard_release_callback_rejects_stale_status_message(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            message_id=776,
        )
        callback = SimpleNamespace(
            message=message,
            from_user=SimpleNamespace(id=123),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())
        service = SimpleNamespace(
            lockdown_status_message_matches=lambda _group_id, _message_id: False,
            disable_manual_lockdown=AsyncMock(),
        )

        with (
            patch("bot.handlers.admin.is_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.is_group_admin_or_higher", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.get_raid_guard_service", return_value=service),
        ):
            await admin.on_raid_guard_disable_callback(
                callback,
                session=session,
                settings=_settings(),
            )

        service.disable_manual_lockdown.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            "防护状态已更新，请使用最新消息操作",
            show_alert=True,
        )

    async def test_raid_guard_release_callback_rejects_unauthorized_group(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            message_id=777,
        )
        callback = SimpleNamespace(
            message=message,
            from_user=SimpleNamespace(id=123),
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())
        service = SimpleNamespace(
            lockdown_status_message_matches=lambda _group_id, _message_id: True,
            disable_manual_lockdown=AsyncMock(),
        )

        with (
            patch("bot.handlers.admin.is_group_authorized", new=AsyncMock(return_value=False)),
            patch("bot.handlers.admin.get_raid_guard_service", return_value=service),
        ):
            await admin.on_raid_guard_disable_callback(
                callback,
                session=session,
                settings=_settings(),
            )

        service.disable_manual_lockdown.assert_not_awaited()
        callback.answer.assert_awaited_once_with(
            "当前群组未授权",
            show_alert=True,
        )

    async def test_settings_entry_allows_authorized_group_admin(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=99, type="private"),
            from_user=SimpleNamespace(id=99),
        )
        settings = _settings()
        settings.miniapp_public_base_url = "https://bot.example.com"
        session = SimpleNamespace(scalar=AsyncMock(return_value=1))
        with patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock:
            await commands.cmd_settings(message, settings=settings, session=session)

        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_seconds"], 0)
        keyboard = answer_mock.await_args.kwargs["reply_markup"]
        self.assertEqual(
            keyboard.inline_keyboard[0][0].web_app.url,
            "https://bot.example.com/settings",
        )

    async def test_clearwarnings_calls_warning_reset_for_target(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/clearwarnings 456",
            reply_to_message=None,
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._clear_user_warning",
                new=AsyncMock(return_value=(2, False)),
            ) as clear_mock,
            patch("bot.handlers.admin._answer", new=AsyncMock()) as answer_mock,
        ):
            await admin.cmd_clearwarnings(message, session=session, settings=settings)

        clear_mock.assert_awaited_once_with(session, -10001, 456)
        self.assertIn("原为 2 次", answer_mock.await_args.args[2])
        session.commit.assert_awaited_once()

    async def test_warning_reset_preserves_banned_state_and_returns_previous_values(self) -> None:
        warning = SimpleNamespace(count=4, is_banned=True)
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(scalar_one_or_none=lambda: warning)
            ),
            delete=AsyncMock(),
        )

        cleared = await admin._clear_user_warning(session, -10001, 456)

        self.assertEqual(cleared, (4, True))
        self.assertEqual(warning.count, 0)
        self.assertTrue(warning.is_banned)
        session.delete.assert_not_awaited()

    async def test_unban_warning_reset_removes_banned_state(self) -> None:
        warning = SimpleNamespace(count=4, is_banned=True)
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(scalar_one_or_none=lambda: warning)
            ),
            delete=AsyncMock(),
        )

        cleared = await admin._clear_user_warning(
            session,
            -10001,
            456,
            preserve_ban=False,
        )

        self.assertEqual(cleared, (4, True))
        session.delete.assert_awaited_once_with(warning)

    async def test_super_admin_unban_prompts_for_scope(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin", full_name="Admin"),
            text="/unban 456",
            reply_to_message=None,
            bot=SimpleNamespace(unban_chat_member=AsyncMock()),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=900)),
        )
        session = SimpleNamespace(commit=AsyncMock())
        settings = _settings()
        settings.super_admin_id = 123

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._answer", new=AsyncMock()),
        ):
            await admin.cmd_unban(message, session=session, settings=settings)

        message.answer.assert_awaited_once()
        kwargs = message.answer.await_args.kwargs
        rendered = message.answer.await_args.args[0]
        self.assertIn("<b>选择解封范围</b>", rendered)
        self.assertIn("<blockquote>请使用下方按钮选择处理范围。</blockquote>", rendered)
        self.assertIn("<blockquote expandable>", rendered)
        self.assertEqual(len(kwargs["reply_markup"].inline_keyboard[0]), 2)
        message.bot.unban_chat_member.assert_not_awaited()

    async def test_super_admin_ban_scope_preserves_replied_message_id(self) -> None:
        sent = SimpleNamespace(message_id=900)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            reply_to_message=SimpleNamespace(message_id=321),
            answer=AsyncMock(return_value=sent),
        )
        key = (-10001, 900)
        admin._BAN_SCOPE_REQUESTS.pop(key, None)
        try:
            await admin._ask_super_admin_scope(
                message,
                action="ban",
                target_id=456,
                reason="广告",
            )

            keyboard = message.answer.await_args.kwargs["reply_markup"]
            callbacks = [
                button.callback_data
                for button in keyboard.inline_keyboard[0]
            ]
            self.assertEqual(
                callbacks,
                ["bsc:b:l:456:321", "bsc:b:g:456:321"],
            )
            request = admin._BAN_SCOPE_REQUESTS[key]
            self.assertEqual(request.target_message_id, 321)
        finally:
            admin._BAN_SCOPE_REQUESTS.pop(key, None)

    def test_spam_command_is_registered_on_router(self) -> None:
        callbacks = [handler.callback for handler in admin.router.message.handlers]

        self.assertIn(admin.cmd_spam, callbacks)

    async def test_spam_command_enqueues_global_ban_with_reply_context(self) -> None:
        progress = SimpleNamespace(message_id=901)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin", full_name="Admin"),
            text="/spam 钓鱼广告",
            reply_to_message=SimpleNamespace(
                message_id=321,
                from_user=SimpleNamespace(id=456, full_name="Spammer"),
            ),
            bot=SimpleNamespace(),
            answer=AsyncMock(return_value=progress),
        )
        session = SimpleNamespace(commit=AsyncMock())
        session_factory = object()

        async def run_inline(*, progress_message, operation, task_title, compact):
            self.assertIs(progress_message, progress)
            self.assertEqual(task_title, "垃圾用户全局封禁")
            self.assertTrue(compact)
            return await operation()

        submission = SimpleNamespace(accepted=True, created=True)
        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._ensure_ban_command_admin",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._ban_target_rejection",
                new=AsyncMock(return_value=""),
            ),
            patch(
                "bot.handlers.admin._queued_operator_can_manage_members",
                new=AsyncMock(return_value=True),
            ) as queued_permission,
            patch(
                "bot.handlers.admin._perform_global_ban",
                new=AsyncMock(return_value="done"),
            ) as perform_global_ban,
            patch("bot.handlers.admin._run_result_job", new=run_inline),
            patch(
                "bot.handlers.admin.submit_privileged_task",
                return_value=submission,
            ) as submit,
        ):
            await admin.cmd_spam(
                message,
                session=session,
                settings=_settings(),
                session_factory=session_factory,
            )

            submit.assert_called_once()
            queued_job = submit.call_args.kwargs["operation"]
            await queued_job()

        submit_kwargs = submit.call_args.kwargs
        self.assertEqual(submit_kwargs["key"], "ban:g:0:456")
        self.assertEqual(submit_kwargs["label"], "global spam ban 456")
        self.assertEqual(submit_kwargs["lane"], "critical_bulk")
        self.assertEqual(submit_kwargs["priority"], 10)
        self.assertEqual(
            submit_kwargs["timeout_seconds"],
            admin._PRIVILEGED_JOB_DEADLINE_SECONDS,
        )
        queued_permission.assert_awaited_once_with(
            bot=message.bot,
            session_factory=session_factory,
            settings=ANY,
            group_id=-10001,
            user_id=123,
        )
        perform_global_ban.assert_awaited_once_with(
            progress,
            session_factory,
            target_id=456,
            reason="钓鱼广告",
            operator_id=123,
            source="spam_command",
            origin_group_id=-10001,
            target_message_id=321,
        )

    async def test_group_ban_queue_rejection_uses_collapsed_error(self) -> None:
        progress = SimpleNamespace(
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
            text="/ban 456",
            reply_to_message=None,
            bot=SimpleNamespace(),
            answer=AsyncMock(return_value=progress),
        )
        session = SimpleNamespace(commit=AsyncMock())

        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._ensure_ban_command_admin",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.submit_privileged_task",
                return_value=SimpleNamespace(accepted=False, created=False),
            ),
        ):
            await admin.cmd_ban(
                message,
                session=session,
                settings=_settings(),
                session_factory=object(),
            )

        text = progress.edit_text.await_args.args[0]
        self.assertIn("<b>本群封禁未入队</b>", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertNotIn("处理结果已返回", text)

    async def test_group_ban_failure_keeps_pending_verification(self) -> None:
        verification = SimpleNamespace(prompt_message_id=321)
        recovery = SimpleNamespace(verification_id=91, lease_until=object())
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
            reply_to_message=SimpleNamespace(
                message_id=444,
                from_user=SimpleNamespace(id=456),
            ),
            bot=SimpleNamespace(delete_message=AsyncMock(return_value=True)),
        )
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

        with (
            patch("bot.handlers.admin._target_is_group_admin", new=AsyncMock(return_value=False)),
            patch("bot.handlers.admin.get_join_verification", new=AsyncMock(return_value=verification)),
            patch(
                "bot.handlers.admin.lease_join_verification_for_unban",
                new=AsyncMock(return_value=recovery),
            ),
            patch(
                "bot.handlers.admin.ban_member",
                new=AsyncMock(side_effect=RuntimeError("denied")),
            ) as ban,
            patch(
                "bot.handlers.admin.complete_leased_join_verification",
                new=AsyncMock(),
            ) as complete,
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()) as delete_prompts,
            patch("bot.handlers.admin.record_ban_event", new=AsyncMock()),
        ):
            text = await admin._perform_group_ban_locked(
                message,
                session,
                _settings(),
                target_id=456,
                reason="test",
            )

        self.assertIn("崩溃恢复工单", text)
        self.assertIn("<blockquote expandable>", text)
        ban.assert_awaited_once_with(message.bot, -10001, 456)
        complete.assert_not_awaited()
        delete_prompts.assert_not_awaited()
        message.bot.delete_message.assert_not_awaited()

    async def test_group_ban_fails_closed_when_admin_status_is_unavailable(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
            reply_to_message=None,
            bot=SimpleNamespace(ban_chat_member=AsyncMock()),
        )
        with patch(
            "bot.handlers.admin._target_is_group_admin",
            new=AsyncMock(return_value=None),
        ):
            text = await admin._perform_group_ban_locked(
                message,
                SimpleNamespace(),
                _settings(),
                target_id=456,
                reason="test",
            )

        self.assertIn("避免误封", text)
        self.assertIn("<blockquote expandable>", text)
        message.bot.ban_chat_member.assert_not_awaited()

    async def test_group_ban_success_cleans_verification_prompt_after_commit(self) -> None:
        verification = SimpleNamespace(prompt_message_id=322)
        recovery = SimpleNamespace(verification_id=92, lease_until=object())
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
            reply_to_message=SimpleNamespace(
                message_id=445,
                from_user=SimpleNamespace(id=456),
            ),
            bot=SimpleNamespace(delete_message=AsyncMock(return_value=True)),
        )
        session = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

        with (
            patch("bot.handlers.admin._target_is_group_admin", new=AsyncMock(return_value=False)),
            patch(
                "bot.handlers.admin.get_join_verification",
                new=AsyncMock(return_value=verification),
            ),
            patch(
                "bot.handlers.admin.lease_join_verification_for_unban",
                new=AsyncMock(return_value=recovery),
            ),
            patch("bot.handlers.admin.ban_member", new=AsyncMock(return_value=True)) as ban,
            patch(
                "bot.handlers.admin.complete_leased_join_verification",
                new=AsyncMock(return_value=True),
            ) as complete,
            patch("bot.handlers.admin._mark_group_banned_after_telegram", new=AsyncMock()) as mark_banned,
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()) as delete_prompts,
            patch("bot.handlers.admin.record_ban_event", new=AsyncMock()),
        ):
            text = await admin._perform_group_ban_locked(
                message,
                session,
                _settings(),
                target_id=456,
                reason="test",
            )

        self.assertEqual(text, "<b>本群封禁完成</b>")
        self.assertNotIn("此操作不会", text)
        self.assertNotIn("<blockquote", text)
        ban.assert_awaited_once_with(message.bot, -10001, 456)
        complete.assert_awaited_once_with(
            session,
            verification_id=92,
            lease_until=recovery.lease_until,
            status="unbanning",
        )
        mark_banned.assert_awaited_once()
        delete_prompts.assert_awaited_once_with(message.bot, {(-10001, 322)})
        message.bot.delete_message.assert_awaited_once_with(
            chat_id=-10001,
            message_id=445,
        )

    async def test_group_unban_only_shows_permission_error_as_collapsed_detail(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
            reply_to_message=None,
            bot=SimpleNamespace(),
        )
        session = SimpleNamespace(
            scalar=AsyncMock(side_effect=[None, None]),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )

        with (
            patch(
                "bot.handlers.admin.is_globally_banned",
                new=AsyncMock(side_effect=[False, False]),
            ),
            patch(
                "bot.handlers.admin.get_join_verification",
                new=AsyncMock(side_effect=[None, None]),
            ),
            patch(
                "bot.handlers.admin.lease_join_verification_for_unban",
                new=AsyncMock(return_value=None),
            ),
            patch("bot.handlers.admin.unban_member", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()),
            patch("bot.handlers.admin.close_private_challenge_messages", new=AsyncMock()),
            patch(
                "bot.handlers.admin.restore_member_permissions",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.admin.record_ban_event", new=AsyncMock()),
        ):
            text = await admin._perform_group_unban_locked(
                message,
                session,
                target_id=456,
            )

        self.assertIn("<b>本群解封完成</b>", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("未确认权限恢复", text)
        self.assertNotIn("此操作不会修改其他群组", text)

    async def test_group_unban_success_only_shows_title(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
            reply_to_message=None,
            bot=SimpleNamespace(),
        )
        session = SimpleNamespace(
            scalar=AsyncMock(side_effect=[None, None]),
            commit=AsyncMock(),
            rollback=AsyncMock(),
        )

        with (
            patch(
                "bot.handlers.admin.is_globally_banned",
                new=AsyncMock(side_effect=[False, False]),
            ),
            patch(
                "bot.handlers.admin.get_join_verification",
                new=AsyncMock(side_effect=[None, None]),
            ),
            patch(
                "bot.handlers.admin.lease_join_verification_for_unban",
                new=AsyncMock(return_value=None),
            ),
            patch("bot.handlers.admin.unban_member", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.delete_verification_prompts", new=AsyncMock()),
            patch("bot.handlers.admin.close_private_challenge_messages", new=AsyncMock()),
            patch(
                "bot.handlers.admin.restore_member_permissions",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.admin.record_ban_event", new=AsyncMock()),
        ):
            text = await admin._perform_group_unban_locked(
                message,
                session,
                target_id=456,
            )

        self.assertEqual(text, "<b>本群解封完成</b>")
        self.assertNotIn("<blockquote", text)
        self.assertNotIn("此操作不会", text)

    async def test_group_unban_global_ban_conflict_is_collapsed(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, full_name="Admin"),
        )

        with patch(
            "bot.handlers.admin.is_globally_banned",
            new=AsyncMock(return_value=True),
        ):
            text = await admin._perform_group_unban_locked(
                message,
                SimpleNamespace(),
                target_id=456,
            )

        self.assertIn("<b>本群解封未完成</b>", text)
        self.assertIn("<blockquote expandable>", text)
        self.assertIn("仍在全局封禁名单", text)

    async def test_lm_list_reply_is_persistent(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/lm",
        )
        settings = _settings()
        fake_memory = SimpleNamespace(list_permanent_memories=AsyncMock(return_value=[]))
        session = SimpleNamespace(commit=AsyncMock())

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_lm(
                message,
                session=session,
                settings=settings,
                session_factory=object(),
            )

        session.commit.assert_awaited_once()
        self.assertEqual(answer_mock.await_args.kwargs["auto_delete_seconds"], 0)
        self.assertIn("永久记忆", answer_mock.await_args.args[2])

    async def test_lm_skill_releases_auth_session_and_uses_factory(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/lm add 记住测试内容",
        )
        settings = _settings()
        events: list[str] = []
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit"))
        )
        session_factory = object()

        async def run_after_commit(*_args: object, **_kwargs: object) -> SkillRunResult:
            self.assertEqual(events, ["commit"])
            events.append("skill")
            return SkillRunResult(
                ok=True,
                skill="memory_manage",
                summary="永久记忆已写入",
            )

        fake_skill = SimpleNamespace(run_skill=AsyncMock(side_effect=run_after_commit))
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch(
                "bot.handlers.commands.memory_holder.get",
                return_value=SimpleNamespace(),
            ),
            patch("bot.handlers.commands._build_skill_service", return_value=fake_skill),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()),
        ):
            await commands.cmd_lm(
                message,
                session=session,
                settings=settings,
                session_factory=session_factory,
            )

        self.assertIsNone(fake_skill.run_skill.await_args.kwargs["session"])
        self.assertIs(
            fake_skill.run_skill.await_args.kwargs["session_factory"],
            session_factory,
        )

    async def test_compact_command_is_registered_on_router(self) -> None:
        callbacks = [handler.callback for handler in commands.router.message.handlers]

        self.assertIn(commands.cmd_compact, callbacks)

    async def test_memory_list_paging_callback_stays_registered(self) -> None:
        callbacks = [handler.callback for handler in commands.router.callback_query.handlers]

        self.assertIn(commands.on_memory_list_paging, callbacks)
        self.assertIn(commands.on_memory_delete, callbacks)

    async def test_compact_releases_auth_session_before_compaction(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/compact",
        )
        settings = _settings()
        events: list[str] = []
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit"))
        )

        async def compact_after_commit(_group_id: int) -> dict:
            self.assertEqual(events, ["commit"])
            events.append("compact")
            return {"status": "ok", "compacted_messages": 7}

        fake_memory = SimpleNamespace(compact_now=AsyncMock(side_effect=compact_after_commit))
        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_compact(message, session=session, settings=settings)

        fake_memory.compact_now.assert_awaited_once_with(-10001)
        self.assertIn("上下文压缩完成", answer_mock.await_args.args[2])
        self.assertIn("7", answer_mock.await_args.args[2])

    async def test_compact_rejects_private_chat(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/compact",
        )
        session = SimpleNamespace(commit=AsyncMock())
        fake_memory = SimpleNamespace(compact_now=AsyncMock())

        with (
            patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_compact(message, session=session, settings=_settings())

        fake_memory.compact_now.assert_not_awaited()
        self.assertIn("请在目标群内使用", answer_mock.await_args.args[2])

    async def test_compact_reports_empty_and_failure_states(self) -> None:
        settings = _settings()
        for status, expected in (
            ("empty", "没有可压缩"),
            ("db_locked", "数据库暂时繁忙"),
            ("llm_empty", "压缩模型未返回摘要"),
        ):
            message = SimpleNamespace(
                chat=SimpleNamespace(id=-10001, type="supergroup"),
                from_user=SimpleNamespace(id=123, username="admin"),
                text="/compact",
            )
            session = SimpleNamespace(commit=AsyncMock())
            fake_memory = SimpleNamespace(
                compact_now=AsyncMock(return_value={"status": status, "compacted_messages": 0})
            )
            with (
                patch("bot.handlers.commands.ensure_group_authorized", new=AsyncMock(return_value=True)),
                patch("bot.handlers.commands.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
                patch("bot.handlers.commands.memory_holder.get", return_value=fake_memory),
                patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
                patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
            ):
                await commands.cmd_compact(message, session=session, settings=settings)

            self.assertIn(expected, answer_mock.await_args.args[2], msg=f"status={status}")

    async def test_addrule_uses_rule_manage_skill(self) -> None:
        progress = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        message = SimpleNamespace(
            message_id=700,
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            from_user=SimpleNamespace(id=123, username="admin"),
            text="/addrule 新增群规 禁止发广告",
            bot=SimpleNamespace(),
            answer=AsyncMock(return_value=progress),
        )
        settings = _settings()
        events: list[str] = []
        submitted: dict[str, object] = {}
        session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: events.append("commit"))
        )
        session_factory = object()

        async def run_after_commit(*_args: object, **_kwargs: object) -> SkillRunResult:
            self.assertEqual(events, ["commit"])
            events.append("skill")
            return SkillRunResult(ok=True, skill="rule_manage", summary="规则添加成功")

        fake_skill = SimpleNamespace(
            run_skill=AsyncMock(side_effect=run_after_commit)
        )

        def submit(**kwargs: object) -> SimpleNamespace:
            submitted.update(kwargs)
            return SimpleNamespace(accepted=True, created=True)

        with (
            patch("bot.handlers.admin.ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin.ensure_group_admin_permission", new=AsyncMock(return_value=True)),
            patch("bot.handlers.admin._build_skill_service", return_value=fake_skill),
            patch(
                "bot.handlers.admin._queued_operator_can_manage_members",
                new=AsyncMock(return_value=True),
            ),
            patch("bot.handlers.admin.submit_privileged_task", side_effect=submit),
            patch(
                "bot.handlers.admin._publish_privileged_result",
                new=AsyncMock(),
            ) as publish,
        ):
            await admin.cmd_addrule(
                message,
                session=session,
                settings=settings,
                session_factory=session_factory,
            )
            await submitted["operation"]()

        message.answer.assert_awaited_once()
        self.assertEqual(submitted["lane"], "policy")
        fake_skill.run_skill.assert_awaited()
        self.assertEqual(fake_skill.run_skill.await_args.args[0], "rule_manage")
        self.assertIsNone(fake_skill.run_skill.await_args.kwargs["session"])
        self.assertIs(
            fake_skill.run_skill.await_args.kwargs["session_factory"],
            session_factory,
        )
        self.assertIn("规则添加成功", publish.await_args.args[1])

    def test_exemptlist_command_and_callbacks_are_registered(self) -> None:
        msg_callbacks = [h.callback for h in admin.router.message.handlers]
        cb_callbacks = [h.callback for h in admin.router.callback_query.handlers]
        self.assertIn(admin.cmd_exemptlist, msg_callbacks)
        self.assertIn(admin.on_exemptlist_paging, cb_callbacks)
        self.assertIn(admin.on_exemptlist_delete, cb_callbacks)
        guide = build_command_guide_context()
        self.assertIn("command: /exemptlist", guide)
        self.assertIn("查看本群审核豁免与回复静默名单", guide)

    def test_moderation_roster_page_renders_combined_list_and_buttons(self) -> None:
        empty_text, empty_kb = admin._build_moderation_roster_page(
            [], mute_all=True, page=0, group_id=-10001
        )
        self.assertIn("<b>审核与静默名单</b>", empty_text)
        self.assertIn("当前没有被豁免审核或被静默回复的成员", empty_text)
        self.assertIn("全群静默", empty_text)
        self.assertIn("<code>-10001</code>", empty_text)
        self.assertIsNone(empty_kb)

        entries = [
            admin._ModerationRosterEntry(
                kind="exempt",
                user_id=101,
                display_name="张三",
                username="zhangsan",
                created_by=999,
                created_at_text="2026-09-28 21:00",
            ),
            admin._ModerationRosterEntry(
                kind="bot",
                user_id=202,
                display_name="通知机器人",
                username="notify_bot",
                created_by=0,
                created_at_text="2026-09-20 12:00",
            ),
            admin._ModerationRosterEntry(
                kind="mute",
                user_id=303,
                display_name="",
                username="",
                created_by=999,
                created_at_text="2026-09-28 22:00",
            ),
        ]
        text, kb = admin._build_moderation_roster_page(
            entries, mute_all=False, page=0, group_id=-10001, group_title="测试群"
        )
        self.assertIn("<b>审核豁免</b>　<code>2</code> 人", text)
        self.assertIn("<b>回复静默</b>　<code>1</code> 人", text)
        self.assertIn("张三 @zhangsan　<code>101</code>", text)
        self.assertIn("类型　审核豁免", text)
        self.assertIn("类型　Bot 白名单", text)
        self.assertIn("（未记录昵称）　<code>303</code>", text)
        self.assertIn("类型　回复静默", text)
        self.assertIn("测试群", text)
        self.assertIsNotNone(kb)
        cb_data = [row[0].callback_data for row in kb.inline_keyboard]
        # Every button carries the group: a card delivered to a DM cannot infer
        # it from the chat it is clicked in.
        self.assertEqual(
            cb_data,
            [
                "exd:exempt:101:0:-10001",
                "exd:bot:202:0:-10001",
                "exd:mute:303:0:-10001",
            ],
        )

    def test_help_catalog_drives_the_telegram_command_menu(self) -> None:
        menu = dict(build_bot_commands())
        self.assertIn("exemptlist", menu)
        self.assertIn("取消", menu["exemptlist"])
        # Argument forms and duplicate base names must not reach Telegram.
        self.assertNotIn("lm add", menu)
        self.assertNotIn("mute all", menu)
        self.assertEqual(len(menu), len(build_bot_commands()))
        for name in menu:
            self.assertNotIn(" ", name)
            self.assertLessEqual(len(menu[name]), 256)

    async def test_exemptlist_in_group_delivers_card_to_private_chat(self) -> None:
        receipt_message = SimpleNamespace(message_id=777)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="测试群"),
            from_user=SimpleNamespace(id=123, username="admin", full_name="管理员"),
            text="/exemptlist",
            message_id=9001,
            bot=SimpleNamespace(),
            answer=AsyncMock(return_value=receipt_message),
        )
        session = SimpleNamespace(
            commit=AsyncMock(),
            get=AsyncMock(return_value=None),
        )
        entries = [
            admin._ModerationRosterEntry(
                kind="exempt",
                user_id=101,
                display_name="张三",
                username="zhangsan",
                created_by=999,
                created_at_text="2026-09-28 21:00",
            )
        ]
        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._load_moderation_roster",
                new=AsyncMock(return_value=(entries, False)),
            ),
            patch(
                "bot.handlers.admin.send_roster_card_private",
                new=AsyncMock(return_value=True),
            ) as deliver,
            patch(
                "bot.handlers.admin.schedule_message_auto_delete_durable",
                new=AsyncMock(return_value=True),
            ) as schedule_delete,
        ):
            await admin.cmd_exemptlist(message, session=session, settings=_settings())

        # The card goes to the operator's DM; the group only sees a receipt that
        # removes itself, so a long roster never clutters the chat.
        self.assertEqual(deliver.await_args.args[1], 123)
        self.assertIn("张三 @zhangsan", deliver.await_args.args[2])
        self.assertIn("-10001", deliver.await_args.args[2])
        message.answer.assert_awaited_once()
        # Only the receipt is scheduled here: the operator's own command line is
        # removed centrally by ManagementCommandCleanupMiddleware.
        schedule_delete.assert_awaited_once()
        self.assertIs(schedule_delete.await_args.args[0], receipt_message)
        self.assertEqual(
            schedule_delete.await_args.args[1],
            admin._ROSTER_NOTICE_AUTO_DELETE_SECONDS,
        )

    async def test_exemptlist_falls_back_to_group_when_dm_is_refused(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup", title="测试群"),
            from_user=SimpleNamespace(id=123, username="admin", full_name="管理员"),
            text="/exemptlist",
            message_id=9001,
            bot=SimpleNamespace(),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock(), get=AsyncMock(return_value=None))
        with (
            patch(
                "bot.handlers.admin.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.ensure_group_admin_permission",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._load_moderation_roster",
                new=AsyncMock(return_value=([], False)),
            ),
            patch(
                "bot.handlers.admin.send_roster_card_private",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "bot.handlers.admin._answer",
                new=AsyncMock(),
            ) as answer_mock,
            patch(
                "bot.handlers.admin.schedule_message_auto_delete_durable",
                new=AsyncMock(return_value=True),
            ) as schedule_delete,
        ):
            await admin.cmd_exemptlist(message, session=session, settings=_settings())

        # A refused DM must not leave the operator with a command that appears
        # to do nothing: the card is posted in the group instead and nothing is
        # scheduled for deletion here (the middleware handles the command line).
        schedule_delete.assert_not_awaited()
        self.assertIn("审核与静默名单", answer_mock.await_args.args[2])

    async def test_private_roster_callback_authorizes_against_payload_group(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=123, type="private"),  # operator's own DM
            edit_text=AsyncMock(),
        )
        callback = SimpleNamespace(
            data="exd:exempt:101:0:-10001",
            message=message,
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        row = SimpleNamespace(id=1, group_id=-10001, user_id=101)
        result = SimpleNamespace(scalar_one_or_none=lambda: row)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=result),
            delete=AsyncMock(),
            commit=AsyncMock(),
            get=AsyncMock(return_value=None),
        )
        with (
            patch(
                "bot.handlers.admin.is_group_authorized",
                new=AsyncMock(return_value=True),
            ) as authorized,
            patch(
                "bot.handlers.admin.is_group_admin_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._load_moderation_roster",
                new=AsyncMock(return_value=([], False)),
            ),
        ):
            await admin.on_exemptlist_delete(
                callback, settings=_settings(), session=session
            )

        # -10001 comes from the button, not from the DM chat id, and the
        # permission checks run against it.
        self.assertEqual(authorized.await_args.args[1], -10001)
        session.delete.assert_awaited_once_with(row)
        message.edit_text.assert_awaited_once()
        callback.answer.assert_awaited_once_with("已取消豁免: 101")

    async def test_private_roster_callback_rejects_unauthorized_operator(self) -> None:
        message = SimpleNamespace(chat=SimpleNamespace(id=123, type="private"))
        callback = SimpleNamespace(
            data="exd:exempt:101:0:-10001",
            message=message,
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        session = SimpleNamespace(commit=AsyncMock())
        with (
            patch(
                "bot.handlers.admin.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin.is_group_admin_authorized",
                new=AsyncMock(return_value=False),
            ),
        ):
            await admin.on_exemptlist_delete(
                callback, settings=_settings(), session=session
            )
        callback.answer.assert_awaited_once_with("仅群管理可操作该列表", show_alert=True)

    async def test_exemptlist_delete_callback_removes_entry_and_refreshes(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=-10001, type="supergroup"),
            reply_markup=None,
            edit_text=AsyncMock(),
        )
        callback = SimpleNamespace(
            data="exd:exempt:101:0",
            message=message,
            from_user=SimpleNamespace(id=123),
            answer=AsyncMock(),
        )
        row = SimpleNamespace(id=1, group_id=-10001, user_id=101)
        result = SimpleNamespace(scalar_one_or_none=lambda: row)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=result),
            delete=AsyncMock(),
            commit=AsyncMock(),
            get=AsyncMock(return_value=None),
        )
        remaining = [
            admin._ModerationRosterEntry(
                kind="mute",
                user_id=303,
                display_name="李四",
                username="lisi",
                created_by=999,
                created_at_text="2026-09-28 22:00",
            )
        ]
        with (
            patch(
                "bot.handlers.admin._callback_user_can_manage_group",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.admin._load_moderation_roster",
                new=AsyncMock(return_value=(remaining, False)),
            ),
        ):
            await admin.on_exemptlist_delete(
                callback, settings=_settings(), session=session
            )
        session.delete.assert_awaited_once_with(row)
        message.edit_text.assert_awaited_once()
        edited_text = message.edit_text.await_args.args[0]
        self.assertIn("李四 @lisi", edited_text)
        callback.answer.assert_awaited_once_with("已取消豁免: 101")


if __name__ == "__main__":
    unittest.main()
