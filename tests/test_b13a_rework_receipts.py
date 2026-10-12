"""B13a 返工 v1：R1–R6 的正式回归（真实 ``init_db`` + 签名 HTTP + 真实业务服务）。

每个用例对应父独立验收探针 v1/v2/v3 实际复现过的一个机制（见交付报告）：

* R1 共享稳定业务 receipt：旧 runner/CLI/reaper 不能重发已经出站过的目标。
* R2 执行边界复核：确认之后撤权威/改目标，绝不再出站。
* R3 对账只用准确 operation key 的 durable receipt，不拿"行被删/水位"当证据。
* R4 周报锚"上一完整周"，渲染失败不得先提交积分副作用。
* R5 商店到期按准确目标+版本处理，未知通知永不解锁重发。
* R6 取消必须传到真实 worker 的发布 CAS 之前。

只替换最外层 transport（``bot.send_message`` 等）与数据库故障注入；权限、占位、
receipt、对账、发布都走真实实现，不 mock service/run 来"证明"业务。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select, update

import bot.services.group_summary as group_summary_module
import bot.services.operations_runs as runs
import bot.services.operations_tasks as tasks
from bot.services import checkin_reminder as checkin, point_shop
from bot.db.models import (
    AuthorizedGroup,
    CheckinReminderPost,
    MemberEntitlement,
    MemberPointAward,
    OperationsRun,
    ScheduledMessage,
    ScheduledMessageOccurrence,
)
from bot.db.sqlite_session import SQLiteSafeAsyncSession
from bot.services.group_summary import GroupSummaryScheduler, SqlGroupSummaryStore
from bot.services.operations_runs import (
    ITEM_STATUS_SUCCEEDED,
    ITEM_STATUS_UNKNOWN,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_UNKNOWN,
)
from bot.services.scheduled_messages import ScheduledMessageService
from bot.utils.timezone import now_shanghai_naive
from tests.test_b13a_operations_center import (
    GROUP_A,
    _OperationsTestCase,
    _summary_config,
)


class ReceiptReplayRegressionTests(_OperationsTestCase):
    """R1：稳定业务 receipt 阻断所有历史恢复入口的重发。"""

    async def test_legacy_reaper_cannot_replay_a_receipted_checkin(self) -> None:
        """签到：发送成功但回填故障 → 占位超期也不能被 reaper 回收后再发一次。"""

        with patch.object(tasks, "reminder_slots", lambda: (0,)), patch.object(
            checkin,
            "mark_reminder_sent",
            new=AsyncMock(side_effect=RuntimeError("synthetic mark fault")),
        ):
            run = await self._run_once("checkin_reminder", {"slot": 0})

        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        self.assertEqual(run["items"][0]["status"], ITEM_STATUS_UNKNOWN, run)
        self.assertFalse(run["items"][0]["resendable"])
        self.assertEqual(len(self.outbound), 1)

        async with self.session_factory() as session:
            await session.execute(
                update(CheckinReminderPost).values(
                    created_at=now_shanghai_naive() - timedelta(days=1)
                )
            )
            await session.commit()
            released = await checkin.reap_stale_reminder_slots(
                session, grace_seconds=900
            )
            outcome = await checkin.send_checkin_slot(
                session,
                self.bot,
                group_id=GROUP_A,
                slot=0,
                auto_delete_seconds=600,
                queue_auto_delete=AsyncMock(return_value=True),
                backfill_auto_delete=AsyncMock(return_value=True),
                session_factory=self.session_factory,
            )

        self.assertEqual(len(self.outbound), 1, "有出站证据的时段绝不能被重发")
        self.assertEqual(outcome.status, checkin.CHECKIN_OUTCOME_ALREADY_DELIVERED)
        # 有出站证据时占位必须被保留（不能靠删占位把风险交给"下次补发"）。
        async with self.session_factory() as session:
            kept = (
                await session.scalars(
                    select(CheckinReminderPost).where(
                        CheckinReminderPost.group_id == GROUP_A
                    )
                )
            ).all()
        self.assertEqual(len(kept), 1)
        del released

    async def test_expired_lease_never_resends_a_receipted_occurrence(self) -> None:
        """定时消息：send 成功但完成回填故障 → 租约过期后常驻 runner 不得再发。"""

        entry_id = await self._seed_scheduled()

        with patch.object(
            ScheduledMessageService,
            "_complete_occurrence",
            new=AsyncMock(return_value=False),
        ):
            run = await self._run_once("scheduled_messages", {"entry_ids": [entry_id]})

        self.assertEqual(len(self.outbound), 1)
        self.assertEqual(run["items"][0]["status"], ITEM_STATUS_UNKNOWN, run)
        self.assertEqual(int(run["items"][0]["external_message_id"]), 1001)
        self.assertFalse(run["items"][0]["resendable"])

        # 把租约全部改成过期：这正是旧实现"再抢一次 → 再发一次"的路径。
        async with self.session_factory() as session:
            await session.execute(
                update(ScheduledMessageOccurrence).values(
                    lease_until=now_shanghai_naive() - timedelta(minutes=5)
                )
            )
            await session.commit()

        service = ScheduledMessageService(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
        )
        await service.run_once()

        self.assertEqual(len(self.outbound), 1, "已出站的 occurrence 绝不能被重发")

    async def test_unknown_weekly_transport_blocks_a_fresh_preview(self) -> None:
        """周报：transport 超时（可能已送达）→ 换新令牌再确认也不能重发。"""

        await self._seed_archive(count=5)

        async def unknown(chat_id, text, **kwargs):
            await self._record_send(chat_id, text, **kwargs)
            raise TimeoutError("synthetic lost transport acknowledgement")

        self.bot.send_message.side_effect = unknown
        run = await self._run_once("weekly_report", {})
        self.bot.send_message.side_effect = self._record_send

        self.assertEqual(len(self.outbound), 1)
        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        self.assertFalse(run["items"][0]["resendable"])

        preview = (await (await self._preview("weekly_report", {})).json())["preview"]
        self.assertNotEqual(preview["status"], "ok", preview)
        self.assertIn(preview["status"], ("blocked", "no_work"))
        self.assertFalse(preview.get("token"))
        self.assertEqual(len(self.outbound), 1, "未知出站结果不能被新预览洗掉")

    async def test_confirmation_is_bound_to_the_selected_targets_only(self) -> None:
        """令牌绑定选中目标快照：同群另一条未选中的到期消息不能被顺带发出。"""

        entry_a = await self._seed_scheduled()
        entry_b = await self._seed_scheduled()

        preview = (await (await self._preview(
            "scheduled_messages", {"entry_ids": [entry_a]}
        )).json())["preview"]
        self.assertEqual(preview["status"], "ok", preview)
        keys = [item["item_key"] for item in preview["targets"]]
        self.assertEqual(len(keys), 1)

        confirmed = await self._confirm(preview["token"])
        run_key = (await confirmed.json())["run"]["run_key"]
        run = (await (await self._wait(run_key)).json())["run"]

        self.assertEqual([item["item_key"] for item in run["items"]], keys)
        self.assertEqual(len(self.outbound), 1)

        async with self.session_factory() as session:
            other = await session.get(ScheduledMessage, int(entry_b))
        self.assertIsNone(other.last_run_at, "未绑定的目标绝不能被顺带发送")
        # 契约（新字段）：确认时必须把选中目标快照持久化，执行/重试都只认它。
        # 旧实现根本没有这个字段（属于"字段缺失"红，不是业务副作用红）。
        self.assertEqual(sorted(run["target_keys"]), sorted(keys))

    async def test_concurrent_retry_is_accepted_exactly_once(self) -> None:
        """重试标识必须由上次 run 决定：并发重试只能产生一个 run。"""

        await self._seed_archive(count=5)

        with patch.object(
            tasks,
            "render_group_quality",
            new=AsyncMock(side_effect=RuntimeError("synthetic render fault")),
        ):
            first = await self._run_once("weekly_report", {"days": 7})

        self.assertEqual(first["status"], RUN_STATUS_FAILED, first)
        self.assertTrue(first["items"][0]["resendable"])

        responses = await asyncio.gather(
            self._post(f"/operations/runs/{first['run_key']}/retry"),
            self._post(f"/operations/runs/{first['run_key']}/retry"),
        )
        statuses = sorted(int(response.status) for response in responses)
        self.assertEqual(statuses, [200, 409], statuses)
        accepted = [response for response in responses if int(response.status) == 200]
        run = (await accepted[0].json())["run"]
        self.assertNotEqual(run["run_key"], first["run_key"])
        await self._wait(run["run_key"])

        rows = await self._runs()
        self.assertEqual(len(rows), 2, "并发重试不能各自生成一个重复 run")


class ExecutionGuardRegressionTests(_OperationsTestCase):
    """R2：真实 runner 的执行边界必须重新复核授权与目标。"""

    async def test_deauthorization_before_execution_sends_nothing(self) -> None:
        adapter = runs.get_task("checkin_reminder")
        original = adapter.run
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held(context):
            entered.set()
            await release.wait()
            return await original(context)

        with patch.object(tasks, "reminder_slots", lambda: (0,)), patch.object(
            adapter, "run", new=held
        ):
            preview = (await (await self._preview(
                "checkin_reminder", {"slot": 0}
            )).json())["preview"]
            confirmed = await self._confirm(preview["token"])
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)
            # 确认已经成功、run 已经在途：这时候把本群从授权列表摘掉。
            async with self.session_factory() as session:
                await session.execute(
                    delete(AuthorizedGroup).where(
                        AuthorizedGroup.group_id == GROUP_A
                    )
                )
                await session.commit()
            release.set()
            run = await runs.await_run(
                self.session_factory,
                run_key=run_key,
                group_id=GROUP_A,
                timeout_seconds=5,
            )

        self.assertEqual(len(self.outbound), 0, "撤权之后绝不能再出站")
        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        for item in run["items"]:
            self.assertNotEqual(item["status"], ITEM_STATUS_SUCCEEDED, item)
            self.assertFalse(item["resendable"], item)


class ReconcileEvidenceRegressionTests(_OperationsTestCase):
    """R3：对账不得把"没有证据"当成"没有副作用"，也不得清零已知证据。"""

    async def test_deleted_entry_never_becomes_proof_of_unsent(self) -> None:
        entry_id = await self._seed_scheduled()

        with patch.object(
            ScheduledMessageService,
            "_complete_occurrence",
            new=AsyncMock(return_value=False),
        ):
            run = await self._run_once("scheduled_messages", {"entry_ids": [entry_id]})

        before = run["items"][0]
        self.assertEqual(int(before["external_message_id"]), 1001)

        async with self.session_factory() as session:
            await session.execute(
                delete(ScheduledMessage).where(ScheduledMessage.id == int(entry_id))
            )
            await session.commit()

        reconciled = await self._post(f"/operations/runs/{run['run_key']}/reconcile")
        self.assertEqual(reconciled.status, 200, await reconciled.text())
        after = (await reconciled.json())["run"]["items"][0]

        self.assertEqual(after["status"], ITEM_STATUS_UNKNOWN, after)
        self.assertFalse(after["resendable"], after)
        self.assertEqual(
            int(after["external_message_id"]), 1001, "已知的出站证据不得被清零"
        )

        # 未知 + 不可安全重发 → 重试必须被拒，不能借重试绕过证据。
        retry = await self._post(f"/operations/runs/{run['run_key']}/retry")
        self.assertEqual(retry.status, 409)

class WeeklyScopeAndEffectsRegressionTests(_OperationsTestCase):
    """R4：周报锚上一完整周，渲染失败不得留下未披露的积分副作用。"""

    async def test_weekly_uses_last_complete_week_and_awards_nothing_on_render_fault(
        self,
    ) -> None:
        last_start, last_end = tasks.last_complete_week()
        await self._seed_activity(week_start=last_start)

        preview = (await (await self._preview("weekly_report", {})).json())["preview"]
        self.assertEqual(preview["status"], "ok", preview)
        self.assertIn(last_start.isoformat(), str(preview["targets"]))
        self.assertIn(last_start.isoformat(), str(preview["targets"][0]["target"]))
        self.assertIn(last_end.isoformat(), str(preview["targets"][0]["target"]))
        self.assertEqual(
            preview["targets"][0]["target"]["week_key"],
            tasks.activity_week_key(last_start),
        )
        self.assertGreater(int(preview["extra"]["award_pending_points"]), 0, preview)

        before = await self._count(MemberPointAward)
        with patch.object(
            tasks,
            "render_group_quality",
            new=AsyncMock(side_effect=RuntimeError("synthetic render fault")),
        ):
            confirmed = await self._confirm(preview["token"])
            run_key = (await confirmed.json())["run"]["run_key"]
            run = (await (await self._wait(run_key)).json())["run"]

        after = await self._count(MemberPointAward)
        self.assertEqual(after, before, "渲染失败时不得留下未披露的积分副作用")
        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertTrue(run["items"][0]["resendable"])
        self.assertEqual(len(self.outbound), 0)


class ShopExpiryEvidenceRegressionTests(_OperationsTestCase):
    """R5：商店到期按准确目标+版本处理，未知通知不解锁重发。"""

    async def test_retained_business_row_never_becomes_a_failed_verdict(self) -> None:
        """撤销已经真实发生、但落库确认故障：行还在 ≠ 没通知过，不得改判失败。"""

        await self._seed_entitlement(group_id=GROUP_A, user_id=7)

        with patch.object(
            point_shop,
            "_drop_snapshot",
            new=AsyncMock(side_effect=RuntimeError("synthetic DB delete failed")),
        ):
            run = await self._run_once("shop_expire", {})

        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        self.assertFalse(run["items"][0]["resendable"], run)

        reconciled = await self._post(f"/operations/runs/{run['run_key']}/reconcile")
        self.assertEqual(reconciled.status, 200, await reconciled.text())
        after = (await reconciled.json())["run"]["items"][0]
        self.assertEqual(after["status"], ITEM_STATUS_UNKNOWN, after)
        self.assertFalse(after["resendable"], after)

        retry = await self._post(f"/operations/runs/{run['run_key']}/retry")
        self.assertEqual(retry.status, 409)
        self.assertEqual(self.bot.set_chat_member_tag.await_count, 1)

    async def test_confirmed_notification_is_never_resent_after_a_commit_fault(
        self,
    ) -> None:
        await self._seed_entitlement(group_id=GROUP_A, user_id=7)
        original_commit = SQLiteSafeAsyncSession.commit
        state = {"notification": False, "faulted": False}

        async def send_then_flag(chat_id, text, **kwargs):
            message = await self._record_send(chat_id, text, **kwargs)
            state["notification"] = True
            return message

        async def faulty_commit(session):
            # 通知已经真实发出之后的第一次提交失败：这一次提交**整体回滚**
            # （权益行仍在），也就是父探针 F7b 的真实序列。
            if state["notification"] and not state["faulted"]:
                state["faulted"] = True
                raise RuntimeError("synthetic business commit fault after notice")
            await original_commit(session)

        self.bot.send_message.side_effect = send_then_flag
        with patch.object(SQLiteSafeAsyncSession, "commit", new=faulty_commit):
            run = await self._run_once("shop_expire", {})

        self.assertTrue(state["faulted"], "故障必须真的发生在通知之后")
        self.assertEqual(len(self.outbound), 1)

        reconciled = await self._post(f"/operations/runs/{run['run_key']}/reconcile")
        self.assertEqual(reconciled.status, 200, await reconciled.text())
        after = (await reconciled.json())["run"]["items"][0]
        self.assertFalse(after["resendable"], after)

        retry = await self._post(f"/operations/runs/{run['run_key']}/retry")
        if retry.status == 200:
            await self._wait((await retry.json())["run"]["run_key"])
        self.assertEqual(len(self.outbound), 1, "已确认发出的通知绝不能被重试再发")

    async def test_run_only_touches_the_selected_version_of_each_target(self) -> None:
        first = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        second = await self._seed_entitlement(group_id=GROUP_A, user_id=6)

        preview = (await (await self._preview("shop_expire", {})).json())["preview"]
        self.assertEqual(preview["status"], "ok", preview)
        keys = [item["item_key"] for item in preview["targets"]]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2, "每个目标的键必须带自己的版本")

        # 确认之后、执行之前：另一个目标被续期（版本变了）。
        async with self.session_factory() as session:
            row = await session.get(MemberEntitlement, int(second))
            row.expires_at = now_shanghai_naive() + timedelta(days=1)
            await session.commit()

        confirmed = await self._confirm(preview["token"])
        run_key = (await confirmed.json())["run"]["run_key"]
        run = (await (await self._wait(run_key)).json())["run"]
        statuses = {item["item_key"]: item["status"] for item in run["items"]}

        self.assertIn(keys[0], statuses)
        self.assertNotIn(keys[1], statuses, "版本已经变化的行不属于这次确认的目标")
        self.assertEqual(statuses[keys[0]], ITEM_STATUS_SUCCEEDED, run)
        self.assertEqual(self.bot.set_chat_member_tag.await_count, 1)

        async with self.session_factory() as session:
            gone = await session.get(MemberEntitlement, int(first))
            kept = await session.get(MemberEntitlement, int(second))
        self.assertIsNone(gone)
        self.assertIsNotNone(kept)
        del run_key


class SummaryCancellationRegressionTests(_OperationsTestCase):
    """R6：取消必须传到真实 worker 的发布 CAS 之前。"""

    async def test_cancel_before_publication_never_publishes_a_summary(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class Supplier:
            async def background_summary_completion(self, messages, **kwargs):
                entered.set()
                await release.wait()
                return "合成摘要：取消之后不得发布的候选内容。"

        await self._seed_archive(count=40)
        store = SqlGroupSummaryStore(self.session_factory)
        scheduler = GroupSummaryScheduler(
            llm=Supplier(),
            store=store,
            config_provider=lambda: _summary_config(enabled=True),
            slot_waiter=None,
        )

        with patch.object(
            group_summary_module, "GROUP_SUMMARY_SCHEDULER", scheduler
        ), patch.object(
            tasks, "group_summary_config", lambda _settings: _summary_config(enabled=True)
        ):
            preview = (await (await self._preview("group_summary", {})).json())["preview"]
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

        self.assertIsNone(published, "取消之后绝不能再发布新版本")
        self.assertTrue(result["worker_finished"])
        self.assertIn(
            result["status"],
            (RUN_STATUS_CANCELLED, RUN_STATUS_PARTIAL, RUN_STATUS_UNKNOWN),
            result,
        )
        settled = await runs.await_run(
            self.session_factory,
            run_key=run_key,
            group_id=GROUP_A,
            timeout_seconds=5,
        )
        self.assertEqual(settled["items"][0]["status"], "cancelled", settled)
        del scheduler


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
