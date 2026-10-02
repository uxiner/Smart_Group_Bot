"""每日签到与积分查询的回归测试。

关键不变量：同一天同一个人只加一次分（唯一索引兜底，不靠"先查再插"），
跨天/断签的连续天数正确，群之间、成员之间互不影响。
"""
import asyncio
import os
import tempfile
import unittest
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.db.engine import init_db
from bot.db.models import MemberCheckin, MemberPointSpend
from bot.services.join_verification import begin_moderation_challenge
from bot.handlers import commands
from bot.services.checkin import (
    CHALLENGE_SKIP_COST,
    SPEND_REASON_CHALLENGE,
    available_points,
    local_today,
    record_checkin,
    spend_points,
    summarize,
)
from sqlalchemy import func, select


def _day(offset: int = 0) -> datetime:
    """以今天为基准的第 offset 天（本地时间），测试里通过 now= 注入。"""

    return datetime(2026, 9, 29, 12, 0, 0) + timedelta(days=offset)


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

    async def _rows(self) -> int:
        async with self.session_factory() as session:
            return int(
                (await session.execute(select(func.count()).select_from(MemberCheckin))).scalar()
                or 0
            )


class CheckinServiceTests(_DbTestCase):
    async def test_first_checkin_awards_one_point(self) -> None:
        async with self.session_factory() as session:
            outcome = await record_checkin(
                session, group_id=-100, user_id=7, display_name="小明", now=_day()
            )
            await session.commit()

        self.assertFalse(outcome.already)
        self.assertEqual(outcome.points_awarded, 1)
        self.assertEqual(outcome.total_points, 1)
        self.assertEqual(outcome.total_days, 1)
        self.assertEqual(outcome.streak, 1)
        self.assertEqual(await self._rows(), 1)

    async def test_second_checkin_same_day_adds_nothing(self) -> None:
        async with self.session_factory() as session:
            await record_checkin(session, group_id=-100, user_id=7, now=_day())
            await session.commit()
        async with self.session_factory() as session:
            outcome = await record_checkin(session, group_id=-100, user_id=7, now=_day())
            await session.commit()

        self.assertTrue(outcome.already)
        self.assertEqual(outcome.points_awarded, 0)
        self.assertEqual(outcome.total_points, 1)
        self.assertEqual(await self._rows(), 1, "重复签到不能再写一行")

    async def test_consecutive_days_build_a_streak(self) -> None:
        # 必须按时间顺序补签：每次加多少分是相对"那一天的连续天数"算的，
        # 倒着补签等于每天都只算第 1 天（生产里只会按天顺序发生）
        for offset in (2, 1, 0):
            async with self.session_factory() as session:
                await record_checkin(
                    session, group_id=-100, user_id=7, now=_day(-offset)
                )
                await session.commit()

        async with self.session_factory() as session:
            outcome = await summarize(session, group_id=-100, user_id=7, now=_day())

        # 连续第 1/2/3 天分别 +1/+2/+3 = 6 分
        self.assertEqual(outcome.total_points, 6)
        self.assertEqual(outcome.total_days, 3)
        self.assertEqual(outcome.streak, 3)
        self.assertTrue(outcome.already)

    async def test_streak_survives_a_day_that_has_not_been_signed_yet(self) -> None:
        for offset in (1, 2):
            async with self.session_factory() as session:
                await record_checkin(
                    session, group_id=-100, user_id=7, now=_day(-offset)
                )
                await session.commit()

        async with self.session_factory() as session:
            outcome = await summarize(session, group_id=-100, user_id=7, now=_day())

        # 今天还没签：连续天数按昨天算，不能显示 0
        self.assertFalse(outcome.already)
        self.assertEqual(outcome.streak, 2)

    async def test_missing_a_day_resets_the_streak(self) -> None:
        for offset in (3, 2):  # 按时间顺序
            async with self.session_factory() as session:
                await record_checkin(
                    session, group_id=-100, user_id=7, now=_day(-offset)
                )
                await session.commit()

        async with self.session_factory() as session:
            outcome = await record_checkin(session, group_id=-100, user_id=7, now=_day())
            await session.commit()

        self.assertEqual(outcome.streak, 1, "断签后连续天数重新算")
        self.assertEqual(outcome.points_awarded, 1, "断签后这次又只加 1 分")
        self.assertEqual(outcome.total_points, 4, "断签不影响已累计的积分")

    async def test_members_and_groups_are_independent(self) -> None:
        async with self.session_factory() as session:
            await record_checkin(session, group_id=-100, user_id=7, now=_day())
            await record_checkin(session, group_id=-100, user_id=8, now=_day())
            await record_checkin(session, group_id=-200, user_id=7, now=_day())
            await session.commit()

        async with self.session_factory() as session:
            mine = await summarize(session, group_id=-100, user_id=7, now=_day())
            other_user = await summarize(session, group_id=-100, user_id=8, now=_day())
            other_group = await summarize(session, group_id=-200, user_id=7, now=_day())

        for outcome in (mine, other_user, other_group):
            self.assertEqual(outcome.total_points, 1)
        self.assertEqual(await self._rows(), 3)

    async def test_local_day_boundary_decides_the_day(self) -> None:
        # 本地 00:05 与 23:55 是同一天的两个时刻；用 UTC 会把它们分到两天
        morning = datetime(2026, 9, 29, 0, 5, 0)
        night = datetime(2026, 9, 29, 23, 55, 0)
        self.assertEqual(local_today(morning), local_today(night))

        async with self.session_factory() as session:
            first = await record_checkin(session, group_id=-100, user_id=7, now=morning)
            await session.commit()
        async with self.session_factory() as session:
            second = await record_checkin(session, group_id=-100, user_id=7, now=night)
            await session.commit()

        self.assertFalse(first.already)
        self.assertTrue(second.already)


