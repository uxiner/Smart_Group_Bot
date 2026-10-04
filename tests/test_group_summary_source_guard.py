"""摘要来源完整性守卫（第二轮 A）：真实 SqlStore / MemoryService 路径的负例与正例。

守着四件事：

1. **新消息插入不动**内容版本 → 已覆盖的有效摘要继续可用（多批滚动稳定）；
2. **原地编辑**（id/行数不变）与**删除**（包括被快照跳过的空行、被预算截断的行）都会
   让内容版本 +1 → 摘要立刻失效、不注入；
3. 守卫与发布**在同一句 SQL 里**（生成期间改/删 → 发布被拒），不靠"前后各查一次"；
4. 本特性之前的旧摘要（没有保护数据）按失效处理，由 worker 重建；外键这类数据库错误
   不静默。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta

from sqlalchemy import select, update

from bot.db.models import GroupArchiveState, GroupSummary
from bot.services.group_summary import (
    GROUP_SUMMARY_BLOCK_MARKER,
    GroupSummaryConfig,
    GroupSummaryScheduler,
    SqlGroupSummaryStore,
)
from bot.utils.timezone import now_shanghai_naive
from tests import test_group_summary as fixtures
from tests.test_group_summary import _rows


class _CapturingLLM:
    def __init__(self, output: str = "重建摘要：只含现存内容。") -> None:
        self.output = output
        self.prompts: list[list[dict]] = []

    async def background_summary_completion(self, messages, **_kwargs) -> str:
        self.prompts.append(list(messages))
        return self.output


class SourceGuardTests(unittest.IsolatedAsyncioTestCase):
    GROUP_ID = fixtures.MemoryIntegrationTests.GROUP_ID
    asyncSetUp = fixtures.MemoryIntegrationTests.asyncSetUp
    asyncTearDown = fixtures.MemoryIntegrationTests.asyncTearDown
    _seed = fixtures.MemoryIntegrationTests._seed
    _memory = fixtures.MemoryIntegrationTests._memory
    _archive_ids = fixtures.MemoryIntegrationTests._archive_ids

    async def _publish(self, store, **overrides):
        ids = await self._archive_ids()
        payload = {
            "summary": "第一版摘要：讨论了显卡行情。",
            "expected_version": 0,
            "covered_from_key": f"{self.GROUP_ID}:0",
            "covered_through_key": f"{self.GROUP_ID}:199",
            "covered_count": 200,
            "source_truncated": False,
            "covered_from_id": ids[0],
            "covered_through_id": ids[199],
        }
        payload.update(overrides)
        return await store.publish(self.GROUP_ID, **payload)

    # -- 正例：新消息不动版本 -------------------------------------------------
    async def test_new_messages_do_not_invalidate_a_valid_summary(self) -> None:
        await self._seed(600)
        store = SqlGroupSummaryStore(self.session_factory)
        self.assertIsNotNone(await self._publish(store))
        memory = self._memory(enabled=True)

        before = await store.content_revision(self.GROUP_ID)
        record = await memory.published_group_summary(self.GROUP_ID)
        self.assertIsNotNone(record)
        self.assertEqual(record.source_revision, before)

        # 真实写入一条新消息（archive_message 路径）
        await memory.archive_message(
            self.GROUP_ID,
            "user",
            "新消息：今天继续聊显卡",
            message_id="new-1",
            telegram_message_id=900_001,
            created_at=now_shanghai_naive(),
            sender_id=7,
            sender_display_name="群友",
            message_type="text",
        )

        self.assertEqual(await store.content_revision(self.GROUP_ID), before)
        self.assertIsNotNone(await memory.published_group_summary(self.GROUP_ID))

    async def test_multi_batch_rolling_stays_valid(self) -> None:
        await self._seed(800)
        store = SqlGroupSummaryStore(self.session_factory)
        ids = await self._archive_ids()
        self.assertIsNotNone(await self._publish(store))
        second = await store.publish(
            self.GROUP_ID,
            summary="第二版摘要：把第一批也累计进来了。",
            expected_version=1,
            covered_from_key=f"{self.GROUP_ID}:200",
            covered_through_key=f"{self.GROUP_ID}:399",
            covered_count=200,
            source_truncated=False,
            covered_from_id=ids[200],
            covered_through_id=ids[399],
        )
        self.assertIsNotNone(second)
        memory = self._memory(enabled=True)

        record = await memory.published_group_summary(self.GROUP_ID)

        self.assertIsNotNone(record)
        self.assertEqual(record.version, 2)
        # 累计覆盖：from 仍是第一批的起点，count 累计
        self.assertEqual(record.covered_from_id, ids[0])
        self.assertEqual(record.covered_count, 400)

    # -- 负例 1：原地编辑（P4） ----------------------------------------------
    async def test_in_place_content_edit_invalidates_and_rebuild_excludes_sentinel(self) -> None:
        await self._seed(600)
        store = SqlGroupSummaryStore(self.session_factory)
        self.assertIsNotNone(
            await self._publish(store, summary="哨兵内容：这批原文后来被原地编辑了。")
        )
        memory = self._memory(enabled=True)
        self.assertIsNotNone(await memory.published_group_summary(self.GROUP_ID))
        before_rows = len(await self._archive_ids())

        # 原地编辑：同一个 message_key、行数与 id 都不变，只改正文（真实 upsert 路径）。
        await memory.archive_message(
            self.GROUP_ID,
            "user",
            "编辑后的正文：内容已经被改掉了",
            message_id="5",  # scoped id = f"{group}:5" → 命中既有归档行（原地编辑）
            telegram_message_id=1005,
            created_at=now_shanghai_naive() - timedelta(hours=1),
            sender_id=7,
            sender_display_name="群友",
            message_type="text",
            edited_at=now_shanghai_naive(),
        )

        self.assertEqual(len(await self._archive_ids()), before_rows)  # 行数没变
        after_revision = await store.content_revision(self.GROUP_ID)
        self.assertEqual(after_revision, 1)
        self.assertIsNone(await memory.published_group_summary(self.GROUP_ID))

        # 重建：提示词不许带上被编辑前的旧摘要（哨兵）
        llm = _CapturingLLM("重建摘要：只含现存内容。")
        scheduler = GroupSummaryScheduler(
            llm=llm,
            store=store,
            config_provider=lambda: GroupSummaryConfig(
                enabled=True, min_refresh_seconds=0.0
            ),
            slot_waiter=None,
        )
        scheduler.notify(self.GROUP_ID)
        outcome = await scheduler.run_group(
            self.GROUP_ID, scheduler._config_provider()
        )

        self.assertEqual(outcome, "published")
        joined = "\n".join(str(item.get("content") or "") for item in llm.prompts[0])
        self.assertNotIn("哨兵内容", joined)
        rebuilt = await store.load(self.GROUP_ID)
        self.assertEqual(rebuilt.version, 2)
        self.assertEqual(rebuilt.source_revision, after_revision)

    # -- 负例 2：空行/预算截断造成的"间隙"删除（P5） -------------------------
    async def test_delete_in_a_snapshot_gap_invalidates(self) -> None:
        """COUNT 口径会漏掉的删除：被跳过的空行与被预算截断的行，删了也要失效。"""

        await self._seed(600)
        # 一条空 content 的归档行（快照会跳过它）
        async with self.session_factory() as session:
            from bot.db.models import GroupMessageArchive

            session.add(
                GroupMessageArchive(
                    group_id=self.GROUP_ID,
                    message_key=f"{self.GROUP_ID}:empty",
                    telegram_message_id=950_000,
                    role="user",
                    direction="inbound",
                    sender_display_name="群友",
                    sender_id=7,
                    message_type="text",
                    content="",
                    raw_text="",
                    sent_at=now_shanghai_naive() - timedelta(hours=2),
                )
            )
            await session.commit()
        ids = await self._archive_ids()
        empty_id = max(ids)  # 空行最后插入 → id 最大（在 recent 窗口之外）
        store = SqlGroupSummaryStore(self.session_factory)
        self.assertIsNotNone(await self._publish(store))
        memory = self._memory(enabled=True)
        self.assertIsNotNone(await memory.published_group_summary(self.GROUP_ID))

        async with self.session_factory() as session:
            from sqlalchemy import delete

            from bot.db.models import GroupMessageArchive

            await session.execute(
                delete(GroupMessageArchive).where(GroupMessageArchive.id == empty_id)
            )
            await session.commit()

        self.assertEqual(await store.content_revision(self.GROUP_ID), 1)
        self.assertIsNone(await memory.published_group_summary(self.GROUP_ID))

    # -- 负例 3：生成期间改/删更早批次（P3）+ 守卫/发布竞态 -------------------
    async def test_mutation_of_an_earlier_batch_blocks_the_publish(self) -> None:
        await self._seed(800)
        store = SqlGroupSummaryStore(self.session_factory)
        self.assertIsNotNone(await self._publish(store))

        # worker 在"生成开始"时读到的版本
        revision_at_generation = await store.content_revision(self.GROUP_ID)

        # 生成期间：删掉**第一批**（更早批次）的一条 + 计数 +1（真实 delete 入口的语义）
        ids = await self._archive_ids()
        memory = self._memory(enabled=True)
        async with self.session_factory() as session:
            from sqlalchemy import delete

            from bot.db.models import GroupMessageArchive

            await session.execute(
                delete(GroupMessageArchive).where(GroupMessageArchive.id == ids[3])
            )
            await session.commit()

        # 发布带着"生成时刻"的版本 → SQL 条件不成立 → 拒绝（版本不变）
        blocked = await store.publish(
            self.GROUP_ID,
            summary="带旧正文的摘要",
            expected_version=1,
            covered_from_key="k",
            covered_through_key="k",
            covered_count=10,
            source_truncated=False,
            covered_from_id=ids[400],
            covered_through_id=ids[420],
            expected_source_revision=revision_at_generation,
        )

        self.assertIsNone(blocked)
        kept = await store.load(self.GROUP_ID)
        self.assertEqual(kept.version, 1)
        self.assertEqual(kept.summary, "第一版摘要：讨论了显卡行情。")

    async def test_guard_and_publish_race_is_decided_by_the_sql_condition(self) -> None:
        """真实并发探针：一个任务改源、另一个用旧版本发布 → 只有一个结果，绝不静默。"""

        await self._seed(800)
        store = SqlGroupSummaryStore(self.session_factory)
        self.assertIsNotNone(await self._publish(store))
        ids = await self._archive_ids()
        revision = await store.content_revision(self.GROUP_ID)

        memory = self._memory(enabled=True)

        async def _mutate() -> None:
            async with self.session_factory() as session:
                from sqlalchemy import delete

                from bot.db.models import GroupMessageArchive

                await session.execute(
                    delete(GroupMessageArchive).where(
                        GroupMessageArchive.id == ids[1]
                    )
                )
                await session.commit()

        async def _publish_with_stale_revision():
            return await store.publish(
                self.GROUP_ID,
                summary="竞态发布",
                expected_version=1,
                covered_from_key="k",
                covered_through_key="k",
                covered_count=5,
                source_truncated=False,
                covered_from_id=ids[700],
                covered_through_id=ids[720],
                expected_source_revision=revision,
            )

        # 两种交错都跑：结果必须一致——源被改之后，带旧版本的发布一定失败。
        await _mutate()
        result = await _publish_with_stale_revision()
        self.assertIsNone(result)

        # 反方向：先发布（源还是原样）→ 成功；随后的修改只影响**下一次**前台读取
        record = await store.load(self.GROUP_ID)
        self.assertEqual(record.version, 1)
        published_first = await store.publish(
            self.GROUP_ID,
            summary="先发布成功",
            expected_version=1,
            covered_from_key="k",
            covered_through_key="k",
            covered_count=5,
            source_truncated=False,
            covered_from_id=ids[700],
            covered_through_id=ids[720],
            expected_source_revision=await store.content_revision(self.GROUP_ID),
        )
        self.assertIsNotNone(published_first)
        self.assertIsNotNone(await memory.published_group_summary(self.GROUP_ID))

    async def test_concurrent_mutation_never_leaves_the_summary_injectable(self) -> None:
        """真并发探针：改源与发布同时发生，**任何交错**下前台都不许再注入旧摘要。"""

        await self._seed(900)
        ids = await self._archive_ids()
        store = SqlGroupSummaryStore(self.session_factory)
        memory = self._memory(enabled=True)

        for round_no in range(10):
            current = await store.load(self.GROUP_ID)
            expected = current.version if current else 0
            revision = await store.content_revision(self.GROUP_ID)
            target_id = ids[100 + round_no]
            from bot.db.models import GroupMessageArchive

            async def _mutate(row_id: int = target_id) -> None:
                async with self.session_factory() as session:
                    from sqlalchemy import delete

                    await session.execute(
                        delete(GroupMessageArchive).where(
                            GroupMessageArchive.id == row_id
                        )
                    )
                    await session.commit()

            async def _publish() -> object:
                return await store.publish(
                    self.GROUP_ID,
                    summary=f"第{round_no + 1}版摘要：竞态轮次。",
                    expected_version=expected,
                    covered_from_key="k",
                    covered_through_key="k",
                    covered_count=5,
                    source_truncated=False,
                    covered_from_id=ids[700],
                    covered_through_id=ids[720],
                    expected_source_revision=revision,
                )

            await asyncio.gather(_mutate(), _publish())

            # 无论两种交错谁先谁后：源已经变了 → 前台绝不注入。
            self.assertIsNone(
                await memory.published_group_summary(self.GROUP_ID),
                f"round {round_no} must not inject after the source changed",
            )

    # -- 兼容与错误处理 ------------------------------------------------------
    async def test_legacy_summary_without_protection_is_stale(self) -> None:
        await self._seed(600)
        store = SqlGroupSummaryStore(self.session_factory)
        self.assertIsNotNone(await self._publish(store))

        # 模拟本特性之前发布的旧行（没有保护数据）
        async with self.session_factory() as session:
            await session.execute(
                update(GroupSummary)
                .where(GroupSummary.group_id == self.GROUP_ID)
                .values(source_revision=-1)
            )
            await session.commit()

        memory = self._memory(enabled=True)
        self.assertIsNone(await memory.published_group_summary(self.GROUP_ID))

        # worker 也必须把它当失效：提示词不带旧正文，重建后带上真实来源版本。
        llm = _CapturingLLM("重建摘要：只含现存内容。")
        scheduler = GroupSummaryScheduler(
            llm=llm,
            store=store,
            config_provider=lambda: GroupSummaryConfig(
                enabled=True, min_refresh_seconds=0.0
            ),
            slot_waiter=None,
        )
        scheduler.notify(self.GROUP_ID)
        outcome = await scheduler.run_group(
            self.GROUP_ID, scheduler._config_provider()
        )

        self.assertEqual(outcome, "published")
        joined = "\n".join(str(item.get("content") or "") for item in llm.prompts[0])
        self.assertNotIn("第一版摘要", joined)
        rebuilt = await store.load(self.GROUP_ID)
        self.assertEqual(rebuilt.source_revision, 0)
        self.assertEqual(rebuilt.version, 2)

    async def test_foreign_key_failure_is_not_swallowed(self) -> None:
        """外键失败不是"唯一键竞争"：必须上抛，不能静默成 stale。"""

        store = SqlGroupSummaryStore(self.session_factory)

        with self.assertRaises(Exception) as ctx:
            await store.publish(
                -999_999,  # 没有 groups 行 → FK 失败
                summary="fk",
                expected_version=0,
                covered_from_key="k",
                covered_through_key="k",
                covered_count=1,
                source_truncated=False,
                covered_from_id=1,
                covered_through_id=2,
            )

        self.assertNotIsInstance(ctx.exception, AssertionError)
        self.assertIn("IntegrityError", type(ctx.exception).__name__)

    async def test_revision_counting_works_without_a_groups_row(self) -> None:
        """归档行可以存在于没有 groups 行的群（维护/导入）：计数必须照样能写。"""

        orphan_group = -424242
        async with self.session_factory() as session:
            from bot.db.models import GroupMessageArchive

            session.add(
                GroupMessageArchive(
                    group_id=orphan_group,
                    message_key=f"{orphan_group}:1",
                    telegram_message_id=1,
                    role="user",
                    direction="inbound",
                    sender_display_name="群友",
                    sender_id=7,
                    message_type="text",
                    content="孤儿群的历史",
                    raw_text="孤儿群的历史",
                    sent_at=now_shanghai_naive() - timedelta(hours=3),
                )
            )
            await session.commit()
        async with self.session_factory() as session:
            from sqlalchemy import delete

            from bot.db.models import GroupMessageArchive

            # 真实删除（触发器维护版本计数），且这个群没有 groups 行
            await session.execute(
                delete(GroupMessageArchive).where(
                    GroupMessageArchive.group_id == orphan_group
                )
            )
            await session.commit()

        store = SqlGroupSummaryStore(self.session_factory)
        self.assertEqual(await store.content_revision(orphan_group), 1)

    async def test_revision_row_is_created_only_for_mutations(self) -> None:
        await self._seed(50)
        store = SqlGroupSummaryStore(self.session_factory)

        self.assertEqual(await store.content_revision(self.GROUP_ID), 0)
        async with self.session_factory() as session:
            rows = (
                await session.execute(select(GroupArchiveState))
            ).scalars().all()
        self.assertEqual(list(rows), [])


if __name__ == "__main__":
    unittest.main()
