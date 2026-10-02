"""每周活跃激励（功能 E）的回归测试。

测的不是"函数能跑"，而是几个会直接影响成员钱包与观感的数字：

- 每天 20 条封顶：第 21 条不再累加（防刷屏要在写入时就成立）；
- 有效消息过滤：命令、机器人、频道身份、管理员、长度 < 2 都不算；
- 门槛：活跃天数 < 3 或发言 < 10 条不发奖；
- 排名与奖励：并列时按发言数、再按 user_id 稳定排序，满榜时总额正好 77 分；
- 幂等：同一周结算两次只加一次分，第二次不抛异常；
- 积分口径：可用余额把奖励算进去，但**不碰**签到连击（不能用假签到发奖）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine, func, inspect, select

from bot.db.engine import init_db
from bot.db.models import (
    Base,
    MemberActivityDaily,
    MemberCheckin,
    MemberPointAward,
)
from bot.handlers import group
from bot.services.activity import (
    MAX_DAILY_MESSAGES,
    MIN_ACTIVE_DAYS,
    WEEKLY_TOTAL_POINTS,
    _upsert_daily,
    _write_award,
    activity_score,
    is_countable_message,
    last_complete_week,
    rank_week,
    record_message_activity,
    render_activity_lines,
    settle_weekly_activity,
    week_key,
)
from bot.services.checkin import (
    available_points,
    build_rank_board,
    record_checkin,
    spend_points,
    summarize,
)

GROUP_ID = -1001364206062
# 2026-09-28 是周一、2026-09-21 是上一个周一：用固定的"上周"，免得测试跟着真实日期漂
WEEK_MONDAY = date(2026, 9, 28)
LAST_WEEK_MONDAY = date(2026, 9, 21)
# 落库用的"当前时间"：2026-09-30（周三，属于 WEEK_MONDAY 那一周）
NOW = datetime(2026, 9, 30, 12, 0, 0)


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

    async def _seed_day(
        self,
        session,
        *,
        user_id: int,
        day: date,
        messages: int = 0,
        replies: int = 0,
        name: str = "",
    ) -> None:
        """直接按天铺数据（等价于那一天真的说了这么多话）。"""

        await _upsert_daily(
            session,
            group_id=GROUP_ID,
            user_id=user_id,
            activity_date=day.isoformat(),
            messages=messages,
            replies_received=replies,
            display_name=name or f"成员{user_id}",
        )

    async def _award_rows(self) -> list[tuple[int, int, str]]:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(
                    MemberPointAward.user_id,
                    MemberPointAward.points,
                    MemberPointAward.ref,
                )
            )
            return sorted((int(u), int(p), str(r)) for u, p, r in rows.all())


# ---------------------------------------------------------------------------
# 表结构：幂等和"一天一行"都靠唯一索引兜底，不能被后人当普通索引改掉
# ---------------------------------------------------------------------------


class ActivitySchemaTests(unittest.TestCase):
    def test_unique_indexes_guard_daily_row_and_award_idempotency(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        daily = {
            index["name"]: index
            for index in inspector.get_indexes("member_activity_daily")
        }
        self.assertTrue(daily["ix_member_activity_daily_day"]["unique"])
        self.assertEqual(
            daily["ix_member_activity_daily_day"]["column_names"],
            ["group_id", "user_id", "activity_date"],
        )

        awards = {
            index["name"]: index
            for index in inspector.get_indexes("member_point_awards")
        }
        # "同一周只发一次"的保证就在这个唯一索引上（也是 ON CONFLICT DO NOTHING 的目标）
        self.assertTrue(awards["ix_member_point_awards_ref"]["unique"])
        self.assertEqual(
            awards["ix_member_point_awards_ref"]["column_names"],
            ["group_id", "user_id", "ref"],
        )


# ---------------------------------------------------------------------------
# 有效消息的判定（纯函数，不碰数据库）
# ---------------------------------------------------------------------------


class ActivityMessageFilterTests(unittest.TestCase):
    def test_normal_member_text_counts(self) -> None:
        self.assertTrue(is_countable_message(text="这个套餐怎么样？"))
        self.assertTrue(is_countable_message(text="ab"))
        self.assertTrue(is_countable_message(text="  好耶  "))

    def test_too_short_text_does_not_count(self) -> None:
        for raw in ("", " ", "  ", "a", "。", "\n", "\t ", " 好 "):
            self.assertFalse(
                is_countable_message(text=raw), f"{raw!r} 不该算有效发言"
            )

    def test_commands_do_not_count(self) -> None:
        for raw in ("/rank", "/checkin 7", "  /me", "/"):
            self.assertFalse(
                is_countable_message(text=raw), f"{raw!r} 是命令，不该算发言"
            )

    def test_bot_channel_and_admin_messages_do_not_count(self) -> None:
        body = "这是一条正常长度的消息"
        self.assertFalse(is_countable_message(text=body, is_bot=True))
        self.assertFalse(is_countable_message(text=body, is_channel=True))
        self.assertFalse(is_countable_message(text=body, is_admin=True))

    def test_only_plain_text_messages_count(self) -> None:
        self.assertFalse(
            is_countable_message(text="看这张图", message_type="photo_caption")
        )
        self.assertFalse(
            is_countable_message(text="[sticker 😀]", message_type="sticker")
        )
        self.assertTrue(is_countable_message(text="看这张图", message_type="text"))

    def test_score_formula(self) -> None:
        self.assertEqual(
            activity_score(messages=30, active_days=5, replies_received=4), 44
        )
        self.assertEqual(activity_score(messages=0, active_days=0, replies_received=0), 0)


# ---------------------------------------------------------------------------
# 日累计：封顶、被回复、按天分行、过滤
# ---------------------------------------------------------------------------


class ActivityDailyRecordTests(_DbTestCase):
    async def _daily(self, user_id: int = 7) -> MemberActivityDaily | None:
        async with self.session_factory() as session:
            return (
                (
                    await session.execute(
                        select(MemberActivityDaily).where(
                            MemberActivityDaily.user_id == user_id,
                            MemberActivityDaily.group_id == GROUP_ID,
                        )
                    )
                )
                .scalars()
                .first()
            )

    async def test_daily_messages_are_capped_at_twenty(self) -> None:
        async with self.session_factory() as session:
            for index in range(25):
                await record_message_activity(
                    session,
                    group_id=GROUP_ID,
                    user_id=7,
                    text=f"第 {index} 句话",
                    display_name="小明",
                    now=NOW,
                )
            await session.commit()

        row = await self._daily()
        self.assertIsNotNone(row)
        self.assertEqual(row.messages, MAX_DAILY_MESSAGES, "第 21 条起不该再累加")
        self.assertEqual(row.display_name, "小明")

    async def test_cap_holds_across_repeat_writes(self) -> None:
        async with self.session_factory() as session:
            await _upsert_daily(
                session,
                group_id=GROUP_ID,
                user_id=7,
                activity_date=NOW.date().isoformat(),
                messages=18,
            )
            await session.commit()
        async with self.session_factory() as session:
            # 一次再写 10 条：只能补到 20，不能到 28
            await _upsert_daily(
                session,
                group_id=GROUP_ID,
                user_id=7,
                activity_date=NOW.date().isoformat(),
                messages=10,
            )
            await session.commit()

        row = await self._daily()
        self.assertEqual(row.messages, MAX_DAILY_MESSAGES)

    async def test_replies_received_are_credited_to_the_replied_member(self) -> None:
        async with self.session_factory() as session:
            await record_message_activity(
                session,
                group_id=GROUP_ID,
                user_id=8,
                text="小明你说的那个问题解决了没",
                display_name="小红",
                reply_to_user_id=7,
                now=NOW,
            )
            await session.commit()

        replier = await self._daily(8)
        target = await self._daily(7)
        self.assertEqual(replier.messages, 1)
        self.assertEqual(replier.replies_received, 0, "回复别人不会被算成被回复")
        self.assertEqual(target.messages, 0, "被回复不涨发言条数")
        self.assertEqual(target.replies_received, 1)

    async def test_self_reply_does_not_count_as_a_reply(self) -> None:
        async with self.session_factory() as session:
            await record_message_activity(
                session,
                group_id=GROUP_ID,
                user_id=7,
                text="补充一句",
                reply_to_user_id=7,
                now=NOW,
            )
            await session.commit()

        row = await self._daily(7)
        self.assertEqual(row.replies_received, 0)

    async def test_filtered_messages_never_create_rows(self) -> None:
        cases = (
            {"user_id": 1, "text": "/rank"},                          # 命令
            {"user_id": 2, "text": "a"},                              # 太短
            {"user_id": 3, "text": "正常发言", "is_bot": True},        # 机器人
            {"user_id": 4, "text": "正常发言", "is_channel": True},    # 频道身份
            {"user_id": 5, "text": "正常发言", "is_admin": True},      # 管理员
            {"user_id": 6, "text": "照片说明", "message_type": "photo_caption"},
        )
        async with self.session_factory() as session:
            for case in cases:
                recorded = await record_message_activity(
                    session, group_id=GROUP_ID, now=NOW, **case
                )
                self.assertFalse(recorded, case)
            await session.commit()

        async with self.session_factory() as session:
            rows = (await session.execute(select(MemberActivityDaily))).scalars().all()
        self.assertEqual(list(rows), [], "被过滤的消息不该留下任何行")

    async def test_each_local_day_gets_its_own_row(self) -> None:
        async with self.session_factory() as session:
            for offset in range(3):
                await record_message_activity(
                    session,
                    group_id=GROUP_ID,
                    user_id=7,
                    text="今天也来聊两句",
                    now=NOW + timedelta(days=offset),
                )
            await session.commit()

        async with self.session_factory() as session:
            days = (
                (
                    await session.execute(
                        select(MemberActivityDaily.activity_date)
                        .where(MemberActivityDaily.user_id == 7)
                        .order_by(MemberActivityDaily.activity_date)
                    )
                )
                .scalars()
                .all()
            )
        self.assertEqual(list(days), ["2026-09-30", "2026-10-01", "2026-10-02"])


# ---------------------------------------------------------------------------
# 排名与发奖
# ---------------------------------------------------------------------------


class WeeklySettlementTests(_DbTestCase):
    async def _settle(self, **kwargs):
        async with self.session_factory() as session:
            result = await settle_weekly_activity(
                session, group_id=GROUP_ID, week_start=LAST_WEEK_MONDAY, **kwargs
            )
            await session.commit()
        return result

    async def test_threshold_needs_three_days_and_ten_messages(self) -> None:
        async with self.session_factory() as session:
            # 达标：3 天 × 4 条 = 12 条
            for offset in range(MIN_ACTIVE_DAYS):
                await self._seed_day(
                    session,
                    user_id=1,
                    day=LAST_WEEK_MONDAY + timedelta(days=offset),
                    messages=4,
                )
            # 不达标：发言很多，但只活跃了 2 天
            for offset in range(MIN_ACTIVE_DAYS - 1):
                await self._seed_day(
                    session,
                    user_id=2,
                    day=LAST_WEEK_MONDAY + timedelta(days=offset),
                    messages=20,
                )
            # 不达标：活跃天数够，但只有 9 条
            for offset in range(MIN_ACTIVE_DAYS):
                await self._seed_day(
                    session,
                    user_id=3,
                    day=LAST_WEEK_MONDAY + timedelta(days=offset),
                    messages=3,
                )
            await session.commit()

        result = await self._settle()
        self.assertEqual([entry.user_id for entry in result.entries], [1])
        self.assertEqual(result.participants, 3, "参与人数含不达标的")
        self.assertEqual(result.qualified, 1)
        self.assertEqual(result.total_points, 25, "只有第 1 名时总额就是 25 分")

    async def test_scores_and_tie_break_by_messages_then_user_id(self) -> None:
        async with self.session_factory() as session:
            # user 3 与 user 9 同分（都是 27）：user 9 发言更多 → 排在前面
            # user 3：15 条 + 6 天次 + 6 被回复 = 27；user 9：18 条 + 6 天次 + 3 被回复 = 27
            for offset in range(MIN_ACTIVE_DAYS):
                await self._seed_day(
                    session,
                    user_id=3,
                    day=LAST_WEEK_MONDAY + timedelta(days=offset),
                    messages=5,
                    replies=2,
                )
                await self._seed_day(
                    session,
                    user_id=9,
                    day=LAST_WEEK_MONDAY + timedelta(days=offset),
                    messages=5,
                    replies=1,
                )
            await self._seed_day(session, user_id=9, day=LAST_WEEK_MONDAY, messages=3)
            # 完全一样的两个人：只能按 user_id 升序
            for user_id in (7, 5):
                for offset in range(MIN_ACTIVE_DAYS):
                    await self._seed_day(
                        session,
                        user_id=user_id,
                        day=LAST_WEEK_MONDAY + timedelta(days=offset),
                        messages=4,
                    )
            await session.commit()

        result = await self._settle()
        self.assertEqual([entry.user_id for entry in result.entries], [9, 3, 5, 7])
        by_user = {entry.user_id: entry for entry in result.entries}
        self.assertEqual(by_user[9].score, by_user[3].score, "这两人必须同分")
        self.assertGreater(
            by_user[9].messages,
            by_user[3].messages,
            "同分时发言多的在前",
        )
        self.assertEqual(by_user[5].score, by_user[7].score, "这两人必须同分")
        for entry in result.entries:
            self.assertEqual(
                entry.score,
                entry.messages + 2 * entry.active_days + entry.replies_received,
            )

    async def test_full_board_pays_exactly_77_points(self) -> None:
        async with self.session_factory() as session:
            # 12 个人达标（发言数递减，名次不并列），只有前 10 名有奖
            for user_id in range(1, 13):
                per_day = 18 - user_id
                for offset in range(MIN_ACTIVE_DAYS):
                    await self._seed_day(
                        session,
                        user_id=user_id,
                        day=LAST_WEEK_MONDAY + timedelta(days=offset),
                        messages=per_day,
                    )
            await session.commit()

        result = await self._settle()
        self.assertEqual(len(result.entries), 10)
        self.assertEqual(result.qualified, 10, "榜单只取前 10 名")
        self.assertEqual(result.total_points, WEEKLY_TOTAL_POINTS)
        self.assertEqual(result.awarded_points, WEEKLY_TOTAL_POINTS)
        self.assertEqual(
            [entry.points for entry in result.entries],
            [25, 12, 12, 4, 4, 4, 4, 4, 4, 4],
        )
        self.assertEqual(
            sum(entry.points for entry in result.entries), WEEKLY_TOTAL_POINTS
        )

    async def test_second_settlement_of_the_same_week_awards_nothing(self) -> None:
        async with self.session_factory() as session:
            for user_id in (1, 2):
                for offset in range(MIN_ACTIVE_DAYS):
                    await self._seed_day(
                        session,
                        user_id=user_id,
                        day=LAST_WEEK_MONDAY + timedelta(days=offset),
                        messages=5,
                    )
            await session.commit()

        first = await self._settle()
        rows_after_first = await self._award_rows()
        second = await self._settle()  # 重复结算不能报错

        self.assertEqual(first.awarded_points, 25 + 12)
        self.assertEqual(second.awarded_points, 0, "同一周不能重复加分")
        self.assertEqual([entry.awarded for entry in second.entries], [False, False])
        self.assertEqual(await self._award_rows(), rows_after_first)
        self.assertEqual(len(rows_after_first), 2)
        for _user_id, _points, ref in rows_after_first:
            self.assertTrue(ref.startswith("weekly-activity:2026-W39:"))

    async def test_dry_run_does_not_write_any_award(self) -> None:
        async with self.session_factory() as session:
            for offset in range(MIN_ACTIVE_DAYS):
                await self._seed_day(
                    session,
                    user_id=1,
                    day=LAST_WEEK_MONDAY + timedelta(days=offset),
                    messages=5,
                )
            await session.commit()

        result = await self._settle(award=False)
        self.assertEqual(result.total_points, 25)
        self.assertEqual(result.awarded_points, 0)
        self.assertEqual(await self._award_rows(), [])

    async def test_only_the_target_week_is_counted(self) -> None:
        async with self.session_factory() as session:
            # 上周（要结算的）
            for offset in range(MIN_ACTIVE_DAYS):
                await self._seed_day(
                    session,
                    user_id=1,
                    day=LAST_WEEK_MONDAY + timedelta(days=offset),
                    messages=5,
                )
            # 本周（不能算进来）
            for offset in range(MIN_ACTIVE_DAYS):
                await self._seed_day(
                    session,
                    user_id=2,
                    day=WEEK_MONDAY + timedelta(days=offset),
                    messages=5,
                )
            await session.commit()

        result = await self._settle(award=False)
        self.assertEqual([entry.user_id for entry in result.entries], [1])
        self.assertEqual(result.participants, 1)

    def test_week_window_helpers(self) -> None:
        # 周三看周报 / 周一早上 09:00 发周报：都指向上一个完整自然周
        self.assertEqual(
            last_complete_week(datetime(2026, 9, 30, 12, 0)),
            (LAST_WEEK_MONDAY, date(2026, 9, 27)),
        )
        self.assertEqual(
            last_complete_week(datetime(2026, 9, 28, 9, 0)),
            (LAST_WEEK_MONDAY, date(2026, 9, 27)),
        )
        self.assertEqual(week_key(LAST_WEEK_MONDAY), "2026-W39")
        self.assertEqual(week_key(WEEK_MONDAY), "2026-W40")

    def test_rank_week_assigns_points_by_position(self) -> None:
        entries = rank_week(
            {3: (3, 15, 0), 5: (3, 15, 0), 9: (3, 20, 0)},
            {3: "三", 5: "五", 9: "九"},
        )
        self.assertEqual([entry.user_id for entry in entries], [9, 3, 5])
        self.assertEqual([entry.rank for entry in entries], [1, 2, 3])
        self.assertEqual([entry.points for entry in entries], [25, 12, 12])


# ---------------------------------------------------------------------------
# 展示文案
# ---------------------------------------------------------------------------


class ActivityBoardTextTests(_DbTestCase):
    async def test_board_text_is_plain_language(self) -> None:
        async with self.session_factory() as session:
            for offset in range(MIN_ACTIVE_DAYS):
                await self._seed_day(
                    session,
                    user_id=1,
                    day=LAST_WEEK_MONDAY + timedelta(days=offset),
                    messages=5,
                    replies=2,
                    name="小明",
                )
            await session.commit()
        async with self.session_factory() as session:
            result = await settle_weekly_activity(
                session, group_id=GROUP_ID, week_start=LAST_WEEK_MONDAY
            )
            await session.commit()

        lines = render_activity_lines(result)
        text = "\n".join(lines)
        self.assertIn("上周活跃榜", text)
        self.assertIn("小明", text)
        self.assertIn("得分 27", text)
        self.assertIn("+25 分", text)
        for forbidden in (
            "member_activity_daily",
            "member_point_awards",
            "score =",
            "weekly-activity",
        ):
            self.assertNotIn(forbidden, text)
        self.assertLessEqual(len(text), 900, "群消息里的榜单要控制长度")

    async def test_board_text_when_nobody_qualifies(self) -> None:
        async with self.session_factory() as session:
            await self._seed_day(
                session, user_id=1, day=LAST_WEEK_MONDAY, messages=2, name="小明"
            )
            await session.commit()
        async with self.session_factory() as session:
            result = await settle_weekly_activity(
                session, group_id=GROUP_ID, week_start=LAST_WEEK_MONDAY
            )
            await session.commit()

        text = "\n".join(render_activity_lines(result))
        self.assertIn("还没有人达标", text)
        self.assertNotIn("+25 分", text)


# ---------------------------------------------------------------------------
# 积分口径：奖励算进可用余额，但不影响签到连击
# ---------------------------------------------------------------------------


class ActivityAwardPointsTests(_DbTestCase):
    async def _qualified_week(self, user_id: int, *, messages: int = 5) -> None:
        async with self.session_factory() as session:
            for offset in range(MIN_ACTIVE_DAYS):
                await self._seed_day(
                    session,
                    user_id=user_id,
                    day=LAST_WEEK_MONDAY + timedelta(days=offset),
                    messages=messages,
                )
            await session.commit()

    async def test_available_points_include_award_and_streak_is_untouched(self) -> None:
        # 先连续签到 3 天：+1 +2 +3 = 6 分，连击 3
        for offset in (2, 1, 0):
            async with self.session_factory() as session:
                await record_checkin(
                    session,
                    group_id=GROUP_ID,
                    user_id=7,
                    display_name="小明",
                    now=NOW - timedelta(days=offset),
                )
                await session.commit()

        await self._qualified_week(7)
        async with self.session_factory() as session:
            result = await settle_weekly_activity(
                session, group_id=GROUP_ID, week_start=LAST_WEEK_MONDAY
            )
            await session.commit()
        self.assertEqual(result.entries[0].points, 25)

        async with self.session_factory() as session:
            self.assertEqual(
                await available_points(session, group_id=GROUP_ID, user_id=7), 31
            )
            outcome = await summarize(session, group_id=GROUP_ID, user_id=7, now=NOW)
            self.assertEqual(outcome.available_points, 31)
            self.assertEqual(outcome.total_points, 31, "累计获得 = 签到 + 奖励")
            self.assertEqual(outcome.streak, 3, "发奖不能改签到连击")
            self.assertEqual(outcome.total_days, 3, "发奖不能伪造签到天数")

        # 签到表里不能多出任何一行（发奖走的是独立流水）
        async with self.session_factory() as session:
            checkins = (
                await session.execute(
                    select(func.count())
                    .select_from(MemberCheckin)
                    .where(MemberCheckin.user_id == 7)
                )
            ).scalar()
        self.assertEqual(int(checkins or 0), 3)

    async def test_rank_board_counts_awards_in_available_points(self) -> None:
        await self._qualified_week(7)
        async with self.session_factory() as session:
            await settle_weekly_activity(
                session, group_id=GROUP_ID, week_start=LAST_WEEK_MONDAY
            )
            await session.commit()

        async with self.session_factory() as session:
            board = await build_rank_board(
                session, group_id=GROUP_ID, user_id=7, now=NOW
            )
        self.assertEqual(board.entries[0].user_id, 7)
        self.assertEqual(board.entries[0].points, 25, "默认榜的可用积分要含奖励")
        self.assertEqual(board.caller_available, 25)

    async def test_week_board_counts_awards_received_this_week(self) -> None:
        async with self.session_factory() as session:
            # created_at 用 NOW（本周三）：本周榜必须算进去
            await _write_award(
                session,
                group_id=GROUP_ID,
                user_id=7,
                points=25,
                ref="weekly-activity:2026-W39:7",
                created_at=NOW,
            )
            await session.commit()

        async with self.session_factory() as session:
            board = await build_rank_board(
                session, group_id=GROUP_ID, user_id=7, week=True, now=NOW
            )
        self.assertEqual([entry.user_id for entry in board.entries], [7])
        self.assertEqual(board.entries[0].points, 25)

    async def test_awarded_points_are_spendable(self) -> None:
        await self._qualified_week(7)
        async with self.session_factory() as session:
            await settle_weekly_activity(
                session, group_id=GROUP_ID, week_start=LAST_WEEK_MONDAY
            )
            await session.commit()

        async with self.session_factory() as session:
            self.assertTrue(
                await spend_points(
                    session,
                    group_id=GROUP_ID,
                    user_id=7,
                    points=2,
                    reason="test",
                    # F-054：spend_points 的 ref 现在是必填幂等键。
                    ref="test:activity-spendable",
                )
            )
            await session.commit()
        async with self.session_factory() as session:
            self.assertEqual(
                await available_points(session, group_id=GROUP_ID, user_id=7), 23
            )

    async def test_settlement_does_not_touch_checkin_rows(self) -> None:
        async with self.session_factory() as session:
            before = (
                await session.execute(select(func.count()).select_from(MemberCheckin))
            ).scalar()

        await self._qualified_week(7)
        async with self.session_factory() as session:
            await settle_weekly_activity(
                session, group_id=GROUP_ID, week_start=LAST_WEEK_MONDAY
            )
            await session.commit()

        async with self.session_factory() as session:
            after = (
                await session.execute(select(func.count()).select_from(MemberCheckin))
            ).scalar()
        self.assertEqual(int(before or 0), int(after or 0))
        self.assertEqual(int(after or 0), 0)


# ---------------------------------------------------------------------------
# 处理器接线：合格的群消息才会进统计，并带上正确的过滤标记
# ---------------------------------------------------------------------------


class _StopFlow(Exception):
    """哨兵：把 on_group_message 拦在统计写入或 LLM 构造处，只验证接线。"""


class GroupMessageActivityWiringTests(unittest.IsolatedAsyncioTestCase):
    def _message(
        self, *, text: str, reply_to=None, user_id: int = 7, is_bot: bool = False
    ):
        return SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_ID, type="supergroup", title="测试群"),
            from_user=SimpleNamespace(
                id=user_id, is_bot=is_bot, username="tester", full_name="测试用户"
            ),
            sender_chat=None,
            text=text,
            message_id=1,
            reply_to_message=reply_to,
            bot=SimpleNamespace(
                me=AsyncMock(return_value=SimpleNamespace(id=1, username="selfbot"))
            ),
        )

    async def _run(self, message, *, is_admin: bool = False):
        """跑一次 handler；正常发言在统计处停住，被过滤的发言在 LLM 构造处停住。"""

        recorded = AsyncMock(side_effect=_StopFlow)
        # bot 用 MagicMock：LLMService 是 mock，但参数会先求值（main_model 等属性）
        settings = SimpleNamespace(bot=MagicMock(), super_admin_id=0)
        with (
            patch(
                "bot.handlers.group.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._record_group_activity_cas",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=is_admin),
            ),
            patch(
                "bot.handlers.group.activity.record_message_activity_safe",
                new=recorded,
            ),
            patch("bot.handlers.group.LLMService", side_effect=_StopFlow),
        ):
            with self.assertRaises(_StopFlow):
                await group.on_group_message(
                    message, session=object(), settings=settings
                )
        return recorded

    async def test_normal_member_text_is_recorded(self) -> None:
        recorded = await self._run(self._message(text="这个固件谁刷过？"))

        recorded.assert_awaited_once()
        kwargs = recorded.await_args.kwargs
        self.assertEqual(kwargs["group_id"], GROUP_ID)
        self.assertEqual(kwargs["user_id"], 7)
        self.assertEqual(kwargs["text"], "这个固件谁刷过？")
        self.assertEqual(kwargs["message_type"], "text")
        self.assertEqual(kwargs["display_name"], "测试用户")
        self.assertFalse(kwargs["is_bot"])
        self.assertFalse(kwargs["is_channel"])
        self.assertFalse(kwargs["is_admin"])
        self.assertEqual(kwargs["reply_to_user_id"], 0)

    async def test_reply_credits_the_replied_member(self) -> None:
        reply_to = SimpleNamespace(
            from_user=SimpleNamespace(id=99, is_bot=False, full_name="别人"),
            text="前面那句",
        )
        recorded = await self._run(
            self._message(text="同意楼上", reply_to=reply_to)
        )

        self.assertEqual(recorded.await_args.kwargs["reply_to_user_id"], 99)

    async def test_reply_to_a_bot_is_not_credited(self) -> None:
        reply_to = SimpleNamespace(
            from_user=SimpleNamespace(id=1, is_bot=True, full_name="机器人"),
            text="我之前说的",
        )
        recorded = await self._run(self._message(text="知道了", reply_to=reply_to))

        self.assertEqual(recorded.await_args.kwargs["reply_to_user_id"], 0)

    async def test_admin_message_never_reaches_the_recorder(self) -> None:
        # 管理员：预筛直接拦掉，连统计任务都不建（流程会走到 LLM 构造才停住）
        recorded = await self._run(self._message(text="大家注意一下"), is_admin=True)

        recorded.assert_not_awaited()

    async def test_command_message_never_reaches_the_recorder(self) -> None:
        recorded = await self._run(self._message(text="/rank"))

        recorded.assert_not_awaited()

    async def test_too_short_message_never_reaches_the_recorder(self) -> None:
        recorded = await self._run(self._message(text="好"))

        recorded.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
