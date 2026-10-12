"""B13a 返工 v5：周报真实结算只能执行**确认方案**里的 ref/金额（R2 同一段窗口）。

返工 v4 把积分写入的准入谓词编进了那条 ``INSERT ... SELECT ... WHERE``（写边界原子化），
但周报适配器只把 ``expected_points`` 带进写边界：逐项比对只覆盖**确认过的成员**，确认
之后**新达标**的人不在比对范围内，于是被顺带发分；周报正文里的"一共发出 N 分"也跟着
写成含新人的金额。父独立验收的 v14 探针就 hold 在真实周报结算入口
（``tasks.settle_weekly_activity(award=True)``）之前，等确认（只有 user 7 / 25 分）走完
``_weekly_plan`` 与方案摘要之后，用独立事务让 user 8 达标并 commit，然后放行：旧实现
``award_rows=[7:25, 8:12]``、周报照发、run ``succeeded``，item_key 还是单人旧方案。

修正后：确认方案同时是真实写边界上的白名单（``only_refs``），并且结算结果里一旦出现
确认方案**之外**的新达标成员，就整体拒绝并 rollback —— 一个字都不写、也不发一份金额
口径对不上的周报。本文件的用例复刻同一个窗口，hold 在**真实结算入口**（不是 adapter
起点），独立 writer 必须真的 commit 先赢。

控制项：两人方案在确认时就在快照里时照常结算 37 分并发送一次；活动手动发放的
"确认之后才达标 = 未选中"契约不受周报这条严格化影响（``not_selected`` 保持原样）。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy import select

import bot.services.activity as activity
import bot.services.operations_runs as runs
import bot.services.operations_tasks as tasks
from bot.db.models import MemberPointAward
from bot.services.activity import award_ref, last_complete_week
from bot.services.operations_runs import (
    ITEM_STATUS_FAILED,
    ITEM_STATUS_SKIPPED,
    ITEM_STATUS_SUCCEEDED,
    RUN_STATUS_FAILED,
    RUN_STATUS_SUCCEEDED,
)
from bot.utils.timezone import now_shanghai_naive
from tests.test_b13a_operations_center import (
    ADMIN_ID,
    GROUP_A,
    _OperationsTestCase,
)


class WeeklyConfirmedSchemeV5Tests(_OperationsTestCase):
    """hold 真实周报结算入口，确认之后新达标的成员不得被顺带发分。"""

    async def _newcomer(self, week_start) -> None:
        """确认之后才达标的新成员：独立 session 自己抢 writer 锁并 commit。"""

        await self._seed_activity(
            week_start=week_start, user_id=8, days=3, messages=12, replies=1
        )

    def _week(self) -> str:
        week_start, _ = last_complete_week(now_shanghai_naive())
        return activity.week_key(week_start)

    def _hold(self, entered: asyncio.Event, release: asyncio.Event, obs: dict):
        """在**真实周报结算入口**前 hold（award=False 的只读重放直接放行）。"""

        original = tasks.settle_weekly_activity

        async def held(session, *args, **kwargs):
            if kwargs.get("award") and not obs.get("entered"):
                obs["entered"] = True
                obs["only_refs"] = kwargs.get("only_refs")
                obs["expected_points"] = dict(kwargs.get("expected_points") or {})
                entered.set()
                await release.wait()
            return await original(session, *args, **kwargs)

        return patch.object(tasks, "settle_weekly_activity", new=held)

    async def _award_rows(self) -> list[MemberPointAward]:
        async with self.session_factory() as session:
            return list((await session.execute(select(MemberPointAward))).scalars().all())

    async def _weekly_confirmed_run(
        self, *, newcomer: bool
    ) -> tuple[dict, list[dict], dict]:
        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        entered = asyncio.Event()
        release = asyncio.Event()
        obs: dict = {}
        with self._hold(entered, release, obs):
            preview = (
                await (
                    await self._preview("weekly_report", {"days": 7}, user=ADMIN_ID)
                ).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)
            if newcomer:
                # hold 期间：真实结算还没开始，独立事务让 user 8 达标并 commit。
                await self._newcomer(week_start)
                obs["newcomer_committed_before_real_settle"] = True
            release.set()
            settled = await runs.await_run(
                self.session_factory,
                run_key=run_key,
                group_id=GROUP_A,
                timeout_seconds=5,
            )
        self.assertTrue(obs.get("entered"), "必须卡在真实周报结算入口之前")
        return settled, list(settled.get("items") or []), obs

    # ---- 核心：确认之后新达标 → 一个字都不写、也不发口径对不上的周报 ----

    async def test_new_member_qualified_after_confirm_is_not_awarded(self) -> None:
        settled, items, obs = await self._weekly_confirmed_run(newcomer=True)

        # 业务事实先断言：确认方案之外的新成员（user 8 / 12 分）一个字都不许写。
        rows = await self._award_rows()
        self.assertEqual(
            sorted((row.user_id, row.points) for row in rows),
            [],
            f"确认方案外的成员不得被顺带发分：{[(r.user_id, r.points) for r in rows]}",
        )
        self.assertEqual(self.bot.send_message.await_count, 0, "不得发送金额口径不符的周报")
        self.assertEqual([chat for chat, _text in self.outbound], [])
        self.assertEqual(settled["status"], RUN_STATUS_FAILED, settled)
        self.assertEqual(settled["reason"], tasks.REASON_SOURCE_CHANGED, settled)
        self.assertTrue(items, "逐项结果必须如实记录")
        for item in items:
            self.assertEqual(item["status"], ITEM_STATUS_FAILED, item)
            self.assertEqual(item["reason"], tasks.REASON_SOURCE_CHANGED, item)
            self.assertFalse(item["resendable"], "方案漂移不得自动重发")
            self.assertIn("重新预览", item["detail"], item)
        # 机制：确认方案必须真的成为写边界上的白名单。
        self.assertEqual(obs["expected_points"], {award_ref(self._week(), 7): 25}, obs)
        self.assertEqual(
            obs["only_refs"],
            {award_ref(self._week(), 7)},
            "确认方案必须是真实写边界上的白名单",
        )

    # ---- 控制：确认时就在方案里的两人照常结算并发送一次 ----

    async def test_confirmed_two_member_scheme_still_settles_and_sends(self) -> None:
        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        await self._newcomer(week_start)

        run = await self._run_once("weekly_report", {"days": 7})

        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        item = run["items"][0]
        self.assertEqual(item["status"], ITEM_STATUS_SUCCEEDED, item)
        rows = await self._award_rows()
        self.assertEqual(
            sorted((row.user_id, row.points) for row in rows), [(7, 25), (8, 12)], rows
        )
        self.assertIn("37 分", item["detail"], item)
        self.assertEqual(self.bot.send_message.await_count, 1, "确认过的方案照常发一次")

    # ---- 控制：周报的严格化不得泄漏到活动手动发放的"未选中"契约 ----

    async def test_activity_new_target_is_still_reported_as_not_selected(self) -> None:
        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        entered = asyncio.Event()
        release = asyncio.Event()
        obs: dict = {}

        with self._hold(entered, release, obs):
            preview = (
                await (await self._preview("activity_award", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)
            await self._newcomer(week_start)
            release.set()
            settled = await runs.await_run(
                self.session_factory,
                run_key=run_key,
                group_id=GROUP_A,
                timeout_seconds=5,
            )

        self.assertEqual(settled["status"], RUN_STATUS_SUCCEEDED, settled)
        rows = await self._award_rows()
        self.assertEqual(
            sorted((row.user_id, row.points) for row in rows),
            [(7, 25)],
            "确认方案外的成员一个字都不许写",
        )
        by_status: dict[str, list[dict]] = {}
        for item in settled["items"]:
            by_status.setdefault(item["status"], []).append(item)
        self.assertEqual(
            len(by_status.get(ITEM_STATUS_SUCCEEDED, [])), 1, settled["items"]
        )
        skipped = by_status.get(ITEM_STATUS_SKIPPED, [])
        self.assertEqual(len(skipped), 1, settled["items"])
        self.assertEqual(skipped[0]["reason"], tasks.REASON_NOT_SELECTED, skipped[0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
