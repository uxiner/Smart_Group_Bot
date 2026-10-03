"""私聊对话历史落库 + 按 token 预算装配的回归测试。

测的是「机器人重启后还记不记得」和「272K 预算到底怎么装历史」这两件事，不是
"函数能跑"：

- **幂等落库**：同一条 Telegram 消息重投递多少次，库里都只有那两行；
- **重启不失忆**：内存缓冲清空（模拟重启）后，下一轮仍能从库里读到之前的对话；
- **预算装配**：从新到旧累积，装不下就停——最近的内容一定在；
- **单条超长**：截断 + 注明，绝不整条丢掉；
- **注入的系统资料块不落库**：``[WEB_SEARCH_RESULTS]`` 不是对话内容；
- **写库异常不影响回复**：库炸了也要把话回出去；
- **留存清理**：按时间删过期行，幂等、可重复执行。

配额/考勤/搜索链路的既有行为由 ``tests/test_private_chat.py`` 守着，这里一个字
都不改它们。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import PrivateChatMessage
from bot.handlers import private_chat as dm_handler
from bot.services import private_chat as dm
from bot.services.dm_search import RESULT_BLOCK
from bot.utils.timezone import now_shanghai_naive

SUPER_ADMIN = 601298409
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
#: 装配测试里每条消息的固定开销（与服务内同口径，用来手算边界）
OVERHEAD = 12


def _settings() -> SimpleNamespace:
    """假设置：bot 段带本期新增的两个配置（私聊历史预算 / 保留天数）。"""

    return SimpleNamespace(
        super_admin_id=SUPER_ADMIN,
        bot=SimpleNamespace(
            private_chat_history_token_budget=dm.PRIVATE_HISTORY_TOKEN_BUDGET,
            private_chat_history_retention_days=dm.PRIVATE_HISTORY_RETENTION_DAYS,
        ),
        firecrawl_api_key="",
    )


def _verdict(tier: str = dm.TIER_MEMBER) -> dm.AccessVerdict:
    return dm.AccessVerdict(True, tier)


def _quota_ok() -> dm.QuotaOutcome:
    return dm.QuotaOutcome(
        allowed=True,
        reason="ok",
        user_used=1,
        per_user_limit=dm.DEFAULT_PER_USER_DAILY_LIMIT,
        global_used=1,
        global_limit=dm.DEFAULT_GLOBAL_DAILY_LIMIT,
        tier=dm.TIER_MEMBER,
    )


def _message(*, text=None, caption=None, message_id=1001) -> MagicMock:
    msg = MagicMock()
    msg.chat = SimpleNamespace(id=4242, type="private")
    msg.from_user = SimpleNamespace(id=777, username="member", is_bot=False, first_name="M")
    msg.text = text
    msg.caption = caption
    msg.message_id = message_id
    for attr in MEDIA_ATTRS:
        setattr(msg, attr, None)
    msg.answer = AsyncMock()
    msg.bot = MagicMock()
    msg.bot.get_chat_member = AsyncMock()
    msg.bot.send_chat_action = AsyncMock()
    return msg


class _ExplodingSession:
    """每条 SQL 都炸、连回滚都炸的假 session（测「写失败不影响回复」用）。

    ``commit()`` 刻意不炸：handler 在准入之后会先提交一次，那是另一条既有路径，
    这里只针对「历史读写」这一段的失败。
    """

    async def execute(self, *args, **kwargs):
        raise RuntimeError("database is locked")

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        raise RuntimeError("rollback unavailable")


# ---------------------------------------------------------------------------
# 纯函数：token 口径与预算装配
# ---------------------------------------------------------------------------


class TokenGaugeTests(unittest.TestCase):
    def test_cjk_aware_gauge_is_shared_with_group_memory(self) -> None:
        """私聊和群聊必须用同一套换算，不能各造一套。"""

        from bot.services import memory
        from bot.utils.tokens import estimate_text_tokens

        self.assertIs(dm.estimate_text_tokens, estimate_text_tokens)
        self.assertIs(memory._estimate_text_tokens, estimate_text_tokens)
        self.assertEqual(estimate_text_tokens("中" * 10), 10)
        self.assertEqual(estimate_text_tokens(""), 0)

    def test_config_readers_are_forgiving_and_clamped(self) -> None:
        self.assertEqual(dm.bounded_history_token_budget(0), 1024)
        self.assertEqual(dm.bounded_history_token_budget(10**9), 2_000_000)
        self.assertEqual(dm.bounded_history_token_budget("nonsense"), 278_528)
        self.assertEqual(dm.bounded_history_token_budget(None), 278_528)
        self.assertEqual(dm.bounded_history_retention_days(0), 1)
        self.assertEqual(dm.bounded_history_retention_days(9999), 365)
        self.assertEqual(dm.bounded_history_retention_days("nonsense"), 30)
        # 设置里缺项/为 None 都不能炸，一律退回默认值
        empty = SimpleNamespace(bot=SimpleNamespace())
        self.assertEqual(dm.private_history_token_budget(empty), 278_528)
        self.assertEqual(dm.private_history_retention_days(empty), 30)
        self.assertEqual(
            dm.private_history_token_budget(SimpleNamespace()), 278_528
        )
        self.assertEqual(
            dm.private_history_token_budget(
                SimpleNamespace(
                    bot=SimpleNamespace(private_chat_history_token_budget=4096)
                )
            ),
            4096,
        )

    def test_message_keys_are_stable_and_role_scoped(self) -> None:
        self.assertEqual(dm.history_message_key(77, "user"), "u:77")
        self.assertEqual(dm.history_message_key(77, "assistant"), "a:77")
        self.assertEqual(dm.history_message_key("77", "assistant"), "a:77")
        # 拿不到整数 message_id 时退化成一次性随机键：宁可多写，也不能互相顶掉
        self.assertNotEqual(
            dm.history_message_key(None, "user"),
            dm.history_message_key(None, "user"),
        )

    def test_injected_system_blocks_are_stripped(self) -> None:
        self.assertEqual(dm.strip_injected_blocks(""), "")
        self.assertEqual(dm.strip_injected_blocks("你好"), "你好")
        dirty = f"你好\n{RESULT_BLOCK}\nfrom: example.com"
        self.assertEqual(dm.strip_injected_blocks(dirty), "你好")


class HistoryAssemblyTests(unittest.TestCase):
    def _row(self, role: str, content: str) -> dict[str, str]:
        return {"role": role, "content": content}

    def test_budget_fills_from_the_newest_and_stops(self) -> None:
        # 每条 203 token + 12 开销 = 215；预算下限 1024 → 正好装 4 条（860），第 5 条超
        rows = [
            self._row("user" if i % 2 == 0 else "assistant", f"第{i}条" + "长" * 200)
            for i in range(13)
        ]
        selected = dm.assemble_private_history(rows, budget_tokens=1024, max_turns=100)

        self.assertEqual(len(selected), 4)
        self.assertEqual(
            [item["content"] for item in selected],
            [rows[i]["content"] for i in (9, 10, 11, 12)],
            "装进来的必须是最近的四条（时间正序）",
        )
        used = sum(
            dm.estimate_text_tokens(item["content"]) + OVERHEAD for item in selected
        )
        self.assertEqual(used, 860)
        self.assertGreater(
            used + dm.estimate_text_tokens(rows[8]["content"]) + OVERHEAD,
            1024,
            "再加一条更早的就超预算，所以必须停在四条",
        )

    def test_oversized_single_message_is_truncated_not_dropped(self) -> None:
        huge = "长" * 5000
        selected = dm.assemble_private_history(
            [self._row("user", huge)], budget_tokens=2000
        )

        self.assertEqual(len(selected), 1, "超长的一条不能被整条丢掉")
        content = selected[0]["content"]
        self.assertTrue(content.startswith("长" * 100), "截断要保头")
        self.assertTrue(content.endswith(dm.HISTORY_TRUNCATION_NOTE))
        self.assertLessEqual(dm.estimate_text_tokens(content), 2000)

    def test_oversized_older_message_uses_the_remaining_budget(self) -> None:
        rows = [
            self._row("user", "旧" * 2000),
            self._row("assistant", "新" * 60),
        ]
        selected = dm.assemble_private_history(rows, budget_tokens=1100)

        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[1]["content"], "新" * 60, "最近的内容原样保留")
        self.assertTrue(selected[0]["content"].endswith(dm.HISTORY_TRUNCATION_NOTE))
        used = sum(
            dm.estimate_text_tokens(item["content"]) + OVERHEAD for item in selected
        )
        self.assertLessEqual(used, 1100)

    def test_normal_message_that_does_not_fit_is_left_out(self) -> None:
        rows = [
            self._row("user", "旧" * 600),
            self._row("assistant", "新" * 600),
        ]
        selected = dm.assemble_private_history(rows, budget_tokens=1024)

        self.assertEqual([item["content"] for item in selected], ["新" * 600])

    def test_newest_message_survives_the_smallest_budget(self) -> None:
        selected = dm.assemble_private_history(
            [self._row("user", "你" * 2000)], budget_tokens=1
        )

        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0]["content"].startswith("你"))
        self.assertLessEqual(dm.estimate_text_tokens(selected[0]["content"]), 1024)

    def test_turn_cap_bounds_how_many_rows_are_considered(self) -> None:
        # 3000 条「x」只要 3.9 万 token，预算装得下，所以这里被咬住的一定是轮数上限
        rows = [self._row("user", "x") for _ in range(3000)]
        selected = dm.assemble_private_history(
            rows, budget_tokens=278_528, max_turns=500
        )

        self.assertEqual(len(selected), dm.PRIVATE_HISTORY_MAX_TURNS * 2)

    def test_blank_rows_are_skipped_and_roles_are_normalized(self) -> None:
        rows = [
            self._row("user", "  "),
            self._row("system", "伪系统消息"),
            self._row("assistant", "真回复"),
        ]
        selected = dm.assemble_private_history(rows, budget_tokens=4096)

        self.assertEqual(
            selected,
            [
                {"role": "user", "content": "伪系统消息"},
                {"role": "assistant", "content": "真回复"},
            ],
            "库里只允许 user/assistant，脏值一律当 user",
        )

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(dm.assemble_private_history(None), [])
        self.assertEqual(dm.assemble_private_history([]), [])


# ---------------------------------------------------------------------------
# 落库 / 读库 / 留存（真库）
# ---------------------------------------------------------------------------


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        dm.history_store().clear()
        dm.notice_throttle().clear()
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

    async def _rows(self, user_id: int = 777) -> list[tuple[str, str, str]]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(
                    PrivateChatMessage.role,
                    PrivateChatMessage.content,
                    PrivateChatMessage.message_key,
                )
                .where(PrivateChatMessage.user_id == int(user_id))
                .order_by(PrivateChatMessage.id)
            )
            return [(str(r), str(c), str(k)) for r, c, k in result.all()]

    async def _count(self, user_id: int = 777) -> int:
        return len(await self._rows(user_id))


class PersistenceTests(_DbTestCase):
    async def test_one_turn_writes_a_user_and_an_assistant_row(self) -> None:
        async with self.session_factory() as session:
            written = await dm.record_private_turn(
                session,
                user_id=777,
                user_content="在吗",
                assistant_content="在的",
                message_id=5001,
            )

        self.assertEqual(written, 2)
        self.assertEqual(
            await self._rows(),
            [("user", "在吗", "u:5001"), ("assistant", "在的", "a:5001")],
        )

    async def test_same_message_key_twice_writes_only_two_rows(self) -> None:
        """Telegram 重投递同一轮：库里还是那两行，不重复。"""

        async with self.session_factory() as session:
            first = await dm.record_private_turn(
                session,
                user_id=777,
                user_content="在吗",
                assistant_content="在的",
                message_id=5001,
            )
        async with self.session_factory() as session:
            second = await dm.record_private_turn(
                session,
                user_id=777,
                user_content="在吗",
                assistant_content="在的",
                message_id=5001,
            )

        self.assertEqual(first, 2)
        self.assertEqual(second, 0, "重复投递不能新增行")
        self.assertEqual(await self._count(), 2)

    async def test_explicit_message_key_is_honored(self) -> None:
        async with self.session_factory() as session:
            await dm.record_private_turn(
                session,
                user_id=777,
                user_content="第一句",
                assistant_content="第一答",
                user_message_key="u:fixed",
                assistant_message_key="a:fixed",
            )
            await dm.record_private_turn(
                session,
                user_id=777,
                user_content="第二句",
                assistant_content="第二答",
                user_message_key="u:fixed",
                assistant_message_key="a:fixed",
            )

        rows = await self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], "第一句", "同一幂等键保留第一份")

    async def test_different_users_do_not_collide(self) -> None:
        async with self.session_factory() as session:
            await dm.record_private_turn(
                session,
                user_id=777,
                user_content="我的",
                assistant_content="好的",
                message_id=9,
            )
            await dm.record_private_turn(
                session,
                user_id=888,
                user_content="他的",
                assistant_content="好的",
                message_id=9,
            )

        self.assertEqual(await self._count(777), 2)
        self.assertEqual(await self._count(888), 2)
        self.assertEqual([r[1] for r in await self._rows(777)], ["我的", "好的"])

    async def test_injected_system_block_never_reaches_the_table(self) -> None:
        async with self.session_factory() as session:
            await dm.record_private_turn(
                session,
                user_id=777,
                user_content=f"帮我查查\n{RESULT_BLOCK}\nfrom: example.com",
                assistant_content=f"查到啦\n{RESULT_BLOCK}\nfrom: example.com",
                message_id=5002,
            )

        for role, content, _key in await self._rows():
            self.assertNotIn(RESULT_BLOCK, content, f"{role} 行不该带注入块")
        self.assertEqual(
            await self._rows(),
            [("user", "帮我查查", "u:5002"), ("assistant", "查到啦", "a:5002")],
        )

    async def test_write_failure_is_swallowed(self) -> None:
        written = await dm.record_private_turn(
            _ExplodingSession(),
            user_id=777,
            user_content="在吗",
            assistant_content="在的",
            message_id=5003,
        )

        self.assertEqual(written, 0)
        self.assertEqual(await self._count(), 0)

    async def test_blank_side_writes_only_the_other_row(self) -> None:
        async with self.session_factory() as session:
            written = await dm.record_private_turn(
                session,
                user_id=777,
                user_content="   ",
                assistant_content="只有回复",
                message_id=5004,
            )

        self.assertEqual(written, 1)
        self.assertEqual(await self._rows(), [("assistant", "只有回复", "a:5004")])


class RestartAndReadTests(_DbTestCase):
    async def test_history_survives_a_restart(self) -> None:
        """新建一个 store/服务实例（模拟重启）后仍能读到之前的历史。"""

        async with self.session_factory() as session:
            await dm.record_private_turn(
                session,
                user_id=777,
                user_content="我叫小明",
                assistant_content="记住啦",
                message_id=1,
            )
            await dm.record_private_turn(
                session,
                user_id=777,
                user_content="今天下雨",
                assistant_content="带伞呀",
                message_id=2,
            )

        # 重启：进程内存全没了，只剩库
        dm.history_store().clear()
        fresh_store = dm.PrivateHistoryStore()
        async with self.session_factory() as session:
            history = await dm.load_private_history(
                session, 777, fallback=fresh_store
            )

        self.assertEqual(
            history,
            [
                {"role": "user", "content": "我叫小明"},
                {"role": "assistant", "content": "记住啦"},
                {"role": "user", "content": "今天下雨"},
                {"role": "assistant", "content": "带伞呀"},
            ],
        )

    async def test_db_read_failure_falls_back_to_the_memory_buffer(self) -> None:
        store = dm.PrivateHistoryStore()
        store.append(777, "user", "内存里的上一句")
        store.append(777, "assistant", "内存里的上一答")

        history = await dm.load_private_history(
            _ExplodingSession(), 777, fallback=store
        )

        self.assertEqual(
            history,
            [
                {"role": "user", "content": "内存里的上一句"},
                {"role": "assistant", "content": "内存里的上一答"},
            ],
        )

    async def test_empty_table_falls_back_to_the_memory_buffer(self) -> None:
        store = dm.PrivateHistoryStore()
        store.append(777, "user", "刚落库前的那一句")

        async with self.session_factory() as session:
            history = await dm.load_private_history(session, 777, fallback=store)

        self.assertEqual(history, [{"role": "user", "content": "刚落库前的那一句"}])

    async def test_db_and_memory_assembly_agree_on_the_same_turn(self) -> None:
        """同一轮内容，读库与读内存兜底必须装配出同一段历史。"""

        store = dm.PrivateHistoryStore()
        async with self.session_factory() as session:
            await dm.record_private_turn(
                session,
                user_id=777,
                user_content="你好呀",
                assistant_content="你也好",
                message_id=3,
            )
            from_db = await dm.load_private_history(session, 777)

        store.append(777, "user", "你好呀")
        store.append(777, "assistant", "你也好")
        from_memory = dm.assemble_private_history(store.history(777))

        self.assertEqual(from_db, from_memory)

    async def test_read_respects_the_token_budget(self) -> None:
        # 每条 203 token + 12 开销 = 215；预算下限 1024 → 只装得下最近 4 条
        async with self.session_factory() as session:
            for index in range(10):
                await dm.record_private_turn(
                    session,
                    user_id=777,
                    user_content=f"第{index}句" + "长" * 200,
                    assistant_content=f"第{index}答" + "长" * 200,
                    message_id=100 + index,
                )
            history = await dm.load_private_history(
                session, 777, budget_tokens=1024
            )

        self.assertEqual(len(history), 4)
        self.assertIn("第9答", history[-1]["content"], "最近的一条一定在")
        self.assertNotIn("第0句", [item["content"] for item in history])


class RetentionTests(_DbTestCase):
    async def _seed(self, *, days_ago: float, key: str, user_id: int = 777) -> None:
        async with self.session_factory() as session:
            await dm.record_private_turn(
                session,
                user_id=user_id,
                user_content="旧消息",
                assistant_content="旧回复",
                message_id=key,
                stamp=now_shanghai_naive() - timedelta(days=days_ago),
            )

    async def _expired_rows_gone(self) -> bool:
        return await self._count() == 0

    async def _run_maintenance_until(self, factory, predicate, *, timeout: float = 5.0) -> None:
        """跑巡检直到 ``await predicate()`` 为真；超时前一直被取消。"""

        task = asyncio.create_task(
            dm.run_private_chat_history_maintenance(
                factory, retention_days_getter=lambda: 30
            )
        )
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
                if await predicate():
                    return
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_prune_removes_only_expired_rows_and_is_idempotent(self) -> None:
        await self._seed(days_ago=40, key="old")
        await self._seed(days_ago=1, key="fresh")
        self.assertEqual(await self._count(), 4)

        async with self.session_factory() as session:
            removed = await dm.prune_private_chat_history(session, retention_days=30)
        self.assertEqual(removed, 2)

        async with self.session_factory() as session:
            again = await dm.prune_private_chat_history(session, retention_days=30)
        self.assertEqual(again, 0, "清理必须幂等")
        self.assertEqual(await self._count(), 2)

    async def test_prune_failure_is_swallowed(self) -> None:
        removed = await dm.prune_private_chat_history(_ExplodingSession())

        self.assertEqual(removed, 0)

    async def test_maintenance_loop_prunes_expired_rows(self) -> None:
        await self._seed(days_ago=90, key="ancient")
        self.assertEqual(await self._count(), 2)

        await self._run_maintenance_until(
            self.session_factory, self._expired_rows_gone
        )

        self.assertEqual(await self._count(), 0)

    async def test_maintenance_loop_survives_a_failing_pass(self) -> None:
        """单次失败只记日志：巡检任务必须还活着，不能自己退出。"""

        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            raise RuntimeError("database is locked")

        task = asyncio.create_task(
            dm.run_private_chat_history_maintenance(
                factory, retention_days_getter=lambda: 30
            )
        )
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                if calls["n"] >= 1:
                    break
            self.assertGreaterEqual(calls["n"], 1)
            self.assertFalse(task.done(), "单次失败不能终止巡检")
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


# ---------------------------------------------------------------------------
# handler：读库装配 + 落库 + 失败不影响回复
# ---------------------------------------------------------------------------


class HandlerHistoryTests(_DbTestCase):
    def _patch_runtime(self, search):
        return (
            patch.object(
                dm_handler, "resolve_access", new=AsyncMock(return_value=_verdict())
            ),
            patch.object(
                dm_handler, "consume_daily_quota", new=AsyncMock(return_value=_quota_ok())
            ),
            patch.object(dm_handler, "_reply_llm", new=MagicMock(return_value=MagicMock())),
            patch.object(dm_handler, "answer_with_search", new=search),
        )

    async def test_turn_is_persisted_with_both_rows(self) -> None:
        captured: list[list[dict]] = []

        async def search(llm, messages, *, stage="dm", user_text="", settings=None, **kwargs):
            captured.append(list(messages))
            return SimpleNamespace(text="在的，怎么了？", searches=0, exhausted=False)

        message = _message(text="你好", message_id=7001)
        patches = self._patch_runtime(search)
        for item in patches:
            item.start()
        try:
            async with self.session_factory() as session:
                await dm_handler.on_private_message(message, session, _settings())
        finally:
            for item in patches:
                item.stop()

        message.answer.assert_awaited_once()
        self.assertEqual(
            await self._rows(),
            [("user", "你好", "u:7001"), ("assistant", "在的，怎么了？", "a:7001")],
        )

    async def test_restart_keeps_the_earlier_turn_for_the_next_reply(self) -> None:
        """端到端「重启不失忆」：内存清空后，下一轮的 prompt 里还有上一轮。"""

        captured: list[list[dict]] = []

        async def search(llm, messages, *, stage="dm", user_text="", settings=None, **kwargs):
            captured.append(list(messages))
            return SimpleNamespace(text="收到", searches=0, exhausted=False)

        settings = _settings()
        patches = self._patch_runtime(search)
        for item in patches:
            item.start()
        try:
            async with self.session_factory() as session:
                await dm_handler.on_private_message(
                    _message(text="我叫小明", message_id=7101), session, settings
                )
            # 模拟重启：进程内存清空，库还在
            dm.history_store().clear()
            async with self.session_factory() as session:
                await dm_handler.on_private_message(
                    _message(text="我叫什么", message_id=7102), session, settings
                )
        finally:
            for item in patches:
                item.stop()

        payload = captured[-1]
        self.assertTrue(
            any("我叫小明" in str(item.get("content")) for item in payload),
            "重启后必须还能看到上一轮说过的话",
        )
        self.assertEqual(len(await self._rows()), 4)

    async def test_injected_search_block_is_not_persisted(self) -> None:
        """检索路径原地往 convo 里塞系统资料块时，落库的仍然只有对话本身。"""

        async def search(llm, messages, *, stage="dm", user_text="", settings=None, **kwargs):
            # 真实现是 copy 后再注入；这里故意原地注入，当成最坏情况来测
            messages.append(
                {"role": "system", "content": f"{RESULT_BLOCK}\nfrom: example.com"}
            )
            return SimpleNamespace(text="查到啦", searches=1, exhausted=False)

        message = _message(text="帮我查查显卡", message_id=7201)
        patches = self._patch_runtime(search)
        for item in patches:
            item.start()
        try:
            async with self.session_factory() as session:
                await dm_handler.on_private_message(message, session, _settings())
        finally:
            for item in patches:
                item.stop()

        rows = await self._rows()
        self.assertEqual(
            rows,
            [("user", "帮我查查显卡", "u:7201"), ("assistant", "查到啦", "a:7201")],
        )
        for _role, content, _key in rows:
            self.assertNotIn(RESULT_BLOCK, content)

    async def test_duplicate_delivery_does_not_duplicate_history(self) -> None:
        async def search(llm, messages, *, stage="dm", user_text="", settings=None, **kwargs):
            return SimpleNamespace(text="收到", searches=0, exhausted=False)

        settings = _settings()
        patches = self._patch_runtime(search)
        for item in patches:
            item.start()
        try:
            for _ in range(3):
                dm.history_store().clear()
                async with self.session_factory() as session:
                    await dm_handler.on_private_message(
                        _message(text="重复投递", message_id=7301), session, settings
                    )
        finally:
            for item in patches:
                item.stop()

        self.assertEqual(await self._count(), 2)

    async def test_image_description_is_stored_like_the_model_sees_it(self) -> None:
        captured: list[list[dict]] = []

        async def search(llm, messages, *, stage="dm", user_text="", settings=None, **kwargs):
            captured.append(list(messages))
            return SimpleNamespace(text="这是一只猫", searches=0, exhausted=False)

        message = _message(caption="这是什么", message_id=7401)
        message.photo = [SimpleNamespace(file_id="p", file_size=1000)]
        patches = list(self._patch_runtime(search)) + [
            patch.object(
                dm_handler,
                "_image_file_info",
                new=MagicMock(return_value=("p", "image/jpeg", 1000)),
            ),
            patch.object(
                dm_handler, "_image_description", new=AsyncMock(return_value="一只猫")
            ),
        ]
        for item in patches:
            item.start()
        try:
            async with self.session_factory() as session:
                await dm_handler.on_private_message(message, session, _settings())
        finally:
            for item in patches:
                item.stop()

        self.assertEqual(
            await self._rows(),
            [
                ("user", "这是什么\n[图片内容] 一只猫", "u:7401"),
                ("assistant", "这是一只猫", "a:7401"),
            ],
        )
        self.assertTrue(
            any("[图片内容]" in str(item.get("content")) and "一只猫" in str(item.get("content"))
                for item in captured[-1]),
            "落库的那段描述必须与交给模型的一致",
        )

    async def test_write_failure_does_not_break_the_reply(self) -> None:
        async def search(llm, messages, *, stage="dm", user_text="", settings=None, **kwargs):
            return SimpleNamespace(text="照常回复", searches=0, exhausted=False)

        message = _message(text="你好", message_id=7501)
        patches = self._patch_runtime(search)
        for item in patches:
            item.start()
        try:
            await dm_handler.on_private_message(message, _ExplodingSession(), _settings())
        finally:
            for item in patches:
                item.stop()

        message.answer.assert_awaited_once()
        self.assertEqual(message.answer.await_args.args[0], "照常回复")
        # 库写不进去，但内存兜底还在：同一进程内的下一轮仍有上下文
        self.assertEqual(
            [item["role"] for item in dm.history_store().history(777)],
            ["user", "assistant"],
        )
        self.assertEqual(await self._count(), 0)


# ---------------------------------------------------------------------------
# 配置默认值（272K / 30 天）
# ---------------------------------------------------------------------------


class ConfigDefaultTests(unittest.TestCase):
    def test_defaults_are_272k_and_30_days(self) -> None:
        from bot.config import BotConfig, Settings
        from bot.services.runtime_config import BotBehaviorConfig

        self.assertEqual(BotConfig().private_chat_history_token_budget, 278_528)
        self.assertEqual(BotConfig().private_chat_history_retention_days, 30)
        self.assertEqual(BotBehaviorConfig().private_chat_history_token_budget, 278_528)
        self.assertEqual(BotBehaviorConfig().private_chat_history_retention_days, 30)

        settings = Settings(_env_file=None)
        self.assertEqual(
            settings.bot.private_chat_history_token_budget, 278_528
        )
        self.assertEqual(settings.bot.private_chat_history_retention_days, 30)

    def test_runtime_config_bounds_are_enforced(self) -> None:
        from pydantic import ValidationError

        from bot.services.runtime_config import BotBehaviorConfig

        with self.assertRaises(ValidationError):
            BotBehaviorConfig(private_chat_history_token_budget=1023)
        with self.assertRaises(ValidationError):
            BotBehaviorConfig(private_chat_history_retention_days=0)
        with self.assertRaises(ValidationError):
            BotBehaviorConfig(private_chat_history_retention_days=366)

    def test_runtime_config_writes_through_to_settings(self) -> None:
        from bot.config import Settings
        from bot.services.runtime_config import RuntimeConfig

        settings = Settings(_env_file=None)
        config = RuntimeConfig.model_validate(
            {
                "bot": {
                    "private_chat_history_token_budget": 300_000,
                    "private_chat_history_retention_days": 60,
                }
            }
        )
        config.apply_to_settings(settings, apply_prompts=False)

        self.assertEqual(settings.bot.private_chat_history_token_budget, 300_000)
        self.assertEqual(settings.bot.private_chat_history_retention_days, 60)

    def test_legacy_import_carries_the_new_fields(self) -> None:
        from bot.config import Settings
        from bot.services.runtime_config import build_legacy_runtime_config

        imported = build_legacy_runtime_config(
            "/tmp/nonexistent-private-chat-history.toml",
            settings=Settings(_env_file=None),
            raw_env={},
        )

        self.assertEqual(imported.bot.private_chat_history_token_budget, 278_528)
        self.assertEqual(imported.bot.private_chat_history_retention_days, 30)


class MainWiringTests(unittest.TestCase):
    """``__main__`` 里的接线必须真的能解析到名字。

    后台清理任务用 ``lambda: private_history_retention_days(settings)`` 每轮现取保留
    天数，这个 lambda 只有在真跑起来之后才会被调用——单元测试里它被 mock 掉，所以
    「忘了 import」这种错全量测试是拦不住的（本分支第一次交付就踩了：pyflakes 报
    ``undefined name 'private_history_retention_days'``）。这里直接断言接线处的名字
    能解析、并且拿得到正确天数。
    """

    def test_main_module_exposes_retention_getter(self) -> None:
        from bot import __main__ as main_module

        self.assertTrue(
            callable(getattr(main_module, "private_history_retention_days", None)),
            "__main__ 必须能解析 private_history_retention_days（清理任务每轮要用）",
        )

    def test_main_module_retention_getter_returns_days(self) -> None:
        from bot import __main__ as main_module
        from bot.config import Settings

        settings = Settings(_env_file=None)
        settings.bot.private_chat_history_retention_days = 45

        self.assertEqual(main_module.private_history_retention_days(settings), 45)


if __name__ == "__main__":
    unittest.main()