class CheckinCommandTests(_DbTestCase):
    _cleaner: AsyncMock
    _message_obj: object

    def _message(self, text: str, *, chat_type: str = "supergroup"):
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=-100, type=chat_type),
            from_user=SimpleNamespace(id=7, full_name="小明", is_bot=False),
        )

    async def _run(self, text: str, *, authorized: bool = True, chat_type="supergroup"):
        answers: list[str] = []
        answer_kwargs: list[dict] = []
        cleaner = AsyncMock(return_value=True)
        message = self._message(text, chat_type=chat_type)

        async def fake_answer(message, settings, body, **kwargs):
            answers.append(body)
            answer_kwargs.append(kwargs)

        with (
            patch.object(commands, "_answer", side_effect=fake_answer),
            patch.object(
                commands, "schedule_message_auto_delete_durable", new=cleaner
            ),
            patch.object(
                commands,
                "ensure_group_authorized",
                new=AsyncMock(return_value=authorized),
            ),
        ):
            async with self.session_factory() as session:
                handler = (
                    commands.cmd_checkin if "checkin" in text else commands.cmd_points
                )
                await handler(message, session, _settings())
        self._cleaner = cleaner
        self._message_obj = message
        return answers, answer_kwargs

    async def test_checkin_reports_the_award(self) -> None:
        answers, _ = await self._run("/checkin")
        self.assertIn("签到成功", answers[-1])
        self.assertIn("第 1 天 +1 分", answers[-1])
        self.assertIn("可用 <b>1</b> 分", answers[-1])
        self.assertEqual(await self._rows(), 1)

    async def test_command_message_and_receipt_both_expire(self) -> None:
        _, kwargs = await self._run("/checkin")

        # 群友发的那条命令 2 秒后删、回执 5 秒后消失（用户指定，改常量即可改时长）
        self._cleaner.assert_awaited_once_with(self._message_obj, 2)
        self.assertEqual(kwargs[-1]["auto_delete_seconds"], 5)
        self.assertEqual(commands.CHECKIN_COMMAND_DELETE_SECONDS, 2)
        self.assertEqual(commands.CHECKIN_RECEIPT_SECONDS, 5)

    async def test_repeat_checkin_is_reported_as_already_done(self) -> None:
        await self._run("/checkin")
        answers, kwargs = await self._run("/checkin")
        self.assertIn("今天已经签过了", answers[-1])
        self.assertEqual(await self._rows(), 1)
        # 重复签到同样要清掉那条命令，否则群里会留下指令
        self._cleaner.assert_awaited_once_with(self._message_obj, 2)
        self.assertEqual(kwargs[-1]["auto_delete_seconds"], 5)

    async def test_cleanup_failure_never_breaks_the_checkin(self) -> None:
        with patch.object(
            commands,
            "schedule_message_auto_delete_durable",
            new=AsyncMock(side_effect=RuntimeError("scheduler down")),
        ):
            answers, _ = await self._run("/checkin")

        self.assertIn("签到成功", answers[-1])
        self.assertEqual(await self._rows(), 1, "清理失败不能影响签到落库")

    async def test_points_command_is_read_only(self) -> None:
        answers, _ = await self._run("/points")
        self.assertIn("未签到", answers[-1])
        self.assertEqual(await self._rows(), 0)

        await self._run("/checkin")
        answers, _ = await self._run("/points")
        self.assertIn("今日已签到", answers[-1])
        self.assertIn("可用 <b>1</b> 分", answers[-1])

    async def test_commands_are_group_only(self) -> None:
        answers, _ = await self._run("/checkin", chat_type="private")
        self.assertIn("仅可在群内使用", answers[-1])

    async def test_unauthorized_group_gets_nothing(self) -> None:
        answers, _ = await self._run("/checkin", authorized=False)
        self.assertEqual(answers, [])
        self.assertEqual(await self._rows(), 0)


