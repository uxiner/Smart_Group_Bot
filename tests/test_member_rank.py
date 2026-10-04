"""积分榜 /rank 与个人档案 /me 的回归测试。

关键行为：
- 默认榜按**可用积分**排（签到流水 − 消费流水），前列并列时签到天数多的在前，
  再按 user_id 稳定排序；
- ``/rank week`` 只统计本周一（Asia/Shanghai）以来的**获得**积分；
- 调用者就算不在 Top10 里，也要拿到自己的名次；
- 全群没有数据时给友好提示，而不是空榜；
- ``/me`` 把签到与违规/封禁状态一次说清（封禁加粗提醒）。

造数据一律用 ``now=`` 注入日期，并按时间顺序调用 ``record_checkin``：每次加多少分
是相对"那一天的连续天数"算出来的，倒着补签会全部只算第 1 天。
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.db.engine import init_db
from bot.db.models import (
    GlobalBan,
    Group,
    MemberCheckin,
    UserWarning,
    Violation,
)
from bot.handlers import commands
from bot.services.checkin import (
    VIOLATION_WINDOW_DAYS,
    build_rank_board,
    local_today,
    member_profile,
    record_checkin,
    spend_points,
)


def _day(offset: int = 0) -> datetime:
    """以「**当前**上海自然日所在周的周二」12:00 为基准的第 offset 天，测试里通过 now= 注入。

    基准必须跟着当前周走：``build_rank_board(week=True)`` 在调用方不传 ``now=`` 时用
    真实时钟算本周一，基准一旦写死日期，跨周之后三个 ``/rank week`` 用例会**永久**
    失败（pending 的 dirty_since 全在上一周，本周榜永远是空的）。

    锚点选周二，因此相对关系与原来完全一致：
    -1 = 本周一（本周内），-2 = 上周日（上周），-4 = 上周五（上周），
    -8 = 上周一 —— 即断言里"上周一到周五连签"的那一段。
    """

    today = local_today()
    monday = today - timedelta(days=today.weekday())
    tuesday = monday + timedelta(days=1)
    return datetime(tuesday.year, tuesday.month, tuesday.day, 12, 0, 0) + timedelta(days=offset)


def _utc(offset_days: float) -> datetime:
    """violations.created_at 存的是 UTC：本地 12:00 减 8 小时再往回推。"""

    return _day() - timedelta(hours=8) - timedelta(days=offset_days)


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _sign(
        self,
        day_offset: int,
        *,
        user_id: int,
        name: str = "",
        group_id: int = -100,
    ):
        """按时间顺序补签（同一个人要从小 offset 到大 offset 依次调用）。"""

        async with self.session_factory() as session:
            outcome = await record_checkin(
                session,
                group_id=group_id,
                user_id=user_id,
                display_name=name or f"用户{user_id}",
                now=_day(day_offset),
            )
            await session.commit()
        return outcome

    async def _insert_checkins(
        self, user_id: int, rows: list[tuple[int, int]], *, group_id: int = -100
    ) -> None:
        """直接写签到流水，用来精确构造"分数相同、天数不同"的并列场景。"""

        async with self.session_factory() as session:
            for day_offset, points in rows:
                session.add(
                    MemberCheckin(
                        group_id=group_id,
                        user_id=user_id,
                        checkin_date=_day(day_offset).date().isoformat(),
                        points=points,
                        display_name=f"用户{user_id}",
                    )
                )
            await session.commit()

    async def _ensure_group(self, group_id: int = -100) -> None:
        """violations 有指向 groups 的外键，造违规数据前先补一行群。"""

        async with self.session_factory() as session:
            if await session.get(Group, group_id) is None:
                session.add(Group(id=group_id, title="测试群", settings={}))
                await session.commit()


class RankBoardServiceTests(_DbTestCase):
    """默认榜：可用积分排序 + 并列规则。"""

    async def test_orders_by_available_points_then_user_id(self) -> None:
        await self._sign(0, user_id=13, name="小美")           # +1 → 1 分，1 天
        await self._sign(-1, user_id=12, name="小刚")          # +1
        await self._sign(0, user_id=12, name="小刚")           # +2 → 3 分，2 天
        await self._sign(-1, user_id=11, name="小红")          # +1
        await self._sign(0, user_id=11, name="小红")           # +2 → 3 分，2 天
        for offset in (-2, -1, 0):
            await self._sign(offset, user_id=10, name="小明")   # +1+2+3 → 6 分，3 天

        async with self.session_factory() as session:
            board = await build_rank_board(session, group_id=-100, user_id=99)

        self.assertEqual([entry.user_id for entry in board.entries], [10, 11, 12, 13])
        self.assertEqual([entry.points for entry in board.entries], [6, 3, 3, 1])
        # 昵称取最近一条签到记录的 display_name
        self.assertEqual(
            [entry.display_name for entry in board.entries], ["小明", "小红", "小刚", "小美"]
        )
        # 11 和 12 同分同天数：按 user_id 稳定排序，结果不能抖
        self.assertEqual(board.members, 4)
        self.assertTrue(board.has_data)
        self.assertEqual(board.caller_rank, 5, "99 没有签到记录，排在所有有效记录之后")
        self.assertEqual(board.caller_points, 0)
        self.assertEqual(board.caller_available, 0)

    async def test_ties_prefer_more_checkin_days_then_user_id(self) -> None:
        await self._insert_checkins(30, [(-2, 5), (-1, 5)])            # 10 分，2 天
        await self._insert_checkins(31, [(-2, 4), (-1, 3), (0, 3)])    # 10 分，3 天
        await self._insert_checkins(32, [(-1, 5), (0, 5)])             # 10 分，2 天

        async with self.session_factory() as session:
            board = await build_rank_board(session, group_id=-100, user_id=30)

        # 31 天数最多排第一；30 和 32 同分同天数，user_id 小的在前
        self.assertEqual([entry.user_id for entry in board.entries], [31, 30, 32])
        self.assertEqual([entry.points for entry in board.entries], [10, 10, 10])
        self.assertEqual(board.caller_rank, 2)

    async def test_spending_points_drops_your_rank(self) -> None:
        for offset in (-2, -1, 0):
            await self._sign(offset, user_id=10, name="小明")   # 6 分
        await self._sign(-1, user_id=11, name="小红")
        await self._sign(0, user_id=11, name="小红")            # 3 分
        await self._sign(0, user_id=12, name="小刚")            # 1 分

        async with self.session_factory() as session:
            before = await build_rank_board(session, group_id=-100, user_id=10)
        self.assertEqual([entry.user_id for entry in before.entries], [10, 11, 12])
        self.assertEqual(before.caller_rank, 1)
        self.assertEqual(before.caller_available, 6)

        async with self.session_factory() as session:
            spent = await spend_points(
                session, group_id=-100, user_id=10, points=5, reason="test", ref="t:1"
            )
            await session.commit()
        self.assertTrue(spent)

        async with self.session_factory() as session:
            after = await build_rank_board(session, group_id=-100, user_id=10)
        # 花掉 5 分后只剩 1 分，被 11 超过；和 12 同为 1 分但签到天数多，排在前面
        self.assertEqual([entry.user_id for entry in after.entries], [11, 10, 12])
        self.assertEqual([entry.points for entry in after.entries], [3, 1, 1])
        self.assertEqual(after.caller_rank, 2)
        self.assertEqual(after.caller_available, 1)


class RankWeekServiceTests(_DbTestCase):
    """``/rank week``：只算本周一以来的获得积分。"""

    async def test_week_board_counts_only_this_week(self) -> None:
        # 上周一到周五连签：+1+2+3+4+5 = 15 分，本周只补了今天一天 +1 分
        for offset in (-8, -7, -6, -5, -4):
            await self._sign(offset, user_id=10, name="小明")
        await self._sign(0, user_id=10, name="小明")
        # 本周一、周二连签 → 本周 3 分
        await self._sign(-1, user_id=11, name="小红")
        await self._sign(0, user_id=11, name="小红")
        # 上周五签过一次，本周没签 → 不进本周榜
        await self._sign(-4, user_id=12, name="小刚")

        async with self.session_factory() as session:
            board = await build_rank_board(session, group_id=-100, user_id=12, week=True)

        self.assertEqual(board.mode, "week")
        self.assertEqual([entry.user_id for entry in board.entries], [11, 10])
        self.assertEqual([entry.points for entry in board.entries], [3, 1], "只算本周获得的积分")
        self.assertEqual(board.members, 2, "本周没签到的人不进榜")
        self.assertEqual(board.caller_rank, 3, "调用者不在榜内也要有名次")
        self.assertEqual(board.caller_points, 0)
        self.assertEqual(board.caller_available, 1, "上周那 1 分仍然算可用积分")

        # 同一批数据的默认榜看的是累计可用积分，不能被本周口径污染
        async with self.session_factory() as session:
            all_board = await build_rank_board(session, group_id=-100, user_id=12)
        self.assertEqual([entry.user_id for entry in all_board.entries], [10, 11, 12])
        self.assertEqual([entry.points for entry in all_board.entries], [16, 3, 1])

    async def test_monday_is_the_week_boundary(self) -> None:
        await self._sign(-2, user_id=20, name="周日用户")   # 9/27 周日 → 上周
        await self._sign(-1, user_id=21, name="周一用户")   # 9/28 周一 → 本周

        async with self.session_factory() as session:
            board = await build_rank_board(session, group_id=-100, user_id=20, week=True)

        self.assertEqual([entry.user_id for entry in board.entries], [21])
        self.assertEqual(board.caller_rank, 2)

    async def test_week_board_without_this_week_data_reports_empty(self) -> None:
        await self._sign(-3, user_id=10, name="小明")   # 只有上周的数据

        async with self.session_factory() as session:
            board = await build_rank_board(session, group_id=-100, user_id=10, week=True)

        self.assertFalse(board.has_data)
        self.assertEqual(board.entries, ())
        self.assertIn("本群本周还没有人签到", commands._render_rank_board(board))


class RankRenderingTests(_DbTestCase):
    """文案层：奖牌、名次行、空榜提示。"""

    async def test_render_marks_top_three_and_always_shows_caller(self) -> None:
        for user_id in range(1, 13):
            await self._sign(0, user_id=user_id)   # 12 个人各 1 分，按 user_id 排

        async with self.session_factory() as session:
            board = await build_rank_board(session, group_id=-100, user_id=12)
        text = commands._render_rank_board(board)

        self.assertIn("<b>本群积分榜</b>", text)
        self.assertIn("🥇 用户1 · 1 分", text)
        self.assertIn("🥈 用户2 · 1 分", text)
        self.assertIn("🥉 用户3 · 1 分", text)
        self.assertIn("4. 用户4 · 1 分", text)
        self.assertEqual(len(board.entries), 10, "只展示 Top10")
        self.assertNotIn("用户11", text)
        self.assertNotIn("用户12", text)
        self.assertIn("你：第 12 名 · 可用 1 分", text, "不在榜内也要显示自己的名次")

    async def test_empty_group_gets_a_friendly_hint(self) -> None:
        async with self.session_factory() as session:
            board = await build_rank_board(session, group_id=-100, user_id=7)
        text = commands._render_rank_board(board)

        self.assertFalse(board.has_data)
        self.assertIn("本群还没有人签到", text)
        self.assertIn("/checkin", text)


class MemberProfileTests(_DbTestCase):
    """``/me`` 的字段来源：签到 + 违规 + 封禁。"""

    async def test_profile_reports_points_checkins_and_violation_window(self) -> None:
        await self._ensure_group(-100)
        await self._ensure_group(-200)
        await self._sign(-1, user_id=7, name="小明")   # +1
        await self._sign(0, user_id=7, name="小明")    # +2 → 3 分，2 天

        async with self.session_factory() as session:
            session.add(UserWarning(group_id=-100, user_id=7, count=3, is_banned=False))
            # 近 30 天内 1 次；40 天前的 1 次不计；别人的、别群的都不算
            session.add(
                Violation(group_id=-100, user_id=7, action_taken="warn", created_at=_utc(5))
            )
            session.add(
                Violation(group_id=-100, user_id=7, action_taken="warn", created_at=_utc(40))
            )
            session.add(
                Violation(group_id=-100, user_id=8, action_taken="warn", created_at=_utc(5))
            )
            session.add(
                Violation(group_id=-200, user_id=7, action_taken="warn", created_at=_utc(5))
            )
            session.add(GlobalBan(user_id=999, reason="广告", source="manual", created_by=0))
            await session.commit()

        async with self.session_factory() as session:
            profile = await member_profile(session, group_id=-100, user_id=7, now=_day())

        self.assertEqual(profile.available_points, 3)
        self.assertEqual(profile.total_points, 3)
        self.assertEqual(profile.spent_points, 0)
        self.assertEqual(profile.streak, 2)
        self.assertEqual(profile.total_days, 2)
        self.assertTrue(profile.signed_today)
        self.assertEqual(profile.next_award, 3, "今天签过了，明天是连续第 3 天 +3 分")
        self.assertEqual(profile.warning_count, 3)
        self.assertEqual(profile.recent_violations, 1)
        self.assertEqual(profile.window_days, VIOLATION_WINDOW_DAYS)
        self.assertFalse(profile.banned)
        self.assertFalse(profile.globally_banned, "别人被全局封禁不能算到自己头上")

    async def test_profile_flags_group_and_global_bans(self) -> None:
        await self._ensure_group(-100)
        async with self.session_factory() as session:
            session.add(UserWarning(group_id=-100, user_id=7, count=1, is_banned=True))
            session.add(
                GlobalBan(user_id=8, reason="spam", source="spam_command", created_by=0)
            )
            await session.commit()

        async with self.session_factory() as session:
            group_banned = await member_profile(session, group_id=-100, user_id=7)
            global_banned = await member_profile(session, group_id=-100, user_id=8)
            clean = await member_profile(session, group_id=-100, user_id=9)

        self.assertTrue(group_banned.group_banned)
        self.assertFalse(group_banned.globally_banned)
        self.assertTrue(group_banned.banned)
        self.assertTrue(global_banned.globally_banned)
        self.assertTrue(global_banned.banned, "全局封禁也算在封禁名单里")
        self.assertEqual(clean.warning_count, 0)
        self.assertEqual(clean.recent_violations, 0)
        self.assertFalse(clean.banned)

    async def test_profile_render_bolds_the_ban(self) -> None:
        await self._ensure_group(-100)
        await self._sign(0, user_id=7, name="小明")
        async with self.session_factory() as session:
            session.add(UserWarning(group_id=-100, user_id=7, count=2, is_banned=True))
            await session.commit()

        async with self.session_factory() as session:
            banned_text = commands._render_member_profile(
                await member_profile(session, group_id=-100, user_id=7, now=_day())
            )
            clean_text = commands._render_member_profile(
                await member_profile(session, group_id=-100, user_id=8, now=_day())
            )

        self.assertIn("<b>我的档案</b>", banned_text)
        self.assertIn("可用积分：<b>1</b> 分", banned_text)
        self.assertIn("连续签到：1 天｜累计签到：1 天", banned_text)
        self.assertIn("今日已签到", banned_text)
        self.assertIn("违规记录：累计 2 次", banned_text)
        self.assertIn("<b>你目前在封禁名单里", banned_text)
        self.assertNotIn("封禁名单", clean_text)
        self.assertIn("今日还没签到，发送 /checkin 可得 +1 分", clean_text)


class MemberCommandTests(_DbTestCase):
    """两个命令接进 router 后的行为（只读、仅群内、需授权）。"""

    def _message(
        self, text: str, *, chat_type: str = "supergroup", user_id: int = 7
    ) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=-100, type=chat_type),
            from_user=SimpleNamespace(id=user_id, full_name="小明", is_bot=False),
        )

    async def _run(
        self,
        text: str,
        *,
        user_id: int = 7,
        authorized: bool = True,
        chat_type: str = "supergroup",
    ) -> list[str]:
        answers: list[str] = []
        message = self._message(text, chat_type=chat_type, user_id=user_id)

        async def fake_answer(message, settings, body, **kwargs):
            answers.append(body)

        with (
            patch.object(commands, "_answer", side_effect=fake_answer),
            patch.object(
                commands,
                "ensure_group_authorized",
                new=AsyncMock(return_value=authorized),
            ),
        ):
            async with self.session_factory() as session:
                handler = commands.cmd_rank if text.startswith("/rank") else commands.cmd_me
                await handler(message, session, _settings())
        return answers

    async def _checkin_rows(self) -> int:
        from sqlalchemy import func, select

        async with self.session_factory() as session:
            return int(
                (
                    await session.execute(
                        select(func.count()).select_from(MemberCheckin)
                    )
                ).scalar()
                or 0
            )

    async def test_rank_command_renders_the_board(self) -> None:
        for user_id in range(1, 13):
            await self._sign(0, user_id=user_id)

        answers = await self._run("/rank", user_id=12)

        self.assertIn("<b>本群积分榜</b>", answers[-1])
        self.assertIn("🥇 用户1 · 1 分", answers[-1])
        self.assertIn("你：第 12 名 · 可用 1 分", answers[-1])

    async def test_rank_week_command_switches_scope(self) -> None:
        for offset in (-8, -7, -6, -5, -4):
            await self._sign(offset, user_id=10, name="小明")
        await self._sign(0, user_id=10, name="小明")
        await self._sign(-1, user_id=11, name="小红")
        await self._sign(0, user_id=11, name="小红")

        answers = await self._run("/rank week", user_id=11)

        self.assertIn("<b>本周积分榜</b>", answers[-1])
        self.assertIn("🥇 小红 · 3 分", answers[-1])
        self.assertIn("你：第 1 名 · 可用 3 分（本周 +3 分）", answers[-1])

    async def test_me_command_reports_profile_and_ban(self) -> None:
        await self._ensure_group(-100)
        await self._sign(-1, user_id=7, name="小明")
        await self._sign(0, user_id=7, name="小明")
        async with self.session_factory() as session:
            session.add(UserWarning(group_id=-100, user_id=7, count=2, is_banned=True))
            await session.commit()

        answers = await self._run("/me", user_id=7)

        self.assertIn("<b>我的档案</b>", answers[-1])
        self.assertIn("可用积分：<b>3</b> 分", answers[-1])
        self.assertIn("违规记录：累计 2 次", answers[-1])
        self.assertIn("<b>你目前在封禁名单里", answers[-1])

    async def test_commands_are_read_only(self) -> None:
        await self._sign(0, user_id=7, name="小明")

        before = await self._checkin_rows()
        await self._run("/rank", user_id=7)
        await self._run("/me", user_id=7)

        self.assertEqual(await self._checkin_rows(), before)

    async def test_commands_are_group_only(self) -> None:
        for text in ("/rank", "/rank week", "/me"):
            answers = await self._run(text, chat_type="private")
            self.assertIn("仅可在群内使用", answers[-1], text)

    async def test_unauthorized_group_gets_nothing(self) -> None:
        for text in ("/rank", "/me"):
            answers = await self._run(text, authorized=False)
            self.assertEqual(answers, [], text)


class RankCatalogTests(unittest.TestCase):
    """新命令必须同时出现在 /help 与 Telegram 命令菜单里（两处都要改）。"""

    def test_help_and_menu_expose_the_new_member_commands(self) -> None:
        from bot.utils.command_catalog import build_bot_commands, build_help_text

        text = build_help_text()
        self.assertIn("/rank：看本群积分榜", text)
        self.assertIn("/me：看自己的积分与违规记录", text)

        names = {name for name, _ in build_bot_commands()}
        self.assertLessEqual({"rank", "me"}, names)


def _settings():
    from bot.config import Settings

    settings = Settings(_env_file=None)
    return settings


if __name__ == "__main__":
    unittest.main()
