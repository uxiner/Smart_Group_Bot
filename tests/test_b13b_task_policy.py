"""B13b / C4 第二子批：按群执行来源与周期计划的正式回归。

真 ``init_db`` + 真 SQLite + 真签名 HTTP + 真运营内核 + 真内部 runner；只在末端
mock Telegram transport。时间一律由调用方显式传入（runner 与纯函数都接受 ``now=``），
不用 sleep、不猜 ``now + 间隔``。

本文件里的 ``task_policy`` / ``periodic_tasks`` 全部在用例内部延迟 import：这样在
改造前的基线上它是**逐个用例的真实失败**（404 / ImportError 出现在用例体里），而不是
一条收集错误被当成"预期红"。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from bot.db.engine import init_db
from bot.db.models import (
    MemberEntitlement,
    MemberPointAward,
    OperationsRun,
)
from bot.services import activity, point_shop
from bot.utils.timezone import now_shanghai_naive

from tests.test_b13a_operations_center import ADMIN_ID, _OperationsTestCase
from tests.test_permanent_memory_create_dedup import GROUP_A, GROUP_B, SUPER_ADMIN

MEMBER_ID = 777

ROOT = Path(__file__).resolve().parents[1]


def _policy_model():
    """B13b 新表：延迟 import，让改造前的基线给出逐用例业务红而不是收集错误。"""

    from bot.db.models import GroupTaskPolicy

    return GroupTaskPolicy


def _policy_event_model():
    from bot.db.models import GroupTaskPolicyEvent

    return GroupTaskPolicyEvent


def _policy_of(document: dict, task_id: str) -> dict:
    for task in document.get("tasks", []):
        if task.get("task_id") == task_id:
            return task
    raise AssertionError(f"payload has no task {task_id!r}: {document!r}")


class TaskPolicyApiTests(_OperationsTestCase):
    """读侧 + CAS + 令牌 + 审计 + 真消费方 + 迁移。"""

    # ---- HTTP 助手（策略面板） ----

    async def _status(self, *, group: int = GROUP_A, user: int = SUPER_ADMIN):
        return await self.client.get(
            f"/api/v1/groups/{group}/operations/task-status",
            headers=self._headers(user),
        )

    async def _policy_get(self, *, group: int = GROUP_A, user: int = SUPER_ADMIN):
        return await self.client.get(
            f"/api/v1/groups/{group}/operations/policy", headers=self._headers(user)
        )

    async def _policy_preview(
        self, body: dict, *, group: int = GROUP_A, user: int = SUPER_ADMIN
    ):
        return await self.client.post(
            f"/api/v1/groups/{group}/operations/policy/preview",
            headers=self._headers(user),
            json=body,
        )

    async def _policy_confirm(
        self, token: str, *, group: int = GROUP_A, user: int = SUPER_ADMIN
    ):
        return await self.client.post(
            f"/api/v1/groups/{group}/operations/policy/confirm",
            headers=self._headers(user),
            json={"token": token},
        )

    async def _policy_audit(
        self, *, group: int = GROUP_A, user: int = SUPER_ADMIN, params: dict | None = None
    ):
        return await self.client.get(
            f"/api/v1/groups/{group}/operations/policy/audit",
            headers=self._headers(user),
            params=params or {},
        )

    async def _switch(
        self,
        task_id: str,
        source: str,
        *,
        group: int = GROUP_A,
        user: int = SUPER_ADMIN,
    ) -> dict:
        previewed = await self._policy_preview(
            {"task_id": task_id, "mode": "source", "source": source},
            group=group,
            user=user,
        )
        self.assertEqual(previewed.status, 200, await previewed.text())
        preview = (await previewed.json())["preview"]
        if not preview["confirmable"]:
            # 来源没有变化：预览如实说"没有变化"，不签发令牌，也不写库。
            self.assertEqual(preview["reason"], "no_change")
            return preview["current"]
        confirmed = await self._policy_confirm(preview["token"], group=group, user=user)
        self.assertEqual(confirmed.status, 200, await confirmed.text())
        return (await confirmed.json())["policy"]

    async def _set_plan(
        self,
        *,
        enabled: bool,
        weekday: int | None = None,
        minute: int | None = None,
        group: int = GROUP_A,
        user: int = SUPER_ADMIN,
    ) -> dict:
        body: dict = {"task_id": "weekly_report", "mode": "plan", "enabled": enabled}
        if weekday is not None:
            body["weekday"] = weekday
        if minute is not None:
            body["minute"] = minute
        previewed = await self._policy_preview(body, group=group, user=user)
        self.assertEqual(previewed.status, 200, await previewed.text())
        preview = (await previewed.json())["preview"]
        confirmed = await self._policy_confirm(preview["token"], group=group, user=user)
        self.assertEqual(confirmed.status, 200, await confirmed.text())
        return (await confirmed.json())["policy"]

    async def _count(self, model) -> int:  # type: ignore[override]
        async with self.session_factory() as session:
            rows = (await session.scalars(select(model))).all()
        return len(rows)

    # ---- 1. 读侧：按群作用域 + 默认值如实 ----

    async def test_policy_read_is_group_scoped_and_defaults_are_honest(self) -> None:
        response = await self._policy_get()
        self.assertEqual(response.status, 200, await response.text())
        document = await response.json()
        self.assertEqual(document["group_id"], GROUP_A)
        self.assertEqual(document["timezone"], "Asia/Shanghai")
        self.assertFalse(document["external_schedule_known"])
        self.assertEqual(
            [task["task_id"] for task in document["tasks"]],
            [
                "scheduled_messages",
                "group_summary",
                "checkin_reminder",
                "activity_award",
                "weekly_report",
                "shop_expire",
            ],
        )
        checkin = _policy_of(document, "checkin_reminder")
        self.assertEqual(checkin["source"], "external")
        self.assertEqual(checkin["source_default"], "external")
        self.assertTrue(checkin["source_switchable"])
        self.assertEqual(sorted(checkin["source_options"]), ["external", "internal"])
        shop = _policy_of(document, "shop_expire")
        self.assertEqual(shop["source"], "internal")
        self.assertTrue(shop["source_switchable"])
        weekly = _policy_of(document, "weekly_report")
        self.assertFalse(weekly["source_switchable"])
        self.assertTrue(weekly["plan_supported"])
        self.assertFalse(weekly["plan_enabled"])
        self.assertIsNone(weekly["anchor_at"])
        self.assertIsNone(weekly["plan_weekday"])
        for task_id in ("scheduled_messages", "group_summary", "activity_award"):
            self.assertFalse(_policy_of(document, task_id)["source_switchable"])
        # 只读：读一次不会写任何策略行或审计。
        self.assertEqual(await self._count(_policy_model()), 0)
        self.assertEqual(await self._count(_policy_event_model()), 0)

    async def test_policy_read_requires_scope_and_stays_group_scoped(self) -> None:
        forbidden = await self._policy_get(user=MEMBER_ID)
        self.assertEqual(forbidden.status, 403, await forbidden.text())
        other_group = await self._policy_get(group=GROUP_B, user=ADMIN_ID)
        self.assertEqual(other_group.status, 403, await other_group.text())
        super_admin = await self._policy_get(group=GROUP_B)
        self.assertEqual(super_admin.status, 200, await super_admin.text())
        self.assertEqual((await super_admin.json())["group_id"], GROUP_B)

    # ---- 2. 预览只读 + 令牌 + 确认后持久读回 ----

    async def test_preview_writes_nothing_and_confirmation_persists_and_audits(self) -> None:
        previewed = await self._policy_preview(
            {"task_id": "checkin_reminder", "mode": "source", "source": "internal"}
        )
        self.assertEqual(previewed.status, 200, await previewed.text())
        preview = (await previewed.json())["preview"]
        self.assertTrue(preview["confirmable"], preview)
        self.assertTrue(preview["read_only"])
        self.assertEqual(preview["mode"], "source")
        self.assertEqual(preview["proposed"]["source"], "internal")
        self.assertTrue(preview["effects"])
        self.assertIn("外部", " ".join(preview["effects"]))
        self.assertEqual(await self._count(_policy_model()), 0)
        self.assertEqual(await self._count(_policy_event_model()), 0)

        tampered = await self._policy_confirm(preview["token"][:-4] + "0000")
        self.assertEqual(tampered.status, 400, await tampered.text())
        self.assertEqual(await self._count(_policy_model()), 0)

        confirmed = await self._policy_confirm(preview["token"])
        self.assertEqual(confirmed.status, 200, await confirmed.text())
        payload = await confirmed.json()
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["policy"]["source"], "internal")
        self.assertEqual(payload["policy"]["revision"], 1)
        self.assertIsNotNone(payload["policy"]["active_from"])
        self.assertEqual(await self._count(_policy_model()), 1)
        events = await self._policy_audit()
        self.assertEqual(events.status, 200, await events.text())
        items = (await events.json())["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "source_switch")
        self.assertEqual(items[0]["task_id"], "checkin_reminder")
        self.assertEqual(items[0]["actor_user_id"], SUPER_ADMIN)
        self.assertEqual(items[0]["after"]["source"], "internal")
        # 持久读回：换一个连接再读，仍是新来源。
        again = await self._policy_get()
        self.assertEqual(
            _policy_of(await again.json(), "checkin_reminder")["source"],
            "internal",
        )

    # ---- 3. CAS 竞争 + 跨群隔离 ----

    async def test_source_switch_is_cas_and_per_group(self) -> None:
        first = await self._policy_preview(
            {"task_id": "shop_expire", "mode": "source", "source": "external"}
        )
        second = await self._policy_preview(
            {"task_id": "shop_expire", "mode": "source", "source": "external"}
        )
        token_one = (await first.json())["preview"]["token"]
        token_two = (await second.json())["preview"]["token"]
        self.assertEqual((await self._policy_confirm(token_one)).status, 200)
        stale = await self._policy_confirm(token_two)
        self.assertEqual(stale.status, 409, await stale.text())
        self.assertEqual((await stale.json())["error"]["code"], "revision_conflict")
        self.assertEqual(await self._count(_policy_model()), 1)
        self.assertEqual(await self._count(_policy_event_model()), 1)

        # 群作用域：A 群的变更不影响 B 群（B 仍是默认值，也没有 B 群的策略行）。
        other = await self._policy_get(group=GROUP_B)
        self.assertEqual(_policy_of(await other.json(), "shop_expire")["source"], "internal")
        self.assertEqual(await self._count(_policy_model()), 1)

        # 令牌绑定群：拿 A 群的令牌去 B 群确认必须被拒。
        cross = await self._policy_confirm(token_one, group=GROUP_B)
        self.assertEqual(cross.status, 403, await cross.text())
        self.assertEqual((await cross.json())["error"]["code"], "preview_group_mismatch")

    async def test_switch_source_is_idempotent_when_nothing_changes(self) -> None:
        await self._switch("checkin_reminder", "internal")
        again = await self._switch("checkin_reminder", "internal")
        self.assertEqual(again["revision"], 1)
        self.assertEqual(await self._count(_policy_event_model()), 1)

    async def test_non_switchable_task_and_unknown_values_are_refused(self) -> None:
        not_switchable = await self._policy_preview(
            {"task_id": "group_summary", "mode": "source", "source": "external"}
        )
        self.assertEqual(not_switchable.status, 409, await not_switchable.text())
        self.assertEqual(
            (await not_switchable.json())["error"]["code"], "source_not_switchable"
        )
        bad_source = await self._policy_preview(
            {"task_id": "checkin_reminder", "mode": "source", "source": "cron"}
        )
        self.assertEqual(bad_source.status, 400, await bad_source.text())
        self.assertEqual((await bad_source.json())["error"]["code"], "invalid_source")
        unknown_field = await self._policy_preview(
            {"task_id": "checkin_reminder", "mode": "source", "source": "internal", "x": 1}
        )
        self.assertEqual(unknown_field.status, 400, await unknown_field.text())
        self.assertEqual(await self._count(_policy_model()), 0)

    # ---- 4. 周期计划：显式 day/time、严格未来、默认关闭 ----

    async def test_weekly_plan_needs_explicit_day_and_time_and_never_backfills(self) -> None:
        missing = await self._policy_preview(
            {"task_id": "weekly_report", "mode": "plan", "enabled": True}
        )
        self.assertEqual(missing.status, 400, await missing.text())
        self.assertEqual((await missing.json())["error"]["code"], "invalid_plan")
        half = await self._policy_preview(
            {"task_id": "weekly_report", "mode": "plan", "enabled": True, "weekday": 2}
        )
        self.assertEqual(half.status, 400, await half.text())
        out_of_range = await self._policy_preview(
            {
                "task_id": "weekly_report",
                "mode": "plan",
                "enabled": True,
                "weekday": 7,
                "minute": 540,
            }
        )
        self.assertEqual(out_of_range.status, 400, await out_of_range.text())
        unsupported = await self._policy_preview(
            {"task_id": "checkin_reminder", "mode": "plan", "enabled": True}
        )
        self.assertEqual(unsupported.status, 409, await unsupported.text())
        self.assertEqual((await unsupported.json())["error"]["code"], "plan_unsupported")
        self.assertEqual(await self._count(_policy_model()), 0)

        now = now_shanghai_naive()
        weekday = (now.weekday() + 1) % 7
        previewed = await self._policy_preview(
            {
                "task_id": "weekly_report",
                "mode": "plan",
                "enabled": True,
                "weekday": weekday,
                "minute": 9 * 60,
            }
        )
        self.assertEqual(previewed.status, 200, await previewed.text())
        preview = (await previewed.json())["preview"]
        self.assertGreater(preview["next_anchor_at"], now.isoformat())
        self.assertTrue(preview["next_anchor_at"].startswith("20"))
        self.assertIn("+08:00", preview["next_anchor_at"])

    async def test_weekly_plan_off_by_default_runs_and_sends_nothing(self) -> None:
        from bot.services.periodic_tasks import WeeklyPlanRunner

        runner = WeeklyPlanRunner(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            manager=self.manager,
        )
        results = await runner.run_once(now=now_shanghai_naive())
        self.assertEqual(results, [])
        self.assertEqual(await self._count(OperationsRun), 0)
        self.assertEqual(self.outbound, [])
        self.assertEqual(await self._count(MemberPointAward), 0)
        # 状态页也必须如实：没有计划就说计划关闭，不编造下一次时刻。
        status = await self._status()
        self.assertEqual(status.status, 200, await status.text())
        weekly = _status_of((await status.json())["task_status"], "weekly_report")
        self.assertEqual(weekly["next_run"]["reason"], "plan_disabled")
        self.assertIsNone(weekly["next_run"]["at"])

    async def test_weekly_plan_fires_once_at_the_anchor_and_advances_it(self) -> None:
        from bot.services import task_policy as policy_module
        from bot.services.periodic_tasks import WeeklyPlanRunner

        now = now_shanghai_naive()
        week_start, _week_end = activity.last_complete_week(now)
        await self._seed_activity(week_start=week_start, user_id=7)
        weekday = (now.weekday() + 2) % 7
        saved = await self._set_plan(enabled=True, weekday=weekday, minute=10 * 60)
        self.assertTrue(saved["plan_enabled"])
        anchor = _parse_stamp(saved["anchor_at"])
        self.assertGreater(anchor, now)

        runner = WeeklyPlanRunner(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            manager=self.manager,
        )
        # 还没到锚点：什么都不做（发布/确认本身绝不会立刻补上一周）。
        self.assertEqual(await runner.run_once(now=now), [])
        self.assertEqual(await self._count(OperationsRun), 0)
        self.assertEqual(self.outbound, [])

        # 时间受控：把锚点挪到"刚刚到点"，runner 必须只发一次、并推进锚点。
        async with self.session_factory() as session:
            row = await session.scalar(
                select(_policy_model()).where(_policy_model().group_id == GROUP_A)
            )
            row.anchor_at = now - timedelta(minutes=1)
            await session.commit()
        results = await runner.run_once(now=now)
        self.assertEqual(len(results), 1, results)
        self.assertTrue(results[0]["scheduled"], results)
        self.assertTrue(results[0]["anchor_advanced"], results)
        run = await self._wait(results[0]["run_key"])
        self.assertEqual(run.status, 200, await run.text())
        payload = (await run.json())["run"]
        self.assertEqual(payload["source"], "schedule")
        self.assertEqual(payload["status"], "succeeded", payload)
        self.assertEqual(self.outbound, [(GROUP_A, self.outbound[0][1])])
        awards = [
            (row.user_id, row.points)
            for row in (
                await self._award_rows()
            )
        ]
        self.assertEqual(awards, [(7, 25)])

        # 同一时刻再跑一次：锚点已经推到未来，不会再发第二次。
        self.assertEqual(await runner.run_once(now=now), [])
        self.assertEqual(len(self.outbound), 1)
        # 重启控制：换一个全新的 runner 实例（模拟进程重启后重新读持久锚点），
        # 仍然不会补发第二次：下一次计划来自库里的 anchor_at，不是内存里的东西。
        fresh = WeeklyPlanRunner(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            manager=self.manager,
        )
        self.assertEqual(await fresh.run_once(now=now), [])
        self.assertEqual(len(self.outbound), 1)
        async with self.session_factory() as session:
            row = await session.scalar(
                select(_policy_model()).where(_policy_model().group_id == GROUP_A)
            )
            expected = policy_module.next_occurrence(now, weekday, 10 * 60)
            self.assertEqual(row.anchor_at, expected)
            # 推进锚点不算"策略变更"：版本不变，否则会把自己刚入队的 run 挡死。
            self.assertEqual(row.revision, int(saved["revision"]))
        self.assertEqual(await self._count(_policy_event_model()), 1)

    async def test_weekly_plan_after_enable_only_schedules_the_future(self) -> None:
        now = now_shanghai_naive()
        weekday = (now.weekday() + 3) % 7
        saved = await self._set_plan(enabled=True, weekday=weekday, minute=23 * 60 + 30)
        anchor = _parse_stamp(saved["anchor_at"])
        self.assertGreater(anchor, now)
        self.assertLessEqual(anchor - now, timedelta(days=7))
        self.assertEqual(anchor.hour, 23)
        self.assertEqual(anchor.minute, 30)
        self.assertEqual(await self._count(OperationsRun), 0)

        disabled = await self._set_plan(enabled=False)
        self.assertFalse(disabled["plan_enabled"])
        self.assertIsNone(disabled["anchor_at"])
        self.assertIsNone(disabled["plan_weekday"])
        self.assertIsNone(disabled["plan_minute"])
        self.assertEqual(await self._count(_policy_event_model()), 2)

    # ---- 5. 内部 runner 真消费策略 + 不补旧时段 ----

    async def test_checkin_internal_runner_consumes_policy_and_skips_old_slots(self) -> None:
        from bot.services.periodic_tasks import CheckinInternalRunner
        from bot.services.task_policy import slot_moment

        day = now_shanghai_naive().date()
        moment = slot_moment(day, 12)
        runner = CheckinInternalRunner(bot=self.bot, session_factory=self.session_factory)

        # 默认（没有任何策略行）＝ external：进程内 runner 一个群都不碰。
        self.assertEqual(await runner.run_once(now=moment + timedelta(minutes=1)), [])
        self.assertEqual(self.outbound, [])

        # 切到 internal：active_from = 切换时刻，比 12:00 晚 ⇒ 这个旧时段不补发。
        await self._switch("checkin_reminder", "internal")
        now = moment + timedelta(minutes=1)
        async with self.session_factory() as session:
            row = await session.scalar(
                select(_policy_model()).where(_policy_model().group_id == GROUP_A)
            )
            self.assertIsNotNone(row.active_from)
            row.active_from = moment + timedelta(minutes=30)
            await session.commit()
        self.assertEqual(await runner.run_once(now=now), [])
        self.assertEqual(self.outbound, [])

        # active_from 早于时段：这一轮必须真的发出去，而且只发一次。
        async with self.session_factory() as session:
            row = await session.scalar(
                select(_policy_model()).where(_policy_model().group_id == GROUP_A)
            )
            row.active_from = moment - timedelta(minutes=5)
            await session.commit()
        results = await runner.run_once(now=now)
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0]["group_id"], GROUP_A)
        self.assertEqual(results[0]["slot"], 12)
        self.assertEqual(len(self.outbound), 1)
        self.assertEqual(self.outbound[0][0], GROUP_A)

        # 占位/幂等：同一个时段第二轮不再发（共享占位 + 出站回执说了算；重跑只报
        # "已经送达"，绝不再发第二条）。
        repeat = await runner.run_once(now=now + timedelta(minutes=1))
        self.assertEqual(len(self.outbound), 1)
        self.assertTrue(repeat, repeat)
        for result in repeat:
            self.assertEqual(result["group_id"], GROUP_A)
            self.assertEqual(result["status"], "already_delivered", result)

    async def test_checkin_external_entry_scope_follows_policy_and_active_from(self) -> None:
        from bot.tools.checkin_reminder import external_slot_scope

        from bot.services.task_policy import slot_moment

        day = now_shanghai_naive().date()
        moment = slot_moment(day, 12)
        async with self.session_factory() as session:
            external, skipped = await external_slot_scope(session, slot=12, day=day)
        self.assertEqual(sorted(external), sorted([GROUP_A, GROUP_B]))
        self.assertEqual(skipped, [])

        # A 群交给进程内 runner：外部入口不再处理它。
        await self._switch("checkin_reminder", "internal")
        async with self.session_factory() as session:
            external, skipped = await external_slot_scope(session, slot=12, day=day)
        self.assertEqual(external, [GROUP_B])
        self.assertEqual(skipped, [])

        # A 群切回外部，但切换生效晚于 12:00：不补这个旧时段。
        await self._switch("checkin_reminder", "external")
        async with self.session_factory() as session:
            row = await session.scalar(
                select(_policy_model()).where(_policy_model().group_id == GROUP_A)
            )
            row.active_from = moment + timedelta(hours=2)
            await session.commit()
        async with self.session_factory() as session:
            external, skipped = await external_slot_scope(session, slot=12, day=day)
        self.assertEqual(external, [GROUP_B])
        self.assertEqual(skipped, [GROUP_A])

    async def test_shop_internal_runner_excludes_explicit_external_groups(self) -> None:
        from bot.services.point_shop import ShopExpiryService, due_entitlements

        now = now_shanghai_naive()
        async with self.session_factory() as session:
            scope, external = await point_shop.shop_group_scope(session)
            # 默认全 internal：沿用改造前的"全量"口径（None），外部入口无事可做。
            self.assertIsNone(scope)
            self.assertEqual(external, [])

        await self._switch("shop_expire", "external")
        async with self.session_factory() as session:
            scope, external = await point_shop.shop_group_scope(session)
            self.assertEqual(external, [GROUP_A])
            self.assertEqual(scope, [GROUP_B])
            # 空作用域绝不能退化成"处理所有群"。
            self.assertEqual(await due_entitlements(session, now=now, group_ids=[]), [])

        before = await self._count(MemberEntitlement)
        service = ShopExpiryService(bot=self.bot, session_factory=self.session_factory)
        processed = await service.run_once(now=now)
        self.assertEqual(processed, [])
        self.assertEqual(await self._count(MemberEntitlement), before)

        # 两个群都在外部时：进程内 runner 明确无事可做（不是"跳过一段就算成功"）。
        await self._switch("shop_expire", "external", group=GROUP_B)
        async with self.session_factory() as session:
            scope, external = await point_shop.shop_group_scope(session)
        self.assertEqual(scope, [])
        self.assertEqual(sorted(external), sorted([GROUP_A, GROUP_B]))
        self.assertEqual(await service.run_once(now=now), [])

    # ---- 6. 策略版本：真实不可逆准入的线性化点 ----

    async def test_policy_change_blocks_the_real_write_boundary_of_an_inflight_run(self) -> None:
        from bot.db.sqlite_session import SQLiteSafeAsyncSession
        from bot.services import task_policy as policy_module

        now = now_shanghai_naive()
        week_start, _week_end = activity.last_complete_week(now)
        await self._seed_activity(week_start=week_start, user_id=7)

        original_write = activity._write_award
        original_lock = SQLiteSafeAsyncSession._acquire_write_lock
        entered = asyncio.Event()
        release = asyncio.Event()
        obs: dict[str, object] = {}

        async def marked(session, **kwargs):
            session.info["b13b_award_admission"] = True
            return await original_write(session, **kwargs)

        async def held(session, *, op):
            if session.info.get("b13b_award_admission") and not obs.get("entered"):
                obs["entered"] = True
                obs["lock_already_held"] = bool(
                    getattr(session, "_sqlite_write_lock_held", None)
                )
                entered.set()
                await release.wait()
            return await original_lock(session, op=op)

        with patch.object(activity, "_write_award", new=marked), patch.object(
            SQLiteSafeAsyncSession, "_acquire_write_lock", new=held
        ):
            previewed = await self._preview("weekly_report", {"days": 7})
            self.assertEqual(previewed.status, 200, await previewed.text())
            preview = (await previewed.json())["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            self.assertEqual(preview["policy_revision"], 0)

            confirmed = await self._confirm(preview["token"])
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)
            self.assertTrue(obs.get("entered"))
            self.assertFalse(obs.get("lock_already_held"))

            # 独立合法 session：管理员在真正的写准入之前改了周期计划（版本 0 → 1）。
            async with self.session_factory() as session:
                outcome = await policy_module.set_weekly_plan(
                    session,
                    group_id=GROUP_A,
                    enabled=True,
                    weekday=(now.weekday() + 1) % 7,
                    minute=9 * 60,
                    expected_revision=0,
                    actor_user_id=ADMIN_ID,
                )
                self.assertTrue(outcome.ok, outcome)
                await session.commit()
            release.set()

            settled = await self._wait(run_key)
            self.assertEqual(settled.status, 200, await settled.text())
            payload = (await settled.json())["run"]

        self.assertEqual(payload["status"], "failed", payload)
        self.assertEqual(payload["reason"], "guard_blocked", payload)
        self.assertEqual(
            [(row.user_id, row.points) for row in await self._award_rows()], []
        )
        self.assertEqual(self.outbound, [])
        for item in payload["items"]:
            self.assertEqual(item["status"], "failed", item)
            self.assertEqual(item["reason"], "guard_blocked", item)
            self.assertFalse(item["resendable"], item)

    # ---- 7. 状态页真源 ----

    async def test_status_reports_the_persisted_policy_without_guessing(self) -> None:
        await self._switch("checkin_reminder", "internal")
        now = now_shanghai_naive()
        await self._set_plan(enabled=True, weekday=(now.weekday() + 1) % 7, minute=8 * 60)

        response = await self._status()
        self.assertEqual(response.status, 200, await response.text())
        task_status = (await response.json())["task_status"]
        checkin = _status_of(task_status, "checkin_reminder")
        self.assertEqual(checkin["source"]["implemented"], "in_process")
        self.assertEqual(checkin["source"]["effective_source"], "internal")
        self.assertEqual(checkin["source"]["runner_task_name"], "checkin_reminder_internal")
        self.assertFalse(checkin["source"]["external_schedule_known"])
        self.assertNotEqual(checkin["next_run"]["reason"], "external_schedule_unknown")
        weekly = _status_of(task_status, "weekly_report")
        self.assertEqual(weekly["trigger"], "wall_clock")
        self.assertEqual(weekly["next_run"]["reason"], "planned_by_scheduler")
        self.assertIsNotNone(weekly["next_run"]["at"])
        self.assertTrue(weekly["details"]["plan_enabled"])
        self.assertIsNotNone(weekly["details"]["plan_anchor_at"])
        self.assertEqual(weekly["details"]["plan_timezone"], "Asia/Shanghai")
        activity_task = _status_of(task_status, "activity_award")
        self.assertEqual(activity_task["trigger"], "event_driven")
        self.assertEqual(activity_task["next_run"]["reason"], "manual_only")
        shop = _status_of(task_status, "shop_expire")
        self.assertEqual(shop["source"]["implemented"], "in_process")
        # 外部宿主 cron 的登记情况永远如实报未知。
        for task_id in ("checkin_reminder", "shop_expire", "weekly_report"):
            self.assertFalse(_status_of(task_status, task_id)["source"]["external_schedule_known"])

    # ---- 8. 审计：有界分页 + 只本群 ----

    async def test_audit_is_bounded_and_group_scoped(self) -> None:
        await self._switch("checkin_reminder", "internal")
        await self._switch("shop_expire", "external")
        await self._set_plan(enabled=True, weekday=1, minute=600)

        first = await self._policy_audit(params={"limit": "2"})
        self.assertEqual(first.status, 200, await first.text())
        page = await first.json()
        self.assertEqual(len(page["items"]), 2)
        self.assertTrue(page["has_more"])
        self.assertIsNotNone(page["next_cursor"])
        second = await self._policy_audit(
            params={"limit": "2", "cursor": str(page["next_cursor"])}
        )
        self.assertEqual(second.status, 200, await second.text())
        tail = await second.json()
        self.assertEqual(len(tail["items"]), 1)
        self.assertFalse(tail["has_more"])
        filtered = await self._policy_audit(params={"task_id": "shop_expire"})
        self.assertEqual(len((await filtered.json())["items"]), 1)
        bad_limit = await self._policy_audit(params={"limit": "0"})
        self.assertEqual(bad_limit.status, 400, await bad_limit.text())
        bad_task = await self._policy_audit(params={"task_id": "not a task"})
        self.assertEqual(bad_task.status, 400, await bad_task.text())
        other = await self._policy_audit(group=GROUP_B)
        self.assertEqual(other.status, 200, await other.text())
        self.assertEqual((await other.json())["items"], [])

    # ---- 9. 老库迁移：补列 + 唯一键 ----

    async def test_existing_database_gets_policy_column_and_unique_index(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = None
        try:
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE operations_runs (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    run_key VARCHAR(64) NOT NULL,
                    token_id VARCHAR(128) NOT NULL,
                    task_id VARCHAR(32) NOT NULL,
                    group_id BIGINT NOT NULL,
                    params JSON,
                    status VARCHAR(16) NOT NULL DEFAULT 'accepted',
                    reason VARCHAR(64) NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    source VARCHAR(16) NOT NULL DEFAULT 'manual',
                    retry_of_run_id INTEGER,
                    target_keys JSON,
                    cancel_requested BOOLEAN NOT NULL DEFAULT 0,
                    guard_revision INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME,
                    started_at DATETIME,
                    finished_at DATETIME
                );
                CREATE TABLE group_task_policies (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    group_id BIGINT NOT NULL,
                    task_id VARCHAR(32) NOT NULL,
                    source VARCHAR(16) NOT NULL DEFAULT '',
                    plan_enabled BOOLEAN NOT NULL DEFAULT 0,
                    plan_weekday INTEGER,
                    plan_minute INTEGER,
                    anchor_at DATETIME,
                    active_from DATETIME,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_by BIGINT NOT NULL DEFAULT 0,
                    updated_at DATETIME
                );
                -- 迁移之前就存在的执行记录：补列不能动旧数据，
                -- policy_revision 默认 0（当时没有策略行）。
                INSERT INTO operations_runs (run_key, token_id, task_id, group_id, status, source)
                VALUES ('legacy-run', 'legacy-token', 'weekly_report', -1001234567890, 'succeeded', 'manual');
                """
            )
            connection.commit()
            connection.close()

            engine, factory = await init_db(f"sqlite+aiosqlite:///{path}")
            check = sqlite3.connect(path)
            try:
                columns = {
                    row[1] for row in check.execute("PRAGMA table_info(operations_runs)")
                }
                self.assertIn("policy_revision", columns)
                unique = {
                    row[1]
                    for row in check.execute("PRAGMA index_list(group_task_policies)")
                    if row[2]
                }
                self.assertIn("ux_group_task_policies_identity", unique)
                self.assertEqual(
                    list(
                        check.execute(
                            "SELECT run_key, policy_revision, status FROM operations_runs"
                        )
                    ),
                    [("legacy-run", 0, "succeeded")],
                )
            finally:
                check.close()

            # 幂等 + 重启：同一个库再初始化一次不报错、不丢数据。
            await engine.dispose()
            engine, factory = await init_db(f"sqlite+aiosqlite:///{path}")
            check = sqlite3.connect(path)
            try:
                columns = {
                    row[1] for row in check.execute("PRAGMA table_info(operations_runs)")
                }
                self.assertIn("policy_revision", columns)
                unique = {
                    row[1]
                    for row in check.execute("PRAGMA index_list(group_task_policies)")
                    if row[2]
                }
                self.assertIn("ux_group_task_policies_identity", unique)
                self.assertEqual(
                    list(
                        check.execute(
                            "SELECT run_key, policy_revision FROM operations_runs"
                        )
                    ),
                    [("legacy-run", 0)],
                )
            finally:
                check.close()

            async with factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO group_task_policies (group_id, task_id, revision) "
                        "VALUES (:group_id, 'weekly_report', 1)"
                    ),
                    {"group_id": GROUP_A},
                )
                await session.commit()
            async with factory() as session:
                with self.assertRaises(IntegrityError):
                    await session.execute(
                        text(
                            "INSERT INTO group_task_policies (group_id, task_id, revision) "
                            "VALUES (:group_id, 'weekly_report', 1)"
                        ),
                        {"group_id": GROUP_A},
                    )
                    await session.commit()
        finally:
            if engine is not None:
                await engine.dispose()
            if os.path.exists(path):
                os.unlink(path)

    # ---- B-checkin-real-startup / B-clock-boundaries / B-shared-receipt ----

    async def test_internal_runners_are_wired_into_real_startup_and_stop_cleanly(
        self,
    ) -> None:
        """真实 __main__ 装配 + 常驻生命周期；下一次时段来自真实调度函数。"""

        source = (ROOT / "bot" / "__main__.py").read_text(encoding="utf-8")
        # 真实启动路径装配（不是写个 helper 却不接）：构造 + create_task + 同批关停。
        self.assertIn("CheckinInternalRunner(", source)
        self.assertIn("WeeklyPlanRunner(", source)
        self.assertIn("checkin_runner.run_forever()", source)
        self.assertIn("weekly_plan_runner.run_forever()", source)
        self.assertIn('name="checkin-reminder-internal-runner"', source)
        self.assertIn('name="weekly-report-plan-runner"', source)
        self.assertIn("_cancel_tasks_bounded(", source)
        self.assertIn('label="background services"', source)

        from bot.services.periodic_tasks import CheckinInternalRunner
        from bot.services.task_policy import slot_moment

        day = now_shanghai_naive().date()
        moment = slot_moment(day, 12)
        await self._switch("checkin_reminder", "internal")
        async with self.session_factory() as session:
            row = await session.scalar(
                select(_policy_model()).where(_policy_model().group_id == GROUP_A)
            )
            row.active_from = moment - timedelta(minutes=5)
            await session.commit()

        # stop() 之后循环不再进入：兜底关停不会挂住。
        stopped = CheckinInternalRunner(bot=self.bot, session_factory=self.session_factory)
        stopped.stop()
        await asyncio.wait_for(stopped.run_forever(), timeout=5)

        # 常驻循环：第一个 tick 就按"当前时段"真实发出；关停走取消（与
        # _cancel_tasks_bounded 同一路径），任务必须以 CancelledError 结束。
        live = CheckinInternalRunner(bot=self.bot, session_factory=self.session_factory)
        with patch(
            "bot.services.periodic_tasks.now_shanghai_naive",
            return_value=moment + timedelta(minutes=1),
        ):
            task = asyncio.create_task(live.run_forever())
            try:
                for _ in range(250):
                    if self.outbound:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(self.outbound), 1, self.outbound)
                self.assertEqual(self.outbound[0][0], GROUP_A)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(task.cancelled())

        # 只读状态里的"下一次"必须来自同一个调度函数与真实时段（9/12/15/18），
        # 不是 now + 间隔。
        status_response = await self._status()
        self.assertEqual(status_response.status, 200, await status_response.text())
        status = _status_of((await status_response.json())["task_status"], "checkin_reminder")
        self.assertEqual(status["trigger"], "wall_clock")
        self.assertEqual(status["source"]["runner_task_name"], "checkin_reminder_internal")
        self.assertEqual(status["next_run"]["reason"], "planned_by_scheduler")
        planned = _parse_stamp(status["next_run"]["at"])
        self.assertEqual(status["details"]["slots"], [9, 12, 15, 18])
        self.assertIn(planned.hour, status["details"]["slots"])
        self.assertEqual(planned.minute, 0)
        self.assertGreater(planned, now_shanghai_naive())
        self.assertLessEqual(
            (planned - now_shanghai_naive()).total_seconds(), 24 * 3600 + 1
        )

        # 零时段控制：没有任何时段可排时如实说"没有配置时段"，不猜一个时刻。
        with patch("bot.services.operations_task_status.reminder_slots", return_value=[]):
            zeroed_response = await self._status()
            zeroed = _status_of(
                (await zeroed_response.json())["task_status"], "checkin_reminder"
            )
        self.assertEqual(zeroed["next_run"]["reason"], "no_slots_configured")
        self.assertIsNone(zeroed["next_run"]["at"])

    async def test_clock_boundaries_across_midnight_and_week_edges(self) -> None:
        """午夜/周边界：下一时段与下一次计划都只锚定严格未来。"""

        from datetime import datetime

        from bot.services.task_policy import (
            SLOT_DUE_GRACE_SECONDS,
            due_slots,
            next_occurrence,
            slot_moment,
            slot_moment_from_key,
        )

        monday = datetime(2026, 10, 5, 10, 0)  # 周一 10:00
        self.assertEqual(monday.weekday(), 0)
        # 正好落在目标分钟 ⇒ 下一周（"现在"不算未来）。
        self.assertEqual(next_occurrence(monday, 0, 600), datetime(2026, 10, 12, 10, 0))
        self.assertEqual(next_occurrence(monday - timedelta(minutes=1), 0, 600), monday)
        # 跨周：周日 23:30 看周一 00:00 ⇒ 次日，不是 7 天后。
        sunday = datetime(2026, 10, 11, 23, 30)
        self.assertEqual(sunday.weekday(), 6)
        self.assertEqual(next_occurrence(sunday, 0, 0), datetime(2026, 10, 12, 0, 0))

        # 时段到点：正好到点算到点；差 1 秒不算；到宽限边界不算；次日凌晨看昨天
        # 23:00 的时段已过宽限 ⇒ 不再算"到点"（不会补发）。
        day = date(2026, 10, 5)
        noon = slot_moment(day, 12)
        self.assertEqual(noon, datetime(2026, 10, 5, 12, 0))
        self.assertEqual(due_slots(now=noon, slots=[12]), [12])
        self.assertEqual(due_slots(now=noon - timedelta(seconds=1), slots=[12]), [])
        self.assertEqual(
            due_slots(now=noon + timedelta(seconds=SLOT_DUE_GRACE_SECONDS), slots=[12]),
            [],
        )
        late = slot_moment(day, 23)
        self.assertEqual(due_slots(now=late + timedelta(minutes=59), slots=[23]), [23])
        self.assertEqual(
            due_slots(now=slot_moment(day + timedelta(days=1), 0), slots=[23]), []
        )
        # 生效时刻晚于时段 ⇒ 这个时段不算到点（切换只锚定未来）。
        self.assertEqual(
            due_slots(now=noon, slots=[12], active_from=noon + timedelta(seconds=1)), []
        )
        # 时段键（占位/回执的稳定身份）能反解出时刻；坏键返回 None，不猜。
        self.assertEqual(slot_moment_from_key("2026-10-05:12"), noon)
        self.assertIsNone(slot_moment_from_key("2026-10-05"))
        self.assertIsNone(slot_moment_from_key("nope:12"))

    async def test_shared_checkin_receipt_blocks_cross_entry_double_send(self) -> None:
        """外部入口发过之后，内部 runner 不再补发第二条（共用占位 + 出站回执）。"""

        from bot.services.checkin_reminder import (
            CHECKIN_OUTCOME_SENT,
            backfill_durable_auto_delete,
            reminder_auto_delete_seconds,
            send_checkin_slot,
            slot_key,
        )
        from bot.services.operations_receipts import (
            TASK_CHECKIN_REMINDER,
            load_receipt,
        )
        from bot.services.periodic_tasks import CheckinInternalRunner
        from bot.services.task_policy import slot_moment
        from bot.utils.telegram import schedule_message_auto_delete_durable

        day = now_shanghai_naive().date()
        moment = slot_moment(day, 12)
        key = slot_key(day, 12)

        # 外部入口（宿主 cron 调的是同一个 send_checkin_slot）先真实发一条。
        async with self.session_factory() as session:
            outcome = await send_checkin_slot(
                session,
                self.bot,
                group_id=GROUP_A,
                slot=12,
                auto_delete_seconds=reminder_auto_delete_seconds(),
                queue_auto_delete=schedule_message_auto_delete_durable,
                backfill_auto_delete=backfill_durable_auto_delete,
                session_factory=self.session_factory,
                protect_outbound=True,
            )
        self.assertEqual(outcome.status, CHECKIN_OUTCOME_SENT, outcome)
        self.assertEqual(len(self.outbound), 1)
        async with self.session_factory() as session:
            receipt = await load_receipt(
                session,
                task_id=TASK_CHECKIN_REMINDER,
                group_id=GROUP_A,
                business_key=key,
            )
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.state, "sent")

        # 这个群随后切到 internal，生效时刻早于 12:00：内部 runner 到点后只能看到
        # "已经送达"，绝不能再发第二条。
        await self._switch("checkin_reminder", "internal")
        async with self.session_factory() as session:
            row = await session.scalar(
                select(_policy_model()).where(_policy_model().group_id == GROUP_A)
            )
            row.active_from = moment - timedelta(minutes=5)
            await session.commit()
        runner = CheckinInternalRunner(bot=self.bot, session_factory=self.session_factory)
        results = await runner.run_once(now=moment + timedelta(minutes=1))
        self.assertEqual(len(self.outbound), 1)
        self.assertTrue(results, results)
        for result in results:
            self.assertEqual(result["group_id"], GROUP_A, result)
            self.assertEqual(result["status"], "already_delivered", result)

    # ---- 辅助 ----

    async def _award_rows(self) -> list[MemberPointAward]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(MemberPointAward))).all())


def _status_of(task_status: dict, task_id: str) -> dict:
    for task in task_status.get("tasks", []):
        if task.get("task_id") == task_id:
            return task
    raise AssertionError(f"task_status has no {task_id!r}")


def _parse_stamp(value: str):
    from datetime import datetime

    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=None)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