class PointProgressionTests(_DbTestCase):
    """连续签到递增 1→10 封顶，断签重来。"""

    async def _sign(self, day_offset: int, *, group_id: int = -100, user_id: int = 7):
        async with self.session_factory() as session:
            outcome = await record_checkin(
                session,
                group_id=group_id,
                user_id=user_id,
                now=_day(-day_offset),
            )
            await session.commit()
        return outcome

    async def test_award_grows_to_ten_then_caps(self) -> None:
        awards = []
        for offset in range(13, 0, -1):  # 从 13 天前一路签到到今天
            awards.append((await self._sign(offset)).points_awarded)

        self.assertEqual(awards[:11], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10])
        self.assertEqual(awards[11:], [10, 10], "第 10 天以后一直是满额 10 分")

        async with self.session_factory() as session:
            outcome = await summarize(session, group_id=-100, user_id=7, now=_day())
        self.assertEqual(outcome.total_points, 1 + 2 + 3 + 4 + 5 + 6 + 7 + 8 + 9 + 10 * 4)
        self.assertEqual(outcome.streak, 13)
        self.assertTrue(outcome.capped)
        self.assertEqual(outcome.available_points, outcome.total_points)

    async def test_missing_a_day_restarts_from_one_point(self) -> None:
        await self._sign(3)
        await self._sign(2)
        # 第 4 天没签：连续断了
        outcome = await self._sign(0)

        self.assertEqual(outcome.points_awarded, 1)
        self.assertEqual(outcome.streak, 1)
        self.assertEqual(outcome.total_points, 1 + 2 + 1)

    async def test_next_award_reflects_the_current_streak(self) -> None:
        await self._sign(2)
        await self._sign(1)

        async with self.session_factory() as session:
            before = await summarize(session, group_id=-100, user_id=7, now=_day())
        self.assertEqual(before.streak, 2)
        self.assertEqual(before.next_award, 3, "今天签下去就能拿连续第 3 天的 3 分")

        await self._sign(0)
        async with self.session_factory() as session:
            after = await summarize(session, group_id=-100, user_id=7, now=_day())
        self.assertEqual(after.streak, 3)
        self.assertEqual(after.next_award, 4)

    async def test_capped_streak_reports_ten_for_tomorrow(self) -> None:
        for offset in range(11, -1, -1):
            await self._sign(offset)

        async with self.session_factory() as session:
            outcome = await summarize(session, group_id=-100, user_id=7, now=_day())
        self.assertEqual(outcome.next_award, 10, "封顶之后明天还是 10 分")


