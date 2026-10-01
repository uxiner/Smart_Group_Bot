"""签到提醒增强（「🛒 积分商店」入口 + 已签到名单）的回归测试。

盯住的不变量：

- 名单渲染：1 人 / 多人 / 空（鼓励句）/ 超 20 人（…等 N 人）；昵称一律 HTML
  转义；整条正文 ≤ 4096；
- 名单取数：按签到先后（最早在前）、同一人只出现一次、是**本地**自然日、
  ``count`` 永远是当天总人数（截断只影响列出来的名字）；
- 提醒键盘：一行两个按钮——签到走 callback（``checkin:v1``），商店走 **URL 深链**
  ``t.me/<bot>?start=shop_<群号>``（**没有** ``callback_data``）；拿不到机器人
  用户名时退化成只有一个签到按钮；群里**不再**有任何商店 callback / 发菜单逻辑；
- 私聊 ``/start shop_<群号>``：回私聊菜单（余额取该用户在那个群的可用积分，不扣分），
  末尾说明 /tag /top /draw 仍在群里用；payload 非法 / 群没授权 / 用户不在群里都只
  友好提示；``/start verify…`` 的既有行为一个字都不变；
- 有人签到成功：提醒里的「人数 + 名单」一起刷新，刷新失败不影响签到落库。

所有 Telegram 调用都被 mock，测试不碰真实 Telegram API。
"""

from __future__ import annotations

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
    GroupMember,
    MemberCheckin,
    MemberPointSpend,
)
from bot.handlers import commands
from bot.services import checkin_reminder as reminder_service
from bot.services.checkin import (
    CHECKIN_BUTTON_TEXT,
    CHECKIN_CALLBACK_DATA,
    local_today,
    record_checkin,
    summarize,
)
from bot.services.join_verification import parse_private_verify_group_id
from bot.services.point_shop import award_points
from bot.services.checkin_reminder import (
    CHECKIN_ROSTER_MAX_NAMES,
    CHECKIN_ROSTER_EMPTY_TEXT,
    SHOP_BUTTON_TEXT,
    TELEGRAM_MESSAGE_MAX_CHARS,
    build_checkin_reminder_keyboard,
    parse_shop_start_payload,
    render_checkin_reminder,
    render_checkin_roster,
    shop_deep_link,
    shop_start_payload,
    slot_key,
    today_checkin_roster,
)


