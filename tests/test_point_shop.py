"""积分商店（头衔 / 置顶求助 / 抽奖）的回归测试。

三条最要紧的不变量，任何一条坏了都算用户真的亏钱，所以都有专门的用例：

1. **可用积分只有一种算法**：签到 + 奖励 − 消费（``available_from_ledgers``）。买完东西
   余额必须等于流水算出来的数，退款之后必须回到原值。
2. **幂等**：同一笔交易的 ``ref`` 重复插入不会重复扣分，退款重复调用不会退两次。
3. **Telegram 失败必退款**：头衔被拒、置顶没权限时，钱要原样退回来。

所有 Telegram 调用都打在 ``FakeBot`` 上，测试绝不碰真实 API。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select
from sqlalchemy import create_engine, inspect

from bot.db.engine import init_db
from bot.db.models import (
    Base,
    AuthorizedGroup,
    MemberCheckin,
    MemberEntitlement,
    MemberPointAward,
    MemberPointSpend,
)
from bot.handlers import commands
from bot.services import point_shop
from bot.services.checkin import (
    available_from_ledgers,
    available_points,
    spend_points,
)
from bot.services.point_shop import (
    KIND_PIN,
    KIND_TAG,
    LOTTERY_DAILY_LIMIT,
    LOTTERY_PRICE,
    LOTTERY_TOTAL_WEIGHT,
    PIN_PRICE,
    TAG_PRICE_7D,
    TAG_PRICE_30D,
    PIN_HOURS,
    PinTarget,
    buy_member_tag,
    buy_pin,
    check_tag_text,
    draw_prize,
    expire_due_entitlements,
    expected_lottery_value,
    lottery_draws_today,
    parse_tag_request,
    play_lottery,
    purchase_stamp,
    refund_points,
    render_balance,
    render_shop_menu,
    resolve_pin_target,
)

GROUP_ID = -1001234567890


def _day(offset: int = 0) -> datetime:
    """基准时间：2026-10-01 12:00（Asia/Shanghai 口径的朴素时间）。"""

    return datetime(2026, 10, 1, 12, 0, 0) + timedelta(days=offset)


def _ms(offset: int) -> datetime:
    """同一秒内的第 offset 毫秒：模拟"连着抽几次"（时间戳不同，ref 才不撞）。"""

    return _day() + timedelta(milliseconds=offset)


def _settings():
    from bot.config import Settings

    return Settings(_env_file=None)


class FakeBot:
    """把商店会用的 Telegram 方法全记下来；需要失败的场景传 error 即可。"""

    def __init__(
        self,
        *,
        status: str = "member",
        member_error: BaseException | None = None,
        tag_error: BaseException | None = None,
        pin_error: BaseException | None = None,
        unpin_error: BaseException | None = None,
        private_error: BaseException | None = None,
        group_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.member_error = member_error
        self.tag_error = tag_error
        self.pin_error = pin_error
        self.unpin_error = unpin_error
        self.private_error = private_error
        self.group_error = group_error
        self.tag_calls: list[tuple[int, int, str]] = []
        self.pin_calls: list[tuple[int, int, bool]] = []
        self.unpin_calls: list[tuple[int, int]] = []
        self.sent: list[tuple[int, str]] = []
        self.member_calls: list[tuple[int, int]] = []

    async def get_chat_member(self, *, chat_id: int, user_id: int):
        self.member_calls.append((int(chat_id), int(user_id)))
        if self.member_error is not None:
            raise self.member_error
        return SimpleNamespace(status=self.status)

    async def set_chat_member_tag(self, *, chat_id: int, user_id: int, tag: str) -> bool:
        self.tag_calls.append((int(chat_id), int(user_id), str(tag)))
        if self.tag_error is not None:
            raise self.tag_error
        return True

    async def pin_chat_message(
        self, *, chat_id: int, message_id: int, disable_notification: bool = False
    ) -> bool:
        self.pin_calls.append((int(chat_id), int(message_id), bool(disable_notification)))
        if self.pin_error is not None:
            raise self.pin_error
        return True

    async def unpin_chat_message(self, *, chat_id: int, message_id: int) -> bool:
        self.unpin_calls.append((int(chat_id), int(message_id)))
        if self.unpin_error is not None:
            raise self.unpin_error
        return True

    async def send_message(self, *, chat_id: int, text: str):
        if int(chat_id) > 0 and self.private_error is not None:
            raise self.private_error
        if int(chat_id) < 0 and self.group_error is not None:
            raise self.group_error
        self.sent.append((int(chat_id), str(text)))
        return SimpleNamespace(message_id=1)


def _bot_error(message: str = "Bad Request: not enough rights") -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


class _CommitFailingSession:
    """把 ``commit()`` 换成一定失败的实现，其余属性全部委托给真实会话。

    模拟"提交时库锁 / 磁盘满"：真 SQLAlchemy 在 commit 失败时会回滚整个事务，
    这里照做——否则测试会读到未提交的中间状态，得出错误结论。
    """

    def __init__(self, session) -> None:
        self._session = session
        self.commit_calls = 0

    def __getattr__(self, name: str):
        return getattr(self._session, name)

    async def commit(self) -> None:
        self.commit_calls += 1
        await self._session.rollback()
        raise RuntimeError("database is locked")


# ---------------------------------------------------------------------------
# 纯函数：头衔校验、抽奖奖池、幂等键
# ---------------------------------------------------------------------------


class TagTextValidationTests(unittest.TestCase):
    def test_accepts_plain_and_chinese_text(self) -> None:
        for text in ("摸鱼冠军", "A", "a" * 16, "我是 小明", "夜猫子-01", "《守望者》"):
            with self.subTest(text=text):
                result = check_tag_text(text)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(result.text, text)

    def test_strips_surrounding_whitespace(self) -> None:
        result = check_tag_text("  摸鱼冠军  ")

        self.assertTrue(result.ok)
        self.assertEqual(result.text, "摸鱼冠军")

    def test_rejects_empty_and_pure_whitespace(self) -> None:
        for text in ("", "   ", "\n"):
            with self.subTest(text=repr(text)):
                result = check_tag_text(text)
                self.assertFalse(result.ok)
                self.assertIn("空白", result.reason)

    def test_rejects_text_longer_than_sixteen_characters(self) -> None:
        result = check_tag_text("字" * 17)

        self.assertFalse(result.ok)
        self.assertIn("最多 16 个字", result.reason)
        self.assertIn("17", result.reason)

    def test_rejects_emoji_from_several_unicode_blocks(self) -> None:
        for text in ("摸鱼😀", "❤️冠军", "🇨🇳", "1️⃣号", "✅通过", "🀄", "🎉", "👨‍👩‍👧"):
            with self.subTest(text=text):
                result = check_tag_text(text)
                self.assertFalse(result.ok, f"emoji 没被拦住：{text}")
                self.assertIn("表情", result.reason)

    def test_accepts_non_emoji_punctuation_and_cjk(self) -> None:
        # 这些在 emoji 区段之外，不能误伤
        for text in ("夜猫子。", "AB！", "名字…", "甲·乙", "小明—2026"):
            with self.subTest(text=text):
                self.assertTrue(check_tag_text(text).ok, text)

    def test_rejects_newlines_and_control_characters(self) -> None:
        for text in ("第一行\n第二行", "带\t制表符", "零宽\u200b字符"):
            with self.subTest(text=repr(text)):
                result = check_tag_text(text)
                self.assertFalse(result.ok, f"控制字符没被拦住：{text!r}")

    def test_rejects_impersonation_blocklist_case_insensitively(self) -> None:
        for text in ("管理员", "群主", "ADMIN", "Admin助理", "官方", "客服小美", "机器人", "my-bot"):
            with self.subTest(text=text):
                result = check_tag_text(text)
                self.assertFalse(result.ok, f"黑名单没拦住：{text}")
                self.assertIn("不能包含", result.reason)


class TagRequestTests(unittest.TestCase):
    def test_defaults_to_the_seven_day_sku(self) -> None:
        request = parse_tag_request("摸鱼冠军")

        self.assertEqual(request.error, "")
        self.assertEqual(request.text, "摸鱼冠军")
        self.assertEqual(request.days, 7)
        self.assertEqual(request.price, TAG_PRICE_7D)

    def test_trailing_marker_selects_the_thirty_day_sku(self) -> None:
        for raw in ("摸鱼冠军 30天", "摸鱼冠军 30d", "摸鱼冠军 --30"):
            with self.subTest(raw=raw):
                request = parse_tag_request(raw)
                self.assertEqual(request.error, "")
                self.assertEqual(request.text, "摸鱼冠军")
                self.assertEqual(request.days, 30)
                self.assertEqual(request.price, TAG_PRICE_30D)

    def test_marker_alone_leaves_no_tag_text(self) -> None:
        request = parse_tag_request("30天")

        self.assertNotEqual(request.error, "")
        self.assertEqual(request.days, 30)

    def test_invalid_text_is_reported_without_a_tag(self) -> None:
        request = parse_tag_request("😀")

        self.assertEqual(request.text, "")
        self.assertIn("表情", request.error)

    def test_a_number_at_the_end_is_still_part_of_the_tag(self) -> None:
        request = parse_tag_request("第 30 名")

        self.assertEqual(request.error, "")
        self.assertEqual(request.text, "第 30 名")
        self.assertEqual(request.days, 7)


class LotteryTableTests(unittest.TestCase):
    def test_weights_add_up_to_ten_thousand(self) -> None:
        self.assertEqual(
            sum(prize.weight for prize in point_shop.LOTTERY_TABLE),
            LOTTERY_TOTAL_WEIGHT,
        )

    def test_probabilities_match_the_product_table(self) -> None:
        table = {prize.points: prize.weight for prize in point_shop.LOTTERY_TABLE}

        self.assertEqual(table[0], 3900)
        self.assertEqual(table[3], 2000)
        self.assertEqual(table[5], 1600)
        self.assertEqual(table[8], 1000)
        self.assertEqual(table[12], 1000)
        self.assertEqual(table[40], 400)
        self.assertEqual(table[100], 100)

    def test_expected_value_is_exactly_six_points(self) -> None:
        # 2026-10-01 管理员指定：期望值**正好** 6.00 分/次（成本 5 分 → 长期每次净赚 1 分）。
        self.assertAlmostEqual(expected_lottery_value(), 6.0, places=6)
        self.assertGreater(expected_lottery_value(), LOTTERY_PRICE)

    def test_boundaries_map_to_the_right_prize(self) -> None:
        cases = {
            0: 0,
            3899: 0,
            3900: 3,
            5899: 3,
            5900: 5,
            7499: 5,
            7500: 8,
            8499: 8,
            8500: 12,
            9499: 12,
            9500: 40,
            9899: 40,
            9900: 100,
            9999: 100,
        }
        for roll, expected in cases.items():
            with self.subTest(roll=roll):
                self.assertEqual(draw_prize(randbelow=lambda _n, r=roll: r).points, expected)

    def test_default_random_source_is_crypto_secure(self) -> None:
        with patch.object(point_shop.secrets, "randbelow", return_value=8600) as roller:
            prize = draw_prize()

        roller.assert_called_once_with(LOTTERY_TOTAL_WEIGHT)
        self.assertEqual(prize.points, 12)


class PurchaseRefTests(unittest.TestCase):
    """ref 要够短（现有列是 String(64)）、稳定、且互不重复。"""

    def test_refs_fit_the_existing_columns(self) -> None:
        now = _day()
        stamp = purchase_stamp(now)
        refs = [
            point_shop.tag_spend_ref(group_id=GROUP_ID, user_id=7, days=7, stamp=stamp),
            point_shop.tag_spend_ref(group_id=GROUP_ID, user_id=7, days=30, stamp=stamp),
            point_shop.pin_spend_ref(group_id=GROUP_ID, user_id=7, stamp=stamp),
            point_shop.lottery_spend_ref(
                group_id=GROUP_ID, user_id=7, day="20261001", stamp=stamp
            ),
            point_shop.lottery_prize_ref(group_id=GROUP_ID, user_id=7, stamp=stamp),
        ]
        for ref in refs:
            with self.subTest(ref=ref):
                self.assertLessEqual(len(ref), 64)
                self.assertLessEqual(len(point_shop.refund_ref(ref)), 64)

    def test_same_moment_gives_the_same_stamp(self) -> None:
        self.assertEqual(purchase_stamp(_day()), purchase_stamp(_day()))

    def test_the_short_and_long_tag_skus_are_different(self) -> None:
        stamp = purchase_stamp(_day())

        self.assertNotEqual(
            point_shop.tag_spend_ref(group_id=GROUP_ID, user_id=7, days=7, stamp=stamp),
            point_shop.tag_spend_ref(group_id=GROUP_ID, user_id=7, days=30, stamp=stamp),
        )


# ---------------------------------------------------------------------------
# 数据库用例
# ---------------------------------------------------------------------------


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self._seed = 0

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _grant_points(
        self, user_id: int, points: int, *, group_id: int = GROUP_ID
    ) -> None:
        """发分走奖励流水（不是签到流水：伪造签到会把连续天数算错）。"""

        self._seed += 1
        async with self.session_factory() as session:
            session.add(
                MemberPointAward(
                    group_id=group_id,
                    user_id=user_id,
                    points=int(points),
                    reason="test_seed",
                    ref=f"seed:{group_id}:{user_id}:{self._seed}",
                )
            )
            await session.commit()

    async def _balance(self, user_id: int, *, group_id: int = GROUP_ID) -> int:
        async with self.session_factory() as session:
            return await available_points(session, group_id=group_id, user_id=user_id)

    async def _ledger_balance(self, user_id: int, *, group_id: int = GROUP_ID) -> int:
        """直接从三本流水算一遍：必须和 available_points 完全一致。"""

        async with self.session_factory() as session:
            earned = (
                await session.execute(
                    select(func.coalesce(func.sum(MemberCheckin.points), 0)).where(
                        MemberCheckin.group_id == group_id,
                        MemberCheckin.user_id == user_id,
                    )
                )
            ).scalar()
            awarded = (
                await session.execute(
                    select(func.coalesce(func.sum(MemberPointAward.points), 0)).where(
                        MemberPointAward.group_id == group_id,
                        MemberPointAward.user_id == user_id,
                    )
                )
            ).scalar()
            spent = (
                await session.execute(
                    select(func.coalesce(func.sum(MemberPointSpend.points), 0)).where(
                        MemberPointSpend.group_id == group_id,
                        MemberPointSpend.user_id == user_id,
                    )
                )
            ).scalar()
        return available_from_ledgers(
            earned=int(earned or 0), awarded=int(awarded or 0), spent=int(spent or 0)
        )

    async def _spends(self, user_id: int, *, group_id: int = GROUP_ID) -> list:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(MemberPointSpend)
                .where(
                    MemberPointSpend.group_id == group_id,
                    MemberPointSpend.user_id == user_id,
                )
                .order_by(MemberPointSpend.id)
            )
            return list(rows.scalars().all())

    async def _awards(self, user_id: int, *, group_id: int = GROUP_ID) -> list:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(MemberPointAward)
                .where(
                    MemberPointAward.group_id == group_id,
                    MemberPointAward.user_id == user_id,
                )
                .order_by(MemberPointAward.id)
            )
            return list(rows.scalars().all())

    async def _entitlements(self, *, group_id: int = GROUP_ID) -> list:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(MemberEntitlement)
                .where(MemberEntitlement.group_id == group_id)
                .order_by(MemberEntitlement.id)
            )
            return list(rows.scalars().all())

    async def _buy_tag(
        self,
        bot: FakeBot,
        *,
        user_id: int = 7,
        raw: str = "摸鱼冠军",
        now: datetime | None = None,
        group_id: int = GROUP_ID,
    ):
        async with self.session_factory() as session:
            reply = await buy_member_tag(
                session,
                bot=bot,
                group_id=group_id,
                user_id=user_id,
                raw_text=raw,
                now=now or _day(),
            )
        return reply

    async def _buy_pin(
        self,
        bot: FakeBot,
        *,
        user_id: int = 7,
        target: PinTarget | None = None,
        now: datetime | None = None,
        group_id: int = GROUP_ID,
    ):
        async with self.session_factory() as session:
            reply = await buy_pin(
                session,
                bot=bot,
                group_id=group_id,
                user_id=user_id,
                target=target or PinTarget(message_id=555, sender_id=user_id),
                now=now or _day(),
            )
        return reply

    async def _draw(
        self,
        *,
        user_id: int = 7,
        now: datetime | None = None,
        roll: int = 0,
        group_id: int = GROUP_ID,
    ):
        async with self.session_factory() as session:
            reply = await play_lottery(
                session,
                group_id=group_id,
                user_id=user_id,
                now=now or _day(),
                randbelow=lambda _total, r=roll: r,
            )
        return reply


# ---------------------------------------------------------------------------
# 头衔
# ---------------------------------------------------------------------------


class TagPurchaseTests(_DbTestCase):
    async def test_buy_tag_charges_and_sets_the_tag(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot()

        reply = await self._buy_tag(bot)

        self.assertEqual(reply.status, "ok")
        self.assertEqual(bot.tag_calls, [(GROUP_ID, 7, "摸鱼冠军")])
        self.assertEqual(await self._balance(7), 70)
        self.assertEqual(await self._balance(7), await self._ledger_balance(7))
        rows = await self._entitlements()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, KIND_TAG)
        self.assertEqual(rows[0].payload, "摸鱼冠军")
        self.assertEqual(rows[0].expires_at, _day() + timedelta(days=7))
        spends = await self._spends(7)
        self.assertEqual([(row.points, row.reason) for row in spends], [(30, "shop_tag")])

    async def test_long_rental_costs_eighty_points_for_thirty_days(self) -> None:
        await self._grant_points(7, 200)
        bot = FakeBot()

        reply = await self._buy_tag(bot, raw="摸鱼冠军 30天")

        self.assertEqual(reply.status, "ok")
        self.assertEqual(bot.tag_calls, [(GROUP_ID, 7, "摸鱼冠军")])
        self.assertEqual(await self._balance(7), 120)
        rows = await self._entitlements()
        self.assertEqual(rows[0].expires_at, _day() + timedelta(days=30))

    async def test_renewal_extends_from_the_old_expiry(self) -> None:
        await self._grant_points(7, 200)
        bot = FakeBot()
        await self._buy_tag(bot, now=_day())

        # 第二天再买 7 天：新到期时间必须是"第一天那次 +14 天"，不是"第二天 +7 天"
        await self._buy_tag(bot, now=_day(1))

        rows = await self._entitlements()
        self.assertEqual(len(rows), 1, "续费不能插出第二行")
        self.assertEqual(rows[0].expires_at, _day() + timedelta(days=14))
        self.assertEqual(await self._balance(7), 140)

    async def test_buying_again_after_expiry_starts_from_now(self) -> None:
        await self._grant_points(7, 200)
        bot = FakeBot()
        await self._buy_tag(bot, now=_day())

        await self._buy_tag(bot, now=_day(30))

        rows = await self._entitlements()
        self.assertEqual(rows[0].expires_at, _day(30) + timedelta(days=7))

    async def test_insufficient_points_does_not_charge(self) -> None:
        await self._grant_points(7, 10)
        bot = FakeBot()

        reply = await self._buy_tag(bot)

        self.assertEqual(reply.status, "insufficient")
        self.assertIn("还差 20 分", reply.text)
        self.assertEqual(reply.available, 10)
        self.assertEqual(bot.tag_calls, [])
        self.assertEqual(await self._spends(7), [])
        self.assertEqual(await self._balance(7), 10)

    async def test_invalid_text_does_not_charge(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot()

        reply = await self._buy_tag(bot, raw="很长的头衔" + "字" * 20)

        self.assertEqual(reply.status, "invalid_tag")
        self.assertEqual(bot.tag_calls, [])
        self.assertEqual(await self._balance(7), 100)

    async def test_administrators_cannot_buy(self) -> None:
        await self._grant_points(7, 100)
        for status in ("administrator", "creator", "ChatMemberStatus.ADMINISTRATOR"):
            with self.subTest(status=status):
                bot = FakeBot(status=status)

                reply = await self._buy_tag(bot)

                self.assertEqual(reply.status, "admin_blocked")
                self.assertIn("管理员和群主", reply.text)
                self.assertEqual(bot.tag_calls, [])
                self.assertEqual(await self._balance(7), 100)

    async def test_unknown_member_status_does_not_charge(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot(member_error=RuntimeError("telegram down"))

        reply = await self._buy_tag(bot)

        self.assertEqual(reply.status, "unknown_member")
        self.assertEqual(await self._balance(7), 100)
        self.assertEqual(bot.tag_calls, [])

    async def test_other_members_cannot_take_the_same_tag(self) -> None:
        await self._grant_points(7, 100)
        await self._grant_points(8, 100)
        bot = FakeBot()
        await self._buy_tag(bot, user_id=7, raw="Tester")

        reply = await self._buy_tag(bot, user_id=8, raw="tester")

        self.assertEqual(reply.status, "duplicate_tag")
        self.assertEqual(await self._balance(8), 100)
        self.assertEqual(len(bot.tag_calls), 1)

    async def test_the_same_member_may_renew_their_own_tag(self) -> None:
        await self._grant_points(7, 200)
        bot = FakeBot()
        await self._buy_tag(bot, raw="Tester")

        reply = await self._buy_tag(bot, raw="tester", now=_day(1))

        self.assertEqual(reply.status, "ok")
        self.assertEqual(len(bot.tag_calls), 2)

    async def test_telegram_failure_refunds_the_points(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot(tag_error=_bot_error("Bad Request: tag is invalid"))

        reply = await self._buy_tag(bot)

        self.assertEqual(reply.status, "telegram_failed")
        self.assertTrue(reply.refunded)
        self.assertIn("已退回 30 分", reply.text)
        self.assertEqual(await self._balance(7), 100)
        self.assertEqual(await self._balance(7), await self._ledger_balance(7))
        awards = await self._awards(7)
        refunds = [row for row in awards if row.reason == "shop_refund"]
        self.assertEqual(len(refunds), 1)
        self.assertTrue(refunds[0].ref.startswith("shop-refund:shop-tag-7d:"))
        self.assertEqual(await self._entitlements(), [], "失败了不能留下生效中的头衔")

    async def test_tags_are_scoped_per_group(self) -> None:
        await self._grant_points(7, 100)
        await self._grant_points(7, 100, group_id=-999)
        bot = FakeBot()
        await self._buy_tag(bot, user_id=7, raw="同名", group_id=GROUP_ID)

        reply = await self._buy_tag(bot, user_id=7, raw="同名", group_id=-999)

        self.assertEqual(reply.status, "ok")


# ---------------------------------------------------------------------------
# 置顶求助
# ---------------------------------------------------------------------------


class PinPurchaseTests(_DbTestCase):
    async def test_requires_a_reply(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot()

        reply = await self._buy_pin(bot, target=PinTarget())

        self.assertEqual(reply.status, "not_reply")
        self.assertIn("/top", reply.text)
        self.assertEqual(await self._balance(7), 100)

    async def test_only_your_own_messages(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot()

        reply = await self._buy_pin(bot, target=PinTarget(message_id=5, sender_id=8))

        self.assertEqual(reply.status, "not_yours")
        self.assertEqual(bot.pin_calls, [])
        self.assertEqual(await self._balance(7), 100)

    async def test_bot_and_channel_messages_are_not_supported(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot()
        targets = (
            PinTarget(message_id=5, sender_id=7, is_bot=True),
            PinTarget(message_id=5, sender_id=0, is_channel=True),
        )
        for target in targets:
            with self.subTest(target=target):
                reply = await self._buy_pin(bot, target=target)
                self.assertEqual(reply.status, "unsupported_target")

        self.assertEqual(bot.pin_calls, [])
        self.assertEqual(await self._balance(7), 100)

    async def test_pins_without_notification_and_charges(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot()

        reply = await self._buy_pin(bot)

        self.assertEqual(reply.status, "ok")
        self.assertEqual(bot.pin_calls, [(GROUP_ID, 555, True)], "置顶不能给全群弹通知")
        self.assertEqual(await self._balance(7), 80)
        rows = await self._entitlements()
        self.assertEqual(rows[0].kind, KIND_PIN)
        self.assertEqual(rows[0].payload, "555")
        self.assertEqual(rows[0].expires_at, _day() + timedelta(hours=PIN_HOURS))

    async def test_one_pin_per_member_but_members_do_not_collide(self) -> None:
        await self._grant_points(7, 100)
        await self._grant_points(8, 100)
        bot = FakeBot()
        await self._buy_pin(bot, user_id=7, now=_day())

        # 同一个人 30 分钟后再买：不扣分，只告诉他还有多久
        again = await self._buy_pin(
            bot, user_id=7, now=_day() + timedelta(minutes=30)
        )
        self.assertEqual(again.status, "already_pinned")
        self.assertIn("还有 330 分钟到期", again.text)
        self.assertEqual(await self._balance(7), 80)

        # 另一个人照常能买，不会把前一个人的置顶顶掉
        other = await self._buy_pin(bot, user_id=8, target=PinTarget(666, 8))
        self.assertEqual(other.status, "ok")
        self.assertEqual(await self._balance(8), 80)
        self.assertEqual(len(await self._entitlements()), 2)

    async def test_insufficient_points_does_not_charge(self) -> None:
        await self._grant_points(7, 10)
        bot = FakeBot()

        reply = await self._buy_pin(bot)

        self.assertEqual(reply.status, "insufficient")
        self.assertIn("还差 10 分", reply.text)
        self.assertEqual(bot.pin_calls, [])
        self.assertEqual(await self._balance(7), 10)

    async def test_pin_failure_refunds(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot(pin_error=_bot_error("Bad Request: not enough rights to pin"))

        reply = await self._buy_pin(bot)

        self.assertEqual(reply.status, "telegram_failed")
        self.assertTrue(reply.refunded)
        self.assertEqual(await self._balance(7), 100)
        self.assertEqual(await self._entitlements(), [])

    def test_resolve_pin_target_reads_the_reply(self) -> None:
        message = SimpleNamespace(
            reply_to_message=SimpleNamespace(
                message_id=42,
                from_user=SimpleNamespace(id=9, is_bot=False),
                sender_chat=None,
            )
        )
        target = resolve_pin_target(message)
        self.assertEqual((target.message_id, target.sender_id), (42, 9))
        self.assertFalse(target.is_bot)
        self.assertFalse(target.is_channel)

        self.assertTrue(resolve_pin_target(SimpleNamespace()).missing)

    def test_resolve_pin_target_marks_channel_and_bot_messages(self) -> None:
        channel = SimpleNamespace(
            reply_to_message=SimpleNamespace(
                message_id=42,
                from_user=None,
                sender_chat=SimpleNamespace(id=-100, type="channel"),
            )
        )
        bot_message = SimpleNamespace(
            reply_to_message=SimpleNamespace(
                message_id=42,
                from_user=SimpleNamespace(id=99, is_bot=True),
                sender_chat=None,
            )
        )

        self.assertTrue(resolve_pin_target(channel).is_channel)
        self.assertTrue(resolve_pin_target(bot_message).is_bot)


# ---------------------------------------------------------------------------
# 事务边界（F-053）：扣费与权益必须一起提交、一起回滚
# ---------------------------------------------------------------------------


class PurchaseAtomicityTests(_DbTestCase):
    """F-053：旧实现"先提交扣费、再调 Telegram、最后写权益"，中间崩溃就会留下
    "被扣分、头衔已设、却没有到期行"的永不过期头衔（也没有退款记录）。
    现在扣费与权益在**同一个事务**提交，Telegram 侧失败时一起撤销。
    """

    async def test_tag_entitlement_is_visible_when_telegram_is_called(self) -> None:
        await self._grant_points(7, 100)
        observed: list[tuple[int, int]] = []

        async def _probe_then_set(bot, chat_id, user_id, tag):
            # 用一条**全新**连接查库：两条记录都看得到，说明它们已经在
            # Telegram 调用之前、由同一个事务提交了。
            async with self.session_factory() as probe:
                spends = (
                    await probe.execute(
                        select(func.count())
                        .select_from(MemberPointSpend)
                        .where(MemberPointSpend.group_id == GROUP_ID)
                    )
                ).scalar()
                entitlements = (
                    await probe.execute(
                        select(func.count())
                        .select_from(MemberEntitlement)
                        .where(MemberEntitlement.group_id == GROUP_ID)
                    )
                ).scalar()
            observed.append((int(spends or 0), int(entitlements or 0)))
            await bot.set_chat_member_tag(chat_id=chat_id, user_id=user_id, tag=tag)

        with patch.object(point_shop, "_set_member_tag", _probe_then_set):
            reply = await self._buy_tag(FakeBot())

        self.assertEqual(reply.status, "ok")
        self.assertEqual(
            observed, [(1, 1)], "Telegram 调用时扣费与权益都必须已经落库"
        )

    async def test_pin_entitlement_is_visible_when_telegram_is_called(self) -> None:
        await self._grant_points(7, 100)
        observed: list[int] = []

        async def _probe_then_pin(bot, chat_id, message_id):
            async with self.session_factory() as probe:
                entitlements = (
                    await probe.execute(
                        select(func.count())
                        .select_from(MemberEntitlement)
                        .where(MemberEntitlement.group_id == GROUP_ID)
                    )
                ).scalar()
            observed.append(int(entitlements or 0))
            await bot.pin_chat_message(chat_id=chat_id, message_id=message_id)

        with patch.object(point_shop, "_pin_message", _probe_then_pin):
            reply = await self._buy_pin(FakeBot())

        self.assertEqual(reply.status, "ok")
        self.assertEqual(observed, [1])

    async def test_entitlement_write_failure_refunds_and_skips_telegram(self) -> None:
        """权益没写成就退款：绝不留"已扣费但永不过期"的头衔。"""

        await self._grant_points(7, 100)
        bot = FakeBot()
        with patch.object(
            point_shop,
            "upsert_entitlement",
            AsyncMock(side_effect=RuntimeError("entitlements table unavailable")),
        ):
            reply = await self._buy_tag(bot)

        self.assertEqual(reply.status, "failed")
        self.assertTrue(reply.refunded)
        self.assertEqual(bot.tag_calls, [], "权益没写成就不该动 Telegram")
        self.assertEqual(await self._balance(7), 100)
        self.assertEqual(await self._entitlements(), [])

    async def test_commit_failure_does_not_refund_free_points(self) -> None:
        """提交失败时整个事务（含扣费）已经回滚，退款反而会凭空加分。"""

        await self._grant_points(7, 100)
        stack = AsyncExitStack()
        self.addAsyncCleanup(stack.aclose)
        session = await stack.enter_async_context(self.session_factory())
        failing = _CommitFailingSession(session)

        reply = await buy_member_tag(
            failing,
            bot=FakeBot(),
            group_id=GROUP_ID,
            user_id=7,
            raw_text="摸鱼冠军",
            now=_day(),
        )

        self.assertEqual(reply.status, "failed")
        self.assertFalse(reply.refunded)
        self.assertIn("没有扣分", reply.text)
        self.assertEqual(reply.available, 100)
        self.assertEqual(await self._balance(7), 100)
        self.assertEqual(await self._entitlements(), [])
        self.assertEqual(
            [row for row in await self._awards(7) if row.reason == "shop_refund"],
            [],
            "没扣成就不能退，否则等于白送积分",
        )

    async def test_renewal_failure_restores_the_previous_expiry(self) -> None:
        """续费失败要把权益还原成购买前的样子，不是删掉上一笔的有效期。"""

        await self._grant_points(7, 200)
        first = await self._buy_tag(FakeBot(), raw="Tester")
        self.assertEqual(first.status, "ok")
        before = (await self._entitlements())[0]
        before_values = (before.payload, before.ref, before.expires_at)

        failing = FakeBot(tag_error=_bot_error("Bad Request: tag is invalid"))
        reply = await self._buy_tag(failing, raw="tester", now=_day(1))

        self.assertEqual(reply.status, "telegram_failed")
        self.assertTrue(reply.refunded)
        after = (await self._entitlements())[0]
        self.assertEqual(
            (after.payload, after.ref, after.expires_at),
            before_values,
            "续费失败必须还原旧到期时间，而不是把这一行删掉",
        )
        self.assertEqual(await self._balance(7), 170)


# ---------------------------------------------------------------------------
# 并发购买（F-006）：同一个人同一类权益不能重复扣费，已付费的续期不能被吞掉
# ---------------------------------------------------------------------------


class PurchaseConcurrencyTests(_DbTestCase):
    """并发 /top 与 /tag：读旧状态和落库必须是一个整体。

    两个并发请求会各自看到"还没有生效中的权益"、"续费从旧到期时间往后加"，
    于是重复扣分（/top 双击各扣 20 分只得到一个置顶），或者第二笔买到的时间被
    第一笔的写入覆盖掉（/tag 续费时长被吞）。这里用两个已经预热好的连接同时发起
    购买，真实复现竞态。
    """

    async def _warm_sessions(self, stack: AsyncExitStack, count: int) -> list:
        """开 count 条连接并各跑一次读，避免建连耗时把两个请求错开而掩盖竞态。"""

        sessions = []
        for _ in range(count):
            session = await stack.enter_async_context(self.session_factory())
            await available_points(session, group_id=GROUP_ID, user_id=7)
            sessions.append(session)
        return sessions

    async def test_concurrent_pins_charge_only_once(self) -> None:
        """F-006：同一个人的两次并发 /top 只允许扣一次 20 分。"""

        await self._grant_points(7, 100)
        bot = FakeBot()

        async with AsyncExitStack() as stack:
            sessions = await self._warm_sessions(stack, 2)
            replies = await asyncio.gather(
                *[
                    buy_pin(
                        session,
                        bot=bot,
                        group_id=GROUP_ID,
                        user_id=7,
                        target=PinTarget(message_id=555, sender_id=7),
                        # 两个请求相差 1 毫秒：ref 不同，唯一索引挡不住重复扣分
                        now=_ms(index),
                    )
                    for index, session in enumerate(sessions)
                ]
            )

        self.assertEqual(
            sorted(reply.status for reply in replies),
            ["already_pinned", "ok"],
            "并发双击 /top 只能有一个买到，另一个必须被「已有置顶」挡住",
        )
        self.assertEqual(await self._balance(7), 80, "只允许扣 20 分")
        self.assertEqual(len(await self._spends(7)), 1)
        self.assertEqual(len(bot.pin_calls), 1, "只允许真正置顶一次")
        entitlements = await self._entitlements()
        self.assertEqual(len(entitlements), 1)
        self.assertEqual(entitlements[0].kind, KIND_PIN)

    async def test_concurrent_tag_renewals_keep_both_paid_durations(self) -> None:
        """F-006：两笔并发 /tag 都付了钱，续期时长必须累加而不是互相覆盖。"""

        await self._grant_points(7, 200)
        bot = FakeBot()

        async with AsyncExitStack() as stack:
            sessions = await self._warm_sessions(stack, 2)
            replies = await asyncio.gather(
                *[
                    buy_member_tag(
                        session,
                        bot=bot,
                        group_id=GROUP_ID,
                        user_id=7,
                        raw_text="摸鱼冠军",
                        now=_ms(index),
                    )
                    for index, session in enumerate(sessions)
                ]
            )

        self.assertEqual([reply.status for reply in replies], ["ok", "ok"])
        self.assertEqual(await self._balance(7), 140, "两笔各扣 30 分")
        self.assertEqual(len(await self._spends(7)), 2)
        entitlements = await self._entitlements()
        self.assertEqual(len(entitlements), 1, "续费只能有一行")
        self.assertEqual(
            entitlements[0].expires_at,
            _day() + timedelta(days=14),
            "第二笔买到的 7 天不能被第一笔的到期时间覆盖掉",
        )


# ---------------------------------------------------------------------------
# 抽奖
# ---------------------------------------------------------------------------


class LotteryPurchaseTests(_DbTestCase):
    async def test_charges_five_points_and_reports_the_prize(self) -> None:
        await self._grant_points(7, 36)

        reply = await self._draw(roll=8600)

        self.assertEqual(reply.status, "ok")
        self.assertIn("🎲 抽奖结果：12 分", reply.text)
        self.assertIn("本次净 +7 分，当前可用 43 分", reply.text)
        self.assertEqual(await self._balance(7), 43)
        self.assertEqual(await self._balance(7), await self._ledger_balance(7))

    async def test_losing_ticket_only_costs_the_ticket(self) -> None:
        await self._grant_points(7, 36)

        reply = await self._draw(roll=0)

        self.assertIn("谢谢参与", reply.text)
        self.assertIn("本次净 -5 分，当前可用 31 分", reply.text)
        self.assertEqual(await self._balance(7), 31)
        self.assertEqual(len(await self._awards(7)), 1, "没中奖就不能有中奖流水")

    async def test_big_prizes_are_paid_out(self) -> None:
        for index, (roll, points) in enumerate(
            ((9900, 100), (9500, 40), (8500, 12), (7500, 8), (5900, 5), (3900, 3))
        ):
            with self.subTest(roll=roll):
                await self._grant_points(7, 36)
                # 每次用不同的毫秒时间戳：同一次抽奖的 ref 靠时间戳区分
                reply = await self._draw(roll=roll, now=_ms(index))
                self.assertEqual(reply.status, "ok")
                self.assertIn(f"{points} 分", reply.text)

        self.assertEqual(await self._balance(7), 36 * 6 - 6 * LOTTERY_PRICE + 168)

    async def test_ten_draws_a_day_then_it_stops(self) -> None:
        await self._grant_points(7, 500)

        for index in range(LOTTERY_DAILY_LIMIT):
            reply = await self._draw(now=_ms(index))
            self.assertEqual(reply.status, "ok", f"第 {index + 1} 次应该能抽")

        blocked = await self._draw(now=_ms(LOTTERY_DAILY_LIMIT))
        self.assertEqual(blocked.status, "daily_limit")
        self.assertIn("每天最多 10 次", blocked.text)
        spent = await self._spends(7)
        self.assertEqual(len(spent), LOTTERY_DAILY_LIMIT, "第 11 次不能再扣分")
        self.assertEqual(await self._balance(7), 500 - LOTTERY_DAILY_LIMIT * LOTTERY_PRICE)

    async def test_the_limit_resets_on_the_next_local_day(self) -> None:
        await self._grant_points(7, 500)
        for index in range(LOTTERY_DAILY_LIMIT):
            await self._draw(now=_ms(index))

        reply = await self._draw(now=_day(1))

        self.assertEqual(reply.status, "ok")
        async with self.session_factory() as session:
            self.assertEqual(
                await lottery_draws_today(
                    session, group_id=GROUP_ID, user_id=7, day="20261001"
                ),
                LOTTERY_DAILY_LIMIT,
            )
            self.assertEqual(
                await lottery_draws_today(
                    session, group_id=GROUP_ID, user_id=7, day="20261002"
                ),
                1,
            )

    async def test_the_limit_is_per_member_and_per_group(self) -> None:
        await self._grant_points(7, 500)
        await self._grant_points(8, 500)
        await self._grant_points(7, 100, group_id=-999)
        for index in range(LOTTERY_DAILY_LIMIT):
            await self._draw(user_id=7, now=_ms(index))

        self.assertEqual((await self._draw(user_id=8)).status, "ok")
        self.assertEqual(
            (await self._draw(user_id=7, group_id=-999)).status, "ok"
        )

    async def test_insufficient_points_does_not_charge(self) -> None:
        await self._grant_points(7, 3)

        reply = await self._draw()

        self.assertEqual(reply.status, "insufficient")
        self.assertIn("还差 2 分", reply.text)
        self.assertEqual(await self._balance(7), 3)
        self.assertEqual(await self._spends(7), [])

    async def test_the_same_ref_cannot_charge_twice(self) -> None:
        """同一毫秒内重复投递同一次抽奖：唯一索引挡住第二次扣分。"""

        await self._grant_points(7, 100)
        async with self.session_factory() as session:
            first = await spend_points(
                session,
                group_id=GROUP_ID,
                user_id=7,
                points=LOTTERY_PRICE,
                reason="shop_lottery",
                ref="lottery:fixed",
            )
            second = await spend_points(
                session,
                group_id=GROUP_ID,
                user_id=7,
                points=LOTTERY_PRICE,
                reason="shop_lottery",
                ref="lottery:fixed",
            )
            await session.commit()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(await self._spends(7)), 1)
        self.assertEqual(await self._balance(7), 95)


# ---------------------------------------------------------------------------
# 退款与流水的幂等
# ---------------------------------------------------------------------------


class LedgerRefundTests(_DbTestCase):
    async def test_refund_is_idempotent(self) -> None:
        await self._grant_points(7, 100)
        async with self.session_factory() as session:
            await spend_points(
                session,
                group_id=GROUP_ID,
                user_id=7,
                points=30,
                reason="shop_tag",
                ref="shop-tag-7d:x",
            )
            await session.commit()

        async with self.session_factory() as session:
            first = await refund_points(
                session, group_id=GROUP_ID, user_id=7, points=30, original_ref="shop-tag-7d:x"
            )
            second = await refund_points(
                session, group_id=GROUP_ID, user_id=7, points=30, original_ref="shop-tag-7d:x"
            )
            await session.commit()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(await self._balance(7), 100)

    async def test_refunds_do_not_touch_checkin_ledger(self) -> None:
        """退款走奖励流水，绝不伪造签到行（否则连续签到天数会算错）。"""

        await self._grant_points(7, 100)
        bot = FakeBot(tag_error=_bot_error())
        await self._buy_tag(bot)

        async with self.session_factory() as session:
            checkins = (
                await session.execute(
                    select(func.count()).select_from(MemberCheckin)
                )
            ).scalar()
        self.assertEqual(int(checkins or 0), 0)


# ---------------------------------------------------------------------------
# D2-02：写入必须能被外层 rollback 撤回来（SAVEPOINT 语义）
# ---------------------------------------------------------------------------


class AwardRollbackTests(_DbTestCase):
    """``award_points`` / ``upsert_entitlement`` 不许用 ``begin_nested()``。

    pysqlite / aiosqlite 只有在第一条 DML 之前才发出 ``BEGIN``；``SAVEPOINT``
    不是 DML，所以最外层的 ``RELEASE SAVEPOINT`` 按 SQLite 语义**就是 COMMIT**。
    后果是调用方 ``session.rollback()`` 撤不回这两次写入——退款先落库、权益还原
    后失败时，用户会同时拿到权益和退款，UI 却写着「退款也失败了」。
    """

    async def test_award_points_is_withdrawn_by_an_outer_rollback(self) -> None:
        async with self.session_factory() as session:
            self.assertTrue(
                await point_shop.award_points(
                    session,
                    group_id=GROUP_ID,
                    user_id=7,
                    points=30,
                    reason=point_shop.AWARD_REASON_REFUND,
                    ref="shop-refund:shop-tag-7d:x",
                )
            )
            await session.rollback()

        self.assertEqual(
            await self._awards(7), [], "award_points 提前提交了，rollback 撤不回来"
        )

    async def test_entitlement_insert_is_withdrawn_by_an_outer_rollback(self) -> None:
        async with self.session_factory() as session:
            await point_shop.upsert_entitlement(
                session,
                group_id=GROUP_ID,
                user_id=7,
                kind=KIND_TAG,
                payload="摸鱼冠军",
                ref="shop-tag-7d:x",
                expires_at=_day(7),
                now=_day(),
            )
            await session.rollback()

        self.assertEqual(
            await self._entitlements(), [], "upsert_entitlement 提前提交了，撤不回来"
        )

    async def test_duplicate_ref_still_returns_false_without_touching_the_tx(self) -> None:
        """换成 ON CONFLICT 之后幂等语义不变：重复 ref 返回 False，不抛异常。"""

        async with self.session_factory() as session:
            first = await point_shop.award_points(
                session,
                group_id=GROUP_ID,
                user_id=7,
                points=30,
                reason=point_shop.AWARD_REASON_REFUND,
                ref="dup-ref",
            )
            await session.commit()
        async with self.session_factory() as session:
            second = await point_shop.award_points(
                session,
                group_id=GROUP_ID,
                user_id=7,
                points=30,
                reason=point_shop.AWARD_REASON_REFUND,
                ref="dup-ref",
            )
            await session.commit()

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(await self._awards(7)), 1)

    async def test_refund_and_entitlement_revert_die_together(self) -> None:
        """端到端 D2-02：续费时 Telegram 拒绝头衔、权益还原又抛错。

        修前：退款已经被 SAVEPOINT 提交，``rollback()`` 撤不回，权益还原也没做成
        ——用户白得 7 天头衔**和** 30 分退款，回执却写「退款也失败了」。
        修后：「退款 + 权益还原」同生共死：两样都没落库，状态退回"这笔续费照旧
        成立"（扣费和权益本来就在调 Telegram 之前提交了），用户没有被白送。
        """

        await self._grant_points(7, 200)
        first = await self._buy_tag(FakeBot(), raw="Tester")
        self.assertEqual(first.status, "ok")
        self.assertEqual(await self._balance(7), 170)
        before_expiry = (await self._entitlements())[0].expires_at
        # 续费（_day(1)）从旧到期时间 _day(7) 往后延 7 天 = _day(14)
        self.assertEqual(before_expiry, _day(7))

        failing = FakeBot(tag_error=_bot_error("Bad Request: tag is invalid"))
        with patch(
            "bot.services.point_shop.restore_entitlement",
            new=AsyncMock(side_effect=RuntimeError("database is locked")),
        ):
            reply = await self._buy_tag(failing, raw="tester", now=_day(1))

        self.assertEqual(reply.status, "telegram_failed")
        self.assertFalse(reply.refunded, "退款没落库就不能报成功")
        self.assertEqual(
            [row for row in await self._awards(7) if row.reason == "shop_refund"],
            [],
            "退款被提前提交了：用户既留着续费的权益又拿回了分（白嫖头衔）",
        )
        # 扣费与权益在调 Telegram 之前就已提交，所以这一对的"都没做"表现为：
        # 续费的 30 分照扣、续期的到期时间照留——与"没有退款行"这件事**自洽**。
        # 修前这里会是 170（钱退了）**且**到期时间 = _day(14)（权益还在），
        # 那才是「白嫖 7 天头衔 + 白拿 30 分」的双重收益。
        self.assertEqual(await self._balance(7), 140, "退款必须随外层事务一起回滚")
        self.assertEqual(
            (await self._entitlements())[0].expires_at,
            _day(14),
            "退款行不存在时，续费的权益必须自洽地留着，不能出现「已退款 + 权益还在」",
        )

    async def test_refund_and_revert_still_both_persist_on_success(self) -> None:
        """对照组：_refund_and_reload 正常时，退款与权益还原必须**都**落库。"""

        await self._grant_points(7, 200)
        self.assertEqual((await self._buy_tag(FakeBot(), raw="Tester")).status, "ok")
        before_row = (await self._entitlements())[0]
        before_values = (before_row.payload, before_row.ref, before_row.expires_at)

        failing = FakeBot(tag_error=_bot_error("Bad Request: tag is invalid"))
        reply = await self._buy_tag(failing, raw="tester", now=_day(1))

        self.assertEqual(reply.status, "telegram_failed")
        self.assertTrue(reply.refunded)
        self.assertEqual(
            [
                row.points
                for row in await self._awards(7)
                if row.reason == "shop_refund"
            ],
            [30],
        )
        self.assertEqual(await self._balance(7), 170)
        after_row = (await self._entitlements())[0]
        self.assertEqual(
            (after_row.payload, after_row.ref, after_row.expires_at), before_values
        )




# ---------------------------------------------------------------------------
# 到期清理
# ---------------------------------------------------------------------------


class ExpirySweepTests(_DbTestCase):
    async def _seed_entitlement(
        self,
        *,
        kind: str,
        payload: str,
        expires_at: datetime,
        user_id: int = 7,
        group_id: int = GROUP_ID,
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                MemberEntitlement(
                    group_id=group_id,
                    user_id=user_id,
                    kind=kind,
                    payload=payload,
                    ref="shop-test",
                    expires_at=expires_at,
                )
            )
            await session.commit()

    async def _sweep(self, bot: FakeBot, *, now: datetime | None = None, **kwargs):
        async with self.session_factory() as session:
            return await expire_due_entitlements(
                session, bot=bot, now=now or _day(), **kwargs
            )

    async def test_expired_tag_is_cleared_and_the_row_is_deleted(self) -> None:
        await self._seed_entitlement(
            kind=KIND_TAG, payload="摸鱼冠军", expires_at=_day(-1)
        )
        bot = FakeBot()

        outcomes = await self._sweep(bot)

        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0].ok)
        self.assertEqual(bot.tag_calls, [(GROUP_ID, 7, "")])
        self.assertEqual(await self._entitlements(), [])
        self.assertEqual(bot.sent[0][0], 7, "先私聊提醒")
        self.assertIn("摸鱼冠军", bot.sent[0][1])

    async def test_not_yet_due_entitlements_are_left_alone(self) -> None:
        await self._seed_entitlement(
            kind=KIND_TAG, payload="摸鱼冠军", expires_at=_day(1)
        )
        bot = FakeBot()

        outcomes = await self._sweep(bot)

        self.assertEqual(outcomes, [])
        self.assertEqual(bot.tag_calls, [])
        self.assertEqual(len(await self._entitlements()), 1)

    async def test_a_second_sweep_does_nothing(self) -> None:
        await self._seed_entitlement(
            kind=KIND_PIN, payload="555", expires_at=_day(-1)
        )
        bot = FakeBot()

        first = await self._sweep(bot)
        second = await self._sweep(bot)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "幂等：重跑不能重复调 Telegram")
        self.assertEqual(bot.unpin_calls, [(GROUP_ID, 555)])
        self.assertEqual(await self._entitlements(), [])

    async def test_every_due_entitlement_is_processed_in_one_pass(self) -> None:
        await self._seed_entitlement(
            kind=KIND_TAG, payload="甲", expires_at=_day(-2), user_id=7
        )
        await self._seed_entitlement(
            kind=KIND_TAG, payload="乙", expires_at=_day(-1), user_id=8
        )
        await self._seed_entitlement(
            kind=KIND_PIN, payload="777", expires_at=_day(-1), user_id=9
        )
        bot = FakeBot()

        outcomes = await self._sweep(bot)

        self.assertEqual(len(outcomes), 3)
        self.assertEqual(len(bot.tag_calls), 2)
        self.assertEqual(bot.unpin_calls, [(GROUP_ID, 777)])
        self.assertEqual(await self._entitlements(), [])

    async def test_dry_run_changes_nothing(self) -> None:
        await self._seed_entitlement(
            kind=KIND_TAG, payload="摸鱼冠军", expires_at=_day(-1)
        )
        bot = FakeBot()

        outcomes = await self._sweep(bot, dry_run=True)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0].action, "dry-run")
        self.assertEqual(bot.tag_calls, [])
        self.assertEqual(bot.sent, [])
        self.assertEqual(len(await self._entitlements()), 1, "dry-run 不能删行")

    async def test_revoke_failure_keeps_the_row_and_defers_the_retry(self) -> None:
        """F-052：撤销失败时不能删行——否则付费头衔永远留在群里且无人重试。

        旧断言（``失败的行也要删``）锁的正是这个 bug：删掉记录只解决了"扫描卡在
        同一条"，代价是"再也撤不下来"。产品行为已按审查结论改变，所以这里改成
        保留行 + 推后 ``expires_at``（避免每轮都撞同一条）。
        """

        await self._seed_entitlement(
            kind=KIND_TAG, payload="摸鱼冠军", expires_at=_day(-1)
        )
        bot = FakeBot(tag_error=_bot_error("Bad Request: user not found"))

        outcomes = await self._sweep(bot)

        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0].ok)
        self.assertFalse(outcomes[0].notified, "没撤下来就不能说已经清除")
        rows = await self._entitlements()
        self.assertEqual(len(rows), 1, "没撤下来就不能删行")
        self.assertGreater(
            rows[0].expires_at,
            _day(),
            "失败的行要推后到期时间再重试，而不是每轮都重扫同一条",
        )

    async def test_deferred_row_is_retried_after_the_interval(self) -> None:
        """F-052：到了重试时间再扫一次，这次撤销成功才删行。"""

        await self._seed_entitlement(
            kind=KIND_TAG, payload="摸鱼冠军", expires_at=_day(-1)
        )
        failing = FakeBot(tag_error=_bot_error("Bad Request: user not found"))
        await self._sweep(failing)

        rows = await self._entitlements()
        self.assertEqual(len(rows), 1)
        retry_at = rows[0].expires_at

        recovering = FakeBot()
        before = await self._sweep(recovering, now=retry_at - timedelta(seconds=1))
        self.assertEqual(before, [], "还没到重试时间就不该再打 Telegram")
        self.assertEqual(recovering.tag_calls, [])

        after = await self._sweep(recovering, now=retry_at + timedelta(seconds=1))
        self.assertEqual(len(after), 1)
        self.assertTrue(after[0].ok)
        self.assertEqual(recovering.tag_calls, [(GROUP_ID, 7, "")])
        self.assertEqual(await self._entitlements(), [])

    async def test_already_unpinned_is_treated_as_success(self) -> None:
        await self._seed_entitlement(
            kind=KIND_PIN,
            payload="555",
            expires_at=_day(-1),
        )
        bot = FakeBot(unpin_error=_bot_error("Bad Request: message is not pinned"))

        outcomes = await self._sweep(bot)

        self.assertTrue(outcomes[0].ok)
        self.assertEqual(await self._entitlements(), [])

    async def test_private_notice_failure_falls_back_to_the_group(self) -> None:
        await self._seed_entitlement(
            kind=KIND_TAG, payload="摸鱼冠军", expires_at=_day(-1)
        )
        bot = FakeBot(private_error=RuntimeError("bot was blocked by the user"))

        await self._sweep(bot)

        self.assertEqual([chat for chat, _text in bot.sent], [GROUP_ID])

    async def test_notify_can_be_switched_off(self) -> None:
        await self._seed_entitlement(
            kind=KIND_PIN, payload="555", expires_at=_day(-1)
        )
        bot = FakeBot()

        await self._sweep(bot, notify=False)

        self.assertEqual(bot.sent, [])
        self.assertEqual(bot.unpin_calls, [(GROUP_ID, 555)])


class ExpirySchemaTests(unittest.TestCase):
    def test_schema_guards_one_slot_per_member_and_indexes_expiry(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("member_entitlements")
        }
        slot = indexes["ix_member_entitlements_slot"]
        self.assertTrue(slot["unique"], "一人一项必须靠唯一索引兜底")
        self.assertEqual(slot["column_names"], ["group_id", "user_id", "kind"])
        self.assertEqual(
            indexes["ix_member_entitlements_expires"]["column_names"], ["expires_at"]
        )


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------


class ShopTextTests(unittest.TestCase):
    def test_menu_lists_every_price_and_exact_usage(self) -> None:
        text = render_shop_menu(available=42)

        self.assertIn("当前可用 <b>42</b> 分", text)
        self.assertIn("30 分", text)
        self.assertIn("80 分", text)
        self.assertIn("20 分", text)
        self.assertIn("5 分", text)
        self.assertIn("/tag 摸鱼冠军", text)
        self.assertIn("/tag 摸鱼冠军 30天", text)
        self.assertIn("/top", text)
        self.assertIn("/draw", text)

    def test_menu_has_no_technical_jargon(self) -> None:
        text = render_shop_menu(available=1)

        for forbidden in (
            "member_point",
            "member_entitlements",
            "ref",
            "ledger",
            "流水",
            "payload",
        ):
            self.assertNotIn(forbidden, text, forbidden)

    def test_balance_text_always_shows_the_number(self) -> None:
        self.assertIn("当前可用 7 分", render_balance(available=7))
        self.assertIn("连续签到 3 天", render_balance(available=7, streak=3))


# ---------------------------------------------------------------------------
# 处理器（用户真正看到的那一层）
# ---------------------------------------------------------------------------


class ShopHandlerTests(_DbTestCase):
    def _message(
        self,
        text: str,
        *,
        bot: FakeBot | None = None,
        user_id: int = 7,
        chat_type: str = "supergroup",
        reply_to=None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=GROUP_ID, type=chat_type),
            from_user=SimpleNamespace(id=user_id, full_name="小明", is_bot=False),
            bot=bot or FakeBot(),
            reply_to_message=reply_to,
        )

    async def _run_handler(self, handler, message) -> list[str]:
        answers: list[str] = []
        keyboards: list[object] = []

        async def fake_answer(message, settings, body, **kwargs):
            answers.append(body)
            keyboards.append(kwargs.get("reply_markup"))

        with (
            patch.object(commands, "_answer", side_effect=fake_answer),
            patch.object(
                commands,
                "ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
        ):
            async with self.session_factory() as session:
                await handler(message, session, _settings())
        self.keyboards = keyboards
        return answers

    async def test_shop_command_renders_menu_with_a_working_button(self) -> None:
        await self._grant_points(7, 42)

        answers = await self._run_handler(commands.cmd_shop, self._message("/shop"))

        self.assertIn("积分商店", answers[-1])
        self.assertIn("当前可用 <b>42</b> 分", answers[-1])
        button = self.keyboards[-1].inline_keyboard[0][0]
        self.assertEqual(button.text, "我的积分")
        self.assertEqual(button.callback_data, "shop:points")

    async def test_shop_command_is_group_only(self) -> None:
        answers = await self._run_handler(
            commands.cmd_shop, self._message("/shop", chat_type="private")
        )

        self.assertIn("仅可在群内使用", answers[-1])

    async def test_tag_command_rejects_emoji_without_charging(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot()

        answers = await self._run_handler(
            commands.cmd_tag, self._message("/tag 摸鱼😀", bot=bot)
        )

        self.assertIn("表情", answers[-1])
        self.assertEqual(bot.tag_calls, [])
        self.assertEqual(await self._balance(7), 100)

    async def test_tag_command_without_argument_explains_usage(self) -> None:
        await self._grant_points(7, 100)

        answers = await self._run_handler(commands.cmd_tag, self._message("/tag"))

        self.assertIn("用法", answers[-1])
        self.assertIn("/tag", answers[-1])

    async def test_tag_command_reports_the_final_state(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot()

        answers = await self._run_handler(
            commands.cmd_tag, self._message("/tag@some_bot 摸鱼冠军", bot=bot)
        )

        self.assertIn("头衔已生效", answers[-1])
        self.assertIn("当前可用 70 分", answers[-1])
        self.assertEqual(bot.tag_calls, [(GROUP_ID, 7, "摸鱼冠军")])

    async def test_top_command_explains_usage_when_not_replying(self) -> None:
        await self._grant_points(7, 100)

        answers = await self._run_handler(commands.cmd_top, self._message("/top"))

        self.assertIn("/top", answers[-1])
        self.assertIn("回复", answers[-1])

    async def test_top_command_rejects_someone_elses_message(self) -> None:
        await self._grant_points(7, 100)
        message = self._message(
            "/top",
            reply_to=SimpleNamespace(
                message_id=9,
                from_user=SimpleNamespace(id=8, is_bot=False),
                sender_chat=None,
            ),
        )

        answers = await self._run_handler(commands.cmd_top, message)

        self.assertIn("只能置顶你自己的消息", answers[-1])
        self.assertEqual(await self._balance(7), 100)

    async def test_top_command_pins_your_own_message(self) -> None:
        await self._grant_points(7, 100)
        bot = FakeBot()
        message = self._message(
            "/top",
            bot=bot,
            reply_to=SimpleNamespace(
                message_id=321,
                from_user=SimpleNamespace(id=7, is_bot=False),
                sender_chat=None,
            ),
        )

        answers = await self._run_handler(commands.cmd_top, message)

        self.assertIn("置顶成功", answers[-1])
        self.assertEqual(bot.pin_calls, [(GROUP_ID, 321, True)])

    async def test_draw_command_renders_the_lottery_receipt(self) -> None:
        await self._grant_points(7, 36)

        with patch.object(point_shop.secrets, "randbelow", return_value=8600):
            answers = await self._run_handler(commands.cmd_draw, self._message("/draw"))

        self.assertIn("🎲 抽奖结果：12 分", answers[-1])
        self.assertIn("本次净 +7 分，当前可用 43 分", answers[-1])

    async def test_balance_button_really_returns_the_balance(self) -> None:
        await self._grant_points(7, 25)
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=GROUP_ID, bot_present=True))
            await session.commit()
        callback = SimpleNamespace(
            data="shop:points",
            message=SimpleNamespace(chat=SimpleNamespace(id=GROUP_ID, type="supergroup")),
            from_user=SimpleNamespace(id=7, is_bot=False),
            answer=AsyncMock(),
        )

        async with self.session_factory() as session:
            await commands.on_shop_balance(callback, _settings(), session)

        callback.answer.assert_awaited_once()
        text = callback.answer.await_args.args[0]
        self.assertIn("当前可用 25 分", text)
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))

    async def test_balance_button_refuses_unauthorized_groups(self) -> None:
        callback = SimpleNamespace(
            data="shop:points",
            message=SimpleNamespace(chat=SimpleNamespace(id=GROUP_ID, type="supergroup")),
            from_user=SimpleNamespace(id=7, is_bot=False),
            answer=AsyncMock(),
        )

        async with self.session_factory() as session:
            await commands.on_shop_balance(callback, _settings(), session)

        self.assertIn("未授权", callback.answer.await_args.args[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
