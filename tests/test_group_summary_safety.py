"""Parent acceptance probes for single-group scheduling and archive deletion safety."""
from __future__ import annotations
import asyncio
import unittest
from unittest.mock import patch
from sqlalchemy import delete
from bot.db.models import GroupMessageArchive
from bot.services.group_summary import GroupSummaryConfig, GroupSummaryScheduler, SqlGroupSummaryStore
from tests.test_group_summary import FakeStore, FakeLLM, _rows
from tests import test_group_summary as fixtures

class SchedulerSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_group_is_claimed_once_before_tasks_start(self):
        cfg = GroupSummaryConfig(enabled=True, global_concurrency=2)
        release = asyncio.Event()
        llm = FakeLLM(release=release)
        scheduler = GroupSummaryScheduler(llm=llm, store=FakeStore({1:_rows(1, 600)}), config_provider=lambda:cfg, slot_waiter=None)
        scheduler.notify(1)
        try:
            await scheduler._pump()
            await asyncio.sleep(0)
            self.assertEqual(len(scheduler._tasks), 1)
            self.assertLessEqual(llm.peak, 1)
        finally:
            release.set()
            await scheduler.shutdown()

    async def test_fully_swallowed_cancel_cannot_publish(self):
        release, started = asyncio.Event(), asyncio.Event()
        class Stubborn:
            async def background_summary_completion(self, messages):
                started.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                return '旧聊天摘要：讨论了显卡行情。'
        cfg = GroupSummaryConfig(enabled=True)
        store=FakeStore({1:_rows(1,600)})
        scheduler=GroupSummaryScheduler(llm=Stubborn(), store=store, config_provider=lambda:cfg, slot_waiter=None)
        scheduler.notify(1)
        task=asyncio.create_task(scheduler.run_group(1,cfg))
        await asyncio.wait_for(started.wait(),1)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(store.publish_calls,0)

    async def test_swallowed_deadline_cannot_publish(self):
        class Stubborn:
            async def background_summary_completion(self, messages):
                try:
                    await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    pass
                return '旧聊天摘要：讨论了显卡行情。'
        cfg=GroupSummaryConfig(enabled=True,deadline_seconds=0.01)
        store=FakeStore({1:_rows(1,600)})
        scheduler=GroupSummaryScheduler(llm=Stubborn(),store=store,config_provider=lambda:cfg,slot_waiter=None)
        scheduler.notify(1)
        await scheduler.run_group(1,cfg)
        self.assertEqual(store.publish_calls,0)

class ArchiveSafetyTests(unittest.IsolatedAsyncioTestCase):
    GROUP_ID=fixtures.MemoryIntegrationTests.GROUP_ID
    asyncSetUp=fixtures.MemoryIntegrationTests.asyncSetUp
    asyncTearDown=fixtures.MemoryIntegrationTests.asyncTearDown
    _seed=fixtures.MemoryIntegrationTests._seed
    _memory=fixtures.MemoryIntegrationTests._memory
    _archive_ids=fixtures.MemoryIntegrationTests._archive_ids

    async def test_deferred_write_notifies_only_after_archive_commit(self):
        memory=self._memory(enabled=True)
        calls=[]
        try:
            with patch('bot.services.memory.notify_group_summary',side_effect=lambda gid:calls.append(gid)):
                await memory.add_message(self.GROUP_ID,'user','群消息',message_id='1',defer_persistence=True)
                self.assertEqual(calls,[])
                self.assertTrue(await memory.flush_pending_writes())
                self.assertEqual(calls,[self.GROUP_ID])
        finally:
            await memory.shutdown()

    async def test_deletion_in_first_of_two_summary_batches_invalidates(self):
        await self._seed(600)
        ids=await self._archive_ids()
        store=SqlGroupSummaryStore(self.session_factory)
        for batch in range(2):
            first=200*batch
            await store.publish(self.GROUP_ID,summary='累计摘要：旧群聊内容。',expected_version=batch,
                covered_from_key=f'{self.GROUP_ID}:{first}',covered_through_key=f'{self.GROUP_ID}:{first+199}',
                covered_count=200,source_truncated=False,covered_from_id=ids[first],covered_through_id=ids[first+199])
        record=await store.load(self.GROUP_ID)
        self.assertEqual(record.covered_from_id,ids[0])
        self.assertEqual(record.covered_count,400)
        async with self.session_factory() as session:
            await session.execute(delete(GroupMessageArchive).where(GroupMessageArchive.id==ids[10]))
            await session.commit()
        self.assertFalse(await store.coverage_intact(self.GROUP_ID,covered_from_id=record.covered_from_id,
            covered_through_id=record.covered_through_id,covered_count=record.covered_count))
        memory=self._memory(enabled=True)
        self.assertIsNone(await memory.published_group_summary(self.GROUP_ID))