class RosterRenderTests(unittest.TestCase):
    """``render_checkin_roster`` / ``render_checkin_reminder`` 的文案规则。"""

    def test_one_name_is_listed_with_the_total_in_parentheses(self) -> None:
        self.assertEqual(
            render_checkin_roster(checked_in=1, names=["ming Li"]),
            "已签到（1）：ming Li",
        )

    def test_several_names_keep_the_given_order(self) -> None:
        line = render_checkin_roster(
            checked_in=3, names=["ming Li", "职业法师刘海柱", "老王"]
        )
        self.assertEqual(line, "已签到（3）：ming Li、职业法师刘海柱、老王")
        self.assertLess(line.index("ming Li"), line.index("职业法师刘海柱"))
        self.assertLess(line.index("职业法师刘海柱"), line.index("老王"))

    def test_empty_roster_shows_the_encouragement(self) -> None:
        self.assertEqual(
            render_checkin_roster(checked_in=0, names=[]), CHECKIN_ROSTER_EMPTY_TEXT
        )
        self.assertIn(
            "还没有人签到，来抢第一个 ☝️",
            render_checkin_reminder(slot=9, checked_in=0, names=[]),
        )

    def test_exactly_twenty_names_are_not_truncated(self) -> None:
        names = [f"u{index}" for index in range(CHECKIN_ROSTER_MAX_NAMES)]
        line = render_checkin_roster(checked_in=20, names=names)
        self.assertNotIn("…等", line)
        self.assertTrue(line.endswith("u19"))
        self.assertIn("已签到（20）：", line)

    def test_more_than_twenty_names_are_truncated_with_a_tail(self) -> None:
        names = [f"u{index}" for index in range(23)]

        line = render_checkin_roster(checked_in=23, names=names)

        self.assertIn("已签到（23）：", line, "括号里是当天总人数")
        self.assertIn("u0、u1", line, "最早签到的必须留下")
        self.assertIn("u19", line)
        self.assertNotIn("u20", line, "第 21 个开始不再列名字")
        self.assertTrue(line.endswith("…等 3 人"), line)

    def test_renderer_also_truncates_when_handed_too_many_names(self) -> None:
        """就算调用方忘了限量，渲染层也只列 20 个。"""

        names = [f"u{index}" for index in range(30)]

        line = render_checkin_roster(checked_in=30, names=names)

        self.assertEqual(line.count("、"), CHECKIN_ROSTER_MAX_NAMES - 1)
        self.assertTrue(line.endswith("…等 10 人"), line)

    def test_nicknames_are_html_escaped(self) -> None:
        line = render_checkin_roster(
            checked_in=2, names=["<b>x</b>", "a & b"]
        )

        self.assertIn("&lt;b&gt;x&lt;/b&gt;", line)
        self.assertIn("a &amp; b", line)
        self.assertNotIn("<b>x</b>", line)
        self.assertNotIn("a & b", line)

    def test_escaped_nicknames_cannot_break_the_whole_message(self) -> None:
        text = render_checkin_reminder(
            slot=9, checked_in=1, names=["</b><a href='x'>boom</a>"]
        )

        self.assertNotIn("</b><a href=", text)
        self.assertIn("&lt;/b&gt;", text)

    def test_roster_line_sits_right_under_the_count_line(self) -> None:
        text = render_checkin_reminder(
            slot=9, checked_in=2, names=["ming Li", "老王"]
        )
        lines = text.splitlines()

        self.assertEqual(
            lines[2], "今日已签到 <b>2</b> 人，点下面的按钮即可签到。"
        )
        self.assertEqual(lines[3], "已签到（2）：ming Li、老王")
        self.assertEqual(lines[4], "本条提醒 10 分钟后自动删除。")

    def test_long_roster_is_trimmed_below_the_telegram_limit(self) -> None:
        """20 个超长昵称转义后会超过 4096：从末尾丢名字，保证发得出去。"""

        names = [f"{index}-" + "x" * 250 for index in range(20)]

        text = render_checkin_reminder(slot=9, checked_in=20, names=names)

        self.assertLessEqual(len(text), TELEGRAM_MESSAGE_MAX_CHARS)
        self.assertIn("已签到（20）：", text)
        self.assertIn("0-", text, "最早的昵称还在")
        self.assertIn("…等", text, "被裁掉的人要折成「…等 N 人」")

    def test_missing_names_with_a_positive_count_never_contradicts_itself(self) -> None:
        """名单为空但人数 > 0（理论上的脏数据）时宁可不显示名单行。"""

        text = render_checkin_reminder(slot=9, checked_in=3, names=[])

        self.assertNotIn("还没有人签到", text)
        self.assertNotIn("已签到（", text)
        self.assertIn("今日已签到 <b>3</b> 人", text)

    def test_default_render_keeps_the_legacy_count_line(self) -> None:
        """不传名单时（老调用方）人数那行逐字不变。"""

        text = render_checkin_reminder(slot=12, checked_in=5)

        self.assertIn("中午好，今天的签到提醒", text)
        self.assertIn("今日已签到 <b>5</b> 人，点下面的按钮即可签到。", text)
        self.assertIn("本条提醒 10 分钟后自动删除。", text)


