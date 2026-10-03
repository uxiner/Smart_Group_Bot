"""1 对 1 私聊（DM）回归测试。

测的是会直接影响钱和观感的那几件事，不是"函数能跑"：

- **准入四态**：超管豁免 / 是成员 / 不是成员 / **查不通（不确定）**——最后一种既
  不能放行（会变成免费代理）也不能拒绝（会误伤真人），必须单独走一条提示；
- **配额两道闸门**：每人 20 条/天、全局 200 条/天；超限要**整体回滚**，不能在库里
  留下"扣了但没用"的脏计数；跨零点自动换行；
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
    return SimpleNamespace(super_admin_id=super_admin_id)


def _bot(*, statuses=None, raises=False) -> MagicMock:
    """假 Bot：``get_chat_member`` 按 group_id 返回给定状态，或直接抛异常。"""

    bot = MagicMock()
    mapping = dict(statuses or {})

    async def _get_chat_member(chat_id, user_id):
        if raises:
            raise RuntimeError("telegram down")
        member = MagicMock()
        member.status = mapping.get(int(chat_id), "left")
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
        cache.put(1, True)
        self.assertIs(cache.get(1), True)
        clock["now"] += 59.0
        self.assertIs(cache.get(1), True)
        clock["now"] += 2.0
        self.assertIsNone(cache.get(1), "过期后必须重新判定")
        cache.put(2, False)
        self.assertEqual(len(cache), 1)
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_capacity_evicts_instead_of_growing(self) -> None:
        cache = dm.MemberAccessCache(max_users=16)
        for uid in range(100):
            cache.put(uid, True)
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
    async def test_per_user_limit_blocks_the_21st(self) -> None:
        day = "2026-10-03"
        for i in range(1, 21):
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
        for _ in range(20):
            async with self.session_factory() as session:
                await dm.consume_daily_quota(session, user_id=777, day=day)
        async with self.session_factory() as session:
            await dm.consume_daily_quota(session, user_id=777, day=day)
        self.assertEqual(await self._stored(777, day), 20, "被拒的那一次必须回滚干净")
        self.assertEqual(
            await self._stored(dm.GLOBAL_COUNTER_USER_ID, day), 20, "全局计数同样要回滚"
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
        for _ in range(20):
            async with self.session_factory() as session:
                await dm.consume_daily_quota(session, user_id=777, day="2026-10-03")
        async with self.session_factory() as session:
            next_day = await dm.consume_daily_quota(session, user_id=777, day="2026-10-04")
        self.assertTrue(next_day.allowed, "跨零点自动换行")
        self.assertEqual(next_day.user_used, 1)
        self.assertEqual(await self._stored(777, "2026-10-04"), 1)

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
        with patch.object(dm_handler, "confirm_authorized_group_member", new=AsyncMock(return_value=True)) as access:
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        message.answer.assert_not_awaited()
        access.assert_not_awaited()

    async def test_non_member_gets_one_notice_and_no_model_call(self) -> None:
        message = _message(text="你好")
        llm = self._fake_llm()
        with patch.object(dm_handler, "confirm_authorized_group_member", new=AsyncMock(return_value=False)), \
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
        with patch.object(dm_handler, "confirm_authorized_group_member", new=AsyncMock(return_value=None)), \
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
        with patch.object(dm_handler, "confirm_authorized_group_member", new=AsyncMock(return_value=True)), \
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
        with patch.object(dm_handler, "confirm_authorized_group_member", new=AsyncMock(return_value=True)), \
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

    async def test_video_is_answered_without_touching_the_model(self) -> None:
        message = _message()
        message.video = SimpleNamespace(file_id="v", file_size=100)
        llm = self._fake_llm()
        ok = dm.QuotaOutcome(True, "ok", 1, 20, 1, 200)
        with patch.object(dm_handler, "confirm_authorized_group_member", new=AsyncMock(return_value=True)), \
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
        with patch.object(dm_handler, "confirm_authorized_group_member", new=AsyncMock(return_value=True)), \
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
        with patch.object(dm_handler, "confirm_authorized_group_member", new=AsyncMock(return_value=True)), \
             patch.object(dm_handler, "consume_daily_quota", new=AsyncMock(return_value=ok)), \
             patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=llm)):
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        self.assertEqual(message.answer.await_args.args[0], dm.BUSY_NOTICE)

    async def test_empty_message_is_ignored(self) -> None:
        message = _message()
        with patch.object(dm_handler, "confirm_authorized_group_member", new=AsyncMock()) as access:
            await dm_handler.on_private_message(message, AsyncMock(), _settings())
        access.assert_not_awaited()
        message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
