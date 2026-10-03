"""1 对 1 私聊（DM）回归测试。

测的是会直接影响钱和观感的那几件事，不是"函数能跑"：

- **准入四态**：超管豁免 / 是成员 / 不是成员 / **查不通（不确定）**——最后一种既
  不能放行（会变成免费代理）也不能拒绝（会误伤真人），必须单独走一条提示；
- **配额阶梯**：普通成员 100 条/天（本档全局 20000）、群管理员 500 条/天（本档全局
  100000）、最高管理员不计数；超限要**整体回滚**，不能在库里留下"扣了但没用"的脏计数；
  跨零点自动换行；两档各记一本全局账；
- **私聊正文只走 user 角色 + 不可信围栏**（F-003 的私聊版）：成员可控文本一个字
  都不许出现在 system 消息里；
- **不落库**：历史只在内存，进程重启即空；
- **handler 分支**：命令不抢、非成员/超限/看不了媒体都**不进模型**（不花钱）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.db.engine import init_db
from bot.db.models import PrivateChatUsage
from bot.handlers import private_chat as dm_handler
from bot.services import private_chat as dm

SUPER_ADMIN = 601298409
GROUP_ID = -1001364206062
MEDIA_ATTRS = (
    "photo",
    "document",
    "sticker",
    "animation",
    "video",
    "video_note",
    "voice",
    "audio",
)


def _settings(super_admin_id: int = SUPER_ADMIN) -> SimpleNamespace:
    # firecrawl_api_key 是顶层字段：检索技能拿的是这个对象本身
    return SimpleNamespace(
        super_admin_id=super_admin_id,
        bot=SimpleNamespace(),
        firecrawl_api_key="",
    )


def _verdict(tier: str = dm.TIER_MEMBER) -> dm.AccessVerdict:
    """按档位造一个准入结论（handler 现在拿到的是「结论 + 档位」）。"""

    if tier == dm.TIER_NONE:
        return dm.AccessVerdict(False, tier)
    if tier == dm.TIER_UNKNOWN:
        return dm.AccessVerdict(None, tier)
    return dm.AccessVerdict(True, tier)


def _bot(*, statuses=None, raises=None, absent_groups=(), failing_groups=()) -> MagicMock:
    """假 Bot：``get_chat_member`` 按 group_id 返回状态、抛「不在群」或抛通用异常。

    ``absent_groups`` 模拟真机行为：对不在群里的人，Telegram 抛的是
    ``Bad Request: member not found``，**不是** ``status="left"``。
    """

    bot = MagicMock()
    mapping = dict(statuses or {})
    absent = {int(g) for g in absent_groups}
    failing = {int(g) for g in failing_groups}

    async def _get_chat_member(chat_id, user_id):
        cid = int(chat_id)
        if cid in absent:
            raise RuntimeError("Telegram server says - Bad Request: member not found")
        if cid in failing:
            raise RuntimeError("telegram down")
        if raises:
            raise RuntimeError(raises if isinstance(raises, str) else "telegram down")
        member = MagicMock()
        member.status = mapping.get(cid, "left")
        member.is_member = member.status != "left"
        return member

    bot.get_chat_member = AsyncMock(side_effect=_get_chat_member)
    return bot


def _message(*, text=None, caption=None) -> MagicMock:
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=4242, type="private")
    msg.from_user = SimpleNamespace(id=777, username="member", is_bot=False, first_name="M")
    msg.text = text
    msg.caption = caption
    for attr in MEDIA_ATTRS:
        setattr(msg, attr, None)
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.get_chat_member = AsyncMock()
    msg.bot.send_chat_action = AsyncMock()
    return msg


# ---------------------------------------------------------------------------
# 自然日与缓存
# ---------------------------------------------------------------------------


class LocalDayKeyTests(unittest.TestCase):
    def test_day_key_uses_local_calendar_day(self) -> None:
        self.assertEqual(dm.local_day_key(datetime(2026, 10, 3, 23, 59)), "2026-10-03")
        self.assertEqual(dm.local_day_key(datetime(2026, 10, 4, 0, 1)), "2026-10-04")
        self.assertEqual(len(dm.local_day_key()), 10)


class MemberAccessCacheTests(unittest.TestCase):
    def test_ttl_and_clear(self) -> None:
        clock = {"now": 1000.0}
        cache = dm.MemberAccessCache(ttl_seconds=60.0, clock=lambda: clock["now"])
        self.assertIsNone(cache.get(1))
        cache.put(1, dm.TIER_MEMBER)
        self.assertEqual(cache.get(1), dm.TIER_MEMBER)
        clock["now"] += 59.0
        self.assertEqual(cache.get(1), dm.TIER_MEMBER)
        clock["now"] += 2.0
        self.assertIsNone(cache.get(1), "过期后必须重新判定")
        cache.put(2, dm.TIER_NONE)
        self.assertEqual(len(cache), 1)
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_capacity_evicts_instead_of_growing(self) -> None:
        cache = dm.MemberAccessCache(max_users=16)
        for uid in range(100):
            cache.put(uid, dm.TIER_MEMBER)
        self.assertLessEqual(len(cache), 16)


class NoticeThrottleTests(unittest.TestCase):
    def test_same_notice_is_throttled_per_user(self) -> None:
        clock = {"now": 0.0}
        throttle = dm.NoticeThrottle(cooldown_seconds=3600.0, clock=lambda: clock["now"])
        self.assertTrue(throttle.allow(7, "quota"))
        self.assertFalse(throttle.allow(7, "quota"), "一小时内同一种提示只说一次")
        self.assertTrue(throttle.allow(7, "media"), "不同种类的提示互不影响")
        self.assertTrue(throttle.allow(8, "quota"), "不同的人互不影响")
        clock["now"] = 3601.0
        self.assertTrue(throttle.allow(7, "quota"))


class HistoryStoreTests(unittest.TestCase):
    def test_history_is_memory_only_and_bounded(self) -> None:
        store = dm.PrivateHistoryStore()
        store.append(1, "user", "在吗")
        store.append(1, "assistant", "在的")
        self.assertEqual(
            store.history(1),
            [
                {"role": "user", "content": "在吗"},
                {"role": "assistant", "content": "在的"},
            ],
        )
        for i in range(100):
            store.append(1, "user", f"第{i}条")
        self.assertLessEqual(len(store.history(1)), dm.HISTORY_MAX_TURNS * 2)
        store.clear(1)
        self.assertEqual(store.history(1), [])

    def test_blank_turn_is_ignored(self) -> None:
        store = dm.PrivateHistoryStore()
        store.append(1, "user", "   ")
        self.assertEqual(store.history(1), [])


# ---------------------------------------------------------------------------
# 准入判定
# ---------------------------------------------------------------------------


class AccessDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_super_admin_is_exempt_without_any_lookup(self) -> None:
        bot = _bot()
        with patch.object(dm, "list_authorized_groups", new=AsyncMock()) as groups:
            allowed = await dm.confirm_authorized_group_member(
                bot, AsyncMock(), _settings(), SUPER_ADMIN, cache=dm.MemberAccessCache()
            )
        self.assertIs(allowed, True)
        bot.get_chat_member.assert_not_awaited()
        groups.assert_not_awaited()

    async def test_member_of_authorized_group_is_allowed_and_cached(self) -> None:
        bot = _bot(statuses={GROUP_ID: "member"})
        cache = dm.MemberAccessCache()
        session = AsyncMock()
        with patch.object(
            dm, "list_authorized_groups", new=AsyncMock(return_value=[SimpleNamespace(group_id=GROUP_ID)])
        ):
            self.assertIs(
                await dm.confirm_authorized_group_member(bot, session, _settings(), 777, cache=cache),
                True,
            )
            self.assertIs(
                await dm.confirm_authorized_group_member(bot, session, _settings(), 777, cache=cache),
                True,
            )
        self.assertEqual(bot.get_chat_member.await_count, 1, "命中缓存后不该再打 Telegram")

    async def test_restricted_follows_is_member(self) -> None:
        bot = _bot(statuses={GROUP_ID: "restricted"})
        with patch.object(
            dm, "list_authorized_groups", new=AsyncMock(return_value=[SimpleNamespace(group_id=GROUP_ID)])
        ):
            self.assertIs(
                await dm.confirm_authorized_group_member(
                    bot, AsyncMock(), _settings(), 777, cache=dm.MemberAccessCache()
                ),
                True,
                "被禁言但还在群 = 群成员",
            )

        bot_left = _bot(statuses={GROUP_ID: "left"})
        with patch.object(
            dm, "list_authorized_groups", new=AsyncMock(return_value=[SimpleNamespace(group_id=GROUP_ID)])
        ):
            self.assertIs(
                await dm.confirm_authorized_group_member(
                    bot_left, AsyncMock(), _settings(), 777, cache=dm.MemberAccessCache()
                ),
                False,
            )

    async def test_member_not_found_is_a_definitive_no_and_gets_cached(self) -> None:
        """真机行为：不在群的人返回 ``member not found`` 异常，不是 status=left。"""

        bot = _bot(absent_groups=[GROUP_ID])
        cache = dm.MemberAccessCache()
        session = AsyncMock()
        with patch.object(
            dm, "list_authorized_groups", new=AsyncMock(return_value=[SimpleNamespace(group_id=GROUP_ID)])
        ):
            self.assertIs(
                await dm.confirm_authorized_group_member(bot, session, _settings(), 777, cache=cache),
                False,
                "陌生人必须是明确的「不是成员」，不能退化成「稍等再试」",
            )
            self.assertIs(
                await dm.confirm_authorized_group_member(bot, session, _settings(), 777, cache=cache),
                False,
            )
        self.assertEqual(bot.get_chat_member.await_count, 1, "结论确定，要缓存，别每条都打 API")

    async def test_absent_in_one_group_but_member_of_another(self) -> None:
        bot = _bot(absent_groups=[GROUP_ID], statuses={-100999: "member"})
        with patch.object(
            dm,
            "list_authorized_groups",
            new=AsyncMock(return_value=[SimpleNamespace(group_id=GROUP_ID), SimpleNamespace(group_id=-100999)]),
        ):
            self.assertIs(
                await dm.confirm_authorized_group_member(
                    bot, AsyncMock(), _settings(), 777, cache=dm.MemberAccessCache()
                ),
                True,
                "只要在一个授权群里就是成员",
            )

    async def test_unknown_when_telegram_fails_and_result_is_not_cached(self) -> None:
        bot = _bot(raises=True)
        cache = dm.MemberAccessCache()
        with patch.object(
            dm, "list_authorized_groups", new=AsyncMock(return_value=[SimpleNamespace(group_id=GROUP_ID)])
        ):
            self.assertIsNone(
                await dm.confirm_authorized_group_member(bot, AsyncMock(), _settings(), 777, cache=cache)
            )
            self.assertEqual(len(cache), 0, "查不通不能缓存结论")
            self.assertIsNone(
                await dm.confirm_authorized_group_member(bot, AsyncMock(), _settings(), 777, cache=cache)
            )
        self.assertEqual(bot.get_chat_member.await_count, 2, "第二次要重试")

    async def test_no_authorized_group_means_not_member(self) -> None:
        bot = _bot()
        with patch.object(dm, "list_authorized_groups", new=AsyncMock(return_value=[])):
            self.assertIs(
                await dm.confirm_authorized_group_member(
                    bot, AsyncMock(), _settings(), 777, cache=dm.MemberAccessCache()
                ),
                False,
            )

    async def test_group_admin_is_recognized_as_admin_tier(self) -> None:
        """群主与群管理员都算「群管理员」档（500/天）。"""

        for status in ("administrator", "creator"):
            with self.subTest(status=status):
                bot = _bot(statuses={GROUP_ID: status})
                with patch.object(
                    dm,
                    "list_authorized_groups",
                    new=AsyncMock(return_value=[SimpleNamespace(group_id=GROUP_ID)]),
                ):
                    verdict = await dm.resolve_access(
                        bot, AsyncMock(), _settings(), 777, cache=dm.MemberAccessCache()
                    )
                self.assertTrue(verdict.allowed)
                self.assertEqual(verdict.tier, dm.TIER_ADMIN)
                self.assertTrue(verdict.is_admin)
                self.assertFalse(verdict.is_super)

    async def test_highest_tier_wins_across_groups(self) -> None:
        bot = _bot(statuses={GROUP_ID: "member", -100999: "administrator"})
        with patch.object(
            dm,
            "list_authorized_groups",
            new=AsyncMock(
                return_value=[
                    SimpleNamespace(group_id=GROUP_ID),
                    SimpleNamespace(group_id=-100999),
                ]
            ),
        ):
            verdict = await dm.resolve_access(
                bot, AsyncMock(), _settings(), 777, cache=dm.MemberAccessCache()
            )
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.tier, dm.TIER_ADMIN, "在一个群是管理员就按管理员档")

    async def test_member_tier_is_not_cached_while_another_lookup_failed(self) -> None:
        """还有一个群没查成时先放行但不缓存：下次重查可能把他升成管理员。"""

        bot = _bot(statuses={GROUP_ID: "member"}, failing_groups=[-100999])
        cache = dm.MemberAccessCache()
        with patch.object(
            dm,
            "list_authorized_groups",
            new=AsyncMock(
                return_value=[
                    SimpleNamespace(group_id=-100999),
                    SimpleNamespace(group_id=GROUP_ID),
                ]
            ),
        ):
            verdict = await dm.resolve_access(bot, AsyncMock(), _settings(), 777, cache=cache)
        self.assertTrue(verdict.allowed, "已查到是成员就先放行，不因另一个群查不通而拒绝")
        self.assertEqual(verdict.tier, dm.TIER_MEMBER)
        self.assertEqual(len(cache), 0, "结论不完整时不该缓存")


# ---------------------------------------------------------------------------
# 配额（真库）
# ---------------------------------------------------------------------------


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    async def _stored(self, user_id: int, day: str) -> int:
        from sqlalchemy import select

        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(PrivateChatUsage).where(
                        PrivateChatUsage.user_id == user_id,
                        PrivateChatUsage.usage_date == day,
                    )
                )
            ).scalar_one_or_none()
            return int(row.messages) if row else 0


class QuotaTests(_DbTestCase):
    async def test_member_limit_blocks_the_101st(self) -> None:
        day = "2026-10-03"
        for i in range(1, 101):
            async with self.session_factory() as session:
                outcome = await dm.consume_daily_quota(session, user_id=777, day=day)
            self.assertTrue(outcome.allowed, f"第 {i} 条应该放行")
        async with self.session_factory() as session:
            blocked = await dm.consume_daily_quota(session, user_id=777, day=day)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "user_limit")
        self.assertEqual(blocked.user_used, dm.DEFAULT_PER_USER_DAILY_LIMIT)

    async def test_denied_call_leaves_no_dirty_counter(self) -> None:
        day = "2026-10-03"
        for _ in range(100):
            async with self.session_factory() as session:
                await dm.consume_daily_quota(session, user_id=777, day=day)
        async with self.session_factory() as session:
            await dm.consume_daily_quota(session, user_id=777, day=day)
        self.assertEqual(await self._stored(777, day), 100, "被拒的那一次必须回滚干净")
        self.assertEqual(
            await self._stored(dm.GLOBAL_COUNTER_USER_ID, day), 100, "全局计数同样要回滚"
        )

    async def test_global_limit_blocks_other_users(self) -> None:
        day = "2026-10-03"
        for uid in (1, 2):
            async with self.session_factory() as session:
                outcome = await dm.consume_daily_quota(
                    session, user_id=uid, day=day, global_limit=2
                )
            self.assertTrue(outcome.allowed)
        async with self.session_factory() as session:
            blocked = await dm.consume_daily_quota(
                session, user_id=3, day=day, global_limit=2
            )
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "global_limit")
        self.assertIn("明天", dm.quota_notice(blocked))

    async def test_quota_resets_on_the_next_local_day(self) -> None:
        for _ in range(100):
            async with self.session_factory() as session:
                await dm.consume_daily_quota(session, user_id=777, day="2026-10-03")
        async with self.session_factory() as session:
            next_day = await dm.consume_daily_quota(session, user_id=777, day="2026-10-04")
        self.assertTrue(next_day.allowed, "跨零点自动换行")
        self.assertEqual(next_day.user_used, 1)
        self.assertEqual(await self._stored(777, "2026-10-04"), 1)

    async def test_record_contact_writes_without_any_limit(self) -> None:
        async with self.session_factory() as session:
            for _ in range(150):  # 远超普通成员 100 条上限，也照样记
                await dm.record_contact(
                    session, user_id=999, day="2026-10-02", stamp=datetime(2026, 10, 2, 21, 3)
                )
        self.assertEqual(await self._stored(999, "2026-10-02"), 150)

    async def test_record_contact_survives_a_broken_session(self) -> None:
        class _Boom:
            async def commit(self) -> None:
                raise RuntimeError("db down")

            async def rollback(self) -> None:
                return None

        await dm.record_contact(_Boom(), user_id=1, day="2026-10-02")  # 不该抛

    async def test_last_contact_returns_the_previous_day_record(self) -> None:
        async with self.session_factory() as session:
            await dm.consume_daily_quota(
                session,
                user_id=777,
                day="2026-10-02",
                stamp=datetime(2026, 10, 2, 21, 3),
            )
        async with self.session_factory() as session:
            record = await dm.last_contact_record(session, user_id=777, day="2026-10-03")
        self.assertIn("2026-10-02", record)
        self.assertIn("21:03", record)

    async def test_last_contact_ignores_today(self) -> None:
        async with self.session_factory() as session:
            await dm.consume_daily_quota(
                session, user_id=777, day="2026-10-03", stamp=datetime(2026, 10, 3, 9, 0)
            )
        async with self.session_factory() as session:
            record = await dm.last_contact_record(session, user_id=777, day="2026-10-03")
        self.assertEqual(record, "", "今天不算「上一次」，否则每句都变成考勤")

    async def test_last_contact_empty_for_a_stranger(self) -> None:
        async with self.session_factory() as session:
            record = await dm.last_contact_record(session, user_id=424242, day="2026-10-03")
        self.assertEqual(record, "", "没有历史就不给，绝不能让模型自己编时间")

    async def test_one_row_per_user_per_day(self) -> None:
        from sqlalchemy import func, select

        for _ in range(3):
            async with self.session_factory() as session:
                await dm.consume_daily_quota(session, user_id=777, day="2026-10-03")
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(func.count()).select_from(PrivateChatUsage).where(
                        PrivateChatUsage.user_id == 777
                    )
                )
            ).scalar_one()
        self.assertEqual(int(rows), 1, "UPSERT 不能留下两行")

    async def test_admin_tier_uses_its_own_limits_and_counter_row(self) -> None:
        day = "2026-10-03"
        async with self.session_factory() as session:
            outcome = await dm.consume_daily_quota(session, user_id=777, is_admin=True, day=day)
        self.assertTrue(outcome.allowed)
        self.assertEqual(outcome.tier, dm.TIER_ADMIN)
        self.assertEqual(outcome.per_user_limit, dm.ADMIN_PER_USER_DAILY_LIMIT)
        self.assertEqual(outcome.global_limit, dm.ADMIN_GLOBAL_DAILY_LIMIT)
        self.assertEqual(await self._stored(777, day), 1)
        self.assertEqual(await self._stored(dm.ADMIN_GLOBAL_COUNTER_USER_ID, day), 1)
        self.assertEqual(
            await self._stored(dm.GLOBAL_COUNTER_USER_ID, day),
            0,
            "管理员不吃普通成员那本全局账",
        )

    async def test_member_and_admin_globals_are_independent(self) -> None:
        day = "2026-10-03"
        for uid in (1, 2):
            async with self.session_factory() as session:
                outcome = await dm.consume_daily_quota(
                    session, user_id=uid, day=day, global_limit=2
                )
            self.assertTrue(outcome.allowed)
        async with self.session_factory() as session:
            blocked = await dm.consume_daily_quota(session, user_id=3, day=day, global_limit=2)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "global_limit")
        async with self.session_factory() as session:
            admin_ok = await dm.consume_daily_quota(session, user_id=4, is_admin=True, day=day)
        self.assertTrue(admin_ok.allowed, "普通成员那档满了，管理员那档照常")

    async def test_member_tier_defaults_to_100_per_day(self) -> None:
        day = "2026-10-03"
        async with self.session_factory() as session:
            outcome = await dm.consume_daily_quota(session, user_id=777, day=day)
        self.assertEqual(outcome.tier, dm.TIER_MEMBER)
        self.assertEqual(outcome.per_user_limit, 100)
        self.assertEqual(outcome.global_limit, 20_000)


# ---------------------------------------------------------------------------
# 回复组装
# ---------------------------------------------------------------------------


class PromptTests(unittest.TestCase):
    MARKER = "INJECTABLE-TEXT-9c3f"

    def test_private_block_and_untrusted_wrapping(self) -> None:
        messages = dm.build_private_chat_messages(
            self.MARKER,
            sender_user_id=777,
            sender_username="member",
            history=[{"role": "user", "content": "上一句"}],
        )
        systems = [m["content"] for m in messages if m["role"] == "system"]
        self.assertTrue(any("[PRIVATE_CHAT]" in s for s in systems), "必须告诉模型这是一对一私聊")
        self.assertFalse(
            any(self.MARKER in s for s in systems),
            "成员可控正文一个字都不许进 system（F-003）",
        )
        last = messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn(self.MARKER, last["content"])
        self.assertIn("untrusted", last["content"])

    def test_image_description_joins_the_user_turn(self) -> None:
        messages = dm.build_private_chat_messages(
            "这是什么",
            image_description="一张猫的照片",
            sender_user_id=777,
        )
        last = messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("[图片内容]", last["content"])
        self.assertIn("一张猫的照片", last["content"])
        self.assertIn("这是什么", last["content"])
        self.assertFalse(any("一张猫的照片" in m["content"] for m in messages if m["role"] == "system"))

    def test_no_history_still_yields_a_complete_payload(self) -> None:
        messages = dm.build_private_chat_messages("你好", sender_user_id=777)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["role"], "user")
        self.assertGreaterEqual(len(messages), 3)


class SplitTests(unittest.TestCase):
    def test_splits_long_replies(self) -> None:
        self.assertEqual(dm_handler._split_for_telegram(""), [])
        self.assertEqual(dm_handler._split_for_telegram("短"), ["短"])
        many_lines = "\n".join(f"第{i}行" + "字" * 200 for i in range(40))
        chunks = dm_handler._split_for_telegram(many_lines)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), dm_handler.MAX_REPLY_CHARS)

    def test_hard_splits_a_single_giant_line(self) -> None:
        chunks = dm_handler._split_for_telegram("字" * (dm_handler.MAX_REPLY_CHARS * 2 + 5))
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= dm_handler.MAX_REPLY_CHARS for c in chunks))


# ---------------------------------------------------------------------------
# handler 分支
# ---------------------------------------------------------------------------


class HandlerBranchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        dm.history_store().clear()
        dm.notice_throttle().clear()

    def _fake_llm(self, reply: str = "在的，怎么了？") -> MagicMock:
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=reply)
        llm.vision_describe = AsyncMock(return_value="一张图")
        return llm

    async def test_commands_are_left_to_the_command_routers(self) -> None:
        message = _message(text="/av SONE-342")
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())) as access:
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        message.answer.assert_not_awaited()
        access.assert_not_awaited()

    async def test_non_member_gets_one_notice_and_no_model_call(self) -> None:
        message = _message(text="你好")
        llm = self._fake_llm()
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict(dm.TIER_NONE))), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock()) as quota, \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        message.answer.assert_awaited_once()
        self.assertEqual(message.answer.await_args.args[0], dm.NOT_MEMBER_NOTICE)
        quota.assert_not_awaited()
        llm.chat.assert_not_awaited()

    async def test_unknown_access_asks_the_user_to_retry(self) -> None:
        message = _message(text="你好")
        llm = self._fake_llm()
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict(dm.TIER_UNKNOWN))), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        self.assertEqual(message.answer.await_args.args[0], dm.ACCESS_UNKNOWN_NOTICE)
        llm.chat.assert_not_awaited()

    async def test_quota_exhausted_does_not_call_the_model(self) -> None:
        message = _message(text="你好")
        llm = self._fake_llm()
        blocked = dm.QuotaOutcome(
            allowed=False,
            reason="user_limit",
            user_used=20,
            per_user_limit=20,
            global_used=30,
            global_limit=200,
        )
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=blocked)), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        self.assertEqual(message.answer.await_args.args[0], dm.LIMIT_NOTICE)
        llm.chat.assert_not_awaited()

    async def test_happy_path_replies_and_keeps_memory_only_history(self) -> None:
        message = _message(text="你好")
        llm = self._fake_llm("在的，怎么了？")
        ok = dm.QuotaOutcome(
            allowed=True,
            reason="ok",
            user_used=1,
            per_user_limit=20,
            global_used=1,
            global_limit=200,
        )
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=ok)), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        message.answer.assert_awaited_once()
        self.assertEqual(message.answer.await_args.args[0], "在的，怎么了？")
        self.assertIsNone(message.answer.await_args.kwargs.get("parse_mode"), "私聊回复不做 HTML 解析")
        llm.chat.assert_awaited_once()
        self.assertEqual(llm.chat.await_args.kwargs.get("stage"), "dm", "用量看板要能单独看到私聊")
        history = dm.history_store().history(message.from_user.id)
        self.assertEqual([h["role"] for h in history], ["user", "assistant"])

    async def test_reply_goes_through_the_search_path(self) -> None:
        """私聊回复必须走带搜索的那条路（stage=dm），否则联网能力等于没有。"""

        message = _message(text="帮我查查这两天显卡的新闻")
        llm = self._fake_llm()
        ok = dm.QuotaOutcome(True, "ok", 1, 20, 1, 200)
        answer = SimpleNamespace(text="查到啦，亲爱的～", searches=1, exhausted=False)
        settings = _settings()
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=ok)), \
             patch.object(dm_handler, "last_contact_record", new=AsyncMock(return_value="")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)), \
             patch.object(
                 dm_handler, "answer_with_search", new=AsyncMock(return_value=answer)
             ) as search:
            await dm_handler.on_private_message(message, AsyncMock(), settings)
        self.assertEqual(search.await_args.kwargs.get("stage"), "dm")
        self.assertEqual(
            search.await_args.kwargs.get("user_text"), "帮我查查这两天显卡的新闻",
            "要把本轮原话交给搜索判断，不能靠猜历史里的 user 消息",
        )
        self.assertIs(
            search.await_args.kwargs.get("settings"), settings,
            "必须把顶层设置交给检索技能（key 在顶层），否则 Firecrawl 到不了、只会落到 ddgs",
        )
        self.assertEqual(message.answer.await_args.args[0], "查到啦，亲爱的～")
        llm.chat.assert_not_awaited()

    async def test_video_is_answered_without_touching_the_model(self) -> None:
        message = _message()
        message.video = SimpleNamespace(file_id="v", file_size=100)
        llm = self._fake_llm()
        ok = dm.QuotaOutcome(True, "ok", 1, 20, 1, 200)
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=ok)), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        self.assertEqual(message.answer.await_args.args[0], dm.MEDIA_UNSUPPORTED_NOTICE)
        llm.chat.assert_not_awaited()
        llm.vision_describe.assert_not_awaited()

    async def test_photo_goes_through_vision_then_chat(self) -> None:
        message = _message(caption="这是什么")
        message.photo = [SimpleNamespace(file_id="p", file_size=1000)]
        llm = self._fake_llm("这是一只猫")
        ok = dm.QuotaOutcome(True, "ok", 1, 20, 1, 200)
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=ok)), \
             patch.object(dm_handler, "_image_file_info", new=MagicMock(return_value=("p", "image/jpeg", 1000))), \
             patch.object(dm_handler, "_image_description", new=AsyncMock(return_value="一只猫")), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        llm.chat.assert_awaited_once()
        payload = llm.chat.await_args.args[0]
        self.assertIn("[图片内容]", payload[-1]["content"])
        self.assertEqual(message.answer.await_args.args[0], "这是一只猫")

    async def test_model_failure_is_swallowed_into_a_busy_notice(self) -> None:
        message = _message(text="你好")
        llm = MagicMock()
        llm.chat = AsyncMock(side_effect=RuntimeError("boom"))
        ok = dm.QuotaOutcome(True, "ok", 1, 20, 1, 200)
        with patch.object(dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=ok)), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        self.assertEqual(message.answer.await_args.args[0], dm.BUSY_NOTICE)

    async def test_super_admin_skips_the_quota_entirely(self) -> None:
        """最高管理员不设限：连计数都不记，别占任何一档的额度。"""

        message = _message(text="你好")
        llm = self._fake_llm("在的")
        with patch.object(
            dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict(dm.TIER_SUPER))
        ), patch.object(
            dm_handler, "last_contact_record", new=AsyncMock(return_value="2026-10-02 21:03")
        ) as attendance, patch.object(
            dm_handler, "record_contact", new=AsyncMock()
        ) as contact, patch.object(
            dm_handler, "consume_daily_quota", new=AsyncMock()
        ) as quota, patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        quota.assert_not_awaited()
        attendance.assert_awaited_once()
        contact.assert_awaited_once()
        llm.chat.assert_awaited_once()
        self.assertEqual(message.answer.await_args.args[0], "在的")

    async def test_admin_tier_is_passed_to_the_quota(self) -> None:
        message = _message(text="你好")
        llm = self._fake_llm("在的")
        admin_ok = dm.QuotaOutcome(
            True,
            "ok",
            1,
            dm.ADMIN_PER_USER_DAILY_LIMIT,
            1,
            dm.ADMIN_GLOBAL_DAILY_LIMIT,
            dm.TIER_ADMIN,
        )
        with patch.object(
            dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict(dm.TIER_ADMIN))
        ), patch.object(
            dm_handler, "consume_daily_quota", new=AsyncMock(return_value=admin_ok)
        ) as quota, patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        self.assertTrue(quota.await_args.kwargs.get("is_admin"), "管理员档要按管理员配额算")

    async def test_empty_message_is_ignored(self) -> None:
        message = _message()
        with patch.object(dm_handler, "resolve_access", new=AsyncMock()) as access:
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        access.assert_not_awaited()
        message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