class ReminderKeyboardContractTests(unittest.TestCase):
    """提醒键盘的两个按钮（与 tests/test_checkin_reminder.py 互为冗余）。"""

    def test_shop_button_is_a_url_deep_link_not_a_callback(self) -> None:
        keyboard = build_checkin_reminder_keyboard(
            bot_username="TestShopBot", group_id=-1001234567890
        )

        self.assertEqual(len(keyboard.inline_keyboard), 1)
        checkin_button, shop_button = keyboard.inline_keyboard[0]
        self.assertEqual(checkin_button.text, CHECKIN_BUTTON_TEXT)
        self.assertEqual(checkin_button.callback_data, CHECKIN_CALLBACK_DATA)
        self.assertEqual(shop_button.text, SHOP_BUTTON_TEXT)
        self.assertIsNone(
            shop_button.callback_data, "商店按钮必须是 URL，不能再是 callback"
        )
        self.assertEqual(
            shop_button.url,
            "https://t.me/TestShopBot?start=shop_-1001234567890",
        )

    def test_deep_link_carries_the_username_and_the_group_id(self) -> None:
        url = shop_deep_link(bot_username="@TestShopBot", group_id=-100)

        self.assertEqual(url, "https://t.me/TestShopBot?start=shop_-100")
        self.assertIn("TestShopBot", url)
        self.assertIn(str(-100), url)
        self.assertTrue(url.endswith(shop_start_payload(-100)))

    def test_deep_link_is_unavailable_without_a_username_or_group(self) -> None:
        for username in ("", "   ", "@", None):
            self.assertIsNone(shop_deep_link(bot_username=username, group_id=-100))
        self.assertIsNone(shop_deep_link(bot_username="TestShopBot", group_id=None))

    def test_keyboard_degrades_to_one_button_without_a_username(self) -> None:
        keyboard = build_checkin_reminder_keyboard(bot_username="", group_id=-100)

        self.assertEqual(
            [button.text for button in keyboard.inline_keyboard[0]],
            [CHECKIN_BUTTON_TEXT],
        )

    def test_no_callback_data_encodes_a_user_id(self) -> None:
        keyboard = build_checkin_reminder_keyboard(
            bot_username="TestShopBot", group_id=-100
        )
        for button in keyboard.inline_keyboard[0]:
            for user_id in (7, 42, 123456789, 999999999):
                self.assertNotIn(str(user_id), button.callback_data or "")

    def test_group_button_callback_is_gone_from_the_router(self) -> None:
        """群里的商店 callback 已经删掉：不该再有任何处理器认领它。"""

        names = [
            getattr(item.callback, "__name__", "")
            for item in commands.router.callback_query.handlers
        ]
        self.assertNotIn("on_reminder_shop_button", names)
        self.assertFalse(hasattr(commands, "on_reminder_shop_button"))
        self.assertFalse(hasattr(reminder_service, "SHOP_CALLBACK_DATA"))


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

    async def _checkin(self, user_id: int, name: str, *, group_id: int = -100) -> None:
        async with self.session_factory() as session:
            await record_checkin(
                session, group_id=group_id, user_id=user_id, display_name=name
            )
            await session.commit()

    async def _spend_rows(self) -> int:
        async with self.session_factory() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(MemberPointSpend)
                    )
                ).scalar()
                or 0
            )

    async def _available(self, user_id: int = 7, *, group_id: int = -100) -> int:
        async with self.session_factory() as session:
            outcome = await summarize(
                session, group_id=group_id, user_id=user_id
            )
            return outcome.available_points


class RosterQueryTests(_DbTestCase):
    """``today_checkin_roster``：顺序、去重、口径、截断。"""

    async def test_empty_day_is_zero_and_no_names(self) -> None:
        async with self.session_factory() as session:
            roster = await today_checkin_roster(session, group_id=-100)

        self.assertEqual(roster.count, 0)
        self.assertEqual(roster.names, ())

    async def test_names_follow_the_checkin_order(self) -> None:
        await self._checkin(9, "老王")
        await self._checkin(7, "ming Li")
        await self._checkin(8, "职业法师刘海柱")

        async with self.session_factory() as session:
            roster = await today_checkin_roster(session, group_id=-100)

        self.assertEqual(roster.count, 3)
        self.assertEqual(
            list(roster.names), ["老王", "ming Li", "职业法师刘海柱"]
        )

    async def test_order_is_reproducible_across_calls(self) -> None:
        for index in range(5):
            await self._checkin(100 + index, f"user{index}")

        async with self.session_factory() as session:
            first = await today_checkin_roster(session, group_id=-100)
        async with self.session_factory() as session:
            second = await today_checkin_roster(session, group_id=-100)

        self.assertEqual(first.names, second.names)

    async def test_one_person_appears_once_even_after_a_repeat_checkin(self) -> None:
        await self._checkin(7, "ming Li")
        await self._checkin(7, "ming Li")  # 第二次是"今天已签"，不该多出一行

        async with self.session_factory() as session:
            roster = await today_checkin_roster(session, group_id=-100)

        self.assertEqual(roster.count, 1)
        self.assertEqual(list(roster.names), ["ming Li"])

    async def test_roster_is_limited_but_the_count_is_the_real_total(self) -> None:
        for index in range(23):
            await self._checkin(200 + index, f"u{index}")

        async with self.session_factory() as session:
            roster = await today_checkin_roster(session, group_id=-100)

        self.assertEqual(roster.count, 23)
        self.assertEqual(len(roster.names), CHECKIN_ROSTER_MAX_NAMES)
        self.assertEqual(
            render_checkin_roster(checked_in=roster.count, names=roster.names),
            "已签到（23）："
            + "、".join(f"u{index}" for index in range(20))
            + "…等 3 人",
        )

    async def test_roster_is_per_group_and_per_local_day(self) -> None:
        await self._authorize(-200)
        await self._checkin(7, "本群的人")
        await self._checkin(7, "别的群的人", group_id=-200)

        async with self.session_factory() as session:
            here = await today_checkin_roster(session, group_id=-100)

        self.assertEqual(here.count, 1)
        self.assertEqual(list(here.names), ["本群的人"])

    async def test_only_today_counts(self) -> None:
        async with self.session_factory() as session:
            session.add(
                MemberCheckin(
                    group_id=-100,
                    user_id=7,
                    checkin_date="2000-01-01",
                    points=1,
                    display_name="古人",
                )
            )
            await session.commit()

        async with self.session_factory() as session:
            roster = await today_checkin_roster(session, group_id=-100)

        self.assertEqual(roster.count, 0)
        self.assertEqual(roster.names, ())

    async def test_blank_display_name_falls_back_to_the_user_id(self) -> None:
        await self._checkin(4242, "")

        async with self.session_factory() as session:
            roster = await today_checkin_roster(session, group_id=-100)

        self.assertEqual(list(roster.names), ["4242"])
        self.assertEqual(
            render_checkin_roster(checked_in=roster.count, names=roster.names),
            "已签到（1）：4242",
            "空昵称要用 user_id 兜底，不能留出「、」这种空条目",
        )


