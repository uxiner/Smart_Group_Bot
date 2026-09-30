"""签到提醒（定时通知 + 一键签到按钮）的回归测试。

盯住的不变量：

- 四段提醒文案各不相同（早上好/中午好/下午好/晚上好），都带签到规则、
  「今日已签到 N 人」和「10 分钟后自动删除」；
- 按钮 ``callback_data`` 是固定常量，**不含用户 ID**；点击者身份只认 ``from_user``；
- 同一 (群, slot_key) 只发一条（幂等），自动删除必须走持久调度器且是 600 秒；
- 单个群发送失败不影响其它群，且失败群的占位会被撤掉（重试能补发）；
- ``/checkin`` 的卡片与按钮 toast 由**同一个**渲染函数产出；`/checkin` 文案逐字未变。

所有 Telegram 调用都被 mock，测试不碰真实 Telegram API。
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select

from bot.db.engine import init_db
from bot.db.models import (
    AuthorizedGroup,
    CheckinReminderPost,
    MemberCheckin,
    TelegramDeleteJob,
)
from bot.handlers import commands
from bot.services import checkin as checkin_service
from bot.utils import telegram as telegram_utils
from bot.utils.timezone import now_shanghai_naive
from bot.services.checkin import (
    CHECKIN_BUTTON_TEXT,
    CHECKIN_CALLBACK_DATA,
    CHECKIN_TOAST_MAX_CHARS,
    CheckinOutcome,
    local_today,
    record_checkin,
    render_checkin_receipt,
    render_checkin_toast,
)
from bot.services.checkin_reminder import (
    REMINDER_AUTO_DELETE_SECONDS,
    REMINDER_SLOTS,
    SLOT_GREETINGS,
    build_checkin_reminder_keyboard,
    claim_reminder_slot,
    count_checkins_today,
    find_reminder_slot,
    mark_reminder_sent,
    normalize_slot,
    release_reminder_slot,
    render_checkin_reminder,
    slot_key,
)
from bot.tools import checkin_reminder as tool


def _outcome(**overrides) -> CheckinOutcome:
    base = dict(
        already=False,
        points_awarded=3,
        total_points=6,
        spent_points=0,
        available_points=6,
        total_days=3,
        streak=3,
        next_award=4,
        capped=False,
    )
    base.update(overrides)
    return CheckinOutcome(**base)


class _FakeBot:
    """假 Bot：只记调用，不发网络请求。"""

    def __init__(self, send=None) -> None:
        self.token = "123456:TEST-TOKEN"
        self.session = SimpleNamespace(close=AsyncMock())
        self.sent: list[int] = []
        self.returned: list[object] = []
        self.send_message = AsyncMock(
            side_effect=self._send if send is None else send
        )

    async def _send(self, chat_id, text, **kwargs):
        self.sent.append(int(chat_id))
        message = SimpleNamespace(
            message_id=900 + len(self.sent),
            chat=SimpleNamespace(id=int(chat_id)),
            text=text,
            reply_markup=kwargs.get("reply_markup"),
        )
        self.returned.append(message)
        return message


class _FakeCleanupScheduler:
    """假删除调度器：不真的起 worker，但记录被怎么装的。"""

    instances: list["_FakeCleanupScheduler"] = []

    def __init__(self, *, bot, session_factory) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.start = AsyncMock()
        self.stop = AsyncMock()
        _FakeCleanupScheduler.instances.append(self)


class ReminderCopyTests(unittest.TestCase):
    """四段文案：问候语不同，规则/人数/自动删除提示都在。"""

    def test_every_slot_has_its_own_greeting_and_the_required_lines(self) -> None:
        openings = set()
        for slot in REMINDER_SLOTS:
            text = render_checkin_reminder(slot=slot, checked_in=5)
            opening = text.splitlines()[0]
            openings.add(opening)
            self.assertIn(SLOT_GREETINGS[slot], text)
            # 签到规则一句话
            self.assertIn("连续签到第 N 天得 N 分", text)
            self.assertIn("最高 10 分", text)
            self.assertIn("断签从 1 分重新开始，已得积分不清零", text)
            # 今日已签到 N 人
            self.assertIn("今日已签到 <b>5</b> 人", text)
            # 自动删除提示
            self.assertIn("本条提醒 10 分钟后自动删除", text)
        self.assertEqual(len(openings), len(REMINDER_SLOTS), "四个时段开头必须各不相同")

    def test_greetings_match_the_time_of_day(self) -> None:
        self.assertIn("早上好", render_checkin_reminder(slot=9, checked_in=0))
        self.assertIn("中午好", render_checkin_reminder(slot=12, checked_in=0))
        self.assertIn("下午好", render_checkin_reminder(slot=15, checked_in=0))
        self.assertIn("晚上好", render_checkin_reminder(slot=18, checked_in=0))

    def test_copy_never_mentions_everyone(self) -> None:
        for slot in REMINDER_SLOTS:
            text = render_checkin_reminder(slot=slot, checked_in=0)
            self.assertNotIn("@all", text)
            self.assertNotIn("所有人", text)

    def test_negative_counts_are_clamped(self) -> None:
        self.assertIn("今日已签到 <b>0</b> 人", render_checkin_reminder(slot=9, checked_in=-3))

    def test_slot_normalization_only_accepts_the_four_slots(self) -> None:
        for slot in REMINDER_SLOTS:
            self.assertEqual(normalize_slot(slot), slot)
            self.assertEqual(normalize_slot(str(slot)), slot)
        for bad in (0, 7, 24, "abc", None, ""):
            self.assertIsNone(normalize_slot(bad), f"{bad!r} 不该被接受")

    def test_slot_key_is_local_day_plus_slot(self) -> None:
        self.assertEqual(slot_key(local_today(), 9), f"{local_today().isoformat()}:9")


class ReminderKeyboardTests(unittest.TestCase):
    def test_button_is_present_with_a_fixed_callback_data(self) -> None:
        keyboard = build_checkin_reminder_keyboard()
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].text, CHECKIN_BUTTON_TEXT)
        self.assertEqual(buttons[0].callback_data, "checkin:v1")
        self.assertEqual(buttons[0].callback_data, CHECKIN_CALLBACK_DATA)

    def test_callback_data_never_encodes_a_user_id(self) -> None:
        button = build_checkin_reminder_keyboard().inline_keyboard[0][0]
        for user_id in (7, 123456789, 999999999):
            self.assertNotIn(str(user_id), button.callback_data or "")

    def test_button_handler_is_registered_on_the_commands_router(self) -> None:
        names = [
            getattr(item.callback, "__name__", "")
            for item in commands.router.callback_query.handlers
        ]
        self.assertIn(
            "on_checkin_button",
            names,
            "按钮回调没挂上路由：装饰器可能被插到了别的函数上",
        )


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        await self._authorize(-100)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _authorize(self, group_id: int) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=int(group_id), bot_present=True))
            await session.commit()

    async def _posts(self, group_id: int | None = None) -> int:
        async with self.session_factory() as session:
            stmt = select(func.count()).select_from(CheckinReminderPost)
            if group_id is not None:
                stmt = stmt.where(CheckinReminderPost.group_id == int(group_id))
            return int((await session.execute(stmt)).scalar() or 0)

    async def _checkin_users(self, group_id: int = -100) -> set[int]:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(MemberCheckin.user_id).where(
                    MemberCheckin.group_id == int(group_id)
                )
            )
            return {int(value) for (value,) in rows.all()}


class CheckinReminderServiceTests(_DbTestCase):
    async def test_today_count_is_per_group_and_per_local_day(self) -> None:
        async with self.session_factory() as session:
            await record_checkin(session, group_id=-100, user_id=7, display_name="小明")
            await record_checkin(session, group_id=-100, user_id=8, display_name="小红")
            await record_checkin(session, group_id=-200, user_id=7, display_name="小明")
            await session.commit()

        async with self.session_factory() as session:
            self.assertEqual(await count_checkins_today(session, group_id=-100), 2)
            self.assertEqual(await count_checkins_today(session, group_id=-200), 1)
            self.assertEqual(await count_checkins_today(session, group_id=-300), 0)

    async def test_same_slot_can_only_be_claimed_once(self) -> None:
        key = slot_key(local_today(), 9)
        async with self.session_factory() as session:
            first = await claim_reminder_slot(session, group_id=-100, key=key)
            await session.commit()
        async with self.session_factory() as session:
            second = await claim_reminder_slot(session, group_id=-100, key=key)
            await session.commit()

        self.assertTrue(first)
        self.assertFalse(second, "同一 (群, slot_key) 第二次占位必须失败")
        self.assertEqual(await self._posts(-100), 1)

    async def test_different_slots_and_groups_have_their_own_keys(self) -> None:
        async with self.session_factory() as session:
            for group_id in (-100, -200):
                for slot in REMINDER_SLOTS:
                    self.assertTrue(
                        await claim_reminder_slot(
                            session, group_id=group_id, key=slot_key(local_today(), slot)
                        )
                    )
            await session.commit()
        self.assertEqual(await self._posts(), len(REMINDER_SLOTS) * 2)

    async def test_released_slot_can_be_claimed_again(self) -> None:
        key = slot_key(local_today(), 12)
        async with self.session_factory() as session:
            await claim_reminder_slot(session, group_id=-100, key=key)
            await session.commit()
        async with self.session_factory() as session:
            await release_reminder_slot(session, group_id=-100, key=key)
            await session.commit()
        async with self.session_factory() as session:
            reclaimed = await claim_reminder_slot(session, group_id=-100, key=key)
            await session.commit()

        self.assertTrue(reclaimed, "发送失败撤掉占位后，重试必须还能发")
        self.assertEqual(await self._posts(-100), 1)

    async def test_find_reminder_slot_maps_message_back_to_its_slot(self) -> None:
        key = slot_key(local_today(), 18)
        async with self.session_factory() as session:
            await claim_reminder_slot(session, group_id=-100, key=key)
            await mark_reminder_sent(session, group_id=-100, key=key, message_id=4321)
            await session.commit()
        async with self.session_factory() as session:
            self.assertEqual(
                await find_reminder_slot(session, group_id=-100, message_id=4321), 18
            )
            self.assertIsNone(
                await find_reminder_slot(session, group_id=-100, message_id=9999)
            )
            self.assertIsNone(
                await find_reminder_slot(session, group_id=-200, message_id=4321)
            )


class CheckinReminderToolTests(_DbTestCase):
    """CLI 工具的端到端行为：真库 + 假 Bot + 假持久调度器。"""

    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            database_url=f"sqlite+aiosqlite:///{self._db_path}",
            bot=SimpleNamespace(token="123456:TEST-TOKEN"),
            bot_token="123456:TEST-TOKEN",
        )

    async def _run_tool(
        self,
        slot: int = 9,
        *,
        dry_run: bool = False,
        send=None,
        stdout: io.StringIO | None = None,
    ):
        bot = _FakeBot(send=send)
        cleaner = AsyncMock(return_value=True)
        buffer = stdout if stdout is not None else io.StringIO()
        _FakeCleanupScheduler.instances = []
        with (
            patch.object(tool, "Settings", new=lambda *args, **kw: self._settings()),
            patch.object(tool, "Bot", new=lambda token: bot),
            patch.object(
                tool, "TelegramCleanupScheduler", new=_FakeCleanupScheduler
            ),
            patch.object(
                tool, "schedule_message_auto_delete_durable", new=cleaner
            ),
        ):
            with contextlib.redirect_stdout(buffer):
                code = await tool._post(slot, dry_run=dry_run)
        return code, bot, cleaner, buffer.getvalue()

    async def test_sends_one_reminder_per_authorized_group(self) -> None:
        await self._authorize(-200)

        code, bot, _cleaner, output = await self._run_tool(9)

        self.assertEqual(code, 0)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(sorted(bot.sent), [-200, -100])
        for call in bot.send_message.await_args_list:
            self.assertEqual(call.kwargs["parse_mode"], "HTML")
            keyboard = call.kwargs["reply_markup"]
            self.assertEqual(
                keyboard.inline_keyboard[0][0].callback_data, CHECKIN_CALLBACK_DATA
            )
            self.assertIn("早上好", call.args[1])
        self.assertEqual(await self._posts(), 2)
        self.assertIn("已发送提醒", output)

    async def test_second_run_of_the_same_slot_sends_nothing(self) -> None:
        await self._authorize(-200)

        _code1, bot1, cleaner1, _out1 = await self._run_tool(9)
        code2, bot2, cleaner2, out2 = await self._run_tool(9)

        self.assertEqual(bot1.send_message.await_count, 2)
        self.assertEqual(bot2.send_message.await_count, 0, "重跑不能重复发")
        self.assertEqual(await self._posts(), 2)
        self.assertEqual(cleaner1.await_count, 2)
        self.assertEqual(cleaner2.await_count, 0)
        self.assertEqual(code2, 0)
        self.assertIn("已发过，跳过", out2)

    async def test_different_slots_are_independent(self) -> None:
        _c1, bot1, _cl1, _o1 = await self._run_tool(9)
        _c2, bot2, _cl2, _o2 = await self._run_tool(12)

        self.assertEqual(bot1.send_message.await_count, 1)
        self.assertEqual(bot2.send_message.await_count, 1)
        self.assertEqual(await self._posts(), 2)

    async def test_auto_delete_is_scheduled_for_exactly_600_seconds(self) -> None:
        await self._authorize(-200)

        _code, bot, cleaner, _output = await self._run_tool(15)

        self.assertEqual(REMINDER_AUTO_DELETE_SECONDS, 600)
        self.assertEqual(cleaner.await_count, 2)
        for call, message in zip(cleaner.await_args_list, bot.returned):
            self.assertEqual(call.args[0], message, "删的必须就是刚发出去的那条")
            self.assertEqual(call.args[1], 600)

    async def test_cleanup_scheduler_is_started_and_torn_down(self) -> None:
        _code, _bot, _cleaner, _output = await self._run_tool(9)

        self.assertEqual(len(_FakeCleanupScheduler.instances), 1)
        scheduler = _FakeCleanupScheduler.instances[0]
        self.assertEqual(scheduler.bot.token, "123456:TEST-TOKEN")
        scheduler.start.assert_awaited_once()
        scheduler.stop.assert_awaited_once()
        self.assertIsNone(
            telegram_utils._TELEGRAM_CLEANUP_SCHEDULER,
            "进程全局调度器必须被清掉，别留给后续调用方",
        )

    async def test_real_scheduler_persists_a_durable_delete_job(self) -> None:
        """不 mock 调度器：验证 telegram_delete_jobs 里真的多了一行。"""

        bot = _FakeBot()
        with (
            patch.object(tool, "Settings", new=lambda *args, **kw: self._settings()),
            patch.object(tool, "Bot", new=lambda token: bot),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                code = await tool._post(9)

        self.assertEqual(code, 0)
        self.assertEqual(bot.send_message.await_count, 1)
        async with self.session_factory() as session:
            jobs = (
                (await session.execute(select(TelegramDeleteJob))).scalars().all()
            )
        self.assertEqual(len(jobs), 1, "持久删除任务必须落库（重启不丢）")
        self.assertEqual(int(jobs[0].chat_id), -100)
        self.assertEqual(int(jobs[0].message_id), bot.returned[0].message_id)
        remaining = (jobs[0].due_at - now_shanghai_naive()).total_seconds()
        self.assertGreater(remaining, 590)
        self.assertLessEqual(remaining, 600)
        self.assertIsNone(telegram_utils._TELEGRAM_CLEANUP_SCHEDULER)

    async def test_one_group_failure_does_not_block_the_others(self) -> None:
        await self._authorize(-200)
        delivered: list[int] = []

        async def flaky(chat_id, text, **kwargs):
            if int(chat_id) == -100:
                raise RuntimeError("bot was kicked from the group")
            delivered.append(int(chat_id))
            return SimpleNamespace(
                message_id=1234, chat=SimpleNamespace(id=int(chat_id))
            )

        code, _bot, cleaner, output = await self._run_tool(9, send=flaky)

        self.assertEqual(delivered, [-200], "一个群失败不能影响另一个群")
        self.assertEqual(code, 1)
        self.assertIn("发送失败", output)
        self.assertEqual(cleaner.await_count, 1)
        # 失败群的占位要撤掉（下次还能补发），成功群的占位保留（不重复发）
        self.assertEqual(await self._posts(-100), 0)
        self.assertEqual(await self._posts(-200), 1)

    async def test_retry_after_a_failure_still_delivers_the_failed_group(self) -> None:
        await self._authorize(-200)
        first = {"count": 0}
        delivered: list[int] = []

        async def flaky(chat_id, text, **kwargs):
            if int(chat_id) == -100 and first["count"] == 0:
                first["count"] += 1
                raise RuntimeError("transient telegram error")
            delivered.append(int(chat_id))
            return SimpleNamespace(
                message_id=1234, chat=SimpleNamespace(id=int(chat_id))
            )

        _code1, _bot1, _cl1, _o1 = await self._run_tool(9, send=flaky)
        self.assertEqual(delivered, [-200])
        code2, _bot2, _cl2, _o2 = await self._run_tool(9, send=flaky)

        self.assertEqual(code2, 0)
        self.assertEqual(delivered, [-200, -100], "重试只补发失败的那个群")

    async def test_dry_run_prints_targets_and_writes_nothing(self) -> None:
        await self._authorize(-200)
        async with self.session_factory() as session:
            await record_checkin(session, group_id=-100, user_id=7, display_name="小明")
            await session.commit()

        code, bot, cleaner, output = await self._run_tool(9, dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(bot.send_message.await_count, 0)
        self.assertEqual(cleaner.await_count, 0)
        self.assertEqual(await self._posts(), 0, "dry-run 不能写库")
        self.assertIn("group=-100", output)
        self.assertIn("group=-200", output)
        self.assertIn("早上好", output)
        self.assertIn("今日已签到 <b>1</b> 人", output)

    async def test_without_authorized_groups_nothing_is_sent(self) -> None:
        async with self.session_factory() as session:
            await session.execute(AuthorizedGroup.__table__.delete())
            await session.commit()

        code, bot, _cleaner, output = await self._run_tool(9)

        self.assertEqual(code, 0)
        self.assertEqual(bot.send_message.await_count, 0)
        self.assertIn("没有授权群", output)

    async def test_missing_token_fails_loudly(self) -> None:
        settings = self._settings()
        settings.bot.token = ""
        settings.bot_token = ""
        with patch.object(tool, "Settings", new=lambda *args, **kw: settings):
            with contextlib.redirect_stdout(io.StringIO()) as buffer:
                code = await tool._post(9)
        self.assertEqual(code, 1)
        self.assertIn("没有配置 bot token", buffer.getvalue())


class CheckinReminderCliTests(unittest.TestCase):
    def test_slot_outside_the_four_is_rejected(self) -> None:
        for bad in ("7", "0", "abc", ""):
            with contextlib.redirect_stdout(io.StringIO()) as buffer:
                code = tool.main(["--slot", bad])
            self.assertEqual(code, 1, f"--slot {bad!r} 必须被拒绝")
            self.assertIn("--slot 只允许 9/12/15/18", buffer.getvalue())

    def test_missing_slot_is_rejected(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            self.assertEqual(tool.main([]), 1)
        self.assertIn("用法", buffer.getvalue())

    def test_main_dispatches_the_parsed_slot(self) -> None:
        seen: dict[str, object] = {}

        async def fake_post(slot: int, *, dry_run: bool = False) -> int:
            seen["slot"] = slot
            seen["dry_run"] = dry_run
            return 0

        with patch.object(tool, "_post", new=fake_post):
            code = tool.main(["--slot", "18", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertEqual(seen, {"slot": 18, "dry_run": True})


class CheckinButtonCallbackTests(_DbTestCase):
    """一键签到按钮：身份、加分、toast 回执、人数刷新。"""

    def _callback(
        self,
        *,
        user_id: int = 7,
        is_bot: bool = False,
        group_id: int = -100,
        chat_type: str = "supergroup",
        message_id: int = 900,
        edit_side_effect=None,
    ) -> SimpleNamespace:
        message = SimpleNamespace(
            message_id=int(message_id),
            chat=SimpleNamespace(id=int(group_id), type=chat_type),
            # 提醒消息是机器人发的：它的作者身份绝不能被当成点击者
            from_user=SimpleNamespace(id=999999, is_bot=True),
            text="提醒原文",
            reply_markup=build_checkin_reminder_keyboard(),
            edit_text=AsyncMock(side_effect=edit_side_effect),
        )
        return SimpleNamespace(
            data=CHECKIN_CALLBACK_DATA,
            from_user=SimpleNamespace(
                id=int(user_id), full_name=f"用户{user_id}", is_bot=is_bot
            ),
            message=message,
            answer=AsyncMock(),
        )

    async def _click(self, callback: SimpleNamespace) -> None:
        async with self.session_factory() as session:
            await commands.on_checkin_button(
                callback, SimpleNamespace(), session
            )

    async def test_first_click_awards_points_and_toasts_the_receipt(self) -> None:
        callback = self._callback(user_id=7)

        await self._click(callback)

        callback.answer.assert_awaited_once_with(
            "签到成功 · 第 1 天 +1 分｜可用 1 分｜连续 1 天"
        )
        self.assertEqual(await self._checkin_users(), {7})

    async def test_second_click_today_only_toasts_already_done(self) -> None:
        await self._click(self._callback(user_id=7))
        callback = self._callback(user_id=7)

        await self._click(callback)

        callback.answer.assert_awaited_once_with(
            "今天已经签过了｜可用 1 分｜连续 1 天｜明天可得 +2 分"
        )
        self.assertEqual(await self._checkin_users(), {7})

    async def test_identity_comes_from_the_clicker_not_the_message_author(self) -> None:
        await self._click(self._callback(user_id=7))
        await self._click(self._callback(user_id=8))

        self.assertEqual(await self._checkin_users(), {7, 8})
        self.assertNotIn(999999, await self._checkin_users(), "消息作者不该被签到")

    async def test_bot_clicks_are_refused(self) -> None:
        callback = self._callback(user_id=4242, is_bot=True)

        await self._click(callback)

        callback.answer.assert_awaited_once_with("请由群成员本人点击签到")
        self.assertEqual(await self._checkin_users(), set())

    async def test_private_chat_is_refused(self) -> None:
        callback = self._callback(chat_type="private")

        await self._click(callback)

        callback.answer.assert_awaited_once_with("一键签到只能在群里使用")
        self.assertEqual(await self._checkin_users(), set())

    async def test_unauthorized_group_is_refused(self) -> None:
        callback = self._callback(group_id=-999)

        await self._click(callback)

        callback.answer.assert_awaited_once_with("本群尚未授权，暂时无法签到")
        self.assertEqual(await self._checkin_users(group_id=-999), set())

    async def test_missing_session_is_refused(self) -> None:
        callback = self._callback()

        await commands.on_checkin_button(callback, SimpleNamespace(), None)

        callback.answer.assert_awaited_once_with("会话未就绪，请稍后再试")

    async def test_click_refreshes_the_checkin_count_on_the_reminder(self) -> None:
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=-100,
                    slot_key=slot_key(local_today(), 9),
                    message_id=900,
                )
            )
            await session.commit()
        callback = self._callback(user_id=7, message_id=900)

        await self._click(callback)

        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.await_args.args[0]
        self.assertIn("早上好", text)
        self.assertIn("今日已签到 <b>1</b> 人", text)
        self.assertEqual(callback.message.edit_text.await_args.kwargs["parse_mode"], "HTML")
        self.assertIs(
            callback.message.edit_text.await_args.kwargs["reply_markup"],
            callback.message.reply_markup,
            "刷新人数不能把按钮弄丢",
        )

    async def test_non_reminder_message_is_never_edited(self) -> None:
        callback = self._callback(user_id=7, message_id=12345)

        await self._click(callback)

        callback.message.edit_text.assert_not_awaited()
        self.assertEqual(await self._checkin_users(), {7})

    async def test_repeat_click_does_not_edit_the_count(self) -> None:
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=-100,
                    slot_key=slot_key(local_today(), 9),
                    message_id=900,
                )
            )
            await session.commit()
        await self._click(self._callback(user_id=7, message_id=900))

        callback = self._callback(user_id=7, message_id=900)
        await self._click(callback)

        callback.message.edit_text.assert_not_awaited()

    async def test_edit_failure_never_breaks_the_checkin(self) -> None:
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=-100,
                    slot_key=slot_key(local_today(), 9),
                    message_id=900,
                )
            )
            await session.commit()
        callback = self._callback(
            user_id=7, message_id=900, edit_side_effect=RuntimeError("message to edit not found")
        )

        await self._click(callback)

        callback.answer.assert_awaited_once_with(
            "签到成功 · 第 1 天 +1 分｜可用 1 分｜连续 1 天"
        )
        self.assertEqual(await self._checkin_users(), {7}, "编辑失败不能影响签到落库")


class CheckinReceiptSourceTests(_DbTestCase):
    """`/checkin` 与按钮回执同源，且命令文案逐字未变。"""

    async def _run_command(self, text: str = "/checkin") -> list[str]:
        answers: list[str] = []
        message = SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=-100, type="supergroup"),
            from_user=SimpleNamespace(id=7, full_name="小明", is_bot=False),
        )

        async def fake_answer(_message, _settings, body, **kwargs):
            answers.append(body)

        with (
            patch.object(commands, "_answer", side_effect=fake_answer),
            patch.object(
                commands, "schedule_message_auto_delete_durable", new=AsyncMock(return_value=True)
            ),
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ),
        ):
            async with self.session_factory() as session:
                await commands.cmd_checkin(message, session, SimpleNamespace())
        return answers

    async def test_command_success_text_is_byte_identical_to_the_extracted_renderer(self) -> None:
        answers = await self._run_command()
        self.assertEqual(
            answers[-1],
            "<b>签到成功 · 第 1 天 +1 分</b>\n"
            "可用 <b>1</b> 分｜连续 1 天｜共签到 1 天\n"
            "明天签到可得 +2 分。",
        )

    async def test_command_repeat_text_is_byte_identical_to_the_extracted_renderer(self) -> None:
        await self._run_command()
        answers = await self._run_command()
        self.assertEqual(
            answers[-1],
            "<b>今天已经签过了</b>\n"
            "可用 <b>1</b> 分｜连续 1 天｜共签到 1 天\n"
            "明天 0 点后再来，可得 +2 分。",
        )

    def test_both_receipts_are_rendered_by_one_shared_function(self) -> None:
        outcome = _outcome()
        seen: list[CheckinOutcome] = []
        original = checkin_service.checkin_receipt

        def spy(value):
            seen.append(value)
            return original(value)

        with patch.object(checkin_service, "checkin_receipt", side_effect=spy):
            card = render_checkin_receipt(outcome)
            toast = render_checkin_toast(outcome)

        self.assertEqual(seen, [outcome, outcome], "两条入口都要走同一个渲染函数")
        self.assertIn("第 3 天 +3 分", card)
        self.assertIn("第 3 天 +3 分", toast)
        self.assertIn("可用 6 分", toast)
        self.assertIn("可用 <b>6</b> 分", card)

    def test_toasts_stay_short_enough_for_telegram(self) -> None:
        success = render_checkin_toast(_outcome())
        repeated = render_checkin_toast(_outcome(already=True, points_awarded=0))
        capped = render_checkin_toast(_outcome(streak=42, capped=True, next_award=10))

        for toast in (success, repeated, capped):
            self.assertLessEqual(len(toast), CHECKIN_TOAST_MAX_CHARS)
            self.assertNotIn("<b>", toast, "toast 是纯文本，不该带 HTML 标签")

    def test_repeated_toast_offers_tomorrows_award(self) -> None:
        toast = render_checkin_toast(_outcome(already=True, points_awarded=0))
        self.assertEqual(toast, "今天已经签过了｜可用 6 分｜连续 3 天｜明天可得 +4 分")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
