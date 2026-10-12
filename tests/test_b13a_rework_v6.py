"""B13a 返工 v6：**持久取消**先于最终副作用准入时的分类（取消 ≠ 权限错误）。

``request_cancel`` 有两个真源：先 ORM 置 ``run.cancel_requested = True`` 并真的
``commit`` 落库，**commit 返回之后**才 ``_CANCEL_EVENTS[run_id].set()``。守卫谓词
（``guard_predicate``）读的是数据库里的 ``cancel_requested``，也读内存事件；
于是"已落库、事件还没置位"的窗口里，真实写边界/出站边界会按 SQL 谓词拒绝，
而只看内存事件的适配器把这次拒绝误报成"授权被撤 / 配置变更 / 普通发送失败"。

父独立验收的 v16 探针正好插在这个窗口：它 patch ``SQLiteSafeAsyncSession.commit``，
在**取消的真实 commit 之后**hold，用独立连接证明 ``cancel_requested=true`` 已落库、
``_CANCEL_EVENTS`` 还没 set，然后放行第一条真实出站、等 worker 收尾。旧实现第二项
item 记 ``failed``/``guard_blocked``（而且还留下 retry 记账），父据此判 B13a 未接受。

本文件的用例**不需要 sleep 抢时序**：窗口由"取消 commit 之后、事件 set 之前"的真实
hold 确定性地建立（第 1 例），或由"守卫已读过、真正的积分 DML 还没抢到 writer 锁"
的真实窗口建立（第 3 例）。修正后未执行项必须如实归为 ``cancelled``/
``cancel_requested``，已发生的副作用不回退，撤权/配置漂移仍然只能是
``guard_blocked``（第 2 例控制项）。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from sqlalchemy import delete, select, update

import bot.services.activity as activity
import bot.services.operations_runs as runs
import bot.services.operations_tasks as tasks
from bot.db.models import (
    Admin,
    MemberPointAward,
    OperationsRun,
    ScheduledMessageOccurrence,
)
from bot.db.sqlite_session import SQLiteSafeAsyncSession
from bot.services.activity import last_complete_week
from bot.services.operations_runs import (
    ITEM_STATUS_CANCELLED,
    ITEM_STATUS_FAILED,
    ITEM_STATUS_SUCCEEDED,
    ITEM_STATUS_UNKNOWN,
    REASON_CANCEL_REQUESTED,
    REASON_GUARD_BLOCKED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_PARTIAL,
    TERMINAL_RUN_STATUSES,
)
from bot.utils.timezone import now_shanghai_naive
from tests.test_b13a_operations_center import (
    ADMIN_ID,
    GROUP_A,
    _OperationsTestCase,
)
from tests.test_permanent_memory_create_dedup import GROUP_B


class CancelBoundaryV6Tests(_OperationsTestCase):
    """持久取消 vs 撤权的真实分类（不靠 sleep、不靠 adapter 入口注入）。"""

    def _statuses(self, run: dict) -> dict[str, dict]:
        return {str(item["item_key"]): dict(item) for item in run["items"]}

    async def _confirm_two_scheduled(self) -> tuple[int, int, str, str, str]:
        """seed 两条定时消息 → 预览 → 确认；返回 (entry_a, entry_b, key_a, key_b, run_key)。

        发起人固定为 ``ADMIN_ID``（普通管理员），这样第 2 例的撤权才真的落在守卫
        的 actor 条件上。
        """

        entry_a = await self._seed_scheduled()
        entry_b = await self._seed_scheduled()
        key_a = await self._scheduled_item_key(entry_a)
        key_b = await self._scheduled_item_key(entry_b)
        preview = await self._preview(
            "scheduled_messages", {"entry_ids": [entry_a, entry_b]}, user=ADMIN_ID
        )
        self.assertEqual(preview.status, 200, await preview.text())
        token = (await preview.json())["preview"]["token"]
        confirmed = await self._confirm(token, user=ADMIN_ID)
        self.assertEqual(confirmed.status, 200, await confirmed.text())
        run_key = (await confirmed.json())["run"]["run_key"]
        return entry_a, entry_b, key_a, key_b, str(run_key)

    def _slow_first_send(self):
        """第一条真实出站停在闸门里，让"取消"有机会赢在第二条之前。"""

        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow_send(chat_id: int, text: str, **kwargs):
            if not started.is_set():
                started.set()
                await gate.wait()
            return await self._record_send(chat_id, text, **kwargs)

        self.bot.send_message.side_effect = slow_send
        return gate, started

    # ---- 第 1 例：真实 commit → 内存事件之间的窗口（基线业务红） ----

    async def test_durable_cancel_before_memory_event_is_reported_as_cancelled(
        self,
    ) -> None:
        gate, started = self._slow_first_send()
        entry_a, entry_b, key_a, key_b, run_key = await self._confirm_two_scheduled()
        await asyncio.wait_for(started.wait(), timeout=5)

        cancel_committed = asyncio.Event()
        commit_release = asyncio.Event()
        obs: dict[str, Any] = {}
        original_commit = SQLiteSafeAsyncSession.commit

        worker_held = asyncio.Event()
        worker_release = asyncio.Event()

        async def held_commit(session) -> None:
            dirty = [
                obj
                for obj in session.sync_session.dirty
                if isinstance(obj, OperationsRun)
                and bool(getattr(obj, "cancel_requested", False))
            ]
            await original_commit(session)
            if not dirty:
                return
            run_id = int(dirty[0].id)
            if runs._ACTIVE_RUNS.get(run_id) is asyncio.current_task():
                # worker 自己的收尾 commit（``_persist_result`` 里把逐项结论与终态
                # 一起落库的那一次）。逐项结论此刻已经算完了——它是在"内存事件还
                # 没置位"的状态下算出来的——而 worker 还没有结束，所以取消请求看到
                # 的 ``worker_finished`` 才是有意义的 True。中间的进度提交（比如刚
                # 拿到 run 时的 running）不是收尾，直接放行。
                if obs.get("worker_held"):
                    return
                if str(getattr(dirty[0], "status", "")) not in TERMINAL_RUN_STATUSES:
                    return
                obs["worker_held"] = True
                worker_held.set()
                await worker_release.wait()
                return
            if obs.get("cancel_held"):
                return
            obs["cancel_held"] = True
            obs["held_run_id"] = run_id
            async with self.session_factory() as probe:
                obs["durable_cancel_requested"] = bool(
                    await probe.scalar(
                        select(OperationsRun.cancel_requested).where(
                            OperationsRun.id == run_id
                        )
                    )
                )
            event = runs._CANCEL_EVENTS.get(run_id)
            obs["memory_cancel_event"] = (
                bool(event.is_set()) if event is not None else False
            )
            cancel_committed.set()
            await commit_release.wait()

        with patch.object(SQLiteSafeAsyncSession, "commit", held_commit):
            cancel_task = asyncio.ensure_future(
                self._post(f"/operations/runs/{run_key}/cancel")
            )
            await asyncio.wait_for(cancel_committed.wait(), timeout=5)
            # 窗口就位：取消已经真的落库（独立连接读得到），内存事件还没置位。
            self.assertTrue(obs["durable_cancel_requested"])
            self.assertFalse(obs["memory_cancel_event"])
            gate.set()
            # 第二条 item 的结论已经算完、worker 正卡在持久化那一步。此刻内存事件
            # 仍然没置位（取消请求还卡在真实 commit 之后），所以"未执行"这个结论
            # 只可能来自持久取消，不可能来自内存事件。
            await asyncio.wait_for(worker_held.wait(), timeout=10)
            event = runs._CANCEL_EVENTS[int(obs["held_run_id"])]
            self.assertFalse(event.is_set())
            commit_release.set()
            # 取消请求越过 commit 之后会立刻去 await 这个还没结束的 worker。
            for _ in range(400):
                if event.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(event.is_set(), "取消请求没有越过真实 commit 继续执行")
            worker_release.set()
            settled = await self._wait(run_key)
            self.assertEqual(settled.status, 200, await settled.text())
            run = dict((await settled.json())["run"])
            cancel = await asyncio.wait_for(cancel_task, timeout=10)

        self.assertTrue(obs.get("cancel_held"), "没有进入持久取消与内存事件之间的真实窗口")
        self.assertTrue(obs.get("worker_held"), "worker 没有停在真实持久化提交上")
        statuses = self._statuses(run)
        # 第一条真实出站已经发生，不能被改写成"没发"。
        self.assertIn(
            statuses[key_a]["status"],
            (ITEM_STATUS_SUCCEEDED, ITEM_STATUS_UNKNOWN),
            statuses[key_a],
        )
        self.assertEqual(self.bot.send_message.await_count, 1)
        # 第二条根本没有执行：必须如实归为取消，而不是权限错误/发送失败。
        self.assertEqual(statuses[key_b]["status"], ITEM_STATUS_CANCELLED, statuses[key_b])
        self.assertEqual(
            statuses[key_b]["reason"], REASON_CANCEL_REQUESTED, statuses[key_b]
        )
        self.assertIn("未执行", str(statuses[key_b]["detail"]))
        self.assertEqual(run["status"], RUN_STATUS_PARTIAL, run)
        # 取消没有执行的项也不得留下"补发已排期"的记账：基线走的是出站守卫拒绝
        # 分支，``deliver_entry_occurrence`` 会在那里调 ``_retry_occurrence``，于是
        # 一条"取消"的项反而被排了重试（与"未执行"矛盾）。取消预检命中时根本不
        # 进入投递，因此两个 entry 的 occurrence 都不该有 next_attempt_at/last_error。
        async with self.session_factory() as session:
            occurrences = list(
                (
                    await session.execute(
                        select(ScheduledMessageOccurrence).where(
                            ScheduledMessageOccurrence.scheduled_message_id.in_(
                                [entry_a, entry_b]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        self.assertFalse(
            [
                (row.scheduled_message_id, row.next_attempt_at, row.last_error)
                for row in occurrences
                if row.next_attempt_at is not None or str(row.last_error or "")
            ],
            "取消/已完成的项不得留下补发排期记账",
        )
        self.assertEqual(cancel.status, 200, await cancel.text())
        payload = (await cancel.json())["run"]
        self.assertTrue(payload["cancel_applied"], payload)
        self.assertTrue(payload["worker_finished"], payload)
        # 写锁已归还：独立 writer 立刻能完成。
        async with self.session_factory() as session:
            session.add(Admin(user_id=1234, group_id=GROUP_B))
            await session.commit()

    # ---- 第 2 例：同一个窗口里撤权必须仍然是 guard_blocked（控制项） ----

    async def test_revocation_in_the_same_window_still_reports_guard_blocked(
        self,
    ) -> None:
        gate, started = self._slow_first_send()
        _a, _b, key_a, key_b, run_key = await self._confirm_two_scheduled()
        await asyncio.wait_for(started.wait(), timeout=5)

        # 独立 session 先 commit 撤销发起人的管理员身份（真实先赢）。
        async with self.session_factory() as session:
            await session.execute(
                delete(Admin).where(
                    Admin.user_id == ADMIN_ID, Admin.group_id == GROUP_A
                )
            )
            await session.commit()

        gate.set()
        settled = await self._wait(run_key)
        self.assertEqual(settled.status, 200, await settled.text())
        run = dict((await settled.json())["run"])

        statuses = self._statuses(run)
        self.assertIn(
            statuses[key_a]["status"],
            (ITEM_STATUS_SUCCEEDED, ITEM_STATUS_UNKNOWN),
            statuses[key_a],
        )
        self.assertEqual(self.bot.send_message.await_count, 1)
        # 撤权不是取消：这里必须保持权限错误，不能被 v6 的重分类吞掉。
        self.assertEqual(statuses[key_b]["status"], ITEM_STATUS_FAILED, statuses[key_b])
        self.assertEqual(statuses[key_b]["reason"], REASON_GUARD_BLOCKED, statuses[key_b])
        self.assertEqual(run["status"], RUN_STATUS_PARTIAL, run)

    # ---- 第 3 例：积分 DML 的真实 writer 准入前持久取消（基线业务红） ----

    async def test_durable_cancel_before_points_write_admission_awards_nothing(
        self,
    ) -> None:
        week_start, _week_end = last_complete_week(now_shanghai_naive())
        week = activity.week_key(week_start)
        await self._seed_activity(
            week_start=week_start, user_id=7, days=5, messages=20, replies=3
        )

        entered = asyncio.Event()
        release = asyncio.Event()
        obs: dict[str, Any] = {}
        real_write_award = activity._write_award
        original_acquire = SQLiteSafeAsyncSession._acquire_write_lock

        async def marked(session, **kwargs):
            session.info["v6_award_admission"] = True
            return await real_write_award(session, **kwargs)

        async def held_lock(self, *, op: str) -> None:
            if self.info.get("v6_award_admission") and not obs.get("held"):
                obs["held"] = True
                obs["op"] = op
                obs["lock_already_held"] = bool(self._sqlite_write_lock_held)
                entered.set()
                await release.wait()
            return await original_acquire(self, op=op)

        with patch.object(activity, "_write_award", marked), patch.object(
            SQLiteSafeAsyncSession, "_acquire_write_lock", held_lock
        ):
            preview = await self._preview("activity_award", {}, user=ADMIN_ID)
            self.assertEqual(preview.status, 200, await preview.text())
            token = (await preview.json())["preview"]["token"]
            confirmed = await self._confirm(token, user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = str((await confirmed.json())["run"]["run_key"])

            await asyncio.wait_for(entered.wait(), timeout=5)
            # 窗口就位：守卫已经读过（当时还没取消），真正的积分 DML 还没抢到
            # writer 锁。现在让持久取消落库，再放行那条 DML。
            async with self.session_factory() as session:
                await session.execute(
                    update(OperationsRun)
                    .where(OperationsRun.run_key == run_key)
                    .values(cancel_requested=True)
                )
                await session.commit()
            release.set()
            settled = await self._wait(run_key)
            self.assertEqual(settled.status, 200, await settled.text())
            run = dict((await settled.json())["run"])

        self.assertTrue(obs.get("held"), "没有进入积分写边界的真实 writer 准入窗口")
        self.assertFalse(obs.get("lock_already_held"))
        # 一行积分都没有：不允许部分发分，也不允许把没发说成发了。
        self.assertEqual(await self._count(MemberPointAward), 0)
        self.assertEqual(run["status"], RUN_STATUS_CANCELLED, run)
        statuses = self._statuses(run)
        self.assertTrue(statuses, run)
        for item in statuses.values():
            self.assertEqual(item["status"], ITEM_STATUS_CANCELLED, item)
            self.assertEqual(item["reason"], REASON_CANCEL_REQUESTED, item)
        # item 身份仍然是"这个群这一周这一位成员"的业务键，没有被换成别的目标。
        self.assertEqual(
            set(statuses), {tasks._activity_item_key(week, 7, 25)}, statuses
        )