class ShopStartPayloadTests(unittest.TestCase):
    """``shop_<群号>`` 的拼装与解析，以及它与入群验证 payload 的不冲突。"""

    def test_payload_round_trips_for_negative_and_positive_ids(self) -> None:
        for group_id in (-1001234567890, -100, 42):
            payload = shop_start_payload(group_id)
            self.assertEqual(payload, f"shop_{group_id}")
            self.assertEqual(parse_shop_start_payload(payload), group_id)

    def test_valid_payloads(self) -> None:
        self.assertEqual(parse_shop_start_payload("shop_-100123"), -100123)
        self.assertEqual(parse_shop_start_payload("shop_42"), 42)
        self.assertEqual(parse_shop_start_payload("  shop_-100  "), -100)

    def test_invalid_payloads(self) -> None:
        for bad in (
            "",
            "shop_",
            "shop_abc",
            "shop_0",
            "shop_-0",
            "shop_1.5",
            "shop_-",
            "shop_ 12",
            "shop_١٢",
            "start",
            None,
        ):
            self.assertIsNone(parse_shop_start_payload(bad), f"{bad!r} 不该被接受")

    def test_shop_payload_never_collides_with_the_join_verification_payload(self) -> None:
        shop = shop_start_payload(-100123)
        self.assertIsNone(parse_private_verify_group_id(shop))
        self.assertIsNone(parse_private_verify_group_id("shop_100"))
        self.assertIsNone(parse_shop_start_payload("verify"))
        self.assertIsNone(parse_shop_start_payload("verify_n100123"))
        # 验证 payload 的解析行为一个字都没变
        self.assertEqual(parse_private_verify_group_id("verify_n100123"), -100123)
        self.assertEqual(parse_private_verify_group_id("verify_p42"), 42)


