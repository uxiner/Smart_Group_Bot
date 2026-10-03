"""第 3 期 A 项：检索结果入档 + 时效 + 留存（``search_result_records``）。

用例锁定的是会直接影响「模型会不会把三天前的价格当现价」的那些口径：

- **入档**：有结果落一行；**空结果也落一行**（``outcome='empty'``），免得反复搜同一句；
- **幂等**：同一 ``(scope, scope_id, query)`` 在 10 分钟窗口内只保留一行（刷新内容与
  时间），超过窗口才算新的一次查询；
- **作用域硬隔离**：私聊留档（scope=private）与群聊留档（scope=group）互不可见——
  这是 C 项隐私红线的数据侧保证；
- **带时效注入**：渲染出来必须带 ``[搜索于 …，距今 …]``，超出该类新鲜窗口要标
  「可能已过期」，且**不加任何强制指令块**；
- **留存清理**：按时间删过期行，幂等、可重复执行、失败只记日志；
- **写失败不影响回复**：库炸了，私聊照样把话回出去。

数据落库用临时 SQLite（与 ``tests/test_private_chat_history.py`` 同一套 ``init_db``），
不需要网络。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import func, select

from bot.db.engine import init_db
from bot.db.models import SearchResultRecord
from bot.services import search_memory as sm
from bot.services.skills.base import SkillRunResult, SkillContext
from bot.services.skills.service import SkillService

NOW = datetime(2026, 10, 3, 21, 40, 0)


class _ExplodingSession:
    """每条 SQL 都炸、连回滚都炸的假 session（测「写失败不影响回复」用）。"""

    async def execute(self, *args, **kwargs):
        raise RuntimeError("database is locked")

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        raise RuntimeError("rollback unavailable")


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

    async def _rows(self) -> list[SearchResultRecord]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(SearchResultRecord).order_by(SearchResultRecord.id)
            )
            return list(result.scalars().all())

    async def _count(self) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(SearchResultRecord)
            )
            return int(result.scalar_one())


# ---------------------------------------------------------------------------
# 纯函数：类别 / 时效 / 渲染
# ---------------------------------------------------------------------------


class KindAndAgeTests(unittest.TestCase):
    def test_kind_classification(self) -> None:
        self.assertEqual(sm.classify_search_kind("5090 现在多少钱"), sm.KIND_PRICE)
        self.assertEqual(sm.classify_search_kind("今天有什么显卡新闻"), sm.KIND_NEWS)
        self.assertEqual(sm.classify_search_kind("Python 3.12 是什么"), sm.KIND_FACT)
        self.assertEqual(sm.classify_search_kind("随便聊聊"), sm.KIND_UNKNOWN)
        self.assertEqual(sm.classify_search_kind(""), sm.KIND_UNKNOWN)

    def test_price_beats_news_when_both_match(self) -> None:
        """「最新报价」既是新闻也是价格，必须按更短的价格窗口算。"""

        self.assertEqual(sm.classify_search_kind("最新显卡报价"), sm.KIND_PRICE)

    def test_age_rendering(self) -> None:
        self.assertEqual(sm.format_age(NOW - timedelta(seconds=30), now=NOW), "刚刚")
        self.assertEqual(sm.format_age(NOW - timedelta(minutes=12), now=NOW), "12 分钟")
        self.assertEqual(sm.format_age(NOW - timedelta(hours=2), now=NOW), "2 小时")
        self.assertEqual(sm.format_age(NOW - timedelta(days=3), now=NOW), "3 天")
        self.assertEqual(sm.format_age(None, now=NOW), "时间未知")
        self.assertEqual(
            sm.format_search_stamp(NOW - timedelta(hours=2), now=NOW),
            "[搜索于 2026-10-03 19:40，距今 2 小时]",
        )

    def test_staleness_follows_each_kind_window(self) -> None:
        # 默认窗口：价格 24h、新闻 48h、事实 7d、未分类 48h
        self.assertTrue(
            sm.is_stale(NOW - timedelta(hours=30), kind=sm.KIND_PRICE, now=NOW)
        )
        self.assertFalse(
            sm.is_stale(NOW - timedelta(hours=30), kind=sm.KIND_NEWS, now=NOW)
        )
        self.assertFalse(
            sm.is_stale(NOW - timedelta(days=6), kind=sm.KIND_FACT, now=NOW)
        )
        self.assertTrue(
            sm.is_stale(NOW - timedelta(days=8), kind=sm.KIND_FACT, now=NOW)
        )
        # 运行时把价格窗口改成 1 小时，30 分钟前就该算新鲜
        self.assertFalse(
            sm.is_stale(
                NOW - timedelta(minutes=30),
                kind=sm.KIND_PRICE,
                now=NOW,
                windows={sm.KIND_PRICE: 1},
            )
        )

    def test_freshness_windows_come_from_settings_and_are_clamped(self) -> None:
        settings = SimpleNamespace(
            bot=SimpleNamespace(
                search_freshness_price_hours=6,
                search_freshness_news_hours=20000,
                search_freshness_fact_hours="nonsense",
            )
        )
        self.assertEqual(sm.search_freshness_hours(settings, sm.KIND_PRICE), 6)
        self.assertEqual(sm.search_freshness_hours(settings, sm.KIND_NEWS), 8760)
        self.assertEqual(sm.search_freshness_hours(settings, sm.KIND_FACT), 168)
        # 配置整段缺失也不能炸
        empty = SimpleNamespace(bot=SimpleNamespace())
        self.assertEqual(sm.freshness_windows(empty)[sm.KIND_PRICE], 24)
        self.assertEqual(sm.search_record_retention_days(empty), 30)
        self.assertEqual(sm.search_record_retention_days(SimpleNamespace()), 30)

    def test_retention_and_freshness_clamps(self) -> None:
        self.assertEqual(sm.bounded_search_record_retention_days(0), 1)
        self.assertEqual(sm.bounded_search_record_retention_days(9999), 365)
        self.assertEqual(sm.bounded_search_record_retention_days(None), 30)
        self.assertEqual(sm.bounded_freshness_hours(0, default=24), 1)
        self.assertEqual(sm.bounded_freshness_hours(10**9, default=24), 8760)

    def test_group_can_read_private_history_defaults_to_false(self) -> None:
        """C 项：私聊 → 群默认禁止，开关必须默认关闭。"""

        self.assertFalse(sm.group_can_read_private_history(SimpleNamespace()))
        self.assertFalse(
            sm.group_can_read_private_history(SimpleNamespace(bot=SimpleNamespace()))
        )
        self.assertFalse(
            sm.group_can_read_private_history(
                SimpleNamespace(bot=SimpleNamespace(group_can_read_private_history=False))
            )
        )
        self.assertTrue(
            sm.group_can_read_private_history(
                SimpleNamespace(bot=SimpleNamespace(group_can_read_private_history=True))
            )
        )


class RenderTests(unittest.TestCase):
    def _records(self) -> list[dict]:
        return [
            {
                "query": "5090 价格",
                "digest": "¥12999 起",
                "sources": [{"title": "行情页", "url": "https://example.com/p"}],
                "kind": sm.KIND_PRICE,
                "outcome": sm.OUTCOME_OK,
                "created_at": NOW - timedelta(hours=30),
            },
            {
                "query": "显卡新闻",
                "digest": "",
                "sources": [],
                "kind": sm.KIND_NEWS,
                "outcome": sm.OUTCOME_EMPTY,
                "created_at": NOW - timedelta(hours=1),
            },
        ]

    def test_block_carries_timestamps_and_expiry_marks(self) -> None:
        block = sm.render_search_records_block(self._records(), now=NOW)
        self.assertTrue(block.startswith(sm.SEARCH_RECORDS_BLOCK))
        self.assertIn("[搜索于 2026-10-02 15:40，距今 1 天]", block)
        self.assertIn("（价格）（可能已过期）", block)
        self.assertIn("[搜索于 2026-10-03 20:40，距今 1 小时]", block)
        # 1 小时前的新闻还在 48h 窗口内，不能标过期
        self.assertNotIn("（新闻）（可能已过期）", block)
        self.assertIn("结果：当时没有查到可用内容", block)
        self.assertIn("来源：行情页 — https://example.com/p", block)

    def test_block_adds_no_imperative_instruction(self) -> None:
        """用户口径：只给时间戳 + 一句中性说明，不许加任何强制指令块。"""

        block = sm.render_search_records_block(self._records(), now=NOW)
        for forbidden in ("必须", "务必", "禁止", "不得", "一定要", "You must"):
            self.assertNotIn(forbidden, block)

    def test_empty_records_render_nothing(self) -> None:
        self.assertEqual(sm.render_search_records_block([], now=NOW), "")
        self.assertEqual(sm.render_search_record_messages([], now=NOW), [])

    def test_messages_split_one_record_per_message(self) -> None:
        """按条拆开，统一闸门才能从最旧的一条开始裁。"""

        messages = sm.render_search_record_messages(self._records(), now=NOW)
        self.assertEqual(len(messages), 3, "头部说明 + 两条留档")
        self.assertTrue(messages[0]["content"].startswith(sm.SEARCH_RECORDS_BLOCK))
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("5090 价格", messages[1]["content"])
        self.assertIn("显卡新闻", messages[2]["content"])

    def test_oldest_records_are_dropped_first_when_capped(self) -> None:
        messages = sm.render_search_record_messages(
            self._records(), now=NOW, max_records=1
        )
        joined = "\n".join(str(item["content"]) for item in messages)
        self.assertIn("显卡新闻", joined)
        self.assertNotIn("5090 价格", joined)


class SummarizeTests(unittest.TestCase):
    def test_ok_result_is_summarized_with_sources(self) -> None:
        result = SkillRunResult(
            ok=True,
            skill="websearch",
            summary="找到 2 条搜索结果",
            payload={
                "results": [
                    {"title": "T1", "url": "u1", "snippet": "S1"},
                    {"title": "T2", "url": "u2"},
                ]
            },
        )
        digest = sm.summarize_skill_result(result)
        self.assertEqual(digest.outcome, sm.OUTCOME_OK)
        self.assertIn("T1：S1", digest.digest)
        self.assertEqual(
            digest.sources,
            [{"title": "T1", "url": "u1"}, {"title": "T2", "url": "u2"}],
        )

    def test_empty_result_is_recorded_as_empty(self) -> None:
        result = SkillRunResult(
            ok=False, skill="websearch", summary="", error="search_timeout"
        )
        digest = sm.summarize_skill_result(result)
        self.assertEqual(digest.outcome, sm.OUTCOME_EMPTY)
        self.assertEqual(digest.sources, [])
        self.assertIn("search_timeout", digest.digest)

    def test_digest_is_truncated(self) -> None:
        result = SkillRunResult(
            ok=True,
            skill="websearch",
            summary="s" * 5000,
            payload={"results": []},
        )
        digest = sm.summarize_skill_result(result, max_chars=100)
        self.assertEqual(len(digest.digest), 100)


# ---------------------------------------------------------------------------
# 落库：幂等 / 作用域隔离 / 留存
# ---------------------------------------------------------------------------


class RecordPersistenceTests(_DbTestCase):
    async def test_successful_search_is_recorded(self) -> None:
        async with self.session_factory() as session:
            written = await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="5090 价格",
                digest="¥12999",
                sources=[{"title": "行情页", "url": "https://example.com/p"}],
                kind=sm.KIND_PRICE,
                outcome=sm.OUTCOME_OK,
                now=NOW,
            )
        self.assertEqual(written, 1)
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].scope, "private")
        self.assertEqual(rows[0].scope_id, 777)
        self.assertEqual(rows[0].kind, "price")
        self.assertEqual(rows[0].outcome, "ok")
        self.assertEqual(rows[0].sources, [{"title": "行情页", "url": "https://example.com/p"}])

    async def test_empty_query_is_not_recorded(self) -> None:
        async with self.session_factory() as session:
            written = await sm.record_search_result(
                session, scope=sm.SCOPE_PRIVATE, scope_id=777, query="   ", now=NOW
            )
        self.assertEqual(written, 0)
        self.assertEqual(await self._count(), 0)

    async def test_same_query_within_the_window_keeps_one_row(self) -> None:
        """10 分钟窗口内重复写入只保留一行（刷新内容与时间）。"""

        async with self.session_factory() as session:
            self.assertEqual(
                await sm.record_search_result(
                    session,
                    scope=sm.SCOPE_PRIVATE,
                    scope_id=777,
                    query="5090 价格",
                    digest="第一次",
                    kind=sm.KIND_PRICE,
                    outcome=sm.OUTCOME_OK,
                    now=NOW,
                ),
                1,
            )
            self.assertEqual(
                await sm.record_search_result(
                    session,
                    scope=sm.SCOPE_PRIVATE,
                    scope_id=777,
                    query="5090 价格",
                    digest="第二次",
                    kind=sm.KIND_PRICE,
                    outcome=sm.OUTCOME_OK,
                    now=NOW + timedelta(minutes=5),
                ),
                1,
            )
        self.assertEqual(await self._count(), 1, "窗口内只能有一行")
        rows = await self._rows()
        self.assertEqual(rows[0].digest, "第二次", "同一行被刷新")
        self.assertEqual(
            rows[0].created_at,
            NOW + timedelta(minutes=5),
            "时间戳要跟着刷新，否则「距今多久」会算错",
        )

    async def test_outside_the_window_is_a_new_row(self) -> None:
        """超过窗口算新的一次查询：价格/新闻可能真的变了，两行都要留。"""

        async with self.session_factory() as session:
            for offset in (0, 11):
                await sm.record_search_result(
                    session,
                    scope=sm.SCOPE_PRIVATE,
                    scope_id=777,
                    query="5090 价格",
                    digest=f"第 {offset} 分钟",
                    kind=sm.KIND_PRICE,
                    outcome=sm.OUTCOME_OK,
                    now=NOW + timedelta(minutes=offset),
                )
        self.assertEqual(await self._count(), 2)

    async def test_idempotency_window_is_configurable(self) -> None:
        async with self.session_factory() as session:
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="q",
                digest="a",
                now=NOW,
                idempotency_window_seconds=0,
            )
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="q",
                digest="b",
                now=NOW + timedelta(seconds=1),
                idempotency_window_seconds=0,
            )
        self.assertEqual(await self._count(), 2)

    async def test_same_query_in_different_scopes_stays_separate(self) -> None:
        async with self.session_factory() as session:
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="5090 价格",
                digest="私聊看到的",
                now=NOW,
            )
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_GROUP,
                scope_id=-100,
                query="5090 价格",
                digest="群里看到的",
                now=NOW,
            )
        self.assertEqual(await self._count(), 2)

    async def test_group_scope_is_invisible_to_private_and_vice_versa(self) -> None:
        async with self.session_factory() as session:
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="私聊问题",
                digest="PRIVATE_ONLY",
                now=NOW,
            )
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_GROUP,
                scope_id=-100,
                query="群问题",
                digest="GROUP_ONLY",
                now=NOW,
            )
        async with self.session_factory() as session:
            private_rows = await sm.load_search_records(
                session, scope=sm.SCOPE_PRIVATE, scope_id=777, now=NOW
            )
            group_rows = await sm.load_search_records(
                session, scope=sm.SCOPE_GROUP, scope_id=-100, now=NOW
            )
            other_group = await sm.load_search_records(
                session, scope=sm.SCOPE_GROUP, scope_id=-200, now=NOW
            )
        self.assertEqual([row["digest"] for row in private_rows], ["PRIVATE_ONLY"])
        self.assertEqual([row["digest"] for row in group_rows], ["GROUP_ONLY"])
        self.assertEqual(other_group, [])

    async def test_load_returns_oldest_first_and_skips_expired(self) -> None:
        async with self.session_factory() as session:
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="老问题",
                digest="old",
                now=NOW - timedelta(days=40),
            )
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="昨天",
                digest="yesterday",
                now=NOW - timedelta(days=1),
            )
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="刚才",
                digest="now",
                now=NOW,
            )
        async with self.session_factory() as session:
            rows = await sm.load_search_records(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                now=NOW,
                max_age_days=30,
            )
        self.assertEqual([row["digest"] for row in rows], ["yesterday", "now"])

    async def test_refreshed_record_still_counts_as_the_most_recent(self) -> None:
        """幂等刷新保留原 id：读取必须按 created_at 排，刚刷新那条要排在最新。"""

        async with self.session_factory() as session:
            for index in range(4):
                await sm.record_search_result(
                    session,
                    scope=sm.SCOPE_PRIVATE,
                    scope_id=777,
                    query=f"问题{index}",
                    digest=f"d{index}",
                    now=NOW + timedelta(minutes=index),
                )
            # 最早那条（id 最小）现在被重新查过一次
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="问题0",
                digest="d0-刷新",
                now=NOW + timedelta(minutes=5),
            )
        async with self.session_factory() as session:
            rows = await sm.load_search_records(
                session, scope=sm.SCOPE_PRIVATE, scope_id=777, limit=1, now=NOW
            )
        self.assertEqual([row["digest"] for row in rows], ["d0-刷新"])

    async def test_write_failure_is_swallowed(self) -> None:
        written = await sm.record_search_result(
            _ExplodingSession(),
            scope=sm.SCOPE_PRIVATE,
            scope_id=777,
            query="q",
            digest="d",
            now=NOW,
        )
        self.assertEqual(written, 0)

    async def test_record_for_result_uses_its_own_session_when_available(self) -> None:
        result = SkillRunResult(
            ok=True,
            skill="websearch",
            summary="找到 1 条",
            payload={"results": [{"title": "T", "url": "u"}]},
        )
        written = await sm.record_search_for_result(
            scope=sm.SCOPE_GROUP,
            scope_id=-100,
            query="显卡新闻",
            result=result,
            session_factory=self.session_factory,
            now=NOW,
        )
        self.assertEqual(written, 1)
        rows = await self._rows()
        self.assertEqual(rows[0].kind, "news")
        self.assertEqual(rows[0].scope_id, -100)


class RetentionTests(_DbTestCase):
    async def test_prune_removes_only_expired_rows_and_is_idempotent(self) -> None:
        async with self.session_factory() as session:
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="老",
                digest="old",
                now=NOW - timedelta(days=40),
            )
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="新",
                digest="fresh",
                now=NOW - timedelta(days=1),
            )
        async with self.session_factory() as session:
            removed = await sm.prune_search_result_records(
                session, retention_days=30, now=NOW
            )
        self.assertEqual(removed, 1)
        async with self.session_factory() as session:
            again = await sm.prune_search_result_records(
                session, retention_days=30, now=NOW
            )
        self.assertEqual(again, 0, "清理必须幂等")
        self.assertEqual(await self._count(), 1)

    async def test_prune_failure_is_swallowed(self) -> None:
        removed = await sm.prune_search_result_records(_ExplodingSession(), now=NOW)
        self.assertEqual(removed, 0)

    async def test_maintenance_loop_prunes_expired_rows(self) -> None:
        async with self.session_factory() as session:
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=777,
                query="远古",
                digest="ancient",
                now=NOW - timedelta(days=90),
            )

        async def _empty() -> bool:
            return await self._count() == 0

        task = asyncio.create_task(
            sm.run_search_record_maintenance(
                self.session_factory, retention_days_getter=lambda: 30
            )
        )
        try:
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
                if await _empty():
                    break
            self.assertTrue(await _empty(), "巡检必须把过期留档清掉")
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_maintenance_loop_survives_a_failing_session_factory(self) -> None:
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            raise RuntimeError("database is locked")

        task = asyncio.create_task(
            sm.run_search_record_maintenance(
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
# 两条链路各自的写入时机
# ---------------------------------------------------------------------------


class DmSearchRecordingTests(_DbTestCase):
    async def test_private_search_is_recorded_with_scope_private(self) -> None:
        from bot.services import dm_search

        llm = MagicMock()
        llm.chat = AsyncMock(return_value="查到啦")
        result = SkillRunResult(
            ok=True,
            skill="websearch",
            summary="找到 1 条搜索结果",
            payload={
                "results": [
                    {"title": "5090 行情", "url": "https://example.com/a", "snippet": "¥12999"}
                ]
            },
        )
        dm_search.search_budget()._used = 0
        async with self.session_factory() as session:
            with patch.object(dm_search, "run_search", new=AsyncMock(return_value=result)):
                answer = await dm_search.answer_with_search(
                    llm,
                    [{"role": "user", "content": "帮我查查最近 5090 的价格"}],
                    user_text="帮我查查最近 5090 的价格",
                    session=session,
                    scope_id=777,
                )
        self.assertEqual(answer.searches, 1)
        self.assertEqual(answer.recorded, 1)
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].scope, "private")
        self.assertEqual(rows[0].scope_id, 777)
        self.assertEqual(rows[0].kind, "price")
        self.assertEqual(rows[0].outcome, "ok")

    async def test_missing_result_is_recorded_as_empty(self) -> None:
        from bot.services import dm_search

        llm = MagicMock()
        llm.chat = AsyncMock(return_value="这次没查到")
        dm_search.search_budget()._used = 0
        async with self.session_factory() as session:
            with patch.object(
                dm_search,
                "run_search",
                new=AsyncMock(
                    return_value=SkillRunResult(
                        ok=False, skill="websearch", summary="", error="empty_result"
                    )
                ),
            ):
                answer = await dm_search.answer_with_search(
                    llm,
                    [{"role": "user", "content": "帮我查查 5090 的价格"}],
                    user_text="帮我查查 5090 的价格",
                    session=session,
                    scope_id=777,
                )
        self.assertEqual(answer.recorded, 1)
        rows = await self._rows()
        self.assertEqual(rows[0].outcome, "empty")

    async def test_write_failure_never_breaks_the_reply(self) -> None:
        from bot.services import dm_search

        llm = MagicMock()
        llm.chat = AsyncMock(return_value="照样回你")
        result = SkillRunResult(
            ok=True, skill="websearch", summary="找到 1 条", payload={"results": []}
        )
        dm_search.search_budget()._used = 0
        with patch.object(dm_search, "run_search", new=AsyncMock(return_value=result)):
            answer = await dm_search.answer_with_search(
                llm,
                [{"role": "user", "content": "帮我查查 5090 的价格"}],
                user_text="帮我查查 5090 的价格",
                session=_ExplodingSession(),
                scope_id=777,
            )
        self.assertEqual(answer.text, "照样回你")
        self.assertEqual(answer.recorded, 0)

    async def test_without_session_nothing_is_written(self) -> None:
        from bot.services import dm_search

        llm = MagicMock()
        llm.chat = AsyncMock(return_value="在的呀")
        dm_search.search_budget()._used = 0
        await dm_search.answer_with_search(
            llm, [{"role": "user", "content": "帮我查查 5090 的价格"}]
        )
        self.assertEqual(await self._count(), 0)


class GroupSearchRecordingTests(_DbTestCase):
    def _service(self) -> SkillService:
        llm = SimpleNamespace(
            main=SimpleNamespace(model="m", fallbacks=[]),
            decision_config=SimpleNamespace(model="d", fallbacks=[]),
            vision_config=SimpleNamespace(model="v", fallbacks=[]),
            moderation_config=SimpleNamespace(model="mod", fallbacks=[]),
            compress_config=SimpleNamespace(model="c", fallbacks=[]),
            embed_config=SimpleNamespace(model="e", fallbacks=[]),
        )
        return SkillService(llm)

    async def test_group_tool_result_is_recorded_with_scope_group(self) -> None:
        service = self._service()
        context = SkillContext(
            chat_id=-100123,
            session_factory=self.session_factory,
        )
        result = SkillRunResult(
            ok=True,
            skill="websearch",
            summary="找到 2 条搜索结果",
            payload={"results": [{"title": "T", "url": "u"}]},
        )
        await service._record_group_search_result(
            name="websearch",
            arguments={"query": "显卡新闻"},
            result=result,
            context=context,
        )
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].scope, "group")
        self.assertEqual(rows[0].scope_id, -100123)
        self.assertEqual(rows[0].query, "显卡新闻")
        self.assertEqual(rows[0].kind, "news")

    async def test_non_search_tools_are_not_recorded(self) -> None:
        service = self._service()
        context = SkillContext(chat_id=-100123, session_factory=self.session_factory)
        result = SkillRunResult(ok=True, skill="send_sticker", summary="已发送")
        for name in ("send_sticker", "doubao_tts", "webfetch"):
            await service._record_group_search_result(
                name=name,
                arguments={"query": "x"},
                result=result,
                context=context,
            )
        self.assertEqual(await self._count(), 0)

    async def test_missing_query_or_chat_id_is_skipped(self) -> None:
        service = self._service()
        result = SkillRunResult(ok=True, skill="websearch", summary="找到 1 条")
        await service._record_group_search_result(
            name="websearch",
            arguments={},
            result=result,
            context=SkillContext(chat_id=-100123, session_factory=self.session_factory),
        )
        await service._record_group_search_result(
            name="websearch",
            arguments={"query": "显卡新闻"},
            result=result,
            context=SkillContext(chat_id=0, session_factory=self.session_factory),
        )
        self.assertEqual(await self._count(), 0)

    async def test_weibo_and_bilibili_keywords_are_recorded(self) -> None:
        service = self._service()
        result = SkillRunResult(
            ok=True, skill="weibo_search", summary="找到 1 条", payload={"results": []}
        )
        await service._record_group_search_result(
            name="weibo_search",
            arguments={"keyword": "显卡"},
            result=result,
            context=SkillContext(chat_id=-100123, session_factory=self.session_factory),
        )
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].query, "显卡")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
