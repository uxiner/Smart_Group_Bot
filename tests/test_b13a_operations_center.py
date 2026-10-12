"""B13a 运营任务中心：真实 init_db + 签名 HTTP + 真实业务服务回归。

只替最末端的**外部副作用**（Telegram 发送 / 模型补全），其余全部走真实实现：
真实 SQLite（``init_db``）、真实签名 HTTP（``VerifyWebServer`` + ``TestClient``）、
真实业务服务（``ScheduledMessageService``、``send_checkin_slot``、
``deliver_group_report``、``GroupSummaryScheduler``、``settle_weekly_activity``、
``expire_due_entitlements``）与真实写侧内核（单次令牌、写事务内守卫复核、持久
审计、取消、重试分类、重启后对账）。
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import bot.services.group_summary as group_summary_module
import bot.services.operations_runs as operations_runs
import bot.services.operations_tasks as operations_tasks
from bot.db.models import (
    Admin,
    Base,
    CheckinReminderPost,
    MemberActivityDaily,
    GroupMessageArchive,
    GroupSummary,
    MemberEntitlement,
    MemberPointAward,
    OperationsRun,
    OperationsRunItem,
    ScheduledMessage,
    WeeklyReportPost,
)
from bot.services.activity import AWARD_REASON_WEEKLY_ACTIVITY, settle_weekly_activity
from bot.services.group_summary import (
    GroupSummaryConfig,
    GroupSummaryScheduler,
    SqlGroupSummaryStore,
)
from bot.services.operations_runs import (
    REASON_ALREADY_DELIVERED,
    ITEM_STATUS_CANCELLED,
    ITEM_STATUS_SKIPPED,
    ITEM_STATUS_SUCCEEDED,
    ITEM_STATUS_UNKNOWN,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_FAILED,
    RUN_STATUS_NO_WORK,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_UNKNOWN,
)
from bot.services.point_shop import KIND_TAG
from bot.services.scheduled_messages import scheduled_message_occurrence_at
from bot.utils.timezone import now_shanghai_naive
from tests.test_group_summary import FakeLLM
from tests.test_permanent_memory_create_dedup import (
    GROUP_A,
    GROUP_B,
    SUPER_ADMIN,
    _RaceInjectedTestCase,
)

ADMIN_ID = 99


def _json(body: dict) -> dict:
    return body


class _OperationsTestCase(_RaceInjectedTestCase):
    """公共夹具：管理员行、真实 Telegram 替身、快照与 HTTP 助手。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # 清掉上一次用例可能残留的内存执行器状态，然后立刻重新绑定——
        # app on_startup 已经绑过一次，reset 不重新绑定会让后台 worker 直接失败。
        operations_runs.reset_execution_runtime()
        operations_runs.bind_execution_runtime(
            settings=self.settings, session_factory=self.session_factory
        )
        self.outbound: list[tuple[int, str]] = []
        self.bot.send_message = AsyncMock(side_effect=self._record_send)
        self.bot.get_me = AsyncMock(return_value=SimpleNamespace(username="synthbot"))
        self.bot.pin_chat_message = AsyncMock(return_value=True)
        self.bot.unpin_chat_message = AsyncMock(return_value=True)
        self.bot.set_chat_member_tag = AsyncMock(return_value=True)
        async with self.session_factory() as session:
            session.add(Admin(user_id=ADMIN_ID, group_id=GROUP_A))
            await session.commit()

    async def asyncTearDown(self) -> None:
        operations_runs.reset_execution_runtime()
        await super().asyncTearDown()

    async def _scheduled_item_key(self, entry_id: int) -> str:
        """定时消息业务键：``entry:<id>:<准确 occurrence>``（R1 要求键含时段）。

        预览、确认与执行必须共用同一个键；测试独立算出来，用来钉住格式。
        """

        async with self.session_factory() as session:
            row = await session.get(ScheduledMessage, int(entry_id))
        occurrence_at = scheduled_message_occurrence_at(row)
        assert occurrence_at is not None
        return f"entry:{int(entry_id)}:{occurrence_at.strftime('%Y-%m-%dT%H:%M:%S')}"

    async def _record_send(self, chat_id: int, text: str, **_kwargs):
        self.outbound.append((int(chat_id), str(text)))
        # 真实的自动删除排队会读 ``sent.chat.id`` —— 替身必须带 chat，
        # 否则这条链会以 AttributeError 静默失效（"持久队列不健康"被当成未知）。
        return SimpleNamespace(
            message_id=1000 + len(self.outbound),
            chat=SimpleNamespace(id=int(chat_id)),
        )

    # ---- 快照 / 查询 ----

    async def _snapshot(self) -> dict[str, list[str]]:
        async with self.session_factory() as session:
            snapshot: dict[str, list[str]] = {}
            for table in Base.metadata.sorted_tables:
                rows = (await session.execute(select(table))).all()
                snapshot[table.name] = sorted(repr(tuple(row)) for row in rows)
            return snapshot

    async def _count(self, model) -> int:
        async with self.session_factory() as session:
            return int(len((await session.scalars(select(model))).all()))

    async def _runs(self) -> list[OperationsRun]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(OperationsRun))).all())

    async def _items(self, run_id: int) -> list[OperationsRunItem]:
        async with self.session_factory() as session:
            return list(
                (
                    await session.scalars(
                        select(OperationsRunItem)
                        .where(OperationsRunItem.run_id == int(run_id))
                        .order_by(OperationsRunItem.item_key)
                    )
                ).all()
            )

    # ---- HTTP 助手 ----

    async def _preview(
        self,
        task_id: str,
        params: dict | None = None,
        *,
        group: int = GROUP_A,
        user: int = SUPER_ADMIN,
    ):
        return await self.client.post(
            f"/api/v1/groups/{group}/operations/preview",
            headers=self._headers(user),
            json={"task_id": task_id, "params": params or {}},
        )

    async def _confirm(
        self,
        token: str,
        *,
        task_id: str | None = None,
        group: int = GROUP_A,
        user: int = SUPER_ADMIN,
    ):
        body: dict = {"token": token}
        if task_id is not None:
            body["task_id"] = task_id
        return await self.client.post(
            f"/api/v1/groups/{group}/operations/runs",
            headers=self._headers(user),
            json=body,
        )

    async def _wait(self, run_key: str, *, group: int = GROUP_A, user: int = SUPER_ADMIN):
        return await self.client.get(
            f"/api/v1/groups/{group}/operations/runs/{run_key}",
            headers=self._headers(user),
            params={"wait": "1"},
        )

    async def _post(self, path: str, *, group: int = GROUP_A, user: int = SUPER_ADMIN):
        return await self.client.post(
            f"/api/v1/groups/{group}{path}", headers=self._headers(user)
        )

    async def _run_once(
        self,
        task_id: str,
        params: dict | None = None,
        *,
        group: int = GROUP_A,
        user: int = SUPER_ADMIN,
    ) -> dict:
        """预览 → 确认 → 等到终态，返回 run 载荷（含逐项结果）。"""

        response = await self._preview(task_id, params, group=group, user=user)
        self.assertEqual(response.status, 200, await response.text())
        preview = (await response.json())["preview"]
        self.assertEqual(preview["status"], "ok", preview)
        confirmed = await self._confirm(preview["token"], group=group, user=user)
        self.assertEqual(confirmed.status, 200, await confirmed.text())
        run = (await confirmed.json())["run"]
        settled = await self._wait(run["run_key"], group=group, user=user)
        self.assertEqual(settled.status, 200, await settled.text())
        return (await settled.json())["run"]

    # ---- 业务数据 ----

    async def _seed_scheduled(
        self,
        *,
        group_id: int = GROUP_A,
        enabled: bool = True,
        interval_minutes: int = 5,
        anchor: timedelta | None = None,
    ) -> int:
        async with self.session_factory() as session:
            row = ScheduledMessage(
                group_id=int(group_id),
                text="合成定时消息",
                schedule_type="interval",
                interval_minutes=int(interval_minutes),
                schedule_time="09:00",
                enabled=bool(enabled),
                created_at=now_shanghai_naive()
                - (anchor if anchor is not None else timedelta(hours=1)),
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _seed_entitlement(self, *, group_id: int, user_id: int) -> int:
        async with self.session_factory() as session:
            row = MemberEntitlement(
                group_id=int(group_id),
                user_id=int(user_id),
                kind=KIND_TAG,
                payload="合成头衔",
                ref=f"order-{group_id}-{user_id}",
                expires_at=now_shanghai_naive() - timedelta(minutes=5),
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _seed_activity(
        self,
        *,
        group_id: int = GROUP_A,
        user_id: int = 7,
        week_start,
        days: int = 5,
        messages: int = 20,
        replies: int = 3,
    ) -> None:
        """周活跃结算的数据源是 ``member_activity_daily``（不是 archive）。"""

        async with self.session_factory() as session:
            for offset in range(days):
                session.add(
                    MemberActivityDaily(
                        group_id=int(group_id),
                        user_id=int(user_id),
                        activity_date=(week_start + timedelta(days=offset)).isoformat(),
                        messages=int(messages),
                        replies_received=int(replies),
                        display_name="合成成员",
                    )
                )
            await session.commit()

    async def _seed_archive(
        self,
        *,
        group_id: int = GROUP_A,
        count: int,
        sender_id: int = 7,
        start_minutes_ago: int = 120,
        step_seconds: int = 30,
    ) -> list[int]:
        base = now_shanghai_naive() - timedelta(minutes=start_minutes_ago)
        async with self.session_factory() as session:
            rows = [
                GroupMessageArchive(
                    group_id=int(group_id),
                    message_key=f"{group_id}:{index}",
                    telegram_message_id=1000 + index,
                    role="user",
                    direction="inbound",
                    sender_display_name="群友",
                    sender_id=int(sender_id),
                    message_type="text",
                    content=f"历史{index}",
                    raw_text=f"历史{index}",
                    sent_at=base + timedelta(seconds=index * step_seconds),
                )
                for index in range(count)
            ]
            session.add_all(rows)
            await session.commit()
            return [int(row.id) for row in rows]


class OperationsKernelHttpTests(_OperationsTestCase):
    """写侧内核 + 六类任务：权限、幂等、守卫、取消、重试、对账。"""

    # ------------------------------------------------------------------
    # 只读目录 / 权限 / 参数
    # ------------------------------------------------------------------

    async def test_catalog_lists_six_tasks_and_requires_group_access(self) -> None:
        response = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/operations/tasks",
            headers=self._headers(SUPER_ADMIN),
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(
            [item["task_id"] for item in body["tasks"]],
            [
                "scheduled_messages",
                "group_summary",
                "checkin_reminder",
                "activity_award",
                "weekly_report",
                "shop_expire",
            ],
        )
        self.assertGreaterEqual(body["preview_ttl_seconds"], 30)
        forbidden = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/operations/tasks",
            headers=self._headers(777),
        )
        self.assertEqual(forbidden.status, 403)
        other = await self.client.get(
            f"/api/v1/groups/{GROUP_B}/operations/tasks",
            headers=self._headers(ADMIN_ID),
        )
        self.assertEqual(other.status, 403)

    async def test_strict_params_and_unknown_fields_are_rejected_before_any_read(
        self,
    ) -> None:
        before = await self._snapshot()
        cases = [
            ({"task_id": "scheduled_messages", "params": {"entry_ids": [True]}}, 400),
            ({"task_id": "scheduled_messages", "params": {"entry_ids": [1.5]}}, 400),
            ({"task_id": "checkin_reminder", "params": {"slot": True}}, 400),
            ({"task_id": "activity_award", "params": {"week_start": "2026-1-1"}}, 400),
            ({"task_id": "weekly_report", "params": {"days": 0}}, 400),
            ({"task_id": "weekly_report", "params": {"days": 400}}, 400),
            ({"task_id": "shop_expire", "params": {"limit": "5"}}, 400),
            ({"task_id": "not_a_task", "params": {}}, 404),
            ({"task_id": "weekly_report", "params": {}, "extra": 1}, 400),
        ]
        for payload, expected in cases:
            response = await self.client.post(
                f"/api/v1/groups/{GROUP_A}/operations/preview",
                headers=self._headers(SUPER_ADMIN),
                json=payload,
            )
            self.assertEqual(response.status, expected, payload)
        self.assertEqual(before, await self._snapshot())

    async def test_member_and_cross_group_confirm_are_rejected(self) -> None:
        self.bot.send_message.reset_mock()
        await self._seed_scheduled()
        preview = await self._preview("scheduled_messages")
        token = (await preview.json())["preview"]["token"]

        member = await self._preview("scheduled_messages", user=777)
        self.assertEqual(member.status, 403)

        # 令牌绑定本群：换一个群的管理员路径也确认不了。
        cross = await self._confirm(token, group=GROUP_B, user=ADMIN_ID)
        self.assertIn(cross.status, (403, 404))
        self.assertEqual(self.bot.send_message.await_count, 0)

    # ------------------------------------------------------------------
    # 单次令牌 / 幂等 / 参数与群绑定
    # ------------------------------------------------------------------

    async def test_preview_is_read_only_and_confirmation_is_single_use(self) -> None:
        entry_id = await self._seed_scheduled()
        before = await self._snapshot()

        response = await self._preview("scheduled_messages", {"entry_ids": [entry_id]})
        self.assertEqual(response.status, 200)
        preview = (await response.json())["preview"]
        self.assertEqual(preview["status"], "ok")
        self.assertEqual(preview["read_only"], True)
        self.assertEqual(preview["confirmable"], True)
        self.assertEqual(
            [t["item_key"] for t in preview["targets"]],
            [await self._scheduled_item_key(entry_id)],
        )
        self.assertEqual(preview["cost"]["telegram_messages"], 1)
        # 预览零副作用：库里一个字节都没变（不写审计、不占位、不发消息）。
        self.assertEqual(before, await self._snapshot())
        self.assertEqual(self.bot.send_message.await_count, 0)

        confirmed = await self._confirm(preview["token"])
        self.assertEqual(confirmed.status, 200)
        first = (await confirmed.json())["run"]
        settled = await self._wait(first["run_key"])
        run = (await settled.json())["run"]
        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        items = run["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], ITEM_STATUS_SUCCEEDED)
        self.assertEqual(items[0]["external_message_id"], 1001)
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.outbound[0][0], GROUP_A)

        # 同一个令牌第二次确认：返回同一个 run，且绝不再次执行。
        again = await self._confirm(preview["token"])
        self.assertEqual(again.status, 200)
        repeat = (await again.json())["run"]
        self.assertEqual(repeat["run_key"], first["run_key"])
        self.assertTrue(repeat["duplicate"])
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(await self._count(OperationsRun), 1)

    async def test_token_expiry_tampering_and_binding_mismatch(self) -> None:
        await self._seed_scheduled()
        real_now = now_shanghai_naive()

        with patch.object(operations_runs, "preview_ttl_seconds", lambda _s: 30):
            response = await self._preview("scheduled_messages")
        token = (await response.json())["preview"]["token"]

        # 篡改 payload：签名对不上。
        head, signature = token.split(".", 1)
        tampered = head[:-2] + ("AA" if not head.endswith("AA") else "BB") + "." + signature
        bad = await self._confirm(tampered)
        self.assertEqual(bad.status, 400)
        self.assertEqual((await bad.json())["error"]["code"], "preview_token_invalid")

        # 过期：令牌有效期 30 秒，确认时已经是 60 秒之后。
        with patch.object(
            operations_runs, "now_shanghai_naive", lambda: real_now + timedelta(seconds=60)
        ):
            expired = await self._confirm(token)
        self.assertEqual(expired.status, 409)
        self.assertEqual((await expired.json())["error"]["code"], "preview_token_expired")

        # 换一个账号确认：令牌绑定了发起人。
        fresh = (await (await self._preview("scheduled_messages")).json())["preview"]
        other_actor = await self._confirm(fresh["token"], user=ADMIN_ID)
        self.assertEqual(other_actor.status, 403)
        # 换一个任务确认：令牌绑定了任务。
        wrong_task = await self._confirm(fresh["token"], task_id="weekly_report")
        self.assertEqual(wrong_task.status, 403)
        self.assertEqual(self.bot.send_message.await_count, 0)
        self.assertEqual(await self._count(OperationsRun), 0)

    async def test_selected_entry_deleted_or_disabled_blocks_confirmation(self) -> None:
        entry_id = await self._seed_scheduled()
        preview = await self._preview("scheduled_messages", {"entry_ids": [entry_id]})
        token = (await preview.json())["preview"]["token"]

        async with self.session_factory() as session:
            row = await session.get(ScheduledMessage, entry_id)
            row.enabled = False
            await session.commit()
        blocked = await self._confirm(token)
        self.assertEqual(blocked.status, 409)
        self.assertEqual((await blocked.json())["error"]["code"], "guard_blocked")
        self.assertEqual(self.bot.send_message.await_count, 0)
        self.assertEqual(await self._count(OperationsRun), 0)

        # 删掉条目同样拒绝（不执行、不写审计）。
        async with self.session_factory() as session:
            row = await session.get(ScheduledMessage, entry_id)
            await session.delete(row)
            await session.commit()
        missing = await self._confirm(token)
        self.assertEqual(missing.status, 409)
        self.assertEqual(await self._count(OperationsRun), 0)

    async def test_config_revision_change_after_preview_blocks_confirmation(self) -> None:
        entry_id = await self._seed_scheduled()
        preview = await self._preview("scheduled_messages", {"entry_ids": [entry_id]})
        token = (await preview.json())["preview"]["token"]

        self.manager._revision = int(self.manager.revision) + 1
        blocked = await self._confirm(token)
        self.assertEqual(blocked.status, 409)
        details = (await blocked.json())["error"]["details"]
        self.assertEqual(details["reason"], "guard_blocked")
        self.assertEqual(self.bot.send_message.await_count, 0)

    async def test_two_concurrent_confirmations_execute_once(self) -> None:
        entry_id = await self._seed_scheduled()
        preview = await self._preview("scheduled_messages", {"entry_ids": [entry_id]})
        token = (await preview.json())["preview"]["token"]

        first, second = await asyncio.gather(
            self._confirm(token), self._confirm(token)
        )
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        keys = {
            (await first.json())["run"]["run_key"],
            (await second.json())["run"]["run_key"],
        }
        self.assertEqual(len(keys), 1, "同一个令牌只能对应同一个 run")
        await self._wait(keys.pop())
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(await self._count(OperationsRun), 1)

    # ------------------------------------------------------------------
    # 失败 / 未知 / 取消 / 重试
    # ------------------------------------------------------------------

    async def test_send_then_bookkeeping_fault_is_unknown_and_never_resent(self) -> None:
        with patch.object(operations_tasks, "reminder_slots", lambda: (0,)):
            with patch(
                "bot.services.checkin_reminder.mark_reminder_sent",
                new=AsyncMock(side_effect=RuntimeError("ledger down")),
            ):
                response = await self._preview("checkin_reminder", {"slot": 0})
                preview = (await response.json())["preview"]
                self.assertEqual(preview["status"], "ok", preview)
                confirmed = await self._confirm(preview["token"])
                run_key = (await confirmed.json())["run"]["run_key"]
                run = (await (await self._wait(run_key)).json())["run"]

        self.assertEqual(run["status"], RUN_STATUS_UNKNOWN, run)
        item = run["items"][0]
        self.assertEqual(item["status"], ITEM_STATUS_UNKNOWN)
        self.assertEqual(item["reason"], "sent_bookkeeping_failed")
        self.assertFalse(item["resendable"], "消息已经出站，绝不允许自动重发")
        self.assertEqual(self.bot.send_message.await_count, 1)

        # 未知执行：对账在没有台账证据时保持未知，重试被硬阻断。
        reconciled = await self._post(f"/operations/runs/{run_key}/reconcile")
        self.assertEqual(reconciled.status, 200)
        self.assertEqual(
            (await reconciled.json())["run"]["items"][0]["status"], ITEM_STATUS_UNKNOWN
        )
        retry = await self._post(f"/operations/runs/{run_key}/retry")
        self.assertEqual(retry.status, 409)
        self.assertEqual((await retry.json())["error"]["code"], "nothing_to_retry")
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_placeholder_unknown_state_blocks_checkin_and_weekly(self) -> None:
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=GROUP_A,
                    slot_key=f"{now.date().isoformat()}:0",
                    message_id=0,
                    created_at=now,
                )
            )
            week_start, _week_end = operations_tasks.last_complete_week(now)
            session.add(
                WeeklyReportPost(
                    target_id=GROUP_A,
                    week_key=operations_tasks.activity_week_key(week_start),
                    message_id=0,
                    created_at=now,
                )
            )
            await session.commit()
        before = await self._snapshot()

        with patch.object(operations_tasks, "reminder_slots", lambda: (0,)):
            checkin = await self._preview("checkin_reminder", {"slot": 0})
        self.assertEqual(checkin.status, 200)
        checkin_preview = (await checkin.json())["preview"]
        self.assertEqual(checkin_preview["status"], "blocked")
        self.assertEqual(checkin_preview["reason"], "in_flight")
        self.assertFalse(checkin_preview["token"])
        self.assertEqual(checkin_preview["confirmable"], False)

        weekly = await self._preview("weekly_report", {})
        weekly_preview = (await weekly.json())["preview"]
        self.assertEqual(weekly_preview["status"], "blocked")
        self.assertEqual(weekly_preview["reason"], "in_flight")
        self.assertFalse(weekly_preview["token"])
        self.assertEqual(self.bot.send_message.await_count, 0)
        self.assertEqual(before, await self._snapshot())

    async def test_pre_send_failure_is_safely_retryable_and_skips_success(self) -> None:
        await self._seed_archive(count=5)

        with patch(
            "bot.services.operations_tasks.render_group_quality",
            new=AsyncMock(side_effect=RuntimeError("render exploded")),
        ):
            first = await self._run_once("weekly_report", {"days": 7})
        self.assertEqual(first["status"], RUN_STATUS_FAILED, first)
        self.assertEqual(first["items"][0]["resendable"], True)
        self.assertEqual(self.bot.send_message.await_count, 0)

        retry = await self._post(f"/operations/runs/{first['run_key']}/retry")
        self.assertEqual(retry.status, 200)
        retried = (await retry.json())["run"]
        self.assertNotEqual(retried["run_key"], first["run_key"])
        settled = await self._wait(retried["run_key"])
        after = (await settled.json())["run"]
        self.assertEqual(after["status"], RUN_STATUS_SUCCEEDED, after)
        self.assertEqual(self.bot.send_message.await_count, 1)

        async with self.session_factory() as session:
            retry_row = (
                await session.scalars(
                    select(OperationsRun).where(
                        OperationsRun.run_key == retried["run_key"]
                    )
                )
            ).one()
        self.assertEqual(retry_row.source, "retry")
        self.assertEqual(int(retry_row.retry_of_run_id), 1)

        # 已经成功的项不会因为重试再发一次：第二次重试被拒。
        again = await self._post(f"/operations/runs/{retried['run_key']}/retry")
        self.assertEqual(again.status, 409)

    async def test_cancel_in_flight_waits_for_worker_and_keeps_real_effects(self) -> None:
        entry_a = await self._seed_scheduled()
        entry_b = await self._seed_scheduled()
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow_send(chat_id, text, **kwargs):
            if not started.is_set():
                started.set()
                await gate.wait()
            return await self._record_send(chat_id, text, **kwargs)

        self.bot.send_message.side_effect = slow_send
        # 键必须在跑之前算出来：投递完成后 ``last_run_at`` 会被推进，interval 条目
        # 就"不再 due"，直接用当下时钟算 occurrence 会得到 None（见
        # ``scheduled_message_occurrence_at`` 的语义）。
        key_a = await self._scheduled_item_key(entry_a)
        key_b = await self._scheduled_item_key(entry_b)
        preview = await self._preview(
            "scheduled_messages", {"entry_ids": [entry_a, entry_b]}
        )
        token = (await preview.json())["preview"]["token"]
        confirmed = await self._confirm(token)
        run_key = (await confirmed.json())["run"]["run_key"]

        await asyncio.wait_for(started.wait(), timeout=5)
        # 取消请求会**等**在途 worker 收尾；worker 正卡在外部发送上，所以先发取消、
        # 再把外部动作放行，才能观察"取消等到了真实收尾"。
        cancel_task = asyncio.ensure_future(self._post(f"/operations/runs/{run_key}/cancel"))
        await asyncio.sleep(0.2)
        gate.set()
        cancel = await asyncio.wait_for(cancel_task, timeout=10)
        self.assertEqual(cancel.status, 200)
        cancelled = (await cancel.json())["run"]
        self.assertTrue(cancelled["worker_finished"], "取消必须等在途 worker 收尾")

        settled = await self._wait(run_key)
        run = (await settled.json())["run"]
        statuses = {item["item_key"]: item["status"] for item in run["items"]}
        self.assertEqual(len(statuses), 2)
        # 已经出站的那一条绝不改写成"没发出去"。
        self.assertIn(
            statuses[key_a],
            (ITEM_STATUS_SUCCEEDED, ITEM_STATUS_UNKNOWN),
        )
        self.assertEqual(statuses[key_b], ITEM_STATUS_CANCELLED)
        self.assertIn(run["status"], (RUN_STATUS_PARTIAL, RUN_STATUS_CANCELLED, RUN_STATUS_UNKNOWN))
        self.assertEqual(self.bot.send_message.await_count, 1)

        # 取消之后写锁必须已经归还：独立写入者能立刻写。
        async with self.session_factory() as session:
            session.add(Admin(user_id=1234, group_id=GROUP_B))
            await session.commit()

    async def test_no_work_disabled_queue_full_backoff_and_not_due(self) -> None:
        # 摘要默认关闭 → blocked/disabled（不是 succeeded）。
        disabled = await self._preview("group_summary", {})
        disabled_preview = (await disabled.json())["preview"]
        self.assertEqual(disabled_preview["status"], "blocked")
        self.assertEqual(disabled_preview["reason"], "disabled")

        # worker 未初始化 → runner_unavailable（不假装排队）。
        with patch.object(group_summary_module, "GROUP_SUMMARY_SCHEDULER", None), patch.object(
            operations_tasks, "group_summary_config", lambda _s: _summary_config(enabled=True)
        ):
            unavailable = await self._preview("group_summary", {})
        self.assertEqual(
            (await unavailable.json())["preview"]["reason"], "runner_unavailable"
        )

        # 退避窗口内 → backoff/deferred。
        scheduler = SimpleNamespace(
            group_is_running=lambda _gid: False,
            group_backoff_until=lambda _gid: now_shanghai_naive().timestamp() + 600,
            active_count=lambda: 0,
            capacity=lambda _cfg: 2,
            manual_pending_count=AsyncMock(return_value=10),
        )
        with patch.object(
            group_summary_module, "GROUP_SUMMARY_SCHEDULER", scheduler
        ), patch.object(
            operations_tasks, "group_summary_config", lambda _s: _summary_config(enabled=True)
        ):
            backoff = await self._preview("group_summary", {})
        self.assertEqual((await backoff.json())["preview"]["reason"], "backoff")

        # 本群已有摘要任务在跑 → queue_full：预览阶段就阻断，既不签发令牌、
        # 也不落审计 run（不把"没做事"包装成 succeeded，也不允许确认后越界执行）。
        runs_before = await self._run_count()
        scheduler.group_is_running = lambda _gid: True
        with patch.object(
            group_summary_module, "GROUP_SUMMARY_SCHEDULER", scheduler
        ), patch.object(
            operations_tasks, "group_summary_config", lambda _s: _summary_config(enabled=True)
        ):
            running = await self._preview("group_summary", {})
            running_preview = (await running.json())["preview"]
        self.assertEqual(running_preview["status"], "blocked")
        self.assertEqual(running_preview["reason"], "queue_full")
        self.assertFalse(running_preview["token"])
        self.assertEqual(await self._run_count(), runs_before)

        # 签到：今天还没到任何时段 → no_work（不补发）。
        with patch.object(operations_tasks, "reminder_slots", lambda: (23, 23)):
            checkin = await self._preview("checkin_reminder", {})
        checkin_preview = (await checkin.json())["preview"]
        if now_shanghai_naive().hour < 23:
            self.assertEqual(checkin_preview["status"], "no_work")

        # 定时消息没到点 → no_work（interval 锚在未来）。
        far = await self._seed_scheduled(anchor=timedelta(minutes=-120))
        not_due = await self._preview("scheduled_messages", {"entry_ids": [far]})
        not_due_preview = (await not_due.json())["preview"]
        self.assertEqual(not_due_preview["status"], "no_work")
        self.assertEqual(not_due_preview["reason"], "not_due")

    # ------------------------------------------------------------------
    # 逐类真实执行（正控制）
    # ------------------------------------------------------------------

    async def test_checkin_reminder_real_ledger_positive_control(self) -> None:
        with patch.object(operations_tasks, "reminder_slots", lambda: (0,)):
            run = await self._run_once("checkin_reminder", {"slot": 0})
        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        item = run["items"][0]
        self.assertEqual(item["status"], ITEM_STATUS_SUCCEEDED)
        key = f"{now_shanghai_naive().date().isoformat()}:0"
        self.assertEqual(item["item_key"], f"checkin:{key}")

        async with self.session_factory() as session:
            row = (
                await session.scalars(
                    select(CheckinReminderPost).where(
                        CheckinReminderPost.group_id == GROUP_A,
                        CheckinReminderPost.slot_key == key,
                    )
                )
            ).one()
        self.assertGreater(int(row.message_id), 0)
        self.assertIsNotNone(row.delivered_at)
        self.assertEqual(self.bot.send_message.await_count, 1)

        # 台账证明已投递：再预览一次是 already_delivered，不会重复发。
        with patch.object(operations_tasks, "reminder_slots", lambda: (0,)):
            again = await self._preview("checkin_reminder", {"slot": 0})
        self.assertEqual((await again.json())["preview"]["reason"], "already_delivered")
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_shop_expire_only_touches_this_group(self) -> None:
        mine = await self._seed_entitlement(group_id=GROUP_A, user_id=5)
        theirs = await self._seed_entitlement(group_id=GROUP_B, user_id=6)

        preview = await self._preview("shop_expire", {})
        preview_body = (await preview.json())["preview"]
        self.assertEqual(preview_body["status"], "ok")
        self.assertEqual(len(preview_body["targets"]), 1)
        self.assertEqual(preview_body["targets"][0]["target"]["user_id"], 5)

        confirmed = await self._confirm(preview_body["token"])
        run_key = (await confirmed.json())["run"]["run_key"]
        run = (await (await self._wait(run_key)).json())["run"]
        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        self.assertEqual(run["items"][0]["status"], ITEM_STATUS_SUCCEEDED)

        async with self.session_factory() as session:
            remaining = list((await session.scalars(select(MemberEntitlement))).all())
        self.assertEqual([int(row.id) for row in remaining], [theirs])
        self.assertEqual(int(remaining[0].group_id), GROUP_B)
        del mine

    async def test_activity_award_is_idempotent_per_week_and_per_user(self) -> None:
        # 上一个完整周：5 天活跃、每天 20 条 → 达标并写入积分流水。
        week_start, _week_end = operations_tasks.last_complete_week(now_shanghai_naive())
        week = operations_tasks.activity_week_key(week_start)
        await self._seed_activity(week_start=week_start)

        run = await self._run_once("activity_award", {})
        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        self.assertTrue(
            any(item["status"] == ITEM_STATUS_SUCCEEDED for item in run["items"]), run
        )
        async with self.session_factory() as session:
            awards = list(
                (
                    await session.scalars(
                        select(MemberPointAward).where(
                            MemberPointAward.group_id == GROUP_A,
                            MemberPointAward.reason == AWARD_REASON_WEEKLY_ACTIVITY,
                        )
                    )
                ).all()
            )
        self.assertTrue(awards)
        for row in awards:
            self.assertTrue(str(row.ref).startswith(f"weekly-activity:{week}:"))

        # 再预览一次：同一周同一人已经发过 → no_work，且不签发确认令牌。
        repeat = await self._preview("activity_award", {})
        repeat_preview = (await repeat.json())["preview"]
        self.assertEqual(repeat_preview["status"], RUN_STATUS_NO_WORK, repeat_preview)
        self.assertEqual(repeat_preview["reason"], "already_delivered")
        self.assertFalse(repeat_preview["token"])
        self.assertFalse(repeat_preview["confirmable"])
        self.assertEqual(len(awards), len(await self._award_refs()))

    async def test_run_reports_no_work_when_the_business_state_changed(self) -> None:
        """预览之后奖励被真实入口抢先入账：确认后必须如实报 no_work。

        这是"确认时状态已变"的真实路径：预览读到待发放，另一个真实入口
        （同一个业务服务）先发了这一周，于是本次执行什么都没写。
        """

        week_start, _week_end = operations_tasks.last_complete_week(now_shanghai_naive())
        week = operations_tasks.activity_week_key(week_start)
        await self._seed_activity(week_start=week_start)
        preview = await self._preview("activity_award", {})
        preview_body = (await preview.json())["preview"]
        self.assertEqual(preview_body["status"], "ok", preview_body)
        self.assertTrue(preview_body["token"])

        async with self.session_factory() as session:
            await settle_weekly_activity(
                session, group_id=GROUP_A, week_start=week_start, award=True
            )
            await session.commit()

        confirmed = await self._confirm(preview_body["token"])
        self.assertEqual(confirmed.status, 200, await confirmed.text())
        run = (await confirmed.json())["run"]
        settled = await self._wait(run["run_key"])
        run = (await settled.json())["run"]

        self.assertEqual(run["status"], RUN_STATUS_NO_WORK, run)
        self.assertTrue(run["items"], run)
        for item in run["items"]:
            self.assertEqual(item["status"], ITEM_STATUS_SKIPPED, item)
            self.assertEqual(item["reason"], REASON_ALREADY_DELIVERED, item)
            self.assertFalse(item["resendable"], item)
        self.assertEqual(
            len(await self._award_refs()),
            len([item for item in run["items"] if f":{week}:" in item["item_key"]]),
        )

    async def _run_count(self) -> int:
        return len(await self._runs())

    async def _award_refs(self) -> list[str]:
        async with self.session_factory() as session:
            return [
                str(row)
                for row in (
                    await session.scalars(
                        select(MemberPointAward.ref).where(
                            MemberPointAward.reason == AWARD_REASON_WEEKLY_ACTIVITY
                        )
                    )
                ).all()
            ]

    async def test_weekly_report_real_delivery_and_ledger_key(self) -> None:
        await self._seed_archive(count=5)
        run = await self._run_once("weekly_report", {"days": 7})
        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        item = run["items"][0]
        self.assertEqual(item["status"], ITEM_STATUS_SUCCEEDED)
        week_start, _week_end = operations_tasks.last_complete_week(
            now_shanghai_naive()
        )
        week = operations_tasks.activity_week_key(week_start)
        async with self.session_factory() as session:
            row = (
                await session.scalars(
                    select(WeeklyReportPost).where(
                        WeeklyReportPost.target_id == GROUP_A,
                        WeeklyReportPost.week_key == week,
                    )
                )
            ).one()
        self.assertGreater(int(row.message_id), 0)
        # 手动触发不会给超管私发成本摘要。
        self.assertEqual([chat for chat, _text in self.outbound], [GROUP_A])

        # 已投递 → 本周再预览就是 already_delivered。
        again = await self._preview("weekly_report", {})
        self.assertEqual(
            (await again.json())["preview"]["reason"], "already_delivered"
        )

    async def test_group_summary_real_generation_publishes_a_version(self) -> None:
        await self._seed_archive(count=40)
        store = SqlGroupSummaryStore(self.session_factory)
        scheduler = GroupSummaryScheduler(
            llm=FakeLLM(output="合成摘要：讨论了显卡行情与群规。"),
            store=store,
            config_provider=lambda: _summary_config(enabled=True),
            slot_waiter=None,
        )
        with patch.object(
            group_summary_module, "GROUP_SUMMARY_SCHEDULER", scheduler
        ), patch.object(
            operations_tasks, "group_summary_config", lambda _s: _summary_config(enabled=True)
        ):
            preview = await self._preview("group_summary", {})
            preview_body = (await preview.json())["preview"]
            self.assertEqual(preview_body["status"], "ok", preview_body)
            self.assertEqual(preview_body["cost"]["llm_tokens"], "unknown")
            confirmed = await self._confirm(preview_body["token"])
            run_key = (await confirmed.json())["run"]["run_key"]
            run = (await (await self._wait(run_key)).json())["run"]

        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)
        self.assertIn("已发布版本 v1", run["items"][0]["detail"])
        async with self.session_factory() as session:
            published = (
                await session.scalars(
                    select(GroupSummary).where(GroupSummary.group_id == GROUP_A)
                )
            ).one()
        self.assertIn("合成摘要", str(published.summary))
        self.assertEqual(int(published.version), 1)

    # ------------------------------------------------------------------
    # 重启恢复 / 进程消失
    # ------------------------------------------------------------------

    async def test_restart_keeps_unknown_and_never_resends_a_success_receipt(
        self,
    ) -> None:
        entry_id = await self._seed_scheduled()
        run = await self._run_once("scheduled_messages", {"entry_ids": [entry_id]})
        self.assertEqual(run["status"], RUN_STATUS_SUCCEEDED, run)

        # 模拟新进程：清空内存执行器注册（审计与业务台账都在库里）。
        operations_runs.reset_execution_runtime()
        operations_runs.bind_execution_runtime(
            settings=self.settings, session_factory=self.session_factory
        )
        after_restart = await self._wait(run["run_key"])
        self.assertEqual(after_restart.status, 200)
        payload = (await after_restart.json())["run"]
        self.assertEqual(payload["status"], RUN_STATUS_SUCCEEDED)
        self.assertEqual(payload["items"][0]["status"], ITEM_STATUS_SUCCEEDED)

        # 已确认成功的 receipt 绝不会因为"没有 worker 引用"被改写成失败或重发。
        retry = await self._post(f"/operations/runs/{run['run_key']}/retry")
        self.assertEqual(retry.status, 409)
        reconciled = await self._post(f"/operations/runs/{run['run_key']}/reconcile")
        self.assertEqual(reconciled.status, 200)
        self.assertEqual(
            (await reconciled.json())["run"]["items"][0]["status"],
            ITEM_STATUS_SUCCEEDED,
        )
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_reconcile_confirms_a_receipt_that_the_ledger_proves(self) -> None:
        now = now_shanghai_naive()
        key = f"{now.date().isoformat()}:0"
        # 人工构造一段"执行中断"的审计：run 未知、但业务台账其实已经登记投递。
        async with self.session_factory() as session:
            run = OperationsRun(
                run_key="a" * 32,
                token_id="b" * 32,
                task_id="checkin_reminder",
                group_id=GROUP_A,
                actor_user_id=SUPER_ADMIN,
                actor_role="superadmin",
                params={"slot": 0},
                guard_revision=0,
                source="manual",
                status=RUN_STATUS_UNKNOWN,
                created_at=now,
                started_at=now,
            )
            session.add(run)
            await session.flush()
            session.add(
                OperationsRunItem(
                    run_id=int(run.id),
                    item_key=f"checkin:{key}",
                    target={"slot": 0, "slot_key": key},
                    status=ITEM_STATUS_UNKNOWN,
                    reason="external_result_unknown",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                CheckinReminderPost(
                    group_id=GROUP_A,
                    slot_key=key,
                    message_id=4242,
                    delivered_at=now,
                    created_at=now,
                )
            )
            await session.commit()
            run_key = str(run.run_key)

        response = await self._post(f"/operations/runs/{run_key}/reconcile")
        self.assertEqual(response.status, 200)
        items = (await response.json())["run"]["items"]
        self.assertEqual(items[0]["status"], ITEM_STATUS_SUCCEEDED)
        self.assertFalse(items[0]["resendable"])
        self.assertEqual(self.bot.send_message.await_count, 0)
        # 对账之后这条已经确认成功，重试必须被拒（不重发）。
        retry = await self._post(f"/operations/runs/{run_key}/retry")
        self.assertEqual(retry.status, 409)


class OperationsRunListTests(_OperationsTestCase):
    """执行记录列表：本群隔离 + 有界分页。"""

    async def test_list_is_group_scoped_and_paginated(self) -> None:
        entry_id = await self._seed_scheduled()
        first = await self._run_once("scheduled_messages", {"entry_ids": [entry_id]})
        # 另一个群的 run（直接写库，模拟历史记录）。
        async with self.session_factory() as session:
            session.add(
                OperationsRun(
                    run_key="c" * 32,
                    token_id="d" * 32,
                    task_id="weekly_report",
                    group_id=GROUP_B,
                    actor_user_id=SUPER_ADMIN,
                    actor_role="superadmin",
                    params={},
                    guard_revision=0,
                    source="manual",
                    status=RUN_STATUS_SUCCEEDED,
                    created_at=now_shanghai_naive(),
                )
            )
            await session.commit()

        response = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/operations/runs",
            headers=self._headers(SUPER_ADMIN),
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        keys = [item["run_key"] for item in body["items"]]
        self.assertEqual(keys, [first["run_key"]])
        self.assertFalse(body["has_more"])

        limited = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/operations/runs",
            headers=self._headers(SUPER_ADMIN),
            params={"limit": 1},
        )
        self.assertEqual(limited.status, 200)
        self.assertEqual(len((await limited.json())["items"]), 1)
        bad = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/operations/runs",
            headers=self._headers(SUPER_ADMIN),
            params={"limit": 999},
        )
        self.assertEqual(bad.status, 400)

        # 别的群的 run_key 在本群路径下是 404（不泄露存在性）。
        cross = await self.client.get(
            f"/api/v1/groups/{GROUP_A}/operations/runs/{'c' * 32}",
            headers=self._headers(SUPER_ADMIN),
        )
        self.assertEqual(cross.status, 404)


def _summary_config(**overrides) -> GroupSummaryConfig:
    # ``recent_raw_messages`` 是"留给上下文、不算待摘要"的最近窗口；夹具把它压到
    # 10，这样种 40 条归档就是 30 条真正的待摘要内容。
    base = GroupSummaryConfig(
        enabled=True,
        min_refresh_seconds=0.0,
        trigger_messages=200,
        recent_raw_messages=10,
    )
    values = {**base.__dict__, **overrides}
    return GroupSummaryConfig(**values)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