class PrivateShopStartTests(_DbTestCase):
    """私聊 ``/start shop_<群号>``：菜单发在私聊，群里不再刷屏。"""

    def _message(
        self, text: str, *, user_id: int = 7, chat_type: str = "private"
    ) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=int(user_id), type=chat_type),
            from_user=SimpleNamespace(
                id=int(user_id), full_name=f"用户{user_id}", is_bot=False
            ),
        )

    async def _add_member(
        self, user_id: int = 7, *, group_id: int = -100, left: bool = False
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                GroupMember(
                    group_id=int(group_id),
                    user_id=int(user_id),
                    full_name=f"用户{user_id}",
                    left=left,
                )
            )
            await session.commit()

    async def _start(self, text: str, *, user_id: int = 7) -> list[str]:
        """走真实的 cmd_start，只把最外层的发送替换成记录。"""

        answers: list[str] = []
        message = self._message(text, user_id=user_id)

        async def fake_answer(_message, _settings, body, **kwargs):
            answers.append(body)

        with patch.object(commands, "_answer", side_effect=fake_answer):
            async with self.session_factory() as session:
                await commands.cmd_start(message, session, SimpleNamespace())
        return answers

    async def test_private_start_shop_replies_the_menu_in_private_chat(self) -> None:
        await self._checkin(7, "ming Li")
        await self._add_member(7)

        answers = await self._start("/start shop_-100")

        self.assertEqual(len(answers), 1, "只回私聊菜单，不再往群里发任何东西")
        menu = answers[0]
        self.assertIn("<b>积分商店</b>", menu)
        self.assertIn("当前可用 <b>1</b> 分", menu)
        # 私聊菜单要指路「积分怎么来」：除了 /checkin，也说清点提醒按钮同样能签到
        self.assertIn("/checkin", menu)
        self.assertIn("签到提醒按钮", menu)
        # 末尾说明：需要群上下文的三个动作仍在群里用
        self.assertIn("/tag", menu)
        self.assertIn("/top", menu)
        self.assertIn("/draw", menu)
        self.assertIn("仍然在群里发", menu)

    async def test_menu_uses_this_users_points_in_that_group(self) -> None:
        await self._authorize(-200)
        await self._add_member(7)
        await self._add_member(7, group_id=-200)
        await self._checkin(7, "本人")
        async with self.session_factory() as session:
            await award_points(
                session,
                group_id=-100,
                user_id=7,
                points=5,
                reason="test_bonus",
                ref="test-bonus:7",
            )
            await session.commit()

        here = await self._start("/start shop_-100")
        there = await self._start("/start shop_-200")

        self.assertIn("当前可用 <b>6</b> 分", here[0])
        self.assertIn("当前可用 <b>0</b> 分", there[0], "另一个群的分不能被串过来")

    async def test_private_menu_never_spends_points(self) -> None:
        await self._checkin(7, "ming Li")
        await self._add_member(7)
        before = await self._available(7)

        answers = await self._start("/start shop_-100")

        self.assertTrue(answers)
        self.assertEqual(await self._spend_rows(), 0, "看菜单不能产生消费流水")
        self.assertEqual(await self._available(7), before)

    async def test_a_broke_member_can_still_look_at_the_menu(self) -> None:
        await self._add_member(7)

        answers = await self._start("/start shop_-100")

        self.assertIn("当前可用 <b>0</b> 分", answers[0])

    async def test_missing_group_id_falls_through_to_the_welcome(self) -> None:
        answers = await self._start("/start shop_")

        self.assertEqual(len(answers), 1)
        self.assertIn("开源信息", answers[0], "解析不出群号就走原来的欢迎文案")
        self.assertNotIn("自定义头衔", answers[0])

    async def test_invalid_payload_falls_through_to_the_welcome(self) -> None:
        for text in ("/start shop_abc", "/start shop_0", "/start", "/start abc"):
            answers = await self._start(text)
            self.assertIn("开源信息", answers[0], text)
            self.assertNotIn("自定义头衔", answers[0], text)

    async def test_unauthorized_group_only_hints(self) -> None:
        await self._add_member(7, group_id=-999)

        answers = await self._start("/start shop_-999")

        self.assertEqual(len(answers), 1)
        self.assertIn("没有授权", answers[0])
        self.assertNotIn("自定义头衔", answers[0], "没授权不能发菜单")

    async def test_user_outside_the_group_only_hints(self) -> None:
        await self._checkin(7, "ming Li")

        answers = await self._start("/start shop_-100")

        self.assertEqual(len(answers), 1)
        self.assertIn("成员", answers[0])
        self.assertNotIn("自定义头衔", answers[0], "不是群成员不能发菜单")

    async def test_a_member_who_left_is_not_a_member_anymore(self) -> None:
        await self._add_member(7, left=True)

        answers = await self._start("/start shop_-100")

        self.assertIn("成员", answers[0])
        self.assertNotIn("自定义头衔", answers[0])

    async def test_menu_render_failure_only_hints(self) -> None:
        await self._add_member(7)

        with patch.object(
            commands, "render_shop_menu", side_effect=RuntimeError("boom")
        ):
            answers = await self._start("/start shop_-100")

        self.assertEqual(len(answers), 1)
        self.assertIn("暂时不可用", answers[0])

    async def test_verify_payload_still_reaches_verification(self) -> None:
        """`/start verify_n…` 必须仍然走入群验证，不被商店分支截胡。"""

        verification = AsyncMock(return_value=True)
        with patch.object(
            commands, "maybe_send_private_verification", new=verification
        ):
            answers = await self._start("/start verify_n100123")

        verification.assert_awaited_once()
        self.assertEqual(
            int(verification.await_args.kwargs["group_id"]), -100123
        )
        self.assertEqual(answers, [], "验证已处理时不该再回欢迎/菜单文案")

    async def test_shop_payload_never_reaches_verification(self) -> None:
        await self._add_member(7)
        verification = AsyncMock(
            side_effect=AssertionError("shop payload 不该进入入群验证")
        )

        with patch.object(
            commands, "maybe_send_private_verification", new=verification
        ):
            answers = await self._start("/start shop_-100")

        verification.assert_not_awaited()
        self.assertIn("<b>积分商店</b>", answers[0])

    async def test_bot_sender_is_ignored(self) -> None:
        await self._add_member(7)
        message = self._message("/start shop_-100")
        message.from_user.is_bot = True
        answers: list[str] = []

        async def fake_answer(_message, _settings, body, **kwargs):
            answers.append(body)

        with patch.object(commands, "_answer", side_effect=fake_answer):
            async with self.session_factory() as session:
                await commands.cmd_start(message, session, SimpleNamespace())

        self.assertEqual(answers, [], "机器人不该拿到菜单，也不该走欢迎文案")


