"""B13a 返工 v4：积分 DML 的**真实 writer 准入**（R2 最后一段窗口）。

返工 v3 已经把守卫视编进摘要发布 CAS 与出站 claim 的同一条 SQL 里，但积分写入当时
仍然是这样：先在 ``settle_weekly_activity`` 里做一次 ``verify_guard`` 的 **SELECT**，
然后再去 ``INSERT``。那个 SELECT **不持有 SQLite writer 锁**，而 ``INSERT`` 自己的
``session.execute`` 还要先等锁——父独立验收的 v13 探针就精确地插在这个等待窗口里：
它在 ``SQLiteSafeAsyncSession._acquire_write_lock`` 入口（``_write_award`` 触发的
那一次，``lock_already_held=false``）hold 住，用一条独立事务 commit 撤销/停群/取消/
改配置，然后才放行真正的 INSERT。旧实现照发不误（``award_rows=1``、run ``succeeded``）。

本文件的用例复刻同一个窗口：**hold 在真实 DML 的 writer 准入之前**（不是
``adapter.run`` 起点、也不是 ``settle_weekly_activity`` 入口），独立 writer 必须真的
commit 先赢。修正后守卫谓词就在那条 ``INSERT ... SELECT ... WHERE`` 里，抢到写锁之后
才求值，于是四类撤销全部一行不写；撤权/停群/配置漂移记为 ``failed``/``guard_blocked``。

**取消一例的分类在返工 v6 之后是 ``cancelled``/``cancel_requested``**：``request_cancel``
先真的把 ``cancel_requested`` 提交落库、之后才置内存事件，所以"已落库、事件未置位"的
窗口里守卫按 SQL 谓词拒绝；v6 起适配器会用持久真源把这种拒绝如实归为取消（不是权限
错误），本文件相应只更新这一例的期望值——业务结论（一行积分都没写、没有任何 succeeded
的项、取消不得被当成发过）保持不放宽。

同时保留正控制（没撤销就照常发分、``created_at`` 落库正确）、legacy 控制
（``guard=None`` 的自动路径行为不变）与"多行循环不得部分发分"的契约。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy import delete, select, update

import bot.services.activity as activity
import bot.services.operations_runs as runs
import bot.services.operations_tasks as tasks
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    MemberPointAward,
    OperationsRun,
    RuntimeConfigRecord,
)
from bot.db.sqlite_session import SQLiteSafeAsyncSession
from bot.services.activity import award_ref, last_complete_week, settle_weekly_activity
from bot.services.operations_guard import PersistedGuard
from bot.services.operations_runs import (
    ITEM_STATUS_CANCELLED,
    ITEM_STATUS_FAILED,
    ITEM_STATUS_SUCCEEDED,
    REASON_CANCEL_REQUESTED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_SUCCEEDED,
)
from bot.utils.timezone import now_shanghai_naive
from tests.test_b13a_operations_center import (
    ADMIN_ID,
    GROUP_A,
    _OperationsTestCase,
)


class PointsWriteAdmissionV4Tests(_OperationsTestCase):
    """hold 真实积分 DML 的 writer 准入，独立 writer 先 commit。"""

    async def _revoke(self, kind: str, run_key: str) -> None:
        """用一条独立 session（自己抢 writer 锁并 commit）让撤销先赢。"""

        async with self.session_factory() as session:
            if kind == "actor":
                await session.execute(
                    delete(Admin).where(
                        Admin.user_id == ADMIN_ID, Admin.group_id == GROUP_A
                    )
                )
            elif kind == "group":
                await session.execute(
                    update(AuthorizedGroup)
                    .where(AuthorizedGroup.group_id == GROUP_A)
                    .values(bot_present=False)
                )
            elif kind == "cancel":
                await session.execute(
                    update(OperationsRun)
                    .where(OperationsRun.run_key == run_key)
                    .values(cancel_requested=True)
                )
            elif kind == "config":
                await session.execute(
                    update(RuntimeConfigRecord)
                    .where(RuntimeConfigRecord.id == 1)
                    .values(revision=RuntimeConfigRecord.revision + 1)
                )
            else:  # pragma: no cover - 用例只传上面四种
                raise AssertionError(f"unknown revocation kind: {kind}")
            await session.commit()

    async def _admission(self, *, revoke: str = "") -> tuple[dict, list[dict], dict]:
        """跑一次真实的 ``activity_award`` run，卡在积分 INSERT 的写锁准入之前。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        entered = asyncio.Event()
        release = asyncio.Event()
        obs: dict = {}
        original_award = activity._write_award
        original_lock = SQLiteSafeAsyncSession._acquire_write_lock

        async def marked(session, *args, **kwargs):
            session.info["v4_award_admission"] = True
            return await original_award(session, *args, **kwargs)

        async def held_lock(session, *, op):
            if session.info.get("v4_award_admission") and not obs.get("entered"):
                obs["entered"] = True
                obs["lock_already_held"] = bool(session._sqlite_write_lock_held)
                obs["op"] = op
                entered.set()
                await release.wait()
            return await original_lock(session, op=op)

        with patch.object(activity, "_write_award", new=marked), patch.object(
            SQLiteSafeAsyncSession, "_acquire_write_lock", new=held_lock
        ):
            preview = (
                await (await self._preview("activity_award", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)
            # hold 期间：另一条 session 真的拿到 writer 锁并 commit 了撤销。
            if revoke:
                await self._revoke(revoke, run_key)
                obs["revocation_committed_before_real_lock"] = True
            release.set()
            settled = await runs.await_run(
                self.session_factory,
                run_key=run_key,
                group_id=GROUP_A,
                timeout_seconds=5,
            )

        self.assertTrue(obs.get("entered"), "必须卡在真实积分写的写锁准入之前")
        self.assertFalse(
            obs.get("lock_already_held"),
            "hold 点必须在真正抢到 writer 锁之前，否则撤销不可能先赢",
        )
        return settled, list(settled.get("items") or []), obs

    def _expected_ref(self) -> str:
        week_start, _ = last_complete_week(now_shanghai_naive())
        return award_ref(activity.week_key(week_start), 7)

    def _expected_item_key(self) -> str:
        """活动 item 身份 = ``activity:{周}:{用户}:{确认当时的点数}``。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        return tasks._activity_item_key(activity.week_key(week_start), 7, 25)

    def _assert_blocked(
        self, settled: dict, items: list[dict], *, cancel: bool = False
    ) -> None:
        self.assertTrue(items, "逐项结果必须如实记录")
        for item in items:
            self.assertEqual(item["item_key"], self._expected_item_key(), item)
            if cancel:
                # 返工 v6：持久取消赢在写边界之前时，未执行的项必须归为取消，
                # 不能包装成"权限错误"。（可重新确认，但不是自动补发。）
                self.assertEqual(item["status"], ITEM_STATUS_CANCELLED, item)
                self.assertEqual(item["reason"], REASON_CANCEL_REQUESTED, item)
                self.assertTrue(item["resendable"], item)
            else:
                self.assertEqual(item["status"], ITEM_STATUS_FAILED, item)
                self.assertEqual(item["reason"], "guard_blocked", item)
                self.assertFalse(item["resendable"], "复核不通过不得自动重发")
        self.assertIn(
            settled["status"], (RUN_STATUS_FAILED, RUN_STATUS_CANCELLED), settled
        )

    async def _award_rows(self) -> list[MemberPointAward]:
        async with self.session_factory() as session:
            return list((await session.execute(select(MemberPointAward))).scalars().all())

    async def test_actor_revoked_before_the_real_write_admission_awards_nothing(self) -> None:
        settled, items, _ = await self._admission(revoke="actor")
        self.assertEqual(await self._count(MemberPointAward), 0, "撤权后不得写积分")
        self.assertEqual(settled["status"], RUN_STATUS_FAILED, settled)
        self.assertEqual(settled["reason"], "guard_blocked", settled)
        self._assert_blocked(settled, items)

    async def test_group_disabled_before_the_real_write_admission_awards_nothing(self) -> None:
        settled, items, _ = await self._admission(revoke="group")
        self.assertEqual(await self._count(MemberPointAward), 0, "停群后不得写积分")
        self.assertEqual(settled["status"], RUN_STATUS_FAILED, settled)
        self.assertEqual(settled["reason"], "guard_blocked", settled)
        self._assert_blocked(settled, items)

    async def test_cancel_before_the_real_write_admission_awards_nothing(self) -> None:
        settled, items, _ = await self._admission(revoke="cancel")
        self.assertEqual(await self._count(MemberPointAward), 0, "取消后不得写积分")
        # 返工 v6 起：持久取消赢在写边界之前 ⇒ 未执行项如实归为取消，run 终态是
        # ``cancelled``/``cancel_requested``。业务结论不变：一行积分都没写。
        self.assertEqual(settled["status"], RUN_STATUS_CANCELLED, settled)
        self.assertEqual(settled["reason"], REASON_CANCEL_REQUESTED, settled)
        self._assert_blocked(settled, items, cancel=True)

    async def test_config_revision_before_the_real_write_admission_awards_nothing(self) -> None:
        settled, items, _ = await self._admission(revoke="config")
        self.assertEqual(await self._count(MemberPointAward), 0, "配置变更后不得写积分")
        self.assertEqual(settled["status"], RUN_STATUS_FAILED, settled)
        self.assertEqual(settled["reason"], "guard_blocked", settled)
        self._assert_blocked(settled, items)

    async def test_normal_write_admission_still_awards(self) -> None:
        """正控制：没人撤销时照常发分，且这条 INSERT…SELECT 的字段落库正确。"""

        settled, items, _ = await self._admission()
        self.assertEqual(settled["status"], RUN_STATUS_SUCCEEDED, settled)
        rows = await self._award_rows()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row.group_id, GROUP_A)
        self.assertEqual(row.user_id, 7)
        self.assertEqual(row.points, 25)
        self.assertEqual(row.ref, self._expected_ref())
        self.assertEqual(row.reason, activity.AWARD_REASON_WEEKLY_ACTIVITY)
        self.assertIsNotNone(row.created_at, "created_at 必须真的落库（榜单按它分周）")
        for item in items:
            self.assertEqual(item["status"], ITEM_STATUS_SUCCEEDED, item)

    async def test_legacy_path_without_run_context_keeps_previous_behaviour(self) -> None:
        """``guard=None``（自动/命令行路径）不因为本批改动而改变既有行为。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        await self._revoke("actor", run_key="")
        async with self.session_factory() as session:
            result = await settle_weekly_activity(
                session, group_id=GROUP_A, week_start=week_start
            )
            await session.commit()
        self.assertEqual(result.award_rejected, "")
        self.assertEqual(result.awarded_points, 25)
        self.assertEqual(await self._count(MemberPointAward), 1)

    async def test_partial_loop_write_is_reported_as_nothing(self) -> None:
        """多行循环里后一行被守卫挡住时，返回值必须是"一个字都没写"。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        await self._seed_activity(week_start=week_start, user_id=8)
        real = activity._write_award
        calls = {"n": 0}

        async def flaky(session, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                # 模拟"这一行的复核没过"：一行都没插进去。
                return False
            return await real(session, **kwargs)

        # 真守卫（此刻一切都合法），所以第一行会真的写进去；第二行返回 False 时
        # ``settle`` 必须自己发现"这一行没进库且守卫在"→ 判定为复核不通过。
        async with self.session_factory() as probe:
            revision = int(
                await probe.scalar(
                    select(RuntimeConfigRecord.revision).where(
                        RuntimeConfigRecord.id == 1
                    )
                )
                or 0
            )
        guard = PersistedGuard(
            group_id=GROUP_A,
            actor_user_id=ADMIN_ID,
            super_admin_id=int(self.settings.super_admin_id),
            guard_revision=revision,
            run_key="v4-partial-loop",
        )
        with patch.object(activity, "_write_award", new=flaky):
            async with self.session_factory() as session:
                result = await settle_weekly_activity(
                    session,
                    group_id=GROUP_A,
                    week_start=week_start,
                    guard=guard,
                )
                # 调用方的契约：见到 award_rejected 必须 rollback。
                await session.rollback()

        self.assertEqual(result.award_rejected, "guard_blocked")
        self.assertEqual(calls["n"], 2, "第二行必须在真实写入处就被挡住")
        self.assertEqual(result.awarded_points, 0, "不得出现部分发分")
        self.assertTrue(
            all(entry.awarded is False for entry in result.entries), result.entries
        )
        self.assertEqual(await self._count(MemberPointAward), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
