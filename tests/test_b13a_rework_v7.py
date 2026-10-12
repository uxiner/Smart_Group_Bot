"""B13a 返工 v7：周报"已入账后取消发送"的复合回执必须保留真实效果。

返工 v6 把"持久取消已落库、内存事件还没置位"这个窗口里的守卫拒绝如实归为取消
（``cancelled`` / ``cancel_requested``），不再报成"授权被撤/配置漂移"。但周报适配器是一个
**复合项**：它先做上一完整周的积分结算并 ``session.commit()``（积分真的入账了），然后才
调用 ``deliver_group_report`` 出站。父独立验收的 v17 探针 hold 在真实出站入口，先独立读回
``MemberPointAward = [7:25]`` 证明积分已入账，再让 ``cancel_requested`` 落库（内存事件不置位）
并放行：取消在写出站这一步生效，于是 v6 的通用取消回执把整项写成"已请求取消，本项未执行"
——抹掉了已经提交、不可回退的积分副作用，也丢掉了已经算好的金额说明。

本文件要求：取消分类（v6）保留，但回执必须如实说明**这一半已经发生**的效果——

* 周报**没有发送**（``sends == 0``）；
* 积分该入账的**已经入账**（``MemberPointAward`` 仍是一行 25 分），回执带上真实的
  ``award_note``（新入账 N 分 / 此前已入账本次幂等跳过 / 没有达标成员三种态）；
* 不得再出现"本项未执行"这种把复合项整体说成没发生过的措辞；
* 重新预览确认可以再发周报，但**不会双发积分**（业务 ref 幂等）。

控制项：正常周报照旧发一次并入账 25 分；取消发生在积分写边界**准入之前**时，仍然是一行
积分都没有、回执就是准确的"本项未执行"；没有达标成员时回执如实说明没有发放积分。
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from sqlalchemy import select, update

import bot.services.activity as activity
import bot.services.operations_runs as runs
import bot.services.operations_tasks as tasks
from bot.db.models import MemberPointAward, OperationsRun
from bot.db.sqlite_session import SQLiteSafeAsyncSession
from bot.services.activity import last_complete_week
from bot.services.operations_runs import (
    ITEM_STATUS_CANCELLED,
    ITEM_STATUS_SUCCEEDED,
    REASON_CANCEL_REQUESTED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_SUCCEEDED,
)
from bot.utils.timezone import now_shanghai_naive
from tests.test_b13a_operations_center import (
    ADMIN_ID,
    GROUP_A,
    _OperationsTestCase,
)


class WeeklyCancelledAfterAwardV7Tests(_OperationsTestCase):
    """hold 真实出站入口：取消只取消"发送"，不得抹掉已经入账的积分。"""

    def _hold_deliver(self, entered: asyncio.Event, release: asyncio.Event, obs: dict):
        """在**真实出站入口** ``deliver_group_report`` 前 hold。"""

        original = tasks.deliver_group_report

        async def held(*args, **kwargs):
            if not obs.get("entered"):
                obs["entered"] = True
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        return patch.object(tasks, "deliver_group_report", new=held)

    async def _award_rows(self) -> list[tuple[int, int]]:
        async with self.session_factory() as session:
            rows = list(
                (await session.execute(select(MemberPointAward))).scalars().all()
            )
        return sorted((int(row.user_id), int(row.points)) for row in rows)

    async def _durable_cancel(self, run_key: str) -> None:
        """只改数据库真源：故意**不**置内存取消事件，复刻"已落库、事件未置位"窗口。"""

        async with self.session_factory() as session:
            await session.execute(
                update(OperationsRun)
                .where(OperationsRun.run_key == run_key)
                .values(cancel_requested=True)
            )
            await session.commit()

    async def _weekly_cancelled_at_delivery(self) -> tuple[dict, dict, list[tuple[int, int]]]:
        """预览 → 确认 → 卡在真实出站入口 → 确认积分已入账 → 持久取消 → 放行。"""

        week_start, _week_end = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        entered = asyncio.Event()
        release = asyncio.Event()
        obs: dict = {}
        with self._hold_deliver(entered, release, obs):
            preview = (
                await (await self._preview("weekly_report", {"days": 7}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = str((await confirmed.json())["run"]["run_key"])
            await asyncio.wait_for(entered.wait(), timeout=5)
            # 出站还没发生，但结算已经 commit：积分此刻必须已经能独立读回来。
            awarded_before_send = await self._award_rows()
            obs["awarded_before_send"] = awarded_before_send
            await self._durable_cancel(run_key)
            release.set()
            settled = await runs.await_run(
                self.session_factory,
                run_key=run_key,
                group_id=GROUP_A,
                timeout_seconds=5,
            )
        self.assertTrue(obs.get("entered"), "必须卡在真实出站入口之前")
        return settled, obs, await self._award_rows()

    # ---- 核心：已入账后取消，回执必须保留真实效果 ----

    async def test_cancel_after_committed_awards_keeps_the_real_effect(self) -> None:
        settled, obs, rows = await self._weekly_cancelled_at_delivery()

        # 业务事实先说话：积分早已入账，取消只取消了"发送"。
        self.assertEqual(
            obs["awarded_before_send"],
            [(7, 25)],
            "出站之前积分必须已经真的入账（否则本用例没有复刻父 v17 的窗口）",
        )
        self.assertEqual(rows, [(7, 25)], "已经入账的积分不得被取消回执抹掉")
        self.assertEqual(
            [chat for chat, _text in self.outbound],
            [],
            "取消已落库，周报不得真的发出去",
        )
        self.assertEqual(self.bot.send_message.await_count, 0, "取消后不得发送")
        self.assertEqual(settled["status"], RUN_STATUS_CANCELLED, settled)
        self.assertEqual(settled["reason"], REASON_CANCEL_REQUESTED, settled)
        items = list(settled.get("items") or [])
        self.assertEqual(len(items), 1, items)
        item = items[0]
        self.assertEqual(item["status"], ITEM_STATUS_CANCELLED, item)
        self.assertEqual(item["reason"], REASON_CANCEL_REQUESTED, item)
        self.assertIn("已入账", item["detail"], item)
        self.assertIn("25", item["detail"], item)
        self.assertNotIn(
            "本项未执行",
            item["detail"],
            "取消发生在积分入账之后，不得把整个复合项说成没有执行过",
        )

        # 重新预览确认可以再发周报，但积分靠业务 ref 幂等，不会双发。
        second = await self._run_once("weekly_report", {"days": 7}, user=ADMIN_ID)
        self.assertEqual(second["status"], RUN_STATUS_SUCCEEDED, second)
        self.assertEqual(self.bot.send_message.await_count, 1, "周报本身可以再发一次")
        self.assertEqual(
            await self._award_rows(), [(7, 25)], "重新确认不得让同一位成员拿两次分"
        )
        self.assertIn("此前已入账", second["items"][0]["detail"], second["items"][0])

    # ---- 控制：正常路径照旧入账并发送一次 ----

    async def test_normal_weekly_report_sends_and_awards_once(self) -> None:
        week_start, _week_end = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        run = await self._run_once("weekly_report", {"days": 7}, user=ADMIN_ID)

        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        item = run["items"][0]
        self.assertEqual(item["status"], ITEM_STATUS_SUCCEEDED, item)
        self.assertIn("已入账 25 分", item["detail"], item)
        self.assertEqual(await self._award_rows(), [(7, 25)])
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual([chat for chat, _text in self.outbound], [GROUP_A])

    # ---- 控制：积分写边界准入之前取消，回执仍然是准确的"未执行" ----

    async def test_cancel_before_points_admission_still_reports_nothing_executed(
        self,
    ) -> None:
        week_start, _week_end = last_complete_week(now_shanghai_naive())
        await self._seed_activity(
            week_start=week_start, user_id=7, days=5, messages=20, replies=3
        )

        entered = asyncio.Event()
        release = asyncio.Event()
        obs: dict = {}
        real_write_award = activity._write_award
        original_acquire = SQLiteSafeAsyncSession._acquire_write_lock

        async def marked(session, **kwargs):
            session.info["v7_award_admission"] = True
            return await real_write_award(session, **kwargs)

        async def held_lock(self, *, op: str) -> None:
            if self.info.get("v7_award_admission") and not obs.get("held"):
                obs["held"] = True
                entered.set()
                await release.wait()
            return await original_acquire(self, op=op)

        with patch.object(activity, "_write_award", marked), patch.object(
            SQLiteSafeAsyncSession, "_acquire_write_lock", held_lock
        ):
            preview = await self._preview("weekly_report", {"days": 7}, user=ADMIN_ID)
            self.assertEqual(preview.status, 200, await preview.text())
            token = (await preview.json())["preview"]["token"]
            confirmed = await self._confirm(token, user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = str((await confirmed.json())["run"]["run_key"])
            await asyncio.wait_for(entered.wait(), timeout=5)
            await self._durable_cancel(run_key)
            release.set()
            settled = await runs.await_run(
                self.session_factory,
                run_key=run_key,
                group_id=GROUP_A,
                timeout_seconds=5,
            )

        self.assertTrue(obs.get("held"), "没有进入积分写边界的真实 writer 准入窗口")
        self.assertEqual(await self._award_rows(), [], "一行积分都不许写")
        self.assertEqual(self.bot.send_message.await_count, 0)
        self.assertEqual(settled["status"], RUN_STATUS_CANCELLED, settled)
        self.assertEqual(settled["reason"], REASON_CANCEL_REQUESTED, settled)
        item = settled["items"][0]
        self.assertEqual(item["status"], ITEM_STATUS_CANCELLED, item)
        self.assertEqual(item["reason"], REASON_CANCEL_REQUESTED, item)
        self.assertEqual(item["detail"], "已请求取消，本项未执行", item)

    # ---- 控制：没有达标成员时，取消回执不得编造金额 ----

    async def test_cancel_without_qualifying_members_reports_no_award(self) -> None:
        week_start, _week_end = last_complete_week(now_shanghai_naive())
        # 门槛是 3 天 / 10 句：这位成员两天两句，未达标。
        await self._seed_activity(
            week_start=week_start, user_id=7, days=2, messages=2, replies=0
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        obs: dict = {}
        with self._hold_deliver(entered, release, obs):
            preview = (
                await (await self._preview("weekly_report", {"days": 7}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = str((await confirmed.json())["run"]["run_key"])
            await asyncio.wait_for(entered.wait(), timeout=5)
            self.assertEqual(await self._award_rows(), [], "没有达标成员就没有入账")
            await self._durable_cancel(run_key)
            release.set()
            settled = await runs.await_run(
                self.session_factory,
                run_key=run_key,
                group_id=GROUP_A,
                timeout_seconds=5,
            )

        self.assertEqual(settled["status"], RUN_STATUS_CANCELLED, settled)
        item = settled["items"][0]
        self.assertEqual(item["status"], ITEM_STATUS_CANCELLED, item)
        self.assertEqual(item["reason"], REASON_CANCEL_REQUESTED, item)
        self.assertIn("没有发放积分", item["detail"], item)
        self.assertNotIn("25", item["detail"], item)
        self.assertEqual(self.bot.send_message.await_count, 0)
        self.assertEqual(await self._award_rows(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
