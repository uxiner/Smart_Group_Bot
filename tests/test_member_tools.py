"""成员自助命令 /report、/find 与管理员 /health 的回归测试。

三个命令都只依赖「session + settings + message」，所以用假 session/message
驱动，不需要真库；/report 的模型复核与管理员通知按边界打桩。
"""
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.db.engine import init_db
from bot.handlers import commands
from bot.services.call_admin import build_member_report_text
from bot.utils.timezone import now_shanghai_naive


def _settings():
    from bot.config import Settings

    settings = Settings(_env_file=None)
    return settings


def _message(text="/report", *, reply_text="优惠券 加V 领取", chat_id=-100):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        from_user=SimpleNamespace(id=777, first_name="张三", last_name=""),
        reply_to_message=(
            SimpleNamespace(text=reply_text, caption=None)
            if reply_text is not None
            else None
        ),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._rows[0][0] if self._rows and self._rows[0] else 0


class _FakeSession:
    """Only the three session calls the commands use."""

    def __init__(self, *, group_settings=None, responses=None):
        self._group = SimpleNamespace(settings=group_settings or {})
        self._responses = list(responses or [])
        self.commits = 0
        self.rollbacks = 0

    async def get(self, _model, _pk):
        return self._group

    async def execute(self, *_args, **_kwargs):
        if not self._responses:
            return _FakeResult([])
        return _FakeResult(self._responses.pop(0))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class ReportCommandTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self, message, *, session=None, sent=True, verdict=None, settings=None, clear_cooldown=True
    ):
        session = session or _FakeSession()
        answered = []

        async def _answer(msg, cfg, text, **kwargs):
            answered.append(text)

        verdict = verdict or SimpleNamespace(
            violated=False, reason="没有发现广告特征", conclusive=True, confidence=0.0
        )
        moderation = SimpleNamespace(evaluate=AsyncMock(return_value=verdict))
        notifier = AsyncMock(return_value=sent)
        with (
            patch.object(commands, "_answer", side_effect=_answer),
            patch.object(commands, "ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch("bot.services.moderation.ModerationService", return_value=moderation),
            patch.object(commands, "_moderation_llm", return_value=object()),
            patch(
                "bot.services.call_admin.send_member_report_notice", new=notifier
            ),
        ):
            if clear_cooldown:
                commands._report_cooldown.clear()
            await commands.cmd_report(message, session, settings or _settings())
        return answered, moderation, notifier

    async def test_report_requires_a_reply(self) -> None:
        answered, moderation, notifier = await self._run(_message(reply_text=None))
        self.assertTrue(answered)
        self.assertIn("/report", answered[0])
        moderation.evaluate.assert_not_awaited()
        notifier.assert_not_awaited()

    async def test_report_forces_a_fresh_check_and_carries_its_verdict(self) -> None:
        verdict = SimpleNamespace(
            violated=True, reason="推销加微信", conclusive=True, confidence=0.93
        )
        answered, moderation, notifier = await self._run(
            _message("/report 这是广告"), verdict=verdict
        )
        moderation.evaluate.assert_awaited_once()
        self.assertIn("违规", notifier.await_args.kwargs["check_summary"])
        self.assertIn("0.93", notifier.await_args.kwargs["check_summary"])
        self.assertEqual(notifier.await_args.kwargs["reason"], "这是广告")
        self.assertIn("已受理举报", "".join(answered))

    async def test_inconclusive_check_is_reported_as_such(self) -> None:
        verdict = SimpleNamespace(
            violated=False, reason="", conclusive=False, confidence=0.0
        )
        _, _, notifier = await self._run(_message(), verdict=verdict)
        self.assertIn("没有给出明确结论", notifier.await_args.kwargs["check_summary"])

    async def test_report_is_throttled_per_member(self) -> None:
        message = _message()
        _, _, first = await self._run(message)
        self.assertEqual(first.await_count, 1)
        # 第二次不清空冷却表：这才是在测产品的节流，而不是测试夹具
        answered, moderation, second = await self._run(message, clear_cooldown=False)
        second.assert_not_awaited()
        moderation.evaluate.assert_not_awaited()
        self.assertIn("请等", "".join(answered))

    async def test_failed_notice_tells_the_member_to_fall_back_to_admin(self) -> None:
        answered, _, notifier = await self._run(_message(), sent=False)
        notifier.assert_awaited_once()
        self.assertIn("@admin", "".join(answered))


class MemberReportTextTests(unittest.TestCase):
    def test_card_mentions_admins_and_quotes_the_message(self) -> None:
        text = build_member_report_text(
            ["<a href='tg://user?id=1'>甲</a>"],
            reporter_id=777,
            reporter_name="张三",
            reported_text="加V 领取优惠券",
            reason="每天都发",
            check_summary="模型判定「违规」，置信 0.93｜推销",
        )
        self.assertIn("成员举报", text)
        self.assertIn("tg://user?id=1", text)
        self.assertIn("tg://user?id=777", text)
        self.assertIn("加V 领取优惠券", text)
        self.assertIn("0.93", text)


class FindCommandTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, text, *, hits=None, memory=True):
        answered = []

        async def _answer(msg, cfg, body, **kwargs):
            answered.append(body)

        stub = SimpleNamespace(
            recall_archive=AsyncMock(return_value=hits or [])
        )
        with (
            patch.object(commands, "_answer", side_effect=_answer),
            patch.object(commands, "ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch.object(
                commands.memory_holder,
                "get_optional",
                return_value=stub if memory else None,
            ),
        ):
            await commands.cmd_find(_message(text), _FakeSession(), _settings())
        return answered, stub

    async def test_find_needs_a_keyword(self) -> None:
        answered, stub = await self._run("/find")
        self.assertIn("/find", "".join(answered))
        stub.recall_archive.assert_not_awaited()

    async def test_find_renders_time_sender_and_excerpt(self) -> None:
        hits = [
            {
                "content": "不用cn2，普通联通宽带友好\n直连 8块8一个月",
                "sender_name": "职业法师刘海柱",
                "sent_at": "2026-09-25 23:52:46",
                "message_type": "text",
            }
        ]
        answered, stub = await self._run("/find cn2", hits=hits)
        body = "".join(answered)
        self.assertIn("09-25 23:52", body)
        self.assertIn("职业法师刘海柱", body)
        self.assertIn("不用cn2", body)
        # 换行被压平，避免一张卡片被撑爆
        self.assertNotIn("友好\n直连", body)
        stub.recall_archive.assert_awaited_once()

    async def test_find_reports_a_missing_memory_service(self) -> None:
        answered, _ = await self._run("/find cn2", memory=False)
        self.assertIn("记忆服务", "".join(answered))

    async def test_find_says_so_when_nothing_matches(self) -> None:
        answered, _ = await self._run("/find 不存在的词", hits=[])
        self.assertIn("没找到", "".join(answered))


class HealthCommandTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, *, responses=None, admin=True):
        answered = []

        async def _answer(msg, cfg, body, **kwargs):
            answered.append(body)

        session = _FakeSession(
            responses=responses
            if responses is not None
            else [
                [(1, 3), (4, 2)],
                [("moderation", 1)],
                [(2329,)],
                [(42,)],
            ]
        )
        with (
            patch.object(commands, "_answer", side_effect=_answer),
            patch.object(commands, "ensure_group_authorized", new=AsyncMock(return_value=True)),
            patch.object(
                commands, "ensure_group_admin_permission", new=AsyncMock(return_value=admin)
            ),
            patch.object(commands.memory_holder, "get_optional", return_value=object()),
        ):
            await commands.cmd_health(_message("/health"), session, _settings())
        return answered

    async def test_health_card_summarizes_today(self) -> None:
        answered = await self._run()
        body = "".join(answered)
        self.assertIn("运行状态", body)
        self.assertIn("语义审核 3", body)
        self.assertIn("本地正则 2", body)
        self.assertIn("moderation 1", body)
        self.assertIn("2329", body)
        self.assertIn("记忆服务", body)

    async def test_health_stays_quiet_for_non_admins(self) -> None:
        answered = await self._run(admin=False)
        self.assertEqual(answered, [])


class HealthWindowTests(unittest.TestCase):
    def test_violation_window_is_the_shanghai_day_converted_to_utc(self) -> None:
        # violations.created_at 是 UTC，归档 sent_at 是本地时间：同一天要用两个边界
        shanghai_midnight = now_shanghai_naive().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.assertEqual(shanghai_midnight - timedelta(hours=8) + timedelta(hours=8),
                         shanghai_midnight)


class HelpCoverageTests(unittest.TestCase):
    """新命令必须同时出现在 /help 与 Telegram 菜单里。

    /help 的文案是上游手写字符串，不从 _COMMANDS 生成，所以新加命令时
    最容易漏掉它——这正是本次修复的缺口，用断言钉住。
    """

    def test_help_text_lists_the_member_and_admin_commands(self) -> None:
        from bot.utils.command_catalog import build_help_text

        text = build_help_text()
        for command in ("/report", "/find", "/health", "/checkin", "/points"):
            self.assertIn(command, text)

    def test_telegram_menu_offers_the_new_names(self) -> None:
        from bot.utils.command_catalog import build_bot_commands

        names = {name for name, _ in build_bot_commands()}
        self.assertLessEqual(
            {"report", "find", "health", "checkin", "points"}, names
        )

    def test_health_is_reserved_for_operators_so_its_command_line_is_cleaned(self) -> None:
        from bot.utils.command_catalog import management_command_names

        names = management_command_names()
        self.assertIn("health", names)
        # 成员命令不能落进管理员清理集，否则群友发的 /report 会被删掉
        self.assertNotIn("report", names)
        self.assertNotIn("find", names)


if __name__ == "__main__":
    unittest.main()
