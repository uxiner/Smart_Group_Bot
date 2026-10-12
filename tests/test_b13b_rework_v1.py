"""B13b 返工 v1 正式回归：自动入口真实准入 + 周计划身份绑定 + 错过窗口 + 真实启动。

覆盖父独立验收复现出的四个机制（R-B1..R-B4）与真实 startup 装配缺口（R-B5）：

* **R-B1**：``CheckinInternalRunner`` / ``ShopExpiryService`` / 外部 CLI 只按"读到的
  来源快照"动手。本文件在**真实不可逆边界**（``claim_reminder_slot`` /
  ``claim_business_dispatch`` 的守卫谓词）上注入"读到来源之后、真正出站之前"的
  合法切换，断言旧来源一个副作用都做不出来。
* **R-B2**：周报入队必须携带**被调度计划自己的身份与 revision**；本文件在真实
  ``enqueue_scheduled_run`` 入口注入"确认之后立刻关闭计划"，断言旧 tick 不能重新
  绑定新 revision 建 run。
* **R-B3**：重启后过期锚点只推进、不补发；容差窗口内正常到点仍然只发一次。
* **R-B4**：入队明确失败既不算"正常无目标"也不静默消耗当期；容差内可重试，可能
  已经落库的入队用**稳定计划键**对账，绝不重复建 run。
* **R-B5**：调用真实 ``bot.__main__.main()`` 装配，证明两个常驻 worker 真的进入、
  默认配置零副作用、策略热改被未来消费、关停时真实结束并走完清理。

全部用真 ``init_db`` + 真 SQLite + 真签名 HTTP + 真运营内核；只在最末端替
Telegram transport。时间由调用方显式传入（``now=``），不用 sleep 猜时间。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import func, select, update

import bot.services.operations_runs as operations_runs
import bot.services.periodic_tasks as periodic_tasks
from bot.db.models import (
    CheckinReminderPost,
    MemberPointAward,
    OperationsRun,
)
from bot.services import activity, task_policy
from bot.services.checkin import local_today
from bot.utils.timezone import now_shanghai_naive

from tests.test_b13a_operations_center import _OperationsTestCase
from tests.test_b13b_task_policy import _parse_stamp, _policy_model, _status_of
from tests.test_permanent_memory_create_dedup import (
    GROUP_A,
    GROUP_B,
    SUPER_ADMIN,
)

__all__ = ["AutomaticEntryAdmissionV1Tests", "WeeklyPlanBindingV1Tests"]


class _PolicyHelperMixin:
    """策略面板 HTTP 助手（与 ``tests/test_b13b_task_policy.py`` 同一套口径）。"""

    async def _status(self, *, group: int = GROUP_A, user: int = SUPER_ADMIN):
        return await self.client.get(
            f"/api/v1/groups/{group}/operations/task-status",
            headers=self._headers(user),
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
            return int(len((await session.scalars(select(model))).all()))

    async def _policy_row(self, task_id: str, *, group: int = GROUP_A):
        async with self.session_factory() as session:
            return await session.scalar(
                select(_policy_model()).where(
                    _policy_model().group_id == int(group),
                    _policy_model().task_id == task_id,
                )
            )

    async def _set_anchor(self, anchor: datetime, *, task_id: str = "weekly_report"):
        async with self.session_factory() as session:
            row = await session.scalar(
                select(_policy_model()).where(
                    _policy_model().group_id == GROUP_A,
                    _policy_model().task_id == task_id,
                )
            )
            row.anchor_at = anchor
            await session.commit()

    async def _award_rows(self) -> list[tuple[int, int]]:
        async with self.session_factory() as session:
            rows = list((await session.scalars(select(MemberPointAward))).all())
        return sorted((int(row.user_id), int(row.points)) for row in rows)

    async def _plan_run_rows(self) -> list[tuple[str, str]]:
        """(稳定计划键, 持久状态) 的只读快照，按 run_key 排序。"""

        async with self.session_factory() as session:
            rows = list((await session.scalars(select(OperationsRun))).all())
        return sorted((str(row.run_key), str(row.status)) for row in rows)


# ---------------------------------------------------------------------------
# 兼容层：基线（B13b 返工前）还没有"自动入口守卫 / 绑定"这套 API 时退回旧口径。
# 这些助手只改变**调用形状**，不改变任何断言：基线上旧入口真的没有守卫，所以真实
# 副作用会发生，测试以业务事实（提醒真的发出去了 / 权益真的被撤销了 / run_key 是
# 随机值）判红，而不是以 ImportError 之类的新签名红冒充机制红。
# ---------------------------------------------------------------------------


def _stable_plan_key(*, group_id: int, task_id: str, anchor_at: datetime) -> str:
    """与产品同口径的稳定计划键；在测试里独立算出，不用产品实现自证。"""

    material = (
        f"{str(task_id).strip()}:{int(group_id)}:"
        f"{anchor_at.replace(microsecond=0).isoformat()}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


async def _slot_bindings(session, *, slot: int, day):
    """``({群号: 守卫}, 跳过的群)``；基线退回旧只读 scope（守卫为 None）。"""

    module = importlib.import_module("bot.tools.checkin_reminder")
    bindings = getattr(module, "external_slot_bindings", None)
    if bindings is not None:
        guards, skipped = await bindings(session, slot=slot, day=day)
        return {int(guard.group_id): guard for guard in guards}, list(skipped)
    group_ids, skipped = await module.external_slot_scope(session, slot=slot, day=day)
    return {int(value): None for value in group_ids}, list(skipped)


async def _send_slot(
    session,
    bot,
    *,
    group_id,
    slot,
    guard,
    session_factory,
    auto_delete_seconds,
    queue_auto_delete,
    backfill_auto_delete,
):
    """真实出站入口；基线签名没有 ``guard`` 参数时不传它（旧行为=无守卫）。"""

    from bot.services import checkin_reminder as checkin_module

    kwargs: dict = {
        "auto_delete_seconds": auto_delete_seconds,
        "queue_auto_delete": queue_auto_delete,
        "backfill_auto_delete": backfill_auto_delete,
        "session_factory": session_factory,
    }
    if guard is not None:
        kwargs["guard"] = guard
    return await checkin_module.send_checkin_slot(
        session, bot, group_id=group_id, slot=slot, **kwargs
    )


async def _shop_plan(session):
    """归一化成 ``(internal, external, guards, governed, policy_groups)``。

    兼容层只为让基线红落在**业务事实**上（基线只有"整库有没有来源记录"的全局开关），
    不让 ``ImportError``/``TypeError``/``ValueError`` 冒充机制红。
    """

    module = importlib.import_module("bot.services.point_shop")
    plan = getattr(module, "shop_source_plan", None)
    if plan is not None:
        values = await plan(session)
        if hasattr(values, "governed"):
            return (
                list(values.internal),
                list(values.external),
                dict(values.guards),
                frozenset(values.governed),
                frozenset(values.policy_groups),
            )
        if len(values) == 4:
            internal, external, guards, managed = values
            return (
                list(internal or []),
                list(external),
                dict(guards),
                frozenset(guards),
                frozenset(guards) if managed else frozenset(),
            )
        internal, external, guards = values
        return (
            list(internal or []),
            list(external),
            dict(guards),
            frozenset(guards),
            frozenset(),
        )
    internal, external = await module.shop_group_scope(session)
    return (list(internal or []), list(external), {}, frozenset(), frozenset())


async def _expire_rows(
    session,
    bot,
    *,
    group_ids=None,
    guards=None,
    governed=None,
    session_factory,
    allow_unmanaged_legacy=True,
):
    """真实到期处理入口；基线没有 ``governed_groups`` 时退回它的全局开关。"""

    module = importlib.import_module("bot.services.point_shop")
    params = inspect.signature(module.expire_due_entitlements).parameters
    kwargs: dict = {"session_factory": session_factory}
    if group_ids is not None:
        kwargs["group_ids"] = list(group_ids)
    if guards is not None:
        kwargs["guards"] = dict(guards)
    if "governed_groups" in params:
        kwargs["governed_groups"] = None if governed is None else frozenset(governed)
        kwargs["allow_unmanaged_legacy"] = allow_unmanaged_legacy
    elif "legacy_fallback" in params:
        # 基线只有"整库有没有来源记录"的全局开关：这正是本轮要证伪的口径。
        kwargs["legacy_fallback"] = not bool(governed)
    return await module.expire_due_entitlements(session, bot=bot, **kwargs)


# ---------------------------------------------------------------------------
# R-B1：自动入口（内部 runner / 外部 CLI）在真实不可逆边界上收口
# ---------------------------------------------------------------------------


class AutomaticEntryAdmissionV1Tests(_PolicyHelperMixin, _OperationsTestCase):
    """读到旧来源之后、真正出站/撤权之前被切换：旧来源必须一个副作用都没有。"""

    async def _switch_checkin_to_internal_with_past_active_from(self, moment) -> None:
        await self._switch("checkin_reminder", "internal")
        async with self.session_factory() as session:
            row = await session.scalar(
                select(_policy_model()).where(
                    _policy_model().group_id == GROUP_A,
                    _policy_model().task_id == "checkin_reminder",
                )
            )
            self.assertIsNotNone(row, "切换后必须有持久策略行")
            row.active_from = moment - timedelta(minutes=5)
            await session.commit()

    async def test_checkin_internal_runner_blocks_a_switch_that_wins_before_dispatch(
        self,
    ) -> None:
        from bot.services import checkin_reminder as checkin_module
        from bot.services.periodic_tasks import CheckinInternalRunner
        from bot.services.task_policy import slot_moment

        day = now_shanghai_naive().date()
        moment = slot_moment(day, 12)
        now = moment + timedelta(minutes=1)
        await self._switch_checkin_to_internal_with_past_active_from(moment)

        runner = CheckinInternalRunner(
            bot=self.bot, session_factory=self.session_factory
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        observed: dict[str, object] = {}
        real_claim = checkin_module.claim_business_dispatch

        async def held(session, **kwargs):
            # 这是真实的出站保护 claim 入口，不是 adapter / run_once 预检查。
            observed["guard_present"] = kwargs.get("guard") is not None
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await real_claim(session, **kwargs)

        with patch.object(checkin_module, "claim_business_dispatch", new=held):
            task = asyncio.create_task(runner.run_once(now=now))
            await asyncio.wait_for(entered.wait(), timeout=5)
            # 独立合法写者：管理员在"读到 internal"之后把该群切回 external。
            await self._switch("checkin_reminder", "external")
            release.set()
            results = await asyncio.wait_for(task, timeout=10)

        self.assertTrue(
            observed.get("guard_present"),
            "内部 runner 的真实出站准入没有携带来源守卫",
        )
        self.assertEqual(self.outbound, [], "来源已切换，旧来源仍然发出了提醒")
        self.assertEqual(
            await self._count(CheckinReminderPost), 0, "被拒绝的时段不应留下占位"
        )
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0]["status"], "send_failed", results)
        self.assertEqual(results[0]["reason"], "guard_blocked", results)

    async def test_checkin_internal_runner_still_sends_when_the_source_holds(self) -> None:
        """正控制：来源没变时同一路径必须真的发出**一条**提醒。"""

        from bot.services.periodic_tasks import CheckinInternalRunner
        from bot.services.task_policy import slot_moment

        day = now_shanghai_naive().date()
        moment = slot_moment(day, 12)
        now = moment + timedelta(minutes=1)
        await self._switch_checkin_to_internal_with_past_active_from(moment)

        runner = CheckinInternalRunner(
            bot=self.bot, session_factory=self.session_factory
        )
        results = await runner.run_once(now=now)
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0]["group_id"], GROUP_A, results)
        self.assertEqual(results[0]["status"], "sent", results)
        self.assertEqual(len(self.outbound), 1, self.outbound)
        self.assertEqual(self.outbound[0][0], GROUP_A)

    async def test_external_cli_entry_drops_a_group_switched_before_its_outbound(
        self,
    ) -> None:
        from bot.services.checkin_reminder import (
            backfill_durable_auto_delete,
            reminder_auto_delete_seconds,
        )
        from bot.services.task_policy import slot_moment
        from bot.utils.telegram import schedule_message_auto_delete_durable

        day = local_today()
        moment = slot_moment(day, 12)
        async with self.session_factory() as session:
            guards, skipped = await _slot_bindings(session, slot=12, day=day)
        self.assertEqual(skipped, [])
        self.assertEqual(sorted(guards), sorted([GROUP_A, GROUP_B]))

        async def send(guard) -> object:
            async with self.session_factory() as session:
                return await _send_slot(
                    session,
                    self.bot,
                    group_id=GROUP_A,
                    slot=12,
                    guard=guard,
                    auto_delete_seconds=reminder_auto_delete_seconds(),
                    queue_auto_delete=schedule_message_auto_delete_durable,
                    backfill_auto_delete=backfill_durable_auto_delete,
                    session_factory=self.session_factory,
                )

        # 读到外部来源之后、真正出站之前，管理员把这个群切成 internal。
        await self._switch("checkin_reminder", "internal")
        blocked = await send(guards[GROUP_A])
        self.assertEqual(self.outbound, [], "旧入口在切换之后仍然发出了提醒")
        self.assertEqual(blocked.status, "send_failed", blocked)
        self.assertEqual(blocked.reason, "guard_blocked", blocked)

        # 正控制：该群回到 external，用**同一份绑定快照之后重新读到**的守卫，
        # 同一路径必须真的发出（含"只发一次"的占位口径）。
        await self._switch("checkin_reminder", "external")
        # 切回 external 会把 active_from 设成"现在"，而 12:00 时段已经过去
        # （future-only 口径，旧时段不补）。这里按父探针的同一口径，把这一群显式
        # 改回"该时段之前就已生效"，才能验证正控制真的会出站。
        model = _policy_model()
        async with self.session_factory() as session:
            row = await session.scalar(
                select(model).where(
                    model.group_id == GROUP_A, model.task_id == "checkin_reminder"
                )
            )
            self.assertIsNotNone(row)
            row.active_from = moment - timedelta(minutes=5)
            row.revision = int(row.revision) + 1
            await session.commit()
        async with self.session_factory() as session:
            guards, _skipped = await _slot_bindings(session, slot=12, day=day)
        self.assertIn(GROUP_A, guards, guards)
        sent = await send(guards[GROUP_A])
        self.assertEqual(sent.status, "sent", sent)
        self.assertEqual(len(self.outbound), 1, self.outbound)

    async def test_shop_internal_service_blocks_a_switch_before_the_real_revoke(
        self,
    ) -> None:
        from bot.services import point_shop as shop_module

        row_id = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        entered = asyncio.Event()
        release = asyncio.Event()
        observed: dict[str, object] = {}
        real_claim = shop_module._claim_expiry_dispatch

        async def held(session_factory, *, item, expires_at, guard=None):
            observed["guard_present"] = guard is not None
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await real_claim(
                session_factory, item=item, expires_at=expires_at, guard=guard
            )

        service = shop_module.ShopExpiryService(
            bot=self.bot, session_factory=self.session_factory
        )
        with patch.object(shop_module, "_claim_expiry_dispatch", new=held):
            task = asyncio.create_task(service.run_once())
            await asyncio.wait_for(entered.wait(), timeout=5)
            # 商店默认 internal，读到之后被切到 external（外部 cron 接手）。
            await self._switch("shop_expire", "external")
            release.set()
            outcomes = await asyncio.wait_for(task, timeout=10)

        self.assertTrue(
            observed.get("guard_present"),
            "内部到期服务的真实出站准入没有携带来源守卫",
        )
        self.assertTrue(outcomes, "本轮应当处理到这一条到期权益（并如实拒绝）")
        self.assertTrue(all(outcome.guard_blocked for outcome in outcomes), outcomes)
        self.assertEqual(
            self.bot.set_chat_member_tag.await_count, 0, "切换之后仍然撤销了头衔"
        )
        self.assertEqual(self.outbound, [], "切换之后仍然发出了到期提醒")
        async with self.session_factory() as session:
            remaining = await session.get(shop_module.MemberEntitlement, int(row_id))
        self.assertIsNotNone(remaining, "被拒绝的权益行不能被删除")

    async def test_shop_external_cli_bindings_block_a_stale_source(self) -> None:
        from bot.db.models import MemberEntitlement

        row_id = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        # 默认 internal：外部 CLI 一条也不处理（真实 no-op）。
        async with self.session_factory() as session:
            _internal, external, _guards, _governed, _policy_groups = await _shop_plan(
                session
            )
        self.assertEqual(external, [])

        # 管理员把 A 群切给外部 cron：外部入口拿到的是这一版守卫。
        await self._switch("shop_expire", "external")
        async with self.session_factory() as session:
            _internal, external, guards, _governed, _policy_groups = await _shop_plan(
                session
            )
        self.assertEqual(external, [GROUP_A], external)

        # 读到外部来源之后、真正撤销之前，管理员又切回 internal。
        await self._switch("shop_expire", "internal")
        async with self.session_factory() as session:
            outcomes = await _expire_rows(
                session,
                self.bot,
                group_ids=external,
                guards=guards,
                session_factory=self.session_factory,
            )
        self.assertEqual(
            self.bot.set_chat_member_tag.await_count,
            0,
            f"旧入口在切换之后仍然撤销了权益：{outcomes!r}",
        )
        self.assertEqual(self.outbound, [], f"旧入口在切换之后仍然发了通知：{outcomes!r}")
        async with self.session_factory() as session:
            remaining = await session.get(MemberEntitlement, row_id)
        self.assertIsNotNone(remaining, "旧入口在切换之后仍然删除了权益行")
        self.assertTrue(
            all(outcome.guard_blocked for outcome in outcomes),
            f"旧入口在切换之后仍然动了权益：{outcomes!r}",
        )

    async def test_shop_revoked_group_with_a_persisted_source_is_not_legacy_bypassed(
        self,
    ) -> None:
        """父 v4：已经写过持久来源、随后被撤权的群，绝不能降级成 legacy 全量。"""

        from bot.db.models import AuthorizedGroup, MemberEntitlement
        from bot.services import point_shop as shop_module

        row_id = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        # 管理员先把 A 群交给外部 cron（留下真实持久来源记录）……
        await self._switch("shop_expire", "external")
        # ……随后这个群的资格被撤掉（机器人被移出/停用），策略行仍然留着。
        async with self.session_factory() as session:
            await session.execute(
                update(AuthorizedGroup)
                .where(AuthorizedGroup.group_id == GROUP_A)
                .values(bot_present=False)
            )
            await session.commit()

        # 同一个库现在没有任何"已授权且来源为 external"的群：它不能因此
        # 退化成"没有策略 ⇒ 全量无约束"，把撤权群的到期权益照旧撤销。
        service = shop_module.ShopExpiryService(
            bot=self.bot, session_factory=self.session_factory
        )
        outcomes = await service.run_once()
        self.assertEqual(
            self.bot.set_chat_member_tag.await_count,
            0,
            f"撤权群被 legacy 全量绕过了：{outcomes!r}",
        )
        self.assertEqual(self.outbound, [], f"撤权群仍然收到了到期提醒：{outcomes!r}")
        async with self.session_factory() as session:
            remaining = await session.get(MemberEntitlement, row_id)
        self.assertIsNotNone(remaining, "撤权群的权益行被删除了")

        # 外部 CLI 的同一份计划也不许把撤权群当成 legacy。
        async with self.session_factory() as session:
            internal, external, guards, governed, policy_groups = await _shop_plan(session)
        self.assertIn(GROUP_A, governed, "写过来源记录、且仍有身份行的群必须算已治理")
        self.assertIn(GROUP_A, policy_groups, policy_groups)
        self.assertNotIn(GROUP_A, internal, internal)
        self.assertNotIn(GROUP_A, external, external)
        async with self.session_factory() as session:
            outcomes = await _expire_rows(
                session,
                self.bot,
                group_ids=external,
                guards=guards,
                governed=governed,
                session_factory=self.session_factory,
                allow_unmanaged_legacy=False,
            )
        self.assertEqual(outcomes, [], outcomes)
        self.assertEqual(self.bot.set_chat_member_tag.await_count, 0)
        async with self.session_factory() as session:
            remaining = await session.get(MemberEntitlement, row_id)
        self.assertIsNotNone(remaining, "外部入口仍然动了撤权群的权益")

    async def test_shop_legacy_full_scope_is_kept_without_any_policy_record(
        self,
    ) -> None:
        """正控制：一行来源记录都没有的老库，保持改造前的全量扫描口径。"""

        from bot.db.models import MemberEntitlement
        from bot.services import point_shop as shop_module

        row_id = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        async with self.session_factory() as session:
            scope, external = await shop_module.shop_group_scope(session)
            internal, external_from_plan, guards, governed, policy_groups = (
                await _shop_plan(session)
            )
        self.assertIsNone(scope, "老库的只读兼容视图必须仍返回全量口径（None）")
        self.assertEqual(external, [])
        self.assertEqual(external_from_plan, [])
        self.assertFalse(policy_groups, "一行策略都没有时不得声称有来源记录")
        self.assertIn(GROUP_A, governed, "有身份行的群在 v3 里算已治理（带守卫）")
        self.assertIn(GROUP_A, guards, "有身份行的群必须有守卫")

        service = shop_module.ShopExpiryService(
            bot=self.bot, session_factory=self.session_factory
        )
        outcomes = await service.run_once()
        self.assertTrue(outcomes, f"老库的到期权益应当照旧处理：{outcomes!r}")
        self.assertEqual(self.bot.set_chat_member_tag.await_count, 1, outcomes)
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(MemberEntitlement, row_id))

    async def test_shop_missing_guard_for_a_governed_group_is_fail_closed(self) -> None:
        """缺 guard 的**已治理**群 fail-closed；历史群（无身份无策略）才走 legacy。"""

        from bot.db.models import MemberEntitlement

        row_id = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        async with self.session_factory() as session:
            skipped = await _expire_rows(
                session,
                self.bot,
                guards={},
                governed={GROUP_A},
                session_factory=self.session_factory,
            )
        self.assertEqual(
            self.bot.set_chat_member_tag.await_count,
            0,
            f"已治理群缺 guard 时仍然撤销了权益：{skipped!r}",
        )
        self.assertEqual(self.outbound, [], f"已治理群缺 guard 时仍然发了通知：{skipped!r}")
        async with self.session_factory() as session:
            self.assertIsNotNone(
                await session.get(MemberEntitlement, row_id),
                "已治理群缺 guard 时仍然删除了权益行",
            )

        # 同一条权益、同一个真实入口：群不在 governed 里（纯历史群）仍然照旧处理，
        # 证明上一段是"逐群 fail-closed"，而不是把商店通道整个关掉。
        async with self.session_factory() as session:
            outcomes = await _expire_rows(
                session,
                self.bot,
                guards={},
                governed=set(),
                session_factory=self.session_factory,
            )
        self.assertIn(
            GROUP_A, [int(outcome.item.group_id) for outcome in outcomes], outcomes
        )
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(MemberEntitlement, row_id))

    async def test_shop_mixed_internal_database_processes_history_and_managed(
        self,
    ) -> None:
        """父 v5 混库：managed 群（internal）与纯历史群共处一库时必须都处理。"""

        from bot.db.models import MemberEntitlement
        from bot.services import point_shop as shop_module

        legacy_group = -1009999999001
        legacy_row = await self._seed_entitlement(group_id=legacy_group, user_id=55)
        managed_row = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        await self._switch("shop_expire", "external")
        await self._switch("shop_expire", "internal")

        async with self.session_factory() as session:
            internal, external, guards, governed, policy_groups = await _shop_plan(session)
        self.assertIn(GROUP_A, internal, internal)
        self.assertEqual(external, [])
        self.assertTrue(policy_groups)
        self.assertIn(GROUP_A, governed, governed)
        self.assertNotIn(legacy_group, governed, "纯历史群不能有身份/策略")
        self.assertNotIn(legacy_group, guards, "纯历史群没有守卫（改造前的 legacy 口径）")

        service = shop_module.ShopExpiryService(
            bot=self.bot, session_factory=self.session_factory
        )
        outcomes = await service.run_once()
        self.assertEqual(
            self.bot.set_chat_member_tag.await_count,
            2,
            f"混库里 managed 群与纯历史群都必须被处理：{outcomes!r}",
        )
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(MemberEntitlement, managed_row))
            self.assertIsNone(await session.get(MemberEntitlement, legacy_row))

    async def test_shop_mixed_revoked_managed_group_does_not_hide_the_history_group(
        self,
    ) -> None:
        """父 v5 混库：撤权 managed 群 fail-closed，但同库纯历史群仍要处理。"""

        from bot.db.models import AuthorizedGroup, MemberEntitlement
        from bot.services import point_shop as shop_module

        legacy_group = -1009999999002
        legacy_row = await self._seed_entitlement(group_id=legacy_group, user_id=55)
        managed_row = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        await self._switch("shop_expire", "external")
        async with self.session_factory() as session:
            row = await session.get(AuthorizedGroup, GROUP_A)
            row.bot_present = False
            await session.commit()

        async with self.session_factory() as session:
            internal, external, guards, governed, policy_groups = await _shop_plan(session)
        self.assertIn(GROUP_A, governed, governed)
        self.assertIn(GROUP_A, policy_groups, policy_groups)
        self.assertNotIn(GROUP_A, internal, "撤权群不能还留在内部范围里")
        self.assertNotIn(GROUP_A, external, "撤权群也不能算外部入口的范围")

        service = shop_module.ShopExpiryService(
            bot=self.bot, session_factory=self.session_factory
        )
        outcomes = await service.run_once()
        self.assertEqual(
            self.bot.set_chat_member_tag.await_count,
            1,
            f"只该撤掉纯历史群那一条（撤权 managed 群必须保留）：{outcomes!r}",
        )
        self.assertEqual(
            [int(user) for user, _text in self.outbound],
            [55],
            f"撤权 managed 群不得发出通知（只该有纯历史群那一条）：{self.outbound!r}",
        )
        async with self.session_factory() as session:
            self.assertIsNotNone(
                await session.get(MemberEntitlement, managed_row),
                "撤权 managed 群被 legacy 全量绕过了",
            )
            self.assertIsNone(await session.get(MemberEntitlement, legacy_row))

    async def test_shop_external_cli_never_touches_a_pure_history_group(self) -> None:
        """外部 CLI 按外部范围处理；纯历史群从来不归它（改造前也不归）。"""

        from bot.db.models import MemberEntitlement

        legacy_group = -1009999999003
        legacy_row = await self._seed_entitlement(group_id=legacy_group, user_id=55)
        managed_row = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        await self._switch("shop_expire", "external")
        async with self.session_factory() as session:
            _internal, external, guards, governed, _policy_groups = await _shop_plan(session)
        self.assertEqual(external, [GROUP_A], external)

        async with self.session_factory() as session:
            outcomes = await _expire_rows(
                session,
                self.bot,
                group_ids=external,
                guards=guards,
                governed=governed,
                session_factory=self.session_factory,
                allow_unmanaged_legacy=False,
            )
        self.assertEqual(
            [int(outcome.item.group_id) for outcome in outcomes], [GROUP_A], outcomes
        )
        self.assertEqual(self.bot.set_chat_member_tag.await_count, 1, outcomes)
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(MemberEntitlement, managed_row))
            self.assertIsNotNone(
                await session.get(MemberEntitlement, legacy_row),
                "外部入口动了纯历史群",
            )

    async def test_shop_policy_read_failure_never_degrades_to_a_full_scan(self) -> None:
        """读策略失败：这一轮什么都不做，绝不按全量跑。"""

        from bot.db.models import MemberEntitlement
        from bot.services import point_shop as shop_module
        from bot.services import task_policy as policy_module

        row_id = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        service = shop_module.ShopExpiryService(
            bot=self.bot, session_factory=self.session_factory
        )
        with patch.object(
            policy_module,
            "load_task_policies",
            new=AsyncMock(side_effect=RuntimeError("synthetic policy read failure")),
        ):
            with self.assertRaises(RuntimeError):
                await service.run_once()
        self.assertEqual(
            self.bot.set_chat_member_tag.await_count, 0, "读策略失败时仍然撤了权益"
        )
        self.assertEqual(self.outbound, [], "读策略失败时仍然发了通知")
        async with self.session_factory() as session:
            self.assertIsNotNone(await session.get(MemberEntitlement, row_id))


# ---------------------------------------------------------------------------
# R-B2 / R-B3 / R-B4：周计划身份绑定、错过窗口、入队故障
# ---------------------------------------------------------------------------


class WeeklyPlanBindingV1Tests(_PolicyHelperMixin, _OperationsTestCase):
    """旧 tick 不能靠"重新读最新 revision"获得授权。"""

    async def _plan_with_targets(self) -> tuple[datetime, dict, datetime, int]:
        """开启一个未来计划并造一个达标成员；返回 (now, 落库计划, 锚点, 版本)。"""

        now = now_shanghai_naive()
        week_start, _week_end = activity.last_complete_week(now)
        await self._seed_activity(week_start=week_start, user_id=7)
        weekday = (now.weekday() + 2) % 7
        await self._set_plan(enabled=True, weekday=weekday, minute=10 * 60)
        row = await self._policy_row("weekly_report")
        self.assertTrue(row.plan_enabled)
        anchor = _parse_stamp(row.anchor_at) if isinstance(row.anchor_at, str) else row.anchor_at
        return now, {"weekday": int(row.plan_weekday), "minute": int(row.plan_minute)}, anchor, int(row.revision)

    def _runner(self) -> "periodic_tasks.WeeklyPlanRunner":
        return periodic_tasks.WeeklyPlanRunner(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            manager=self.manager,
        )

    async def _due_anchor(self, now: datetime) -> None:
        """把持久锚点挪到"刚刚到点"（容差窗口内）。"""

        await self._set_anchor(now - timedelta(minutes=1))

    async def test_plan_disabled_before_the_real_enqueue_cannot_bind_the_new_revision(
        self,
    ) -> None:
        from bot.services.task_policy import next_occurrence

        now, plan, anchor, revision = await self._plan_with_targets()
        await self._due_anchor(now)
        entered = asyncio.Event()
        release = asyncio.Event()
        observed: dict[str, object] = {}
        real_enqueue = operations_runs.enqueue_scheduled_run

        async def held(session_factory, settings, manager, **kwargs):
            # 真实入队入口：计划身份必须随调用一起传下来。
            observed.setdefault("plan_present", kwargs.get("plan") is not None)
            observed.setdefault(
                "plan_revision", int(getattr(kwargs.get("plan"), "revision", -1))
            )
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await real_enqueue(session_factory, settings, manager, **kwargs)

        with patch.object(operations_runs, "enqueue_scheduled_run", new=held):
            task = asyncio.create_task(self._runner().run_once(now=now))
            await asyncio.wait_for(entered.wait(), timeout=5)
            # 独立合法写者：管理员在"读到已开启的旧版本"之后关闭计划。
            await self._set_plan(enabled=False)
            release.set()
            results = await asyncio.wait_for(task, timeout=15)

        self.assertTrue(observed.get("plan_present"), "入队没有携带被调度计划的身份")
        self.assertEqual(observed.get("plan_revision"), revision)
        self.assertEqual(len(results), 1, results)
        self.assertFalse(results[0]["scheduled"], results)
        self.assertFalse(results[0]["anchor_advanced"], results)
        self.assertEqual(results[0]["state"], "plan_changed", results)
        self.assertEqual(await self._count(OperationsRun), 0, "已关闭的计划仍然建了 run")
        self.assertEqual(self.outbound, [], "已关闭的计划仍然发出了周报")
        self.assertEqual(await self._award_rows(), [], "已关闭的计划仍然发了积分")
        row = await self._policy_row("weekly_report")
        self.assertFalse(row.plan_enabled)
        self.assertIsNone(row.anchor_at)
        self.assertGreater(int(row.revision), revision)
        # 锚点没有被旧 tick 复活成"下一期"。
        self.assertNotEqual(
            row.anchor_at, next_occurrence(now, plan["weekday"], plan["minute"])
        )

    async def test_normal_due_plan_binds_its_own_revision_and_stable_key(self) -> None:
        from bot.services.task_policy import next_occurrence

        now, plan, anchor, revision = await self._plan_with_targets()
        await self._due_anchor(now)
        row = await self._policy_row("weekly_report")
        expected_key = _stable_plan_key(
            group_id=GROUP_A,
            task_id="weekly_report",
            anchor_at=_parse_stamp(row.anchor_at),
        )

        results = await self._runner().run_once(now=now)
        self.assertEqual(len(results), 1, results)
        self.assertTrue(results[0]["scheduled"], results)
        # 稳定计划键是 R-B4 的对账基础：同一个锚点必须永远映射到同一个键，
        # 否则"提交成功但回执丢失"的重试无法识别"这一期已经入队"。
        self.assertEqual(results[0]["run_key"], expected_key, "计划 run 必须用稳定键")
        self.assertTrue(results[0]["anchor_advanced"], results)
        self.assertEqual(results[0]["state"], "fired", results)
        run = await self._wait(results[0]["run_key"])
        self.assertEqual(run.status, 200, await run.text())
        payload = (await run.json())["run"]
        self.assertEqual(payload["source"], "schedule")
        self.assertEqual(payload["status"], "succeeded", payload)
        self.assertEqual(int(payload["policy_revision"]), revision, payload)
        self.assertEqual(self.outbound, [(GROUP_A, self.outbound[0][1])])
        self.assertEqual(await self._award_rows(), [(7, 25)])
        row = await self._policy_row("weekly_report")
        self.assertEqual(
            _parse_stamp(row.anchor_at),
            next_occurrence(now, plan["weekday"], plan["minute"]),
        )
        self.assertEqual(int(row.revision), revision, "推进锚点不得自升版本")

    async def test_restart_with_an_overdue_anchor_advances_without_catchup(self) -> None:
        from bot.services.task_policy import next_occurrence

        now, plan, _anchor, revision = await self._plan_with_targets()
        # 进程停机很久：持久锚点已经是 8 天前。
        await self._set_anchor(now - timedelta(days=8))

        results = await self._runner().run_once(now=now)
        self.assertEqual(len(results), 1, results)
        self.assertFalse(results[0]["scheduled"], results)
        self.assertEqual(results[0]["state"], "missed", results)
        self.assertEqual(await self._count(OperationsRun), 0)
        self.assertEqual(self.outbound, [], "重启补发了上一周的周报")
        self.assertEqual(await self._award_rows(), [], "重启补发了上一周的积分")
        row = await self._policy_row("weekly_report")
        self.assertEqual(
            _parse_stamp(row.anchor_at),
            next_occurrence(now, plan["weekday"], plan["minute"]),
            "错过窗口后必须把锚点持久推进到严格未来",
        )
        self.assertEqual(int(row.revision), revision)
        # 再跑一次：已经是未来锚点，什么都不做。
        self.assertEqual(await self._runner().run_once(now=now), [])
        self.assertEqual(await self._count(OperationsRun), 0)

    async def test_task_status_reports_a_missed_window_honestly(self) -> None:
        from bot.services.task_policy import next_occurrence

        now, plan, _anchor, _revision = await self._plan_with_targets()
        await self._set_anchor(now - timedelta(days=8))
        response = await self._status()
        self.assertEqual(response.status, 200, await response.text())
        weekly = _status_of((await response.json())["task_status"], "weekly_report")
        self.assertEqual(weekly["next_run"]["reason"], "plan_window_missed")
        self.assertEqual(
            _parse_stamp(weekly["next_run"]["at"]),
            next_occurrence(now, plan["weekday"], plan["minute"]),
        )

    async def test_enqueue_failure_is_not_silently_consumed_and_retries(self) -> None:
        now, _plan, _anchor, revision = await self._plan_with_targets()
        await self._due_anchor(now)
        boom = RuntimeError("synthetic pre-enqueue storage failure")
        failures: list[dict] = []

        async def exploding(*args, **kwargs):
            raise boom

        from bot.services import periodic_tasks as runner_module

        with patch.object(operations_runs, "enqueue_scheduled_run", new=exploding), patch.object(
            runner_module,
            "record_background_failure",
            new=lambda **kwargs: (failures.append(kwargs) or 1),
        ):
            results = await self._runner().run_once(now=now)
        self.assertEqual(len(results), 1, results)
        self.assertFalse(results[0]["scheduled"], results)
        self.assertFalse(results[0]["anchor_advanced"], results)
        self.assertEqual(results[0]["state"], "enqueue_unconfirmed", results)
        self.assertEqual(await self._count(OperationsRun), 0)
        self.assertEqual(self.outbound, [])
        self.assertEqual(await self._award_rows(), [])
        self.assertTrue(failures, "入队明确失败必须留下健康失败证据")
        self.assertEqual(failures[0]["service"], "weekly_report_internal_plan")
        self.assertIn("RuntimeError", type(failures[0]["error"]).__name__)
        row = await self._policy_row("weekly_report")
        self.assertEqual(
            int(row.revision), revision, "入队失败不得改动策略版本"
        )
        # 锚点没有被静默消耗掉：还是那个到点的旧锚点，容差窗口内可以重试。
        self.assertLess(_parse_stamp(row.anchor_at), now)

        # 故障消失之后重试：同一期正常入队、正常发分、正常推进锚点（只发一次）。
        results = await self._runner().run_once(now=now)
        self.assertEqual(len(results), 1, results)
        self.assertTrue(results[0]["scheduled"], results)
        self.assertEqual(results[0]["state"], "fired", results)
        await self._wait(results[0]["run_key"])
        self.assertEqual(len(self.outbound), 1)
        self.assertEqual(await self._award_rows(), [(7, 25)])
        self.assertGreater(_parse_stamp((await self._policy_row("weekly_report")).anchor_at), now)

    # ---- R-B4：真实 commit ACK 边界必须由产品消费者按稳定键恢复 ----

    async def test_real_commit_ack_loss_is_recovered_by_the_product_consumer(
        self,
    ) -> None:
        """真实 INSERT + 真实 commit 之后回执丢失：唯一 run 必须被真正启动一次。

        这里不手动调用 ``_spawn_execution``、也不把整条 enqueue 换成抛异常的替身：
        只在**真实 SQLite 边界**上（执行到 INSERT operations_runs、commit 已经成功
        返回之后）抛一次异常，模拟"提交成功但调用方拿不到回执"。恢复必须由产品
        消费者（稳定计划键对账）完成。
        """

        from bot.db.sqlite_session import SQLiteSafeAsyncSession
        from bot.services.task_policy import next_occurrence

        now, plan, _anchor, _revision = await self._plan_with_targets()
        await self._due_anchor(now)
        row = await self._policy_row("weekly_report")
        stable_key = _stable_plan_key(
            group_id=GROUP_A,
            task_id="weekly_report",
            anchor_at=_parse_stamp(row.anchor_at),
        )

        execute = SQLiteSafeAsyncSession.execute
        commit = SQLiteSafeAsyncSession.commit
        real_spawn = operations_runs._spawn_execution
        observed = {"insert_seen": 0, "ack_failed": 0, "spawn_calls": 0}
        marked: set[int] = set()

        async def wrapped_execute(session, statement, *args, **kwargs):
            outcome = await execute(session, statement, *args, **kwargs)
            if str(statement).upper().startswith("INSERT INTO OPERATIONS_RUNS "):
                marked.add(id(session))
                observed["insert_seen"] += 1
            return outcome

        async def wrapped_commit(session, *args, **kwargs):
            await commit(session, *args, **kwargs)
            if id(session) in marked:
                marked.discard(id(session))
                observed["ack_failed"] += 1
                raise RuntimeError(
                    "synthetic acknowledgment lost after real OperationsRun commit"
                )

        def wrapped_spawn(run_id, *, bot=None):
            observed["spawn_calls"] += 1
            return real_spawn(run_id, bot=bot)

        with patch.object(
            SQLiteSafeAsyncSession, "execute", new=wrapped_execute
        ), patch.object(
            SQLiteSafeAsyncSession, "commit", new=wrapped_commit
        ), patch.object(operations_runs, "_spawn_execution", new=wrapped_spawn):
            first = await self._runner().run_once(now=now)

        self.assertEqual(observed["insert_seen"], 1, observed)
        self.assertEqual(observed["ack_failed"], 1, observed)
        # 回执丢了，但记录是真的：唯一一条，而且正是稳定计划键。
        rows = await self._plan_run_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0][0], stable_key, rows)
        # 恢复是真启动（spawn 一次），不是把异常当成"没发生"再人工补跑。
        self.assertEqual(observed["spawn_calls"], 1, observed)
        self.assertEqual(len(first), 1, first)
        self.assertTrue(first[0]["scheduled"], first)
        self.assertEqual(first[0]["state"], "started", first)
        self.assertEqual(first[0]["run_key"], stable_key, first)
        self.assertTrue(first[0]["anchor_advanced"], first)

        settled = await self._wait(stable_key)
        self.assertEqual(settled.status, 200, await settled.text())
        payload = (await settled.json())["run"]
        self.assertEqual(payload["status"], "succeeded", payload)
        self.assertEqual(len(self.outbound), 1, self.outbound)
        self.assertEqual(await self._award_rows(), [(7, 25)])
        self.assertEqual(
            _parse_stamp((await self._policy_row("weekly_report")).anchor_at),
            next_occurrence(now, plan["weekday"], plan["minute"]),
        )
        # 同一期不会再被当成本期重跑一次。
        self.assertEqual(await self._runner().run_once(now=now), [])
        self.assertEqual(len(self.outbound), 1, self.outbound)
        self.assertEqual(await self._award_rows(), [(7, 25)])

    async def test_failure_before_the_real_commit_keeps_the_anchor_and_retries(
        self,
    ) -> None:
        """提交之前就失败：不能留下记录、不能消耗当期，容差内必须能重试成功。"""

        from bot.db.sqlite_session import SQLiteSafeAsyncSession

        now, _plan, _anchor, revision = await self._plan_with_targets()
        await self._due_anchor(now)
        execute = SQLiteSafeAsyncSession.execute
        observed = {"insert_seen": 0}

        async def failing_execute(session, statement, *args, **kwargs):
            if str(statement).upper().startswith("INSERT INTO OPERATIONS_RUNS "):
                observed["insert_seen"] += 1
                raise RuntimeError("synthetic insert failure before the real commit")
            return await execute(session, statement, *args, **kwargs)

        from bot.services import periodic_tasks as runner_module

        failures: list[dict] = []
        with patch.object(
            SQLiteSafeAsyncSession, "execute", new=failing_execute
        ), patch.object(
            runner_module,
            "record_background_failure",
            new=lambda **kwargs: (failures.append(kwargs) or 1),
        ):
            first = await self._runner().run_once(now=now)
        self.assertEqual(observed["insert_seen"], 1, observed)
        self.assertEqual(await self._count(OperationsRun), 0, "提交前失败不得留下记录")
        self.assertEqual(len(first), 1, first)
        self.assertFalse(first[0]["scheduled"], first)
        self.assertFalse(first[0]["anchor_advanced"], first)
        self.assertEqual(first[0]["state"], "enqueue_unconfirmed", first)
        self.assertEqual(self.outbound, [])
        self.assertEqual(await self._award_rows(), [])
        self.assertTrue(failures, "入队明确失败必须留下健康失败证据")
        row = await self._policy_row("weekly_report")
        self.assertEqual(int(row.revision), revision, "入队失败不得改动策略版本")
        self.assertLess(_parse_stamp(row.anchor_at), now, "当期不得被静默消耗")

        # 故障消失之后重试：同一期正常入队、正常发分、正常推进锚点（只发一次）。
        second = await self._runner().run_once(now=now)
        self.assertEqual(len(second), 1, second)
        self.assertTrue(second[0]["scheduled"], second)
        self.assertEqual(second[0]["state"], "fired", second)
        await self._wait(second[0]["run_key"])
        self.assertEqual(len(self.outbound), 1, self.outbound)
        self.assertEqual(await self._award_rows(), [(7, 25)])

    async def test_ack_loss_after_the_consumer_spawned_does_not_start_a_second_run(
        self,
    ) -> None:
        """已经真的启动（spawn 之后）才丢回执：绝不能再来第二次执行。"""

        now, _plan, _anchor, _revision = await self._plan_with_targets()
        await self._due_anchor(now)
        real_payload = operations_runs._run_payload
        real_spawn = operations_runs._spawn_execution
        calls = {"payload": 0, "spawn": 0}

        def failing_payload(run):
            calls["payload"] += 1
            if calls["payload"] == 1:
                # 这条调用发生在 _spawn_execution 之后：run 已经在跑，但调用方
                # 拿不到回执。
                raise RuntimeError("synthetic response lost after the run started")
            return real_payload(run)

        def counting_spawn(run_id, *, bot=None):
            calls["spawn"] += 1
            return real_spawn(run_id, bot=bot)

        with patch.object(
            operations_runs, "_run_payload", new=failing_payload
        ), patch.object(operations_runs, "_spawn_execution", new=counting_spawn):
            first = await self._runner().run_once(now=now)

        self.assertEqual(calls["spawn"], 1, calls)
        self.assertEqual(len(first), 1, first)
        self.assertTrue(first[0]["scheduled"], first)
        self.assertIn(first[0]["state"], {"in_flight", "settled"}, first)
        rows = await self._plan_run_rows()
        self.assertEqual(len(rows), 1, rows)
        settled = await self._wait(first[0]["run_key"])
        self.assertEqual((await settled.json())["run"]["status"], "succeeded")
        self.assertEqual(len(self.outbound), 1, self.outbound)
        self.assertEqual(await self._award_rows(), [(7, 25)])

    async def test_reconciliation_beats_a_preview_without_targets(self) -> None:
        """目标已经被消费、预览说"无目标"时，稳定键上的记录仍必须被对账接管。"""

        from bot.services.operations_runs import TaskPreview

        now, _plan, _anchor, _revision = await self._plan_with_targets()
        await self._due_anchor(now)
        async with self.session_factory() as session:
            view = await task_policy.load_policy(
                session, group_id=GROUP_A, task_id="weekly_report"
            )
        no_spawn = lambda *args, **kwargs: None  # noqa: E731
        # 模拟"提交成功但进程在启动执行之前死掉"：记录持久存在，但没有消费者。
        with patch.object(operations_runs, "_spawn_execution", new=no_spawn):
            created = await operations_runs.enqueue_scheduled_run(
                self.session_factory,
                self.settings,
                self.manager,
                task_id="weekly_report",
                group_id=GROUP_A,
                params={"days": 7},
                bot=self.bot,
                now=now,
                plan=view,
            )
        self.assertTrue(created["scheduled"], created)
        rows = await self._plan_run_rows()
        self.assertEqual(rows, [(created["run_key"], "accepted")], rows)

        # 这一期的目标已经被消费掉：真实预览现在只会返回"无目标"。
        empty = TaskPreview(status="ok", targets=(), reason="no_targets", detail="预览没有目标")
        adapter = operations_runs.get_task("weekly_report")
        with patch.object(type(adapter), "preview", new=AsyncMock(return_value=empty)):
            recovered = await self._runner().run_once(now=now)
        self.assertEqual(len(recovered), 1, recovered)
        self.assertEqual(recovered[0]["state"], "started", recovered)
        self.assertTrue(recovered[0]["scheduled"], recovered)
        self.assertEqual(recovered[0]["run_key"], created["run_key"])
        settled = await self._wait(created["run_key"])
        self.assertEqual((await settled.json())["run"]["status"], "succeeded")
        self.assertEqual(len(self.outbound), 1, self.outbound)
        self.assertEqual(await self._award_rows(), [(7, 25)])

    async def test_two_ticks_at_the_same_anchor_execute_exactly_one_run(self) -> None:
        """同一个锚点被两个 runner 同时抢：只允许一条 run、一份效果。"""

        now, _plan, _anchor, _revision = await self._plan_with_targets()
        await self._due_anchor(now)
        batches = await asyncio.gather(
            self._runner().run_once(now=now),
            self._runner().run_once(now=now),
        )
        results = [item for batch in batches for item in batch]
        self.assertTrue(results, results)
        self.assertLessEqual(
            sum(1 for item in results if item["state"] == "fired"),
            1,
            f"同一期被抢出了多次 fired：{results!r}",
        )
        rows = await self._plan_run_rows()
        self.assertEqual(len(rows), 1, rows)
        settled = await self._wait(rows[0][0])
        self.assertEqual((await settled.json())["run"]["status"], "succeeded")
        self.assertEqual(len(self.outbound), 1, self.outbound)
        self.assertEqual(await self._award_rows(), [(7, 25)])

    async def test_a_restart_recovers_a_never_started_plan_run(self) -> None:
        """进程重启（全新 runner 实例）后，那条从未开始的 run 必须被接管。"""

        now, _plan, _anchor, _revision = await self._plan_with_targets()
        await self._due_anchor(now)
        async with self.session_factory() as session:
            view = await task_policy.load_policy(
                session, group_id=GROUP_A, task_id="weekly_report"
            )
        no_spawn = lambda *args, **kwargs: None  # noqa: E731
        with patch.object(operations_runs, "_spawn_execution", new=no_spawn):
            created = await operations_runs.enqueue_scheduled_run(
                self.session_factory,
                self.settings,
                self.manager,
                task_id="weekly_report",
                group_id=GROUP_A,
                params={"days": 7},
                bot=self.bot,
                now=now,
                plan=view,
            )
        self.assertTrue(created["scheduled"], created)
        self.assertEqual(
            await self._plan_run_rows(), [(created["run_key"], "accepted")]
        )

        # 重启：全新实例（不共享上一进程的本地活跃表），只带持久状态。
        operations_runs._ACTIVE_RUNS.clear()
        results = await self._runner().run_once(now=now)
        self.assertEqual(len(results), 1, results)
        self.assertEqual(results[0]["state"], "started", results)
        settled = await self._wait(created["run_key"])
        self.assertEqual((await settled.json())["run"]["status"], "succeeded")
        self.assertEqual(len(self.outbound), 1, self.outbound)
        self.assertEqual(await self._award_rows(), [(7, 25)])



# ---------------------------------------------------------------------------
# R-B5：真实 startup 装配（父做"抑制目标 worker 启动"的 mutation 时必须红）
# ---------------------------------------------------------------------------


class _StartupFakeBot:
    """只替末端 transport 的假机器人：其余方法一律 no-op 协程。"""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.me = AsyncMock(
            return_value=SimpleNamespace(
                id=1, username="fixture", full_name="Fixture", first_name="Fixture"
            )
        )
        self.get_me = AsyncMock(return_value=SimpleNamespace(username="fixture"))
        self.session = SimpleNamespace(close=AsyncMock())
        self.send_message = AsyncMock(side_effect=self._record)

    async def _record(self, chat_id, text, **kwargs):
        self.sent.append((int(chat_id), str(text)))
        return SimpleNamespace(
            message_id=1000 + len(self.sent), chat=SimpleNamespace(id=int(chat_id))
        )

    async def _noop(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._noop


class RealStartupRunnerWiringTests(_PolicyHelperMixin, _OperationsTestCase):
    """真实 ``bot.__main__.main()``：两个常驻 worker 真进入、真消费、真结束。

    **清理证据不能靠 await_count**：``AsyncMock`` 抛异常时计数一样增长。这里用夹具
    真实 ``init_db`` 出来的引擎——也就是 main 的 ``session_factory`` 真正绑定的那个
    引擎（同一个临时库）——把"成功完成"记在真实 ``dispose()`` **返回之后**；
    产品清理捕获到的异常由 ``bot.__main__.log`` 上那条 ``"%s cleanup failed"``
    反映。父的 cleanup-mutation 探针给 ``dispose`` 注入 ``side_effect`` 时，本文件
    必须红（注入后 ``dispose`` 的真实实现根本不会跑，completion 记不上）。
    """

    async def _run_real_main(self, *, fail_bot_close: bool) -> dict[str, object]:
        import bot.__main__ as main_module
        import bot.services.memory_holder as memory_holder_module
        from bot import loader as loader_module
        from bot.services import group_budget as budget_module
        from bot.services import periodic_tasks as runner_module
        from tests.test_alert_main_failure_cleanup import _FakeDispatcher

        # 真实 main 会把进程级的 memory holder 装配起来，而它没有"反初始化"入口。
        # 本用例是唯一跑真实 main 的地方，必须自己把这项全局状态还原，否则后续
        # 用例会突然看到"记忆服务已就绪"。这是测试卫生，不是产品语义。
        previous_holder = memory_holder_module._instance
        self.addCleanup(setattr, memory_holder_module, "_instance", previous_holder)

        # 进程级预算服务/告警钩子同理：产品关停会把成本护栏指回基线，这里再兜一层，
        # 保证本用例绝不会把临时库泄漏给后续用例（否则后续摘要预算查询会报
        # ``no such table: group_budgets``）。
        baseline_budget_service = getattr(budget_module, "_SHARED_SERVICE", None)
        baseline_budget_enqueuer = budget_module.alert_enqueuer()
        self.addCleanup(
            setattr, budget_module, "_SHARED_SERVICE", baseline_budget_service
        )
        self.addCleanup(
            budget_module.configure_alert_enqueuer, baseline_budget_enqueuer
        )
        baseline_budget_bot = main_module._BUDGET_ALERT_BOT[0]

        moment = now_shanghai_naive().replace(
            hour=12, minute=5, second=0, microsecond=0
        )
        week_start, _week_end = activity.last_complete_week(moment)
        await self._seed_activity(week_start=week_start, user_id=7)

        bot_holder = _StartupFakeBot()

        # main 关停时 dispose 的必须是**它自己那个 session_factory 真正绑定的引擎**
        # （同一次夹具 ``init_db`` 出来的、同一个临时库）。早先的版本在这里另建了
        # 第二个引擎，却把 main 的消费者指向 ``self.session_factory``：于是"完成
        # 计数"只证明释放了另一个库的引擎，还会意外写默认库
        # （父 startup-resource-observer-v1 的 ``same_engine=false``、
        # ``disposed_database=sqlite+aiosqlite:///data/bot.db``）。父的 mutation 探针
        # 需要一个带 ``side_effect`` 的对象来注入失败，而真实 ``AsyncEngine.dispose``
        # 是只读属性，所以把**这个真实引擎**包进替身（真实实现 await 返回之后才记
        # 完成；注入失败时真实实现根本不会跑，completion 也就记不上）。
        observing_engine = getattr(self.session_factory, "kw", {}).get("bind")
        self.assertIs(observing_engine, self.engine)
        completed: list[str] = []
        real_engine_dispose = observing_engine.dispose

        async def completed_engine_dispose() -> None:
            await real_engine_dispose()
            completed.append("database engine")

        engine_holder = SimpleNamespace(
            dispose=AsyncMock(side_effect=completed_engine_dispose)
        )

        close_attempts: list[str] = []

        async def bot_session_close() -> None:
            close_attempts.append("Bot HTTP session")
            if fail_bot_close:
                raise RuntimeError("synthetic Bot session close failure")
            completed.append("Bot HTTP session")

        bot_holder.session = SimpleNamespace(
            close=AsyncMock(side_effect=bot_session_close)
        )

        real_log = main_module.log
        log_spy = Mock(wraps=real_log)

        observed: dict[str, object] = {"cancel_labels": []}
        real_supervise = main_module._run_with_background_supervision
        real_cancel = main_module._cancel_tasks_bounded

        # 默认态"真的进入"同步点：光在 all_tasks() 里按名字看到任务不够——父把
        # `checkin_runner.run_forever()` 换成 `asyncio.sleep(3600)` 时任务名仍在。
        # 观察点放在**真实异步循环体**里真正调用 run_once 的那一刻，而不是同步
        # wrapper 被调用时。
        entered = {"checkin": asyncio.Event(), "weekly": asyncio.Event()}
        real_checkin_once = runner_module.CheckinInternalRunner.run_once
        real_plan_once = runner_module.WeeklyPlanRunner.run_once

        async def checkin_once(instance, *args, **kwargs):
            entered["checkin"].set()
            return await real_checkin_once(instance, *args, **kwargs)

        async def plan_once(instance, *args, **kwargs):
            entered["weekly"].set()
            return await real_plan_once(instance, *args, **kwargs)

        async def supervising(delivery, *, background_tasks):
            names = [task.get_name() for task in background_tasks]
            observed["background_names"] = names
            # 父的 mutation（把目标 worker 从 background_tasks 里去掉）必须在这里失败。
            self.assertIn("checkin-reminder-internal-runner", names)
            self.assertIn("weekly-report-plan-runner", names)
            runners = [
                task
                for task in background_tasks
                if task.get_name()
                in {"checkin-reminder-internal-runner", "weekly-report-plan-runner"}
            ]
            observed["runner_tasks"] = runners
            return await real_supervise(delivery, background_tasks=runners)

        async def cancelling(tasks, *, label, timeout=None):
            tasks = list(tasks)
            if label == "background services":
                observed["cancel_labels"].append(label)
                observed["cancelled_names"] = [task.get_name() for task in tasks]
            if timeout is None:
                return await real_cancel(tasks, label=label)
            return await real_cancel(tasks, label=label, timeout=timeout)

        async def controlled_delivery(**_kwargs):
            loop = asyncio.get_running_loop()
            wanted = {
                "checkin-reminder-internal-runner",
                "weekly-report-plan-runner",
            }
            deadline = loop.time() + 20
            while loop.time() < deadline:
                live = {task.get_name() for task in asyncio.all_tasks()}
                if wanted <= live:
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail(f"真实 main 没有启动这两个 worker：{sorted(live)}")
            # 名字存在还不够：必须真的进入各自的循环体并跑过一次 run_once。
            deadline = loop.time() + 20
            while loop.time() < deadline:
                if entered["checkin"].is_set() and entered["weekly"].is_set():
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail(
                    "目标 worker 没有真正进入 run_forever 循环体："
                    f"checkin={entered['checkin'].is_set()} "
                    f"weekly={entered['weekly'].is_set()} "
                    f"live={sorted(task.get_name() for task in asyncio.all_tasks())}"
                )
            # 默认配置零副作用：签到默认 external（内部 runner 不该发），商店默认
            # internal 但没有任何到期权益，周报计划默认 off。
            async with self.session_factory() as session:
                self.assertEqual(
                    int(
                        await session.scalar(
                            select(func.count()).select_from(CheckinReminderPost)
                        )
                        or 0
                    ),
                    0,
                )
                self.assertEqual(
                    int(
                        await session.scalar(
                            select(func.count()).select_from(OperationsRun)
                        )
                        or 0
                    ),
                    0,
                )
                self.assertEqual(
                    int(
                        await session.scalar(
                            select(func.count()).select_from(MemberPointAward)
                        )
                        or 0
                    ),
                    0,
                )
            self.assertEqual(bot_holder.sent, [], "默认配置下真实 worker 就发了消息")

            # 策略热改：真实写持久策略行，之后**不重启**进程。
            policy_model = _policy_model()
            async with self.session_factory() as session:
                session.add(
                    policy_model(
                        group_id=GROUP_A,
                        task_id="checkin_reminder",
                        source="internal",
                        active_from=moment - timedelta(minutes=5),
                        revision=1,
                    )
                )
                session.add(
                    policy_model(
                        group_id=GROUP_A,
                        task_id="weekly_report",
                        source="internal",
                        plan_enabled=True,
                        plan_weekday=moment.weekday(),
                        plan_minute=moment.hour * 60 + moment.minute,
                        anchor_at=moment - timedelta(seconds=30),
                        active_from=moment - timedelta(minutes=5),
                        revision=1,
                    )
                )
                await session.commit()

            with patch.object(
                runner_module, "now_shanghai_naive", return_value=moment
            ):
                deadline = loop.time() + 30
                while loop.time() < deadline:
                    if len(bot_holder.sent) >= 2:
                        break
                    await asyncio.sleep(0.05)
                else:
                    self.fail(
                        f"热改之后 worker 没有消费新策略：sent={bot_holder.sent!r}"
                    )
            return "controlled-delivery-finished"

        watchdog = Mock()
        # 这两个**不能**换成纯 Mock：它们正是产品用来还原进程级共享状态的清理步骤
        # （成本护栏基线、LLM 客户端）。只断言 called 而不真的执行，就会把本次启动
        # 的预算服务留在进程里，污染后续用例。因此记录 + 调用真实实现，并且只在
        # 真实返回之后才记 completion。
        real_close_llm = main_module.close_llm_clients
        real_restore_guard = main_module._restore_cost_guard_baseline
        close_llm_completions: list[str] = []
        restore_guard_returns: list[object] = []

        async def completed_close_llm(*args, **kwargs):
            result = await real_close_llm(*args, **kwargs)
            close_llm_completions.append("LiteLLM HTTP clients")
            completed.append("LiteLLM HTTP clients")
            return result

        def completed_restore_guard(*args, **kwargs):
            result = real_restore_guard(*args, **kwargs)
            restore_guard_returns.append(result)
            completed.append("cost guard baseline")
            return result

        with ExitStack() as stack:
            # 真实启动会真的去 bind Mini App 监听端口。这里把它换成 0（临时端口）：
            # 装配与关停仍然是真的，只是不占用固定端口，避免与同一台机器上并行运行
            # 的其它测试/进程抢 8480 而给出与本次契约无关的红。
            stack.enter_context(patch.object(self.settings, "miniapp_listen_port", 0))
            stack.enter_context(patch.object(
                main_module, "load_bootstrap_settings", lambda: self.settings
            ))
            stack.enter_context(patch.object(
                main_module, "validate_bootstrap_settings", lambda settings: None
            ))
            stack.enter_context(patch.object(
                main_module,
                "init_db",
                AsyncMock(return_value=(engine_holder, self.session_factory)),
            ))
            stack.enter_context(patch.object(
                main_module, "create_bot", lambda settings: bot_holder
            ))
            stack.enter_context(patch.object(
                main_module, "_prefetch_model_context_metadata", AsyncMock()
            ))
            stack.enter_context(patch.object(
                main_module, "configure_logging", lambda *a, **k: None
            ))
            stack.enter_context(patch.object(
                main_module, "log_process_identity", lambda: None
            ))
            stack.enter_context(patch.object(
                main_module, "log_enforcement_switch_state", lambda settings: None
            ))
            stack.enter_context(patch.object(
                main_module, "warm_privileged_operator_cache", AsyncMock()
            ))
            stack.enter_context(patch.object(
                main_module, "_register_update_middlewares", lambda *a, **k: None
            ))
            stack.enter_context(patch.object(
                loader_module, "create_bot", lambda settings: bot_holder
            ))
            stack.enter_context(patch.object(
                main_module, "run_update_delivery", controlled_delivery
            ))
            stack.enter_context(patch.object(
                main_module, "_run_with_background_supervision", supervising
            ))
            stack.enter_context(patch.object(
                main_module, "_cancel_tasks_bounded", cancelling
            ))
            stack.enter_context(patch.object(runner_module, "CHECKIN_TICK_SECONDS", 5.0))
            stack.enter_context(patch.object(runner_module, "PLAN_TICK_SECONDS", 5.0))
            stack.enter_context(patch.object(main_module, "dp", _FakeDispatcher()))
            stack.enter_context(patch.object(
                runner_module.CheckinInternalRunner, "run_once", new=checkin_once
            ))
            stack.enter_context(patch.object(
                runner_module.WeeklyPlanRunner, "run_once", new=plan_once
            ))
            stack.enter_context(patch.object(
                main_module, "close_llm_clients", new=completed_close_llm
            ))
            stack.enter_context(patch.object(
                main_module, "_restore_cost_guard_baseline", new=completed_restore_guard
            ))
            stack.enter_context(patch.object(
                main_module, "_arm_forced_exit_watchdog", new=lambda timeout: watchdog
            ))
            stack.enter_context(patch.object(main_module, "log", new=log_spy))
            await main_module.main()

        cleanup_errors = [
            str(call.args[1]) if len(call.args) > 1 else ""
            for call in log_spy.exception.call_args_list
            if call.args and str(call.args[0]) == "%s cleanup failed"
        ]
        observed.update(
            completed=completed,
            close_attempts=close_attempts,
            cleanup_errors=cleanup_errors,
            entered={key: value.is_set() for key, value in entered.items()},
            close_llm_completions=close_llm_completions,
            restore_guard_returns=restore_guard_returns,
            watchdog=watchdog,
            bot_holder=bot_holder,
            baseline_budget_bot=baseline_budget_bot,
        )
        return observed

    def _assert_common_shutdown(self, observed: dict[str, object]) -> None:
        self.assertEqual(
            observed["entered"], {"checkin": True, "weekly": True},
            "目标 worker 没有真正进入 run_forever 循环体",
        )
        # 两个 worker 真的被关停，而且是走了产品的"background services"边界。
        runners = observed.get("runner_tasks") or []
        self.assertEqual(len(runners), 2, observed.get("background_names"))
        for task in runners:
            self.assertTrue(task.done(), f"{task.get_name()} 在 main 返回后仍在跑")
        self.assertIn("background services", observed["cancel_labels"])
        for name in (
            "checkin-reminder-internal-runner",
            "weekly-report-plan-runner",
        ):
            self.assertIn(name, observed.get("cancelled_names", []))
        self.assertTrue(
            observed["watchdog"].set.called,
            "有序关停没有走到最后（硬超时看门狗未被置位）",
        )
        # 热改真的被两个 worker 未来消费：签到提醒一条 + 周报一条（含真实积分）。
        bot_holder = observed["bot_holder"]
        self.assertEqual(len(bot_holder.sent), 2, bot_holder.sent)

    async def test_real_main_starts_both_runners_and_stops_them_cleanly(self) -> None:
        """成功控制：真实启动/真实消费/清理**完成**（不是 await_count）。"""

        observed = await self._run_real_main(fail_bot_close=False)
        self._assert_common_shutdown(observed)
        # 成功控制：没有任何清理被捕获到异常。
        self.assertEqual(
            observed["cleanup_errors"], [], "成功路径不该出现清理异常"
        )
        completed = observed["completed"]
        # 三个清理步骤都必须"真实返回之后"记上完成。
        for marker in (
            "Bot HTTP session",
            "database engine",
            "LiteLLM HTTP clients",
            "cost guard baseline",
        ):
            self.assertIn(marker, completed, completed)
        self.assertEqual(
            observed["close_attempts"], ["Bot HTTP session"], observed["close_attempts"]
        )
        self.assertEqual(
            observed["restore_guard_returns"],
            [True],
            "相邻的成本护栏基线没有真的还原（否则后续 LLM 调用会拿着已 dispose 的引擎查预算）",
        )
        async with self.session_factory() as session:
            posts = int(
                await session.scalar(
                    select(func.count()).select_from(CheckinReminderPost)
                )
                or 0
            )
            runs = list((await session.scalars(select(OperationsRun))).all())
        self.assertEqual(posts, 1)
        self.assertEqual(len(runs), 1, "热改之后应当只建一条计划 run")
        self.assertEqual(await self._award_rows(), [(7, 25)])
        import bot.__main__ as main_module

        self.assertIs(
            main_module._BUDGET_ALERT_BOT[0],
            observed["baseline_budget_bot"],
            "真实启动把合成 Bot 泄漏进了进程级预算告警槽位（关停必须还原）",
        )

    async def test_shutdown_captures_a_bot_close_failure_and_completes_the_rest(
        self,
    ) -> None:
        """故意让 Bot HTTP 会话关闭失败：必须被捕获，且后续清理照常完成。"""

        observed = await self._run_real_main(fail_bot_close=True)
        self._assert_common_shutdown(observed)
        # 失败被产品的 cleanup 边界记下来（不是静默当成"释放成功"）……
        self.assertIn(
            "Bot HTTP session",
            observed["cleanup_errors"],
            observed["cleanup_errors"],
        )
        # ……而且后面的清理没有被它打断：真实引擎的 dispose 仍然真实返回完成，
        # 并且排在失败之后。
        completed = list(observed["completed"])
        self.assertIn("database engine", completed, completed)
        self.assertIn("cost guard baseline", completed, completed)
        self.assertNotIn(
            "Bot HTTP session",
            completed,
            "关闭失败的会话不能被记成已释放",
        )
        self.assertNotIn(
            "database engine", observed["cleanup_errors"], observed["cleanup_errors"]
        )