class PointSpendingTests(_DbTestCase):
    """积分消费：余额 = 获得 − 消费，同一 ref 只能扣一次。"""

    async def _earn(self, days: int, *, user_id: int = 7) -> None:
        for offset in range(days, 0, -1):
            async with self.session_factory() as session:
                await record_checkin(
                    session, group_id=-100, user_id=user_id, now=_day(-offset)
                )
                await session.commit()

    async def test_balance_is_earned_minus_spent(self) -> None:
        await self._earn(2)  # +1 +2 = 3 分

        async with self.session_factory() as session:
            self.assertEqual(
                await available_points(session, group_id=-100, user_id=7), 3
            )
            spent = await spend_points(
                session,
                group_id=-100,
                user_id=7,
                points=CHALLENGE_SKIP_COST,
                reason=SPEND_REASON_CHALLENGE,
                ref="challenge:1",
            )
            await session.commit()
        self.assertTrue(spent)

        async with self.session_factory() as session:
            outcome = await summarize(session, group_id=-100, user_id=7, now=_day())
        self.assertEqual(outcome.total_points, 3)
        self.assertEqual(outcome.spent_points, 2)
        self.assertEqual(outcome.available_points, 1)

    async def test_spending_below_the_cost_is_refused(self) -> None:
        await self._earn(1)  # 只有 1 分

        async with self.session_factory() as session:
            spent = await spend_points(
                session,
                group_id=-100,
                user_id=7,
                points=CHALLENGE_SKIP_COST,
                reason=SPEND_REASON_CHALLENGE,
                ref="challenge:9",
            )
            await session.commit()
        self.assertFalse(spent)

        async with self.session_factory() as session:
            self.assertEqual(
                await available_points(session, group_id=-100, user_id=7), 1
            )

    async def test_the_same_challenge_cannot_be_charged_twice(self) -> None:
        await self._earn(3)  # 6 分

        async with self.session_factory() as session:
            first = await spend_points(
                session, group_id=-100, user_id=7, points=2, ref="challenge:5"
            )
            await session.commit()
        async with self.session_factory() as session:
            second = await spend_points(
                session, group_id=-100, user_id=7, points=2, ref="challenge:5"
            )
            await session.commit()

        self.assertTrue(first)
        self.assertFalse(second, "同一张质询重复点按钮不能再扣一次")
        async with self.session_factory() as session:
            self.assertEqual(
                await available_points(session, group_id=-100, user_id=7), 4
            )

    async def test_a_missing_idempotency_key_is_rejected_loudly(self) -> None:
        """F-054：``ref`` 不能为空。

        SQLite 的唯一索引把 NULL 当作互不相等，旧的 ``ref: str | None = None``
        默认值等于"不传 ref 就没有任何去重保护"。现在缺 ref 在调用点就报错，
        而不是静默写下一行无法去重的消费流水。
        """

        await self._earn(2)  # +1 +2 = 3 分

        async with self.session_factory() as session:
            with self.assertRaises(ValueError):
                await spend_points(
                    session,
                    group_id=-100,
                    user_id=7,
                    points=2,
                    reason=SPEND_REASON_CHALLENGE,
                    ref=None,
                )
            with self.assertRaises(ValueError):
                await spend_points(
                    session,
                    group_id=-100,
                    user_id=7,
                    points=2,
                    reason=SPEND_REASON_CHALLENGE,
                    ref="   ",
                )
            await session.commit()

        # 被拒的调用没有写下任何消费流水，余额原样。
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(func.count()).select_from(MemberPointSpend)
                )
            ).scalar()
            self.assertEqual(int(rows or 0), 0)
            self.assertEqual(
                await available_points(session, group_id=-100, user_id=7), 3
            )

    async def test_ref_is_a_required_keyword_argument(self) -> None:
        """F-054：漏传 ``ref`` 是 TypeError，不会退化成一个不需要去重的调用。"""

        async with self.session_factory() as session:
            with self.assertRaises(TypeError):
                await spend_points(
                    session, group_id=-100, user_id=7, points=2, reason="test"
                )

    async def test_concurrent_spends_with_different_refs_cannot_overdraw(self) -> None:
        """F-005：两个**不同 ref** 的并发消费不能各自读到同一份旧余额。

        旧写法是"先 SELECT 余额、再 INSERT 消费行"两步：两个请求都会看到 3 分、
        双双通过检查，最终余额变成 -1（相当于 0 分买到东西）。余额条件现在写在
        ``INSERT ... SELECT ... WHERE`` 里，与扣分是同一条写语句。

        ``AsyncExitStack`` 里的预热读是为了让两条连接都先建好：否则第二条连接的
        建连耗时会把两个请求错开，反而掩盖了旧实现的竞态。
        """

        await self._earn(2)  # +1 +2 = 3 分

        async def _spend(session, ref: str) -> bool:
            charged = await spend_points(
                session,
                group_id=-100,
                user_id=7,
                points=CHALLENGE_SKIP_COST,
                reason=SPEND_REASON_CHALLENGE,
                ref=ref,
            )
            await session.commit()
            return charged

        async with AsyncExitStack() as stack:
            sessions = [
                await stack.enter_async_context(self.session_factory())
                for _ in range(2)
            ]
            for session in sessions:
                await available_points(session, group_id=-100, user_id=7)
            results = await asyncio.gather(
                _spend(sessions[0], "challenge:race-a"),
                _spend(sessions[1], "challenge:race-b"),
            )

        self.assertEqual(sum(results), 1, "只有一笔消费能成功")
        async with self.session_factory() as session:
            self.assertEqual(
                await available_points(session, group_id=-100, user_id=7),
                1,
                "余额必须等于 3 - 2，绝不允许被扣成负数",
            )

    async def test_concurrent_spends_never_go_negative_when_balance_is_tight(self) -> None:
        """F-005：余额刚好够一笔时，并发三笔也只允许成功一笔。"""

        await self._earn(1)  # 只有 1 分

        async def _spend(session, ref: str) -> bool:
            charged = await spend_points(
                session,
                group_id=-100,
                user_id=7,
                points=1,
                reason=SPEND_REASON_CHALLENGE,
                ref=ref,
            )
            await session.commit()
            return charged

        async with AsyncExitStack() as stack:
            sessions = [
                await stack.enter_async_context(self.session_factory())
                for _ in range(3)
            ]
            for session in sessions:
                await available_points(session, group_id=-100, user_id=7)
            results = await asyncio.gather(
                _spend(sessions[0], "challenge:tight-a"),
                _spend(sessions[1], "challenge:tight-b"),
                _spend(sessions[2], "challenge:tight-c"),
            )

        self.assertEqual(sum(results), 1)
        async with self.session_factory() as session:
            balance = await available_points(session, group_id=-100, user_id=7)
        self.assertGreaterEqual(balance, 0, "可用积分永远不能是负数")
        self.assertEqual(balance, 0)