class ReminderRosterRefreshTests(_DbTestCase):
    """点击「✅ 一键签到」后：提醒里的「人数 + 名单」一起刷新。"""

    def _callback(
        self,
        *,
        user_id: int = 7,
        message_id: int = 900,
        edit_side_effect=None,
    ) -> SimpleNamespace:
        message = SimpleNamespace(
            message_id=int(message_id),
            chat=SimpleNamespace(id=-100, type="supergroup"),
            from_user=SimpleNamespace(id=999999, is_bot=True),
            text="提醒原文",
            reply_markup=build_checkin_reminder_keyboard(),
            edit_text=AsyncMock(side_effect=edit_side_effect),
        )
        return SimpleNamespace(
            data=CHECKIN_CALLBACK_DATA,
            from_user=SimpleNamespace(
                id=int(user_id), full_name=f"用户{user_id}", is_bot=False
            ),
            message=message,
            answer=AsyncMock(),
        )

    async def _register_reminder(self, message_id: int = 900) -> None:
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=-100,
                    slot_key=slot_key(local_today(), 9),
                    message_id=int(message_id),
                )
            )
            await session.commit()

    async def _click(self, callback: SimpleNamespace) -> None:
        async with self.session_factory() as session:
            await commands.on_checkin_button(callback, SimpleNamespace(), session)

    async def test_count_and_roster_are_both_refreshed(self) -> None:
        await self._checkin(8, "早到的人")
        await self._register_reminder()
        callback = self._callback(user_id=7)

        await self._click(callback)

        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.await_args.args[0]
        self.assertIn("今日已签到 <b>2</b> 人", text)
        self.assertIn("已签到（2）：早到的人、用户7", text)
        self.assertEqual(
            callback.message.edit_text.await_args.kwargs["parse_mode"], "HTML"
        )
        self.assertIs(
            callback.message.edit_text.await_args.kwargs["reply_markup"],
            callback.message.reply_markup,
            "刷新名单不能把两个按钮弄丢",
        )

    async def test_refresh_failure_never_breaks_the_checkin(self) -> None:
        await self._register_reminder()
        callback = self._callback(
            user_id=7,
            edit_side_effect=RuntimeError("message to edit not found"),
        )

        await self._click(callback)

        callback.answer.assert_awaited_once()
        self.assertIn("签到成功", callback.answer.await_args.args[0])
        async with self.session_factory() as session:
            roster = await today_checkin_roster(session, group_id=-100)
        self.assertEqual(list(roster.names), ["用户7"], "编辑失败不能影响签到落库")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
