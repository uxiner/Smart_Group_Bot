"""B13a 返工 v2：父独立验收 v5–v9 暴露的六机制缺口的正式回归。

每个用例对应父 v5–v9 探针在 0781cb37 上真实复现过的一个缺口（见交付报告）：

* R1 出站前持久保护：send 之后连接被重置 / 出站后确认写失败，旧 reaper、
  普通共享入口、旧 runner 都不能再发第二次。
* R2 执行边界复核与目标绑定：确认之后撤权威、确认之后才达标的成员、
  点数方案变化，都不能再产生积分或发布。
* R3 摘要对账只用**本次 run 自己**的发布证据，别的发布者的时间水位不算。
* R4 提交回执丢失（ack loss）不得谎称"没有发放积分"。
* R5 商店到期的业务台账与 Telegram 往返分属不同事务；私聊结果未知不换收件人。
* R6 取消必须进入发布 CAS 的同一句 SQL，而不是发布前的一次无锁检查。

只替换最外层 transport 与数据库故障注入；权限、占位、receipt、对账、发布都走
真实实现，不 mock service/run 来"证明"业务。
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import delete, func, select, update

import bot.services.checkin_reminder as checkin
import bot.services.group_summary as group_summary_module
import bot.services.operations_runs as runs
import bot.services.operations_tasks as tasks
import bot.services.point_shop as point_shop
import bot.services.scheduled_messages as scheduled
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    CheckinReminderPost,
    GroupSummary,
    MemberActivityDaily,
    MemberEntitlement,
    MemberPointAward,
    OperationsBusinessReceipt,
    OperationsRun,
    ScheduledMessageOccurrence,
)
from bot.db.sqlite_session import SQLiteSafeAsyncSession
from bot.services.activity import last_complete_week
from bot.services.group_summary import GroupSummaryScheduler, SqlGroupSummaryStore
from bot.services.operations_receipts import TASK_SHOP_EXPIRE
from bot.services.operations_runs import (
    ITEM_STATUS_SUCCEEDED,
    ITEM_STATUS_UNKNOWN,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_UNKNOWN,
)
from bot.services.point_shop import ShopExpiryService
from bot.services.scheduled_messages import ScheduledMessageService
from bot.utils.timezone import now_shanghai_naive
from tests.test_b13a_operations_center import (
    ADMIN_ID,
    GROUP_A,
    _OperationsTestCase,
    _summary_config,
)


class ProtectedDispatchV2Tests(_OperationsTestCase):
    """R1：保护必须在最终出站**之前**持久化，send 之后的任何结果都不许重发。"""

    async def test_checkin_connection_reset_after_send_is_never_resent(self) -> None:
        """真实传输记录 send 之后才抛 ConnectionResetError（回执丢失）。

        这是"发送已经出站、但本地无从证明"的典型形态：受保护入口必须保持未知，
        并且旧 reaper / 普通共享入口都不能再发第二次。
        """

        fire = [True]

        async def flaky(chat_id, text, **kwargs):
            result = await self._record_send(chat_id, text, **kwargs)
            if fire[0] and int(chat_id) == GROUP_A:
                fire[0] = False
                raise ConnectionResetError("synthetic ack reset after receive")
            return result

        with patch.object(tasks, "reminder_slots", lambda: (0,)), patch.object(
            self.bot, "send_message", new=AsyncMock(side_effect=flaky)
        ):
            run = await self._run_once("checkin_reminder", {"slot": 0})

        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        self.assertEqual(len(self.outbound), 1, self.outbound)

        # 普通共享入口（旧 CLI 走的那条）也必须被同一份证据挡住。
        async with self.session_factory() as session:
            outcome = await checkin.send_checkin_slot(
                session,
                self.bot,
                group_id=GROUP_A,
                slot=0,
                auto_delete_seconds=0,
                # 真实可 await 的队列/兜底 stub：``None`` 会在"真的又发了一次"的
                # 路径上先抛 TypeError，把业务红掩盖成夹具错误。
                queue_auto_delete=AsyncMock(return_value=True),
                backfill_auto_delete=AsyncMock(return_value=True),
                session_factory=self.session_factory,
            )
        self.assertEqual(len(self.outbound), 1, (outcome, self.outbound))
        self.assertEqual(
            outcome.status, checkin.CHECKIN_OUTCOME_ALREADY_DELIVERED, outcome
        )

    async def test_checkin_receipt_write_failure_still_blocks_the_reaper(self) -> None:
        """send 成功但**确认写入失败**：保护仍然必须挡住 reaper 与普通入口。

        这正是父 v5 的第二条：仅"尽力补写 receipt"不足以覆盖死进程/写失败；
        保护必须在发送之前就已经落库。
        """

        with patch.object(tasks, "reminder_slots", lambda: (0,)), patch.object(
            checkin,
            "record_receipt",
            new=AsyncMock(
                side_effect=RuntimeError("synthetic receipt writer unavailable after send")
            ),
        ):
            run = await self._run_once("checkin_reminder", {"slot": 0})

        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        self.assertEqual(len(self.outbound), 1, self.outbound)

        # 推进占位年龄到 reaper 的回收窗口之内。
        async with self.session_factory() as session:
            posts = list((await session.scalars(select(CheckinReminderPost))).all())
            self.assertTrue(posts, "签到占位必须已经被写入")
            for post in posts:
                post.created_at = now_shanghai_naive() - timedelta(days=1)
            await session.commit()

        async with self.session_factory() as session:
            reaped = await checkin.reap_stale_reminder_slots(
                session, grace_seconds=900
            )

        async with self.session_factory() as session:
            again = await checkin.send_checkin_slot(
                session,
                self.bot,
                group_id=GROUP_A,
                slot=0,
                auto_delete_seconds=0,
                # 真实可 await 的队列/兜底 stub：``None`` 会在"真的又发了一次"的
                # 路径上先抛 TypeError，把业务红掩盖成夹具错误。
                queue_auto_delete=AsyncMock(return_value=True),
                backfill_auto_delete=AsyncMock(return_value=True),
                session_factory=self.session_factory,
            )

        self.assertEqual(len(self.outbound), 1, (reaped, again, self.outbound))

    async def test_scheduled_receipt_write_failure_survives_lease_expiry(self) -> None:
        """定时消息：send 成功但确认写入失败，租约过期后旧 runner 也不能重发。"""

        entry_id = await self._seed_scheduled()

        with patch.object(
            scheduled,
            "record_receipt",
            new=AsyncMock(
                side_effect=RuntimeError("synthetic receipt writer unavailable after send")
            ),
        ):
            run = await self._run_once("scheduled_messages", {"entry_ids": [entry_id]})

        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        self.assertEqual(len(self.outbound), 1, self.outbound)

        async with self.session_factory() as session:
            occurrences = list(
                (await session.scalars(select(ScheduledMessageOccurrence))).all()
            )
            self.assertTrue(occurrences)
            for row in occurrences:
                row.lease_until = now_shanghai_naive() - timedelta(hours=1)
            await session.commit()

        await ScheduledMessageService(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
        ).run_once()

        self.assertEqual(len(self.outbound), 1, self.outbound)

    async def test_weekly_connection_reset_after_send_blocks_a_fresh_preview(self) -> None:
        """周报：send 之后连接被重置 → 新预览必须被出站证据挡住。"""

        await self._seed_archive(count=5)
        fire = [True]

        async def flaky(chat_id, text, **kwargs):
            result = await self._record_send(chat_id, text, **kwargs)
            if fire[0] and int(chat_id) == GROUP_A:
                fire[0] = False
                raise ConnectionResetError("synthetic ack reset after receive")
            return result

        with patch.object(
            self.bot, "send_message", new=AsyncMock(side_effect=flaky)
        ):
            run = await self._run_once("weekly_report", {})

        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        self.assertEqual(len(self.outbound), 1, self.outbound)

        fresh = await self._preview("weekly_report", {})
        self.assertEqual(fresh.status, 200, await fresh.text())
        preview = (await fresh.json())["preview"]
        self.assertNotEqual(preview["status"], "ok", preview)
        self.assertEqual(preview["status"], "blocked", preview)

    async def test_legacy_cli_and_manual_share_the_same_business_week(self) -> None:
        """R1/R4：旧 CLI 用"发送周"、运营中心用"业务周"，两者必须落到同一个业务键。"""

        await self._seed_archive(count=5)

        run = await self._run_once("weekly_report", {})
        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        self.assertEqual(len(self.outbound), 1, self.outbound)

        import bot.tools.weekly_report as cli

        self.bot.session = SimpleNamespace(close=AsyncMock())
        with (
            patch.object(cli, "Settings", lambda *a, **k: self.settings),
            patch.object(
                cli,
                "init_db",
                new=AsyncMock(
                    return_value=(
                        SimpleNamespace(dispose=AsyncMock()),
                        self.session_factory,
                    )
                ),
            ),
            patch.object(cli, "Bot", lambda *a, **k: self.bot),
        ):
            code = await cli._send_reports(7)

        self.assertEqual(code, 0, code)
        # 同一个业务周，旧 CLI 也必须被本次 run 的出站证据挡住（不重复发）。
        # 注意：测试群里只有 GROUP_A 是本次运营中心的目标，其余授权群本来就该正常发。
        group_a = [chat for chat, _ in self.outbound if int(chat) == GROUP_A]
        self.assertEqual(len(group_a), 1, self.outbound)


class BoundaryGuardV2Tests(_OperationsTestCase):
    """R2：真实执行边界的复核 + 目标/点数方案绑定。"""

    async def test_activity_actor_revocation_before_execution_awards_nothing(self) -> None:
        """活动积分：确认之后发起人被撤权 → 一分都不能发。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        original = tasks.ActivityAwardTask.run
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held(self_task, context):
            entered.set()
            await release.wait()
            return await original(self_task, context)

        with patch.object(tasks.ActivityAwardTask, "run", new=held):
            preview = (
                await (await self._preview("activity_award", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)

            async with self.session_factory() as session:
                await session.execute(
                    delete(Admin).where(
                        Admin.user_id == ADMIN_ID, Admin.group_id == GROUP_A
                    )
                )
                await session.commit()

            release.set()
            await self._wait(run_key, user=ADMIN_ID)

        self.assertEqual(await self._count(MemberPointAward), 0, "撤权后不得发分")

    async def test_summary_actor_revocation_before_execution_publishes_nothing(self) -> None:
        """摘要：确认之后发起人被撤权 → 不得调用 supplier、不得发布新版本。"""

        await self._seed_archive(count=40)
        calls: list[int] = []

        class Supplier:
            async def background_summary_completion(self, messages, **kwargs):
                calls.append(1)
                return "合成摘要：撤权之后不得发布的候选内容。"

        store = SqlGroupSummaryStore(self.session_factory)
        scheduler = GroupSummaryScheduler(
            llm=Supplier(),
            store=store,
            config_provider=lambda: _summary_config(enabled=True),
            slot_waiter=None,
        )
        original = tasks.GroupSummaryTask.run
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held(self_task, context):
            entered.set()
            await release.wait()
            return await original(self_task, context)

        with patch.object(
            group_summary_module, "GROUP_SUMMARY_SCHEDULER", scheduler
        ), patch.object(
            tasks,
            "group_summary_config",
            lambda _settings: _summary_config(enabled=True),
        ), patch.object(
            tasks.GroupSummaryTask, "run", new=held
        ):
            preview = (
                await (await self._preview("group_summary", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)

            async with self.session_factory() as session:
                await session.execute(
                    delete(Admin).where(
                        Admin.user_id == ADMIN_ID, Admin.group_id == GROUP_A
                    )
                )
                await session.commit()

            release.set()
            await self._wait(run_key, user=ADMIN_ID)

        self.assertEqual(calls, [], "撤权之后不得再调用 supplier")
        self.assertIsNone(await store.load(GROUP_A), "撤权之后不得发布新版本")

    async def test_activity_new_target_after_preview_is_not_awarded(self) -> None:
        """活动积分：确认之后才达标的成员不能被顺带发放。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        preview = (await (await self._preview("activity_award", {})).json())["preview"]
        self.assertEqual(preview["status"], "ok", preview)
        bound = {target["item_key"] for target in preview["targets"]}
        self.assertTrue(bound)

        # 预览之后才达标的新成员。
        await self._seed_activity(week_start=week_start, user_id=8)
        confirmed = await self._confirm(preview["token"])
        run_key = (await confirmed.json())["run"]["run_key"]
        await self._wait(run_key)

        async with self.session_factory() as session:
            rows = list((await session.scalars(select(MemberPointAward))).all())

        self.assertTrue(rows, "已确认的目标本身仍然应该发放")
        paid = {int(row.user_id) for row in rows}
        self.assertTrue(
            paid.issubset({7}), f"确认之后才达标的成员不得发分：{paid}"
        )

    async def test_selected_member_who_stops_qualifying_is_never_awarded(self) -> None:
        """活动积分：确认的目标在确认之后不再达标（移群/记录消失）→ 整次拒绝，不发分。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        preview = (await (await self._preview("activity_award", {})).json())["preview"]
        self.assertEqual(preview["status"], "ok", preview)
        self.assertTrue(preview["targets"])

        # 确认之前，这个成员的活动记录消失（例如退群后记录被清理，或不再达标）。
        async with self.session_factory() as session:
            await session.execute(
                delete(MemberActivityDaily).where(
                    MemberActivityDaily.group_id == GROUP_A,
                    MemberActivityDaily.user_id == 7,
                )
            )
            await session.commit()

        confirmed = await self._confirm(preview["token"])
        run_key = (await confirmed.json())["run"]["run_key"]
        run = (await (await self._wait(run_key)).json())["run"]

        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertEqual(run["reason"], tasks.REASON_SOURCE_CHANGED, run)
        self.assertEqual(await self._count(MemberPointAward), 0)

    async def test_activity_point_plan_change_is_rejected(self) -> None:
        """点数方案变化 → 整次拒绝（不静默变更既定积分标准）。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7, days=5, messages=20)

        preview = (await (await self._preview("activity_award", {})).json())["preview"]
        self.assertEqual(preview["status"], "ok", preview)
        item_key = preview["targets"][0]["item_key"]
        changed = item_key.rsplit(":", 1)[0] + ":999999"

        context = runs.RunContext(
            run_id=0,
            run_key="synthetic-plan-change",
            task_id="activity_award",
            group_id=GROUP_A,
            params={},
            source="manual",
            retry_of_run_id=None,
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            cancel_event=asyncio.Event(),
            target_keys=(changed,),
            actor_user_id=ADMIN_ID,
            actor_role="admin",
            # 真实 run 的 guard_revision 取确认时的持久配置 revision；这里用手工上下文
            # 直接取当前值，保证被复核的是"点数方案变化"而不是配置 revision。
            guard_revision=int(self.manager.revision),
        )
        result = await tasks.ActivityAwardTask().run(context)

        self.assertEqual(result.status, RUN_STATUS_FAILED, result)
        self.assertEqual(result.reason, tasks.REASON_SOURCE_CHANGED, result)
        self.assertEqual(await self._count(MemberPointAward), 0)

    async def test_activity_config_revision_change_before_execution_awards_nothing(self) -> None:
        """活动积分：确认之后持久配置被改动（revision 前进）→ 不得发分。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        # 让本次确认带上一个**非零**的持久配置快照（生产里 revision 来自持久历史）。
        self.manager._revision = 3

        original = tasks.ActivityAwardTask.run
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held(self_task, context):
            entered.set()
            await release.wait()
            return await original(self_task, context)

        with patch.object(tasks.ActivityAwardTask, "run", new=held):
            preview = (
                await (await self._preview("activity_award", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)

            # 另一位管理员在确认之后改动了持久配置：revision 前进。
            self.manager._revision = int(self.manager.revision) + 1

            release.set()
            await self._wait(run_key, user=ADMIN_ID)

        self.assertEqual(await self._count(MemberPointAward), 0, "配置变更后不得发分")
        async with self.session_factory() as session:
            row = await session.scalar(
                select(OperationsRun).where(OperationsRun.run_key == run_key)
            )
        self.assertEqual(row.status, RUN_STATUS_FAILED)
        self.assertEqual(row.reason, tasks.REASON_GUARD_BLOCKED)

    async def test_summary_config_revision_change_before_execution_publishes_nothing(self) -> None:
        """摘要：确认之后持久配置被改动（revision 前进）→ 不得调用 supplier、不得发布。"""

        await self._seed_archive(count=40)
        calls: list[int] = []

        class Supplier:
            async def background_summary_completion(self, messages, **kwargs):
                calls.append(1)
                return "合成摘要：配置变更之后不得发布的候选内容。"

        store = SqlGroupSummaryStore(self.session_factory)
        # 让本次确认带上一个**非零**的持久配置快照（生产里 revision 来自持久历史）。
        self.manager._revision = 3
        scheduler = GroupSummaryScheduler(
            llm=Supplier(),
            store=store,
            config_provider=lambda: _summary_config(enabled=True),
            slot_waiter=None,
        )
        original = tasks.GroupSummaryTask.run
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held(self_task, context):
            entered.set()
            await release.wait()
            return await original(self_task, context)

        with patch.object(
            group_summary_module, "GROUP_SUMMARY_SCHEDULER", scheduler
        ), patch.object(
            tasks,
            "group_summary_config",
            lambda _settings: _summary_config(enabled=True),
        ), patch.object(
            tasks.GroupSummaryTask, "run", new=held
        ):
            preview = (
                await (await self._preview("group_summary", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)

            self.manager._revision = int(self.manager.revision) + 1

            release.set()
            await self._wait(run_key, user=ADMIN_ID)

        self.assertEqual(calls, [], "配置变更之后不得再调用 supplier")
        self.assertIsNone(await store.load(GROUP_A), "配置变更之后不得发布新版本")

    async def test_retry_never_picks_up_a_target_that_appeared_later(self) -> None:
        """部分重试只处理原 run 的可重试目标：后来才达标的新目标不被顺带发放。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        # 预览阶段也会调用 settle（award=False，且 rollback 不写库），所以只让真正
        # 发放的那一次失败，否则预览自己就 500 了。
        real_settle = tasks.settle_weekly_activity

        async def settle_failing_award(session, **kwargs):
            if kwargs.get("award"):
                raise RuntimeError("synthetic settlement failure")
            return await real_settle(session, **kwargs)

        with patch.object(tasks, "settle_weekly_activity", new=settle_failing_award):
            first = await self._run_once("activity_award", {})
        self.assertEqual(first["status"], RUN_STATUS_FAILED, first)
        self.assertEqual(await self._count(MemberPointAward), 0)

        # 第一次执行之后才达标的新成员：重试不能把它当成新目标。
        await self._seed_activity(week_start=week_start, user_id=8)

        retry = await self._post(f"/operations/runs/{first['run_key']}/retry")
        self.assertEqual(retry.status, 200, await retry.text())
        retried = (await retry.json())["run"]
        settled = await self._wait(retried["run_key"])
        retried = (await settled.json())["run"]

        async with self.session_factory() as session:
            paid = {
                int(row.user_id)
                for row in (await session.scalars(select(MemberPointAward))).all()
            }

        self.assertTrue(paid, f"原来被确认的目标本身仍然应该发放：{retried}")
        self.assertTrue(
            paid.issubset({7}), f"重试不得顺带发放后来才出现的新目标：{paid}"
        )


class SummaryEvidenceV2Tests(_OperationsTestCase):
    """R3：对账只能用本次 run 自己的发布证据。"""

    async def test_reconcile_ignores_another_publishers_watermark(self) -> None:
        """别的发布者写了更晚的版本 → 本次 run 不得被判成功。"""

        class FailingSupplier:
            async def background_summary_completion(self, messages, **kwargs):
                raise RuntimeError("synthetic supplier failure")

        await self._seed_archive(count=40)
        store = SqlGroupSummaryStore(self.session_factory)
        scheduler = GroupSummaryScheduler(
            llm=FailingSupplier(),
            store=store,
            config_provider=lambda: _summary_config(enabled=True),
            slot_waiter=None,
        )

        with patch.object(
            group_summary_module, "GROUP_SUMMARY_SCHEDULER", scheduler
        ), patch.object(
            tasks,
            "group_summary_config",
            lambda _settings: _summary_config(enabled=True),
        ):
            preview = (await (await self._preview("group_summary", {})).json())[
                "preview"
            ]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"])
            run_key = (await confirmed.json())["run"]["run_key"]
            run = (await (await self._wait(run_key)).json())["run"]
            self.assertEqual(run["status"], RUN_STATUS_FAILED, run)

            # 另一个发布者在本次 run 之后发布了 v17。
            async with self.session_factory() as session:
                session.add(
                    GroupSummary(
                        group_id=GROUP_A,
                        summary="unrelated publisher control",
                        version=17,
                        generated_at=now_shanghai_naive() + timedelta(seconds=5),
                    )
                )
                await session.commit()

            response = await self._post(f"/operations/runs/{run_key}/reconcile")
            self.assertEqual(response.status, 200, await response.text())
            body = (await response.json())["run"]

        self.assertNotEqual(body["status"], "succeeded", body)
        statuses = {item["item_key"]: item["status"] for item in body["items"]}
        self.assertNotIn(ITEM_STATUS_SUCCEEDED, statuses.values(), body)
        self.assertIn(ITEM_STATUS_UNKNOWN, statuses.values(), body)


class CommitAcknowledgementV2Tests(_OperationsTestCase):
    """R4：提交回执丢失（ack loss）不得谎称"没有发放积分"。"""

    async def _uncertain_commit(self, fired: list[bool]):
        original = SQLiteSafeAsyncSession.commit

        async def commit(session):
            await original(session)
            if fired[0]:
                return
            awarded = await session.scalar(
                select(func.count()).select_from(MemberPointAward)
            )
            if awarded:
                fired[0] = True
                raise RuntimeError("synthetic commit acknowledgement lost")

        return commit

    async def test_activity_commit_ack_loss_is_unknown_not_never_awarded(self) -> None:
        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        fired = [False]

        with patch.object(
            SQLiteSafeAsyncSession,
            "commit",
            new=await self._uncertain_commit(fired),
        ):
            run = await self._run_once("activity_award", {})

        self.assertTrue(fired[0], "故障注入必须真的落在结算提交之后")
        self.assertGreater(await self._count(MemberPointAward), 0)
        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        blob = json.dumps(run, ensure_ascii=False)
        self.assertNotIn("没有发放积分", blob)
        self.assertNotIn("没有写入任何奖励", blob)

    async def test_weekly_commit_ack_loss_does_not_claim_no_points(self) -> None:
        await self._seed_archive(count=5)
        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        fired = [False]

        with patch.object(
            SQLiteSafeAsyncSession,
            "commit",
            new=await self._uncertain_commit(fired),
        ):
            run = await self._run_once("weekly_report", {})

        self.assertTrue(fired[0], "故障注入必须真的落在结算提交之后")
        self.assertGreater(await self._count(MemberPointAward), 0)
        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        blob = json.dumps(run, ensure_ascii=False)
        self.assertNotIn("没有发放积分", blob)
        self.assertNotIn("没有写入任何奖励", blob)
        self.assertEqual(self.outbound, [], "提交结果未知时不得发出周报")


class ShopExpiryTransactionV2Tests(_OperationsTestCase):
    """R5：业务台账与 Telegram 往返分属不同事务；未知通知不换收件人。"""

    async def test_writer_lock_is_released_while_the_notice_transport_is_held(
        self,
    ) -> None:
        await self._seed_entitlement(group_id=GROUP_A, user_id=7)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held(chat_id, text, **kwargs):
            if int(chat_id) == 7:
                entered.set()
                await release.wait()
            return await self._record_send(chat_id, text, **kwargs)

        async def independent_write() -> None:
            async with self.session_factory() as session:
                await session.execute(
                    update(AuthorizedGroup)
                    .where(AuthorizedGroup.group_id == GROUP_A)
                    .values(bot_present=True)
                )
                await session.commit()

        with patch.object(self.bot, "send_message", new=AsyncMock(side_effect=held)):
            service = asyncio.ensure_future(
                ShopExpiryService(
                    bot=self.bot, session_factory=self.session_factory
                ).run_once()
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            writer = await asyncio.wait_for(
                asyncio.shield(asyncio.ensure_future(independent_write())), timeout=1
            )
            release.set()
            await asyncio.wait_for(service, timeout=10)

        self.assertIsNone(writer, "transport 持有期间独立写入者必须能提交")
        self.assertEqual(len(self.outbound), 1, self.outbound)

        async with self.session_factory() as session:
            receipts = list(
                (
                    await session.scalars(
                        select(OperationsBusinessReceipt).where(
                            OperationsBusinessReceipt.task_id == TASK_SHOP_EXPIRE
                        )
                    )
                ).all()
            )
        self.assertEqual(len(receipts), 1, receipts)
        self.assertEqual(receipts[0].state, "sent", receipts[0].state)

    async def test_private_notice_timeout_never_falls_back_to_the_group(self) -> None:
        await self._seed_entitlement(group_id=GROUP_A, user_id=7)

        async def flaky(chat_id, text, **kwargs):
            if int(chat_id) == 7:
                await self._record_send(chat_id, text, **kwargs)
                raise TimeoutError("synthetic private notice timeout")
            return await self._record_send(chat_id, text, **kwargs)

        with patch.object(
            self.bot, "send_message", new=AsyncMock(side_effect=flaky)
        ):
            await ShopExpiryService(
                bot=self.bot, session_factory=self.session_factory
            ).run_once()

        self.assertEqual([chat for chat, _ in self.outbound], [7], self.outbound)

    async def test_failed_revoke_releases_only_its_own_dispatch_protection(self) -> None:
        """撤销失败/被续期时这次确定没发提醒：自己刚抢到的保护必须放掉。"""

        await self._seed_entitlement(group_id=GROUP_A, user_id=7)
        with patch.object(
            self.bot,
            "set_chat_member_tag",
            new=AsyncMock(side_effect=RuntimeError("synthetic revoke failure")),
        ):
            await ShopExpiryService(
                bot=self.bot, session_factory=self.session_factory
            ).run_once()

        self.assertEqual(self.outbound, [], "撤销失败时不该发任何提醒")
        async with self.session_factory() as session:
            receipts = list(
                (
                    await session.scalars(
                        select(OperationsBusinessReceipt).where(
                            OperationsBusinessReceipt.task_id == TASK_SHOP_EXPIRE
                        )
                    )
                ).all()
            )
            rows = list(
                (
                    await session.scalars(
                        select(MemberEntitlement).where(
                            MemberEntitlement.group_id == GROUP_A,
                            MemberEntitlement.user_id == 7,
                        )
                    )
                ).all()
            )
        self.assertEqual(
            receipts, [], "自己抢到的保护不能留成永久 dispatching 残留"
        )
        self.assertEqual(len(rows), 1, "撤不下来时必须保留权益行并推后重试")

    async def test_definitively_rejected_notice_releases_the_protection(self) -> None:
        """提醒被明确拒收（不是"结果未知"）时同样不留下无主的 dispatching。"""

        await self._seed_entitlement(group_id=GROUP_A, user_id=7)

        async def blocked(chat_id, text, **kwargs):
            # 用真实 aiogram 明确拒收类型：文本里带 "Forbidden" 的普通 RuntimeError
            # 已经不再被当成"证明没有出站"（返工 v3 的 R1 收紧），只保留业务断言。
            raise TelegramForbiddenError(
                method=SimpleNamespace(chat_id=chat_id, text=text),
                message="Forbidden: bot was blocked by the user",
            )

        with patch.object(self.bot, "send_message", new=AsyncMock(side_effect=blocked)):
            await ShopExpiryService(
                bot=self.bot, session_factory=self.session_factory
            ).run_once()

        self.assertEqual(self.outbound, [], self.outbound)
        async with self.session_factory() as session:
            receipts = list(
                (
                    await session.scalars(
                        select(OperationsBusinessReceipt).where(
                            OperationsBusinessReceipt.task_id == TASK_SHOP_EXPIRE
                        )
                    )
                ).all()
            )
        self.assertEqual(receipts, [], receipts)

    def test_entitlement_key_keeps_subsecond_precision(self) -> None:
        """**不用**新签名：同一秒内不同微秒的到期不能塌成同一个业务键。

        这是基线（旧代码只精确到秒）真实存在的语义缺口，因此在旧树上也是
        真正的业务红，而不是"新参数不存在"的接口红。
        """

        moment = datetime(2026, 10, 11, 9, 0, 0, 123456)
        base = point_shop.entitlement_expiry_key(
            group_id=GROUP_A, user_id=7, kind="tag", expires_at=moment
        )
        other_micro = point_shop.entitlement_expiry_key(
            group_id=GROUP_A,
            user_id=7,
            kind="tag",
            expires_at=moment.replace(microsecond=123457),
        )
        same = point_shop.entitlement_expiry_key(
            group_id=GROUP_A, user_id=7, kind="tag", expires_at=moment
        )

        self.assertEqual(base, same)
        self.assertNotEqual(base, other_micro, "到期精度必须保留到微秒")

    def test_entitlement_key_distinguishes_same_second_orders(self) -> None:
        """新签名（``ref=``）的功能覆盖：同一微秒的两张订单不能共用一个键。

        基线不接受 ``ref`` 参数，所以这一条在旧树上属于**接口红**（新能力起步），
        不计入"旧代码业务红"的统计。
        """

        moment = datetime(2026, 10, 11, 9, 0, 0, 123456)
        first = point_shop.entitlement_expiry_key(
            group_id=GROUP_A, user_id=7, kind="tag", expires_at=moment, ref="order-1"
        )
        second = point_shop.entitlement_expiry_key(
            group_id=GROUP_A, user_id=7, kind="tag", expires_at=moment, ref="order-2"
        )
        repeated = point_shop.entitlement_expiry_key(
            group_id=GROUP_A, user_id=7, kind="tag", expires_at=moment, ref="order-1"
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, second, "同一微秒的不同订单不能塌成同一个键")


class PublicationCancellationV2Tests(_OperationsTestCase):
    """R6：取消必须进入真实发布 CAS 的同一句 SQL。"""

    async def test_cancel_durable_before_the_publish_cas_prevents_publication(
        self,
    ) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class Supplier:
            async def background_summary_completion(self, messages, **kwargs):
                return "合成摘要：取消之后不得发布的候选内容。"

        await self._seed_archive(count=40)
        store = SqlGroupSummaryStore(self.session_factory)
        original_publish = store.publish

        async def held_publish(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_publish(*args, **kwargs)

        scheduler = GroupSummaryScheduler(
            llm=Supplier(),
            store=store,
            config_provider=lambda: _summary_config(enabled=True),
            slot_waiter=None,
        )

        with patch.object(
            store, "publish", new=held_publish
        ), patch.object(
            group_summary_module, "GROUP_SUMMARY_SCHEDULER", scheduler
        ), patch.object(
            tasks,
            "group_summary_config",
            lambda _settings: _summary_config(enabled=True),
        ):
            preview = (await (await self._preview("group_summary", {})).json())[
                "preview"
            ]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"])
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)

            cancel_task = asyncio.ensure_future(
                runs.request_cancel(
                    self.session_factory,
                    run_key=run_key,
                    group_id=GROUP_A,
                    wait_seconds=3,
                )
            )
            flagged = False
            for _ in range(200):
                async with self.session_factory() as session:
                    flagged = bool(
                        await session.scalar(
                            select(OperationsRun.cancel_requested).where(
                                OperationsRun.run_key == run_key
                            )
                        )
                    )
                if flagged:
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(flagged, "取消必须被持久化")

            release.set()
            result = await asyncio.wait_for(cancel_task, timeout=10)
            published = await store.load(GROUP_A)

        self.assertIsNone(published, "取消先赢时不得发布新版本")
        self.assertTrue(result["worker_finished"])
        self.assertIn(
            result["status"],
            (RUN_STATUS_CANCELLED, RUN_STATUS_PARTIAL, RUN_STATUS_UNKNOWN),
            result,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