class ModerationCardPointsTests(_DbTestCase):
    """质询卡是否给出"花分免除"这一行，取决于当事人余额。"""

    def _bot(self):
        return SimpleNamespace(
            restrict_chat_member=AsyncMock(),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=900)),
        )

    async def _begin(self, bot, user_id: int):
        async with self.session_factory() as session:
            started = await begin_moderation_challenge(
                bot=bot,
                session=session,
                settings=_settings(),
                group_id=-100,
                user_id=user_id,
                display_name=f"用户{user_id}",
                bot_username="my_bot",
                reason="疑似发布广告",
                rule_action="ban",
            )
            await session.commit()
        return started

    async def test_one_point_is_not_enough_for_the_skip_row(self) -> None:
        async with self.session_factory() as session:
            await record_checkin(session, group_id=-100, user_id=950, now=_day(-1))
            await session.commit()
        bot = self._bot()

        self.assertTrue(await self._begin(bot, 950))

        keyboard = bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(len(keyboard.inline_keyboard), 2)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "jv:p:950")

    async def test_two_or_more_points_adds_the_skip_row_on_top(self) -> None:
        for offset in (2, 1):  # +1 +2 = 3 分
            async with self.session_factory() as session:
                await record_checkin(session, group_id=-100, user_id=951, now=_day(-offset))
                await session.commit()
        bot = self._bot()

        self.assertTrue(await self._begin(bot, 951))

        keyboard = bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(len(keyboard.inline_keyboard), 3)
        self.assertIn("消耗 2 积分免除质询", keyboard.inline_keyboard[0][0].text)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "jv:s:951")
        self.assertEqual(keyboard.inline_keyboard[1][0].callback_data, "jv:p:951")
        self.assertEqual(keyboard.inline_keyboard[2][0].callback_data, "jv:a:951")
        # 卡片正文也要告诉本人这条权益
        self.assertIn("消耗 2 积分直接免除", bot.send_message.await_args.args[1])


def _settings(**overrides):
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.moderation.enabled = True
    settings.join_verification_enabled = True
    settings.join_verification_turnstile_site_key = "site-key"
    settings.join_verification_turnstile_secret_key = "secret-key"
    settings.join_verification_public_base_url = "https://verify.example.com"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


if __name__ == "__main__":
    unittest.main()
