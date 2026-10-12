"""B13a 返工 v3：父独立验收 v10 / v11 暴露的 R1、R2 剩余窗口的正式回归。

v2 只在 ``adapter.run`` 的起点查了一次守卫；从"起点"到"真实副作用"之间还有多次
await（生成摘要、重新排名、渲染、抢出站保护），父 v10 的三例证明确认之后撤权仍能
分别发布一版摘要、发一份积分、发一条周报。v11 又证明两件事：

* 第二次排名可以改掉已确认的点数方案，而审计会谎称"没有选中"；
* 真实 ``TelegramNetworkError`` 因为消息文本里带 "chat not found" 就被当成
  Telegram 的确定性拒收，于是被安全重发。

本文件的每个用例都把事件**hold 在真实业务服务 / 真实 CAS 入口**（``publish``、
``settle_weekly_activity(award=True)``、``deliver_group_report``、出站 dispatch
claim），在 hold 期间用一条独立事务撤销发起人的群管理员身份，然后释放原函数：
断言的是"真实副作用一个都没发生"，不是"某个 mock 没被调用"。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramServerError,
)
from aiogram.methods import SendMessage
from sqlalchemy import delete

import bot.services.checkin_reminder as checkin
import bot.services.group_summary as group_summary_module
import bot.services.operations_tasks as tasks
import bot.services.point_shop as point_shop
from bot.db.models import (
    Admin,
    MemberEntitlement,
    MemberPointAward,
    OperationsRunItem,
)
from bot.services.activity import last_complete_week
from bot.services.group_summary import GroupSummaryScheduler, SqlGroupSummaryStore
from bot.services.operations_receipts import definitely_not_delivered
from bot.services.operations_runs import (
    ITEM_STATUS_FAILED,
    ITEM_STATUS_SKIPPED,
    RUN_STATUS_FAILED,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_UNKNOWN,
)
from bot.services.runtime_config import RuntimeConfigManager
from bot.utils.timezone import now_shanghai_naive
from tests.test_b13a_operations_center import (
    ADMIN_ID,
    GROUP_A,
    SUPER_ADMIN,
    _OperationsTestCase,
    _summary_config,
)


class _BoundaryHoldMixin:
    """在真实业务入口 hold 一次，hold 期间可以提交独立的撤权事务。"""

    def _hold(self, target, attribute: str, *, only: object = None):
        """返回 ``(patch, entered, release)``：释放后调用原函数。

        ``only`` 给出时才 hold——用于 ``settle_weekly_activity`` 这种在同一进程里
        既被 dry-run（``award=False``）又被真实写入调用的入口。
        """

        original = getattr(target, attribute)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held(*args, **kwargs):
            if only is not None and not only(*args, **kwargs):
                return await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        if isinstance(target, type):
            return patch.object(target, attribute, new=held), entered, release
        return patch.object(target, attribute, new=held), entered, release


class RealBoundaryGuardV3Tests(_BoundaryHoldMixin, _OperationsTestCase):
    """R2：持久守卫必须落在真实不可逆准入点，而不是 ``adapter.run`` 起点。"""

    async def _revoke_actor(self) -> None:
        async with self.session_factory() as session:
            await session.execute(
                delete(Admin).where(
                    Admin.user_id == ADMIN_ID, Admin.group_id == GROUP_A
                )
            )
            await session.commit()

    async def _run_payload(
        self, run_key: str, *, user: int = SUPER_ADMIN
    ) -> tuple[dict, list[OperationsRunItem]]:
        # 撤权之后发起人自己已经读不到这个 run 了（真实权限路径会 403），所以
        # 用真超管读同一条 run 的终态：守卫要挡的是**副作用**，不是审计读取。
        settled = await self._wait(run_key, user=user)
        self.assertEqual(settled.status, 200, await settled.text())
        run = (await settled.json())["run"]
        rows = await self._runs()
        self.assertEqual(len(rows), 1, rows)
        return run, await self._items(rows[0].id)

    async def test_activity_revocation_at_settlement_boundary_awards_nothing(self) -> None:
        """撤权提交在真实 ``settle_weekly_activity(award=True)`` 入口之后。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        holder, entered, release = self._hold(
            tasks,
            "settle_weekly_activity",
            only=lambda *a, **kw: bool(kw.get("award", True)),
        )
        with holder:
            preview = (
                await (await self._preview("activity_award", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)
            await self._revoke_actor()
            release.set()
            run, items = await self._run_payload(run_key)

        self.assertEqual(await self._count(MemberPointAward), 0, "撤权后不得发分")
        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertEqual(run["reason"], tasks.REASON_GUARD_BLOCKED, run)
        self.assertTrue(items, "逐项结果必须如实记录")
        for item in items:
            self.assertEqual(item.status, ITEM_STATUS_FAILED, item.detail)
            self.assertEqual(item.reason, tasks.REASON_GUARD_BLOCKED, item.detail)
            self.assertFalse(item.resendable, "复核不通过不得自动重发")
            self.assertLessEqual(len(item.detail), 300, "detail 只放安全文案")

    async def test_summary_revocation_at_publication_boundary_publishes_nothing(self) -> None:
        """撤权提交在真实 ``SqlGroupSummaryStore.publish`` 入口之后。"""

        await self._seed_archive(count=40)

        class Supplier:
            async def background_summary_completion(self, messages, **kwargs):
                return "合成摘要：撤权之后不得发布的候选内容。"

        store = SqlGroupSummaryStore(self.session_factory)
        scheduler = GroupSummaryScheduler(
            llm=Supplier(),
            store=store,
            config_provider=lambda: _summary_config(enabled=True),
            slot_waiter=None,
        )
        holder, entered, release = self._hold(store, "publish")
        with holder, patch.object(
            group_summary_module, "GROUP_SUMMARY_SCHEDULER", scheduler
        ), patch.object(
            tasks,
            "group_summary_config",
            lambda _settings: _summary_config(enabled=True),
        ):
            preview = (
                await (await self._preview("group_summary", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            # 生成已经跑完（候选内容已拿到），卡在真正的发布 CAS 之前。
            await asyncio.wait_for(entered.wait(), timeout=10)
            await self._revoke_actor()
            release.set()
            run, items = await self._run_payload(run_key)

        self.assertIsNone(await store.load(GROUP_A), "撤权之后不得发布任何版本")
        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertEqual(run["reason"], tasks.REASON_GUARD_BLOCKED, run)
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item.status, ITEM_STATUS_FAILED, item.detail)
            self.assertEqual(item.reason, tasks.REASON_GUARD_BLOCKED, item.detail)
            self.assertFalse(item.resendable)

    async def test_weekly_revocation_at_delivery_boundary_sends_nothing(self) -> None:
        """撤权提交在真实 ``deliver_group_report`` 入口之后（积分已在授权时提交）。"""

        await self._seed_archive(count=5)
        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        holder, entered, release = self._hold(tasks, "deliver_group_report")
        with holder:
            preview = (
                await (await self._preview("weekly_report", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=10)
            await self._revoke_actor()
            release.set()
            run, items = await self._run_payload(run_key)

        self.assertEqual(self.outbound, [], "撤权之后一条消息都不许出站")
        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertEqual(run["reason"], tasks.REASON_GUARD_BLOCKED, run)
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item.status, ITEM_STATUS_FAILED, item.detail)
            self.assertEqual(item.reason, tasks.REASON_GUARD_BLOCKED, item.detail)
            self.assertFalse(item.resendable, "出站前复核不通过不得自动重发")

    async def test_shop_revocation_at_dispatch_claim_boundary_changes_nothing(self) -> None:
        """撤权提交在真实出站 dispatch claim 入口之后：不撤权益、不删行、不提醒。"""

        await self._seed_entitlement(group_id=GROUP_A, user_id=7)

        holder, entered, release = self._hold(point_shop, "_claim_expiry_dispatch")
        with holder:
            preview = (
                await (await self._preview("shop_expire", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            self.assertTrue(preview["targets"], preview)
            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)
            await self._revoke_actor()
            release.set()
            run, items = await self._run_payload(run_key)

        self.assertEqual(self.outbound, [], "复核不通过不得发送到期提醒")
        self.assertEqual(await self._count(MemberEntitlement), 1, "权益行不得被删除")
        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertEqual(run["reason"], tasks.REASON_GUARD_BLOCKED, run)
        for item in items:
            self.assertEqual(item.status, ITEM_STATUS_FAILED, item.detail)
            self.assertFalse(item.resendable)

    async def test_recomputed_points_after_confirmation_are_rejected_not_audited_away(
        self,
    ) -> None:
        """v11 第一例：第二次排名改了点数方案，不得写下去、更不得谎称"没选中"。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        holder, entered, release = self._hold(
            tasks,
            "settle_weekly_activity",
            only=lambda *a, **kw: bool(kw.get("award", True)),
        )
        with holder:
            preview = (
                await (await self._preview("activity_award", {}, user=ADMIN_ID)).json()
            )["preview"]
            self.assertEqual(preview["status"], "ok", preview)
            selected = {
                int(entry["target"]["user_id"]): int(entry["target"]["points"])
                for entry in preview["targets"]
            }
            self.assertEqual(selected, {7: 25}, preview)

            confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
            self.assertEqual(confirmed.status, 200, await confirmed.text())
            run_key = (await confirmed.json())["run"]["run_key"]
            await asyncio.wait_for(entered.wait(), timeout=5)

            # 执行期间出现一个同分、但排序在 user 7 之前的成员：真实重算会把
            # 25 分给 user 6、把 12 分给 user 7 —— 与确认时披露的方案不一致。
            await self._seed_activity(week_start=week_start, user_id=6)
            release.set()
            run, items = await self._run_payload(run_key)

        self.assertEqual(
            await self._count(MemberPointAward), 0, "未确认的点数一个都不许写"
        )
        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertEqual(run["reason"], tasks.REASON_SOURCE_CHANGED, run)
        self.assertTrue(items)
        for item in items:
            self.assertNotEqual(item.status, ITEM_STATUS_SKIPPED, item.detail)
            self.assertEqual(item.status, ITEM_STATUS_FAILED, item.detail)
            self.assertEqual(item.reason, tasks.REASON_SOURCE_CHANGED, item.detail)
            self.assertFalse(item.resendable)
        reasons = {item.reason for item in items}
        self.assertNotIn(tasks.REASON_NOT_SELECTED, reasons, items)

    # ---- 控制：守卫不能把正常路径一起挡掉 ----

    async def test_normal_path_still_awards_for_a_present_group_admin(self) -> None:
        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        run = await self._run_once("activity_award", {}, user=ADMIN_ID)

        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        self.assertGreater(await self._count(MemberPointAward), 0)

    async def test_super_admin_keeps_the_existing_exemption(self) -> None:
        """真超管不依赖 ``Admin`` 行，与既有 ``_actor_still_authorized`` 一致。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)
        await self._revoke_actor()

        run = await self._run_once("activity_award", {}, user=SUPER_ADMIN)

        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        self.assertGreater(await self._count(MemberPointAward), 0)

    async def test_config_revision_committed_by_another_manager_blocks_the_write(
        self,
    ) -> None:
        """另一个管理会话提交新配置 → 持久 revision 前进 → 已确认的 run 必须停。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        preview = (
            await (await self._preview("activity_award", {}, user=ADMIN_ID)).json()
        )["preview"]
        self.assertEqual(preview["status"], "ok", preview)
        confirmed = await self._confirm(preview["token"], user=ADMIN_ID)
        self.assertEqual(confirmed.status, 200, await confirmed.text())
        run_key = (await confirmed.json())["run"]["run_key"]

        second = RuntimeConfigManager(
            session_factory=self.session_factory, settings=self.settings
        )
        await second.initialize()
        before = int(second.revision)
        await second.save(
            second.config.public_payload(),
            expected_revision=before,
            updated_by=ADMIN_ID,
        )
        self.assertEqual(
            int(second.revision), before + 1, "这次提交必须真的让持久 revision 前进"
        )

        run, items = await self._run_payload(run_key)

        self.assertEqual(await self._count(MemberPointAward), 0, "配置变了不得再发分")
        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertEqual(run["reason"], tasks.REASON_GUARD_BLOCKED, run)
        for item in items:
            self.assertEqual(item.status, ITEM_STATUS_FAILED, item.detail)
            self.assertFalse(item.resendable)

    async def test_guard_query_failure_fails_closed(self) -> None:
        """复核查询本身失败 → 一个字都不写（fail-closed），而不是"查不出就照发"。"""

        week_start, _ = last_complete_week(now_shanghai_naive())
        await self._seed_activity(week_start=week_start, user_id=7)

        # 局部导入：这个模块本身就是本批新增的持久守卫内核，旧树上不存在。
        # 放在测试体内可以让"隔离基线红"是**真实行为红**，而不是整个文件的
        # 收集期 ImportError（收集错误不能冒充预期红检）。
        from bot.services import operations_guard

        with patch.object(
            operations_guard,
            "verify_guard",
            new=AsyncMock(side_effect=RuntimeError("synthetic guard query unavailable")),
        ):
            run = await self._run_once("activity_award", {}, user=ADMIN_ID)

        self.assertEqual(await self._count(MemberPointAward), 0)
        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertEqual(run["reason"], tasks.REASON_GUARD_BLOCKED, run)


class TypedNetworkUnknownV3Tests(_BoundaryHoldMixin, _OperationsTestCase):
    """R1：只有真实 Telegram 拒收类型才释放保护；typed 网络异常必须保持未知。"""

    async def test_typed_network_error_is_unknown_and_never_resent(self) -> None:
        exc = TelegramNetworkError(
            method=SendMessage(chat_id=GROUP_A, text="synthetic"),
            message="proxy connection failed: chat not found",
        )
        fire = [True]

        async def flaky(chat_id, text, **kwargs):
            result = await self._record_send(chat_id, text, **kwargs)
            if fire[0] and int(chat_id) == GROUP_A:
                fire[0] = False
                raise exc
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
                queue_auto_delete=AsyncMock(return_value=True),
                backfill_auto_delete=AsyncMock(return_value=True),
                session_factory=self.session_factory,
            )
        self.assertEqual(len(self.outbound), 1, (outcome, self.outbound))
        self.assertEqual(
            outcome.status, checkin.CHECKIN_OUTCOME_ALREADY_DELIVERED, outcome
        )

    async def test_old_string_matched_rejection_no_longer_releases_protection(self) -> None:
        """旧判据只看名字/文本，于是普通 RuntimeError 里的拒绝短语也会被当拒收。"""

        calls = [0]

        async def flaky(chat_id, text, **kwargs):
            result = await self._record_send(chat_id, text, **kwargs)
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("Forbidden: bot was blocked by the user")
            return result

        with patch.object(tasks, "reminder_slots", lambda: (0,)), patch.object(
            self.bot, "send_message", new=AsyncMock(side_effect=flaky)
        ):
            run = await self._run_once("checkin_reminder", {"slot": 0})

        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        self.assertEqual(len(self.outbound), 1, self.outbound)

    async def test_real_forbidden_error_still_releases_the_protection(self) -> None:
        """正控制：真实 ``TelegramForbiddenError`` 是确定性拒收，保护应当释放。"""

        exc = TelegramForbiddenError(
            method=SendMessage(chat_id=GROUP_A, text="synthetic"),
            message="Forbidden: bot was blocked by the user",
        )
        fire = [True]

        async def flaky(chat_id, text, **kwargs):
            if fire[0] and int(chat_id) == GROUP_A:
                fire[0] = False
                raise exc
            return await self._record_send(chat_id, text, **kwargs)

        with patch.object(tasks, "reminder_slots", lambda: (0,)), patch.object(
            self.bot, "send_message", new=AsyncMock(side_effect=flaky)
        ):
            run = await self._run_once("checkin_reminder", {"slot": 0})

        self.assertEqual(run["status"], RUN_STATUS_FAILED, run)
        self.assertEqual(self.outbound, [], "确定性拒收时一条都没出站")
        items = await self._items((await self._runs())[0].id)
        self.assertTrue(items)
        self.assertTrue(
            any(item.resendable for item in items), "确定性拒收必须允许安全重试"
        )


class RefusalJudgementV3Tests(unittest.TestCase):
    """``definitely_not_delivered`` 的判据表：只有真实 Telegram 拒收类才算数。"""

    def test_only_real_telegram_refusals_release_the_protection(self) -> None:
        method = SimpleNamespace(chat_id=GROUP_A, text="synthetic")
        unknown = [
            TelegramNetworkError(method=method, message="proxy connection failed: chat not found"),
            TelegramServerError(method=method, message="Internal Server Error"),
            TimeoutError("synthetic timeout"),
            ConnectionResetError("synthetic reset"),
            asyncio.CancelledError(),
            RuntimeError("Forbidden: bot was blocked by the user"),
            RuntimeError("Bad Request: chat not found"),
        ]
        impostor = type("TelegramForbiddenError", (RuntimeError,), {})()
        unknown.append(impostor)

        for exc in unknown:
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(definitely_not_delivered(exc))

        refusals = [
            TelegramForbiddenError(method=method, message="Forbidden: bot was blocked"),
            TelegramBadRequest(method=method, message="Bad Request: chat not found"),
        ]
        for exc in refusals:
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(definitely_not_delivered(exc))
