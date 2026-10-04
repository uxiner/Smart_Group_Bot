"""摘要功能"机制"回归（第②项收口）：并发原子性、失效重建、调度推进、有界状态、
扫描顺序、落后时的原文保留、配置/预算口径。

用受控 fake 模型 + 真实 sqlite，不联网、不碰生产、不发 Telegram。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import delete

from bot.config import BotConfig, Settings, apply_top_level_budget_overrides
from bot.db.models import GroupMessageArchive
from bot.services import model_limits as ml
from bot.services.group_summary import (
    GROUP_SUMMARY_BLOCK_MARKER,
    GroupSummaryConfig,
    GroupSummaryScheduler,
    SqlGroupSummaryStore,
    fit_summary_prompt,
)
from bot.services.llm import LLMService
from bot.utils.timezone import now_shanghai_naive
from tests import test_group_summary as fixtures
from tests.test_group_summary import FakeLLM, FakeStore, _rows


class _CapturingLLM:
    """记录提示词；可选在"模型调用期间"执行一个副作用（模拟删原文）。"""

    def __init__(self, output: str = "重建摘要：只含现存内容。", *, side_effect=None) -> None:
        self.output = output
        self.side_effect = side_effect
        self.prompts: list[list[dict]] = []

    async def background_summary_completion(self, messages, **_kwargs) -> str:
        self.prompts.append(list(messages))
        if self.side_effect is not None:
            await self.side_effect()
        return self.output


class _DeferredLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def background_summary_completion(self, messages, **_kwargs) -> str:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return "摘要：讨论了显卡行情。"


class AtomicPublishTests(unittest.IsolatedAsyncioTestCase):
    GROUP_ID = fixtures.MemoryIntegrationTests.GROUP_ID
    asyncSetUp = fixtures.MemoryIntegrationTests.asyncSetUp
    asyncTearDown = fixtures.MemoryIntegrationTests.asyncTearDown
    _seed = fixtures.MemoryIntegrationTests._seed
    _archive_ids = fixtures.MemoryIntegrationTests._archive_ids

    async def test_concurrent_publish_same_version_accepts_exactly_one(self) -> None:
        """真正的原子 CAS：同一 expected_version 并发只有一次成功、水位不倒退。"""

        await self._seed(60)
        ids = await self._archive_ids()
        store = SqlGroupSummaryStore(self.session_factory)
        watermark = 0
        for round_no in range(5):
            current = await store.load(self.GROUP_ID)
            expected = current.version if current else 0
            through = ids[20 + round_no]
            first, second = await asyncio.gather(
                store.publish(
                    self.GROUP_ID,
                    summary=f"A{round_no}",
                    expected_version=expected,
                    covered_from_key="k",
                    covered_through_key="k",
                    covered_count=1,
                    source_truncated=False,
                    covered_from_id=ids[0],
                    covered_through_id=through,
                ),
                store.publish(
                    self.GROUP_ID,
                    summary=f"B{round_no}",
                    expected_version=expected,
                    covered_from_key="k",
                    covered_through_key="k",
                    covered_count=1,
                    source_truncated=False,
                    covered_from_id=ids[0],
                    covered_through_id=through,
                ),
                return_exceptions=True,
            )
            winners = [
                item
                for item in (first, second)
                if item is not None and not isinstance(item, Exception)
            ]
            self.assertEqual(len(winners), 1, f"round {round_no} must accept exactly one")
            record = await store.load(self.GROUP_ID)
            self.assertEqual(record.version, expected + 1)
            self.assertGreaterEqual(int(record.covered_through_id), watermark)
            watermark = int(record.covered_through_id)

    async def test_lower_watermark_needs_an_explicit_rebuild(self) -> None:
        await self._seed(60)
        ids = await self._archive_ids()
        store = SqlGroupSummaryStore(self.session_factory)
        await store.publish(
            self.GROUP_ID,
            summary="v1",
            expected_version=0,
            covered_from_key="k",
            covered_through_key="k",
            covered_count=5,
            source_truncated=False,
            covered_from_id=ids[0],
            covered_through_id=ids[20],
        )

        backwards = await store.publish(
            self.GROUP_ID,
            summary="倒退",
            expected_version=1,
            covered_from_key="k",
            covered_through_key="k",
            covered_count=5,
            source_truncated=False,
            covered_from_id=ids[0],
            covered_through_id=ids[5],
        )
        self.assertIsNone(backwards)

        rebuilt = await store.publish(
            self.GROUP_ID,
            summary="重建",
            expected_version=1,
            covered_from_key="k",
            covered_through_key="k",
            covered_count=5,
            source_truncated=False,
            covered_from_id=ids[3],
            covered_through_id=ids[5],
            allow_watermark_rewind=True,
            reset_coverage=True,
        )
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.version, 2)
        self.assertEqual(rebuilt.covered_from_id, ids[3])
        self.assertEqual(rebuilt.covered_count, 5)

    async def test_time_out_of_order_rows_are_never_permanently_skipped(self) -> None:
        """补录（旧 sent_at、新 id）的行也必须能被摘要覆盖：扫描顺序与水位统一为 id。"""

        await self._seed(10)
        ids = await self._archive_ids()
        # 再插一条**补录**行：sent_at 更旧，但 id 更大（插入序在后）。
        async with self.session_factory() as session:
            session.add(
                GroupMessageArchive(
                    group_id=self.GROUP_ID,
                    message_key=f"{self.GROUP_ID}:backfill",
                    telegram_message_id=999_999,
                    role="user",
                    direction="inbound",
                    sender_display_name="群友",
                    sender_id=7,
                    message_type="text",
                    content="补录的旧消息",
                    raw_text="补录的旧消息",
                    sent_at=now_shanghai_naive() - timedelta(days=30),
                )
            )
            await session.commit()
        store = SqlGroupSummaryStore(self.session_factory)

        # 先把前 5 条标成已覆盖
        covered = await store.publish(
            self.GROUP_ID,
            summary="前五条",
            expected_version=0,
            covered_from_key="k",
            covered_through_key="k",
            covered_count=5,
            source_truncated=False,
            covered_from_id=ids[0],
            covered_through_id=ids[4],
        )
        self.assertIsNotNone(covered)

        # recent_raw=0 ⇒ 全部 11 条都在"近期窗口之外"
        pending = await store.pending_count(
            self.GROUP_ID, recent_raw_messages=0, after_id=ids[4]
        )
        self.assertEqual(pending, 6)  # ids[5..9] + 补录那条
        snapshot = await store.read_snapshot(
            self.GROUP_ID,
            recent_raw_messages=0,
            after_id=ids[4],
            max_messages=200,
            max_input_tokens=16_384,
        )
        self.assertEqual(len(snapshot), 6)
        self.assertEqual(max(item.message_id for item in snapshot), max(ids) + 1)


class StaleRebuildTests(unittest.IsolatedAsyncioTestCase):
    GROUP_ID = fixtures.MemoryIntegrationTests.GROUP_ID
    asyncSetUp = fixtures.MemoryIntegrationTests.asyncSetUp
    asyncTearDown = fixtures.MemoryIntegrationTests.asyncTearDown
    _seed = fixtures.MemoryIntegrationTests._seed
    _memory = fixtures.MemoryIntegrationTests._memory
    _archive_ids = fixtures.MemoryIntegrationTests._archive_ids

    def _scheduler(self, store, llm, **overrides) -> GroupSummaryScheduler:
        base = GroupSummaryConfig(enabled=True, min_refresh_seconds=0.0)
        config = GroupSummaryConfig(**{**base.__dict__, **overrides}) if overrides else base
        return GroupSummaryScheduler(
            llm=llm, store=store, config_provider=lambda: config, slot_waiter=None
        )

    async def test_deleted_coverage_rebuilds_without_merging_old_content(self) -> None:
        """旧摘要失效后重建：提示词不带旧正文、覆盖范围重置、前台只看到新摘要。"""

        await self._seed(600)
        ids = await self._archive_ids()
        store = SqlGroupSummaryStore(self.session_factory)
        published = await store.publish(
            self.GROUP_ID,
            summary="哨兵内容：这段旧摘要覆盖的消息已被删除。",
            expected_version=0,
            covered_from_key=f"{self.GROUP_ID}:0",
            covered_through_key=f"{self.GROUP_ID}:199",
            covered_count=200,
            source_truncated=False,
            covered_from_id=ids[0],
            covered_through_id=ids[199],
        )
        self.assertIsNotNone(published)

        # 覆盖范围内删掉一条 → 旧摘要失效（前台立刻不再注入）
        async with self.session_factory() as session:
            await session.execute(
                delete(GroupMessageArchive).where(GroupMessageArchive.id == ids[10])
            )
            await session.commit()
        memory = self._memory(enabled=True)
        self.assertIsNone(await memory.published_group_summary(self.GROUP_ID))

        # worker 重建：提示词里绝不能带旧正文（否则会把已删除内容合并回来）
        llm = _CapturingLLM("重建摘要：只包含现存内容。")
        scheduler = self._scheduler(store, llm)
        scheduler.notify(self.GROUP_ID)
        outcome = await scheduler.run_group(
            self.GROUP_ID, scheduler._config_provider()
        )

        self.assertEqual(outcome, "published")
        self.assertTrue(llm.prompts)
        joined_prompt = "\n".join(
            str(item.get("content") or "") for item in llm.prompts[0]
        )
        self.assertNotIn("哨兵内容", joined_prompt)
        rebuilt = await store.load(self.GROUP_ID)
        self.assertEqual(rebuilt.version, 2)
        self.assertEqual(rebuilt.summary, "重建摘要：只包含现存内容。")
        self.assertEqual(rebuilt.covered_from_id, ids[0])
        # 重建覆盖的是**现存**的最近 200 条：删掉一条后范围往后挪一格、计数仍是 200。
        self.assertEqual(rebuilt.covered_count, 200)
        self.assertEqual(rebuilt.covered_through_id, ids[200])
        # 重建后的覆盖是完整的 → 前台恢复注入，且不含哨兵
        blocks = await memory._format_system_memory_blocks(self.GROUP_ID)
        joined_blocks = "\n".join(str(block.get("content") or "") for block in blocks)
        self.assertIn(GROUP_SUMMARY_BLOCK_MARKER, joined_blocks)
        self.assertIn("重建摘要", joined_blocks)
        self.assertNotIn("哨兵内容", joined_blocks)

    async def test_source_deleted_during_generation_is_not_published(self) -> None:
        await self._seed(600)
        ids = await self._archive_ids()
        store = SqlGroupSummaryStore(self.session_factory)
        await store.publish(
            self.GROUP_ID,
            summary="旧摘要",
            expected_version=0,
            covered_from_key=f"{self.GROUP_ID}:0",
            covered_through_key=f"{self.GROUP_ID}:199",
            covered_count=200,
            source_truncated=False,
            covered_from_id=ids[0],
            covered_through_id=ids[199],
        )

        async def _delete_during_generation() -> None:
            async with self.session_factory() as session:
                await session.execute(
                    delete(GroupMessageArchive).where(
                        GroupMessageArchive.id == ids[300]
                    )
                )
                await session.commit()

        llm = _CapturingLLM("摘要：模型调用期间源被删。", side_effect=_delete_during_generation)
        scheduler = self._scheduler(store, llm)
        scheduler.notify(self.GROUP_ID)
        outcome = await scheduler.run_group(
            self.GROUP_ID, scheduler._config_provider()
        )

        self.assertEqual(outcome, "source_changed")
        kept = await store.load(self.GROUP_ID)
        self.assertEqual(kept.version, 1)
        self.assertEqual(kept.summary, "旧摘要")


class AdvancementTests(unittest.IsolatedAsyncioTestCase):
    async def test_notify_during_run_requeues_the_group(self) -> None:
        store = FakeStore({1: _rows(1, 600)})
        llm = _DeferredLLM()
        cfg = GroupSummaryConfig(enabled=True, min_refresh_seconds=0.0)
        scheduler = GroupSummaryScheduler(
            llm=llm, store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        scheduler.notify(1)
        task = asyncio.create_task(scheduler.run_group(1, cfg))
        await asyncio.wait_for(llm.started.wait(), 1.0)

        scheduler.notify(1)  # 运行期间又有新消息
        llm.release.set()
        await asyncio.wait_for(task, 1.0)

        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["requeued_redirty_total"], 1)
        # 还在 pending 里，并且可以再次被 claim（公平重排，不用等新通知）
        self.assertIn(1, scheduler._pending)
        self.assertEqual(scheduler._claim_next(cfg), 1)

    async def test_backlog_after_success_requeues_and_wakes_itself(self) -> None:
        rows = _rows(1, 800)  # recent_raw 200 ⇒ 600 条未摘要；单批最多 200
        store = FakeStore({1: rows})
        llm = FakeLLM()
        cfg = GroupSummaryConfig(
            enabled=True,
            recent_raw_messages=200,
            batch_max_messages=200,
            trigger_messages=200,
            min_refresh_seconds=0.0,
        )
        scheduler = GroupSummaryScheduler(
            llm=llm, store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        scheduler.notify(1)
        first = await scheduler.run_group(1, cfg)
        self.assertEqual(first, "published")
        self.assertEqual(scheduler.snapshot()["requeued_backlog_total"], 1)
        self.assertIn(1, scheduler._pending)
        # min_refresh=0 ⇒ 可以立刻继续（不需要群里再来消息）
        self.assertEqual(scheduler._claim_next(cfg), 1)
        second = await scheduler.run_group(1, cfg)
        self.assertEqual(second, "published")
        self.assertEqual(len(llm.calls), 2)
        # 第三批（剩下 200 条）也会被重排；此时积压降下来了就自然收尾
        await scheduler.run_group(1, cfg)

    async def test_backlog_requeue_respects_min_refresh_and_wakes_itself(self) -> None:
        rows = _rows(1, 800)
        store = FakeStore({1: rows})
        llm = FakeLLM()
        cfg = GroupSummaryConfig(
            enabled=True,
            recent_raw_messages=200,
            batch_max_messages=200,
            trigger_messages=200,
            min_refresh_seconds=2.0,
        )
        scheduler = GroupSummaryScheduler(
            llm=llm, store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        scheduler.notify(1)
        self.assertEqual(await scheduler.run_group(1, cfg), "published")

        # 有积压：按最小刷新间隔排到队尾，并且**自己**会醒来（不需要新通知）
        self.assertIsNone(scheduler._claim_next(cfg))
        wake_in = scheduler._next_wake_in(cfg)
        self.assertIsNotNone(wake_in)
        self.assertLessEqual(wake_in, 2.0)
        self.assertEqual(scheduler.snapshot()["queue_expired_total"], 0)

    async def test_intentional_waits_are_never_counted_as_queue_expiry(self) -> None:
        store = FakeStore({1: _rows(1, 600)})
        cfg = GroupSummaryConfig(
            enabled=True, min_refresh_seconds=5.0, queue_wait_seconds=0.2
        )
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        scheduler.notify(1)
        scheduler._last_success_at[1] = scheduler._clock()

        self.assertIsNone(scheduler._claim_next(cfg))

        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["queue_expired_total"], 0)
        self.assertGreaterEqual(snapshot["skipped_not_ready_total"], 1)
        self.assertIn(1, scheduler._pending)
        wake_in = scheduler._next_wake_in(cfg)
        self.assertIsNotNone(wake_in)
        self.assertLessEqual(wake_in, 5.0)

    async def test_failure_backoff_is_also_an_intentional_wait(self) -> None:
        store = FakeStore({1: _rows(1, 600)})
        cfg = GroupSummaryConfig(
            enabled=True,
            queue_wait_seconds=0.2,
            failure_backoff_seconds=5.0,
            min_refresh_seconds=0.0,
        )
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        scheduler.notify(1)
        scheduler._register_failure(1, cfg)

        self.assertIsNone(scheduler._claim_next(cfg))

        self.assertEqual(scheduler.snapshot()["queue_expired_total"], 0)
        self.assertIsNotNone(scheduler._next_wake_in(cfg))


class BoundedStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_aux_maps_are_pruned_and_capped(self) -> None:
        store = FakeStore({})
        cfg = GroupSummaryConfig(enabled=True, pending_capacity=4)
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(), store=store, config_provider=lambda: cfg, slot_waiter=None
        )
        now = scheduler._clock()
        for group_id in range(300):
            scheduler._backoff_until[group_id] = now - 1.0  # 过期且不在 pending
            scheduler._failures[group_id] = 1
            scheduler._last_success_at[group_id] = now - 99_999.0
            scheduler.metrics.last_success[group_id] = {"version": 1}

        await scheduler._pump()

        snapshot = scheduler.snapshot()
        self.assertLessEqual(len(scheduler._backoff_until), 256)
        self.assertLessEqual(len(scheduler._failures), 256)
        self.assertLessEqual(len(scheduler._last_success_at), 256)
        self.assertLessEqual(len(scheduler.metrics.last_success), 200)
        self.assertGreater(snapshot["aux_pruned_total"], 0)

    async def test_effective_concurrency_follows_the_gate_capacity(self) -> None:
        cfg = GroupSummaryConfig(enabled=True, global_concurrency=8)
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=FakeStore({}),
            config_provider=lambda: cfg,
            slot_waiter=None,
            background_capacity=lambda: 2,
        )

        self.assertEqual(scheduler._effective_concurrency(cfg), 2)
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["global_concurrency"], 8)
        self.assertEqual(snapshot["effective_concurrency"], 2)

        unlimited = GroupSummaryScheduler(
            llm=FakeLLM(),
            store=FakeStore({}),
            config_provider=lambda: cfg,
            slot_waiter=None,
        )
        self.assertEqual(unlimited._effective_concurrency(cfg), 8)


class PartialCoverageTests(unittest.IsolatedAsyncioTestCase):
    GROUP_ID = fixtures.MemoryIntegrationTests.GROUP_ID
    asyncSetUp = fixtures.MemoryIntegrationTests.asyncSetUp
    asyncTearDown = fixtures.MemoryIntegrationTests.asyncTearDown
    _seed = fixtures.MemoryIntegrationTests._seed
    _memory = fixtures.MemoryIntegrationTests._memory
    _archive_ids = fixtures.MemoryIntegrationTests._archive_ids

    async def test_behind_summary_keeps_a_wider_raw_window(self) -> None:
        """摘要落后时保留更多未覆盖原文；赶上水位后才收敛到 recent N。"""

        await self._seed(800)
        ids = await self._archive_ids()
        store = SqlGroupSummaryStore(self.session_factory)
        await store.publish(
            self.GROUP_ID,
            summary="只覆盖了最早 200 条。",
            expected_version=0,
            covered_from_key="k",
            covered_through_key="k",
            covered_count=200,
            source_truncated=False,
            covered_from_id=ids[0],
            covered_through_id=ids[199],
        )
        memory = self._memory(enabled=True, recent_raw=200)

        behind = await memory.load_group_history_by_budget(self.GROUP_ID)

        self.assertGreater(len(behind), memory.group_summary_recent_raw_messages)
        self.assertLessEqual(len(behind), memory.group_history_max_messages)

        # 摘要追平（覆盖到 ids[599]，只剩最近 200 条在窗口内）
        await store.publish(
            self.GROUP_ID,
            summary="已覆盖到 599。",
            expected_version=1,
            covered_from_key="k",
            covered_through_key="k",
            covered_count=400,
            source_truncated=False,
            covered_from_id=ids[0],
            covered_through_id=ids[599],
        )
        caught_up = await memory.load_group_history_by_budget(self.GROUP_ID)

        self.assertEqual(len(caught_up), memory.group_summary_recent_raw_messages)

    async def test_without_a_published_summary_the_sliding_window_is_kept(self) -> None:
        await self._seed(800)
        memory = self._memory(enabled=True, recent_raw=200)

        history = await memory.load_group_history_by_budget(self.GROUP_ID)

        self.assertGreater(len(history), memory.group_summary_recent_raw_messages)


class ConfigAndBudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_env_seed_reaches_the_bot_config(self) -> None:
        env = {
            "GROUP_SUMMARY_ENABLED": "true",
            "GROUP_SUMMARY_TRIGGER_MESSAGES": "77",
            "GROUP_SUMMARY_DEADLINE_SECONDS": "9.5",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = Settings(_env_file=None)
            apply_top_level_budget_overrides(settings)

        self.assertTrue(settings.bot.group_summary_enabled)
        self.assertEqual(settings.bot.group_summary_trigger_messages, 77)
        self.assertEqual(settings.bot.group_summary_deadline_seconds, 9.5)
        self.assertTrue(settings.group_summary_enabled)

    async def test_summary_call_passes_the_api_output_limit(self) -> None:
        config = BotConfig(group_summary_max_tokens=3333)
        llm = LLMService(config.main_model, config.main_model, compress=config.main_model)
        captured: dict = {}

        async def _fake_chat_with_fallbacks(**kwargs):
            captured["candidates"] = kwargs["candidates"]
            return "ok"

        with patch.object(
            llm, "_chat_with_fallbacks", side_effect=_fake_chat_with_fallbacks
        ):
            result = await llm.background_summary_completion(
                [{"role": "user", "content": "x"}], max_tokens=3333
            )

        self.assertEqual(result, "ok")
        self.assertTrue(captured["candidates"])
        self.assertEqual(captured["candidates"][0].max_tokens, 3333)

    def test_summary_prompt_fits_the_batch_input_budget(self) -> None:
        from bot.services.group_summary import SummarySnapshotMessage
        from bot.services.model_limits import estimate_messages_tokens

        snapshot = [
            SummarySnapshotMessage(
                message_key=f"g:{index}",
                role="user",
                content="很长的中文原文" * 200,
                message_id=index + 1,
            )
            for index in range(200)
        ]
        budget = 4096

        messages, kept, truncated = fit_summary_prompt(
            group_id=-1, previous=None, snapshot=snapshot,
            source_truncated=False, max_input_tokens=budget,
        )

        self.assertTrue(kept)
        self.assertLess(len(kept), len(snapshot))
        self.assertTrue(truncated)
        self.assertLessEqual(estimate_messages_tokens(messages), budget)

    def test_memory_input_budget_matches_the_final_gate_reserve(self) -> None:
        from bot.services.memory import MemoryService

        class Stub:
            class main:
                model = "stub"

        memory = MemoryService(BotConfig(), Stub(), session_factory=object())
        llm = LLMService(
            BotConfig().main_model,
            BotConfig().main_model,
            compress=BotConfig().main_model,
        )
        candidate = llm._chat_candidates(llm.main)[0]

        self.assertEqual(memory._llm_input_budget(), 245_760)
        self.assertEqual(llm.input_token_budget(candidate), 245_760)

    def test_business_budget_caps_memory_even_without_metadata(self) -> None:
        from bot.services.memory import MemoryService

        class Stub:
            class main:
                model = "stub"

        ml.reset_model_limits_for_tests()
        self.addCleanup(ml.reset_model_limits_for_tests)
        memory = MemoryService(
            BotConfig(context_budget_tokens=100_000, context_reserve_tokens=20_000),
            Stub(),
            session_factory=object(),
        )

        self.assertEqual(memory.max_context, 100_000)
        self.assertEqual(memory._llm_input_budget(), 80_000)
        self.assertEqual(memory.group_history_token_budget, 80_000)


if __name__ == "__main__":
    unittest.main()
