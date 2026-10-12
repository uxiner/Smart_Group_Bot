"""第三优先第一批：只读运营任务状态面板 —— 契约与只读性测试。

这些测试先于实现编写（TDD 红检）。端点
``GET /api/v1/groups/{id}/operations/task-status`` 在实现落地之前**不存在**，
因此 HTTP 层的红检是 404（"该特性尚未实现"的语义），而不是 import / fixture
异常。

设计约束（逐条对应计划）：
* 严格逐群鉴权：缺签名 401、普通成员 403、别的群管理员 403、本群管理员 200、
  最高管理员豁免。
* 只读：整表逐字段快照对账 + 替身 Telegram 入口零调用。
* 不把「有记录」误标成功：占位行、调度水位锚、退避台账都不等于成功投递。
* 未知一律 ``null`` + ``reason``，绝不猜 0 或猜时间。
* 不泄漏：不返回原始正文、异常原文、目标用户 id、原始 message_id、target_id=0
  的超管成本摘要。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import event, select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import (
    Admin,
    AuthorizedGroup,
    CheckinReminderPost,
    Group,
    GroupSummary,
    GroupSummaryFailureState,
    MemberEntitlement,
    MemberPointAward,
    ScheduledMessage,
    ScheduledMessageOccurrence,
    WeeklyReportPost,
)
from bot.services.operations_task_status import (
    TASK_IDS,
    collect_operations_task_status,
    scheduled_message_next_plan,
)
from bot.services.scheduled_messages import scheduled_message_occurrence_at
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer
from bot.utils.timezone import SHANGHAI_TZ, now_shanghai_naive

BOT_TOKEN = "42:TEST_TOKEN"

GROUP_A = -901
GROUP_B = -902
ADMIN_A = 99
ADMIN_B = 100
SUPER_ADMIN = 42

EXPECTED_TASK_IDS = (
    "scheduled_messages",
    "group_summary",
    "checkin_reminder",
    "activity_award",
    "weekly_report",
    "shop_expire",
)

#: 只读对账覆盖的全部表（逐字段，不只是 count）。
SNAPSHOT_MODELS = (
    ScheduledMessage,
    ScheduledMessageOccurrence,
    GroupSummary,
    GroupSummaryFailureState,
    CheckinReminderPost,
    WeeklyReportPost,
    MemberPointAward,
    MemberEntitlement,
)

#: 仅属于 B 群的标记串：A 群响应里出现任何一个都算跨群泄漏。
GROUP_B_MARKERS = (
    "-902",
    "2099-W44",
    "2099-12-31:9",
    "8123457",
)


def _signed_init_data(
    user_id: int,
    *,
    auth_date: int | None = None,
    bot_token: str = BOT_TOKEN,
) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-operations-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "Owner"},
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(
        secret,
        check_string.encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(pairs)


def _iso(value: datetime) -> str:
    return value.replace(tzinfo=SHANGHAI_TZ).isoformat()


class _OperationsStatusTestCase(unittest.IsolatedAsyncioTestCase):
    """共享 harness：临时 SQLite + 合成签名的 TestClient。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN,
            config_master_key="operations-status-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8480
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-operations-status.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        #: 替身 Telegram 入口：状态端点是只读的，绝不该碰它们。
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            send_message=AsyncMock(),
            delete_message=AsyncMock(),
            pin_chat_message=AsyncMock(),
            unpin_chat_message=AsyncMock(),
            send_chat_action=AsyncMock(),
        )
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.client = TestClient(TestServer(server.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    @staticmethod
    def _headers(user_id: int = SUPER_ADMIN) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id)}"}

    def _url(self, group_id: int) -> str:
        return f"/api/v1/groups/{group_id}/operations/task-status"

    async def _seed_group(
        self,
        group_id: int,
        *,
        title: str,
        admin_user_id: int | None = None,
    ) -> None:
        async with self.session_factory() as session:
            session.add(AuthorizedGroup(group_id=group_id, authorized_by=SUPER_ADMIN))
            session.add(Group(id=group_id, title=title, settings={}))
            await session.flush()
            if admin_user_id is not None:
                session.add(
                    Admin(group_id=group_id, user_id=admin_user_id, role="admin")
                )
            await session.commit()

    async def _seed_both_groups(self) -> None:
        await self._seed_group(GROUP_A, title="Group A", admin_user_id=ADMIN_A)
        await self._seed_group(GROUP_B, title="Group B", admin_user_id=ADMIN_B)

    async def _get(self, group_id: int, *, user_id: int = SUPER_ADMIN):
        return await self.client.get(self._url(group_id), headers=self._headers(user_id))

    async def _task_status(self, group_id: int, *, user_id: int = SUPER_ADMIN) -> dict:
        response = await self._get(group_id, user_id=user_id)
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        self.assertTrue(body["ok"])
        return body["task_status"]

    async def _snapshot(self) -> dict[str, list[tuple]]:
        """逐字段快照：任何一次写入（哪怕只动 updated_at）都会被发现。"""

        snapshot: dict[str, list[tuple]] = {}
        async with self.session_factory() as session:
            for model in SNAPSHOT_MODELS:
                primary_key = list(model.__table__.primary_key.columns)
                rows = (
                    await session.scalars(select(model).order_by(*primary_key))
                ).all()
                snapshot[model.__tablename__] = [
                    tuple(
                        sorted(
                            (column.name, repr(getattr(row, column.name)))
                            for column in model.__table__.columns
                        )
                    )
                    for row in rows
                ]
        return snapshot

    def _assert_no_group_b_markers(self, text: str) -> None:
        for marker in GROUP_B_MARKERS:
            self.assertNotIn(marker, text, f"cross-group leak: {marker}")

    @staticmethod
    def _task(task_status: dict, task_id: str) -> dict:
        return next(
            task for task in task_status["tasks"] if task["task_id"] == task_id
        )


class OperationsTaskStatusApiTests(_OperationsStatusTestCase):
    # ------------------------------------------------------------------ 鉴权

    async def test_missing_signature_is_rejected(self) -> None:
        await self._seed_both_groups()
        response = await self.client.get(self._url(GROUP_A))
        self.assertEqual(response.status, 401)

    async def test_ordinary_member_is_rejected(self) -> None:
        await self._seed_both_groups()
        response = await self._get(GROUP_A, user_id=7123401)
        self.assertEqual(response.status, 403)

    async def test_other_group_admin_is_rejected(self) -> None:
        await self._seed_both_groups()
        response = await self._get(GROUP_A, user_id=ADMIN_B)
        self.assertEqual(response.status, 403)
        self.assertEqual(
            (await response.json())["error"]["code"], "group_access_denied"
        )

    async def test_group_admin_can_read_own_group(self) -> None:
        await self._seed_both_groups()
        response = await self._get(GROUP_A, user_id=ADMIN_A)
        self.assertEqual(response.status, 200, await response.text())

    async def test_super_admin_is_exempt(self) -> None:
        await self._seed_both_groups()
        response = await self._get(GROUP_B, user_id=SUPER_ADMIN)
        self.assertEqual(response.status, 200, await response.text())

    async def test_invalid_group_id_uses_existing_error_contract(self) -> None:
        response = await self.client.get(
            "/api/v1/groups/not-a-number/operations/task-status",
            headers=self._headers(),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "invalid_group_id")

    # ------------------------------------------------------- 响应契约与固定集合

    async def test_response_has_fixed_task_set_and_full_contract(self) -> None:
        await self._seed_both_groups()
        task_status = await self._task_status(GROUP_A)

        self.assertEqual(task_status["timezone"], "Asia/Shanghai")
        self.assertEqual(task_status["group_id"], GROUP_A)
        self.assertTrue(task_status["generated_at"].endswith("+08:00"))
        self.assertEqual(
            [task["task_id"] for task in task_status["tasks"]], list(EXPECTED_TASK_IDS)
        )
        self.assertEqual(list(TASK_IDS), list(EXPECTED_TASK_IDS))

        for task in task_status["tasks"]:
            self.assertTrue(task["title"], task["task_id"])
            self.assertIn(
                task["trigger"],
                {"wall_clock", "event_driven", "external_cron"},
                task["task_id"],
            )
            self.assertIn(
                task["source"]["implemented"],
                {"in_process", "external_cron_only"},
                task["task_id"],
            )
            self.assertIsInstance(task["source"]["external_schedule_known"], bool)
            self.assertFalse(task["source"]["external_schedule_known"])
            self.assertTrue(task["source"]["external_schedule_reason"])
            self.assertIsInstance(task["evidence"], list)
            self.assertIsInstance(task["unknowns"], list)
            # 每个 null 时刻都必须带 reason，绝不猜时间。
            for section in ("next_run", "last_success"):
                self.assertIn("at", task[section], task["task_id"])
                self.assertTrue(task[section]["reason"], task["task_id"])
            self.assertIn("reason", task["last_failure"])
            self.assertTrue(task["last_failure"]["reason"], task["task_id"])

    async def test_external_cron_tasks_report_unknown_schedule_not_a_guess(self) -> None:
        await self._seed_both_groups()
        task_status = await self._task_status(GROUP_A)

        # 签到默认来源是 external（现场 cron）：程序只报 external_cron_only，宿主
        # cron 的登记情况不可观测，所以下一时刻只能是 unknown，绝不猜。
        checkin = self._task(task_status, "checkin_reminder")
        self.assertEqual(checkin["trigger"], "external_cron")
        self.assertIsNone(checkin["next_run"]["at"], "checkin_reminder")
        self.assertEqual(
            checkin["next_run"]["reason"],
            "external_schedule_unknown",
            "checkin_reminder",
        )

        # B13b 更正：活动结算**没有独立自动计划**（人工发起 / 随周报结算），周报是
        # 人工任务 + **默认关闭**的周期计划（现网没有周报 cron）。两者都只能报
        # "没有下一时刻"的 reason，不许给 `now + 间隔` 之类编造的时刻。
        for task_id, reason in (
            ("activity_award", "manual_only"),
            ("weekly_report", "plan_disabled"),
        ):
            task = self._task(task_status, task_id)
            self.assertEqual(task["trigger"], "event_driven", task_id)
            self.assertIsNone(task["next_run"]["at"], task_id)
            self.assertEqual(task["next_run"]["reason"], reason, task_id)
            self.assertNotEqual(
                task["source"]["effective_source"],
                "external",
                f"{task_id} 没有外部执行来源，不许声称外部计划",
            )
        self.assertIsNone(
            self._task(task_status, "weekly_report")["details"]["plan_anchor_at"]
        )

    async def test_not_recorded_failure_is_null_and_never_zero(self) -> None:
        await self._seed_both_groups()
        task_status = await self._task_status(GROUP_A)
        for task_id in (
            "checkin_reminder",
            "activity_award",
            "weekly_report",
            "shop_expire",
        ):
            task = self._task(task_status, task_id)
            self.assertIsNone(task["last_failure"]["count"], task_id)
            self.assertEqual(task["last_failure"]["reason"], "not_recorded", task_id)

    # ------------------------------------------------------------- 跨群隔离

    async def test_group_b_data_never_leaks_into_group_a(self) -> None:
        await self._seed_both_groups()
        delivered_at = datetime(2099, 10, 30, 9, 1, 1)
        async with self.session_factory() as session:
            session.add(
                GroupSummaryFailureState(
                    group_id=GROUP_B,
                    failure_count=41,
                    backoff_until=delivered_at,
                    updated_at=delivered_at,
                )
            )
            session.add(
                CheckinReminderPost(
                    group_id=GROUP_B,
                    slot_key="2099-12-31:9",
                    message_id=612345,
                    delivered_at=delivered_at,
                )
            )
            session.add(
                MemberPointAward(
                    group_id=GROUP_B,
                    user_id=8123457,
                    points=25,
                    reason="weekly_activity",
                    ref="weekly-activity:2099-W44:8123457",
                )
            )
            session.add(
                WeeklyReportPost(
                    target_id=GROUP_B,
                    week_key="2099-W44",
                    message_id=612346,
                )
            )
            await session.commit()

        response = await self._get(GROUP_A)
        self.assertEqual(response.status, 200)
        text = await response.text()
        self._assert_no_group_b_markers(text)
        task_status = json.loads(text)["task_status"]

        self.assertIsNone(self._task(task_status, "weekly_report")["last_success"]["at"])
        self.assertEqual(
            self._task(task_status, "weekly_report")["details"]["recent_delivered_week_keys"],
            [],
        )
        self.assertEqual(
            self._task(task_status, "checkin_reminder")["details"]["delivered_count"], 0
        )
        self.assertEqual(
            self._task(task_status, "activity_award")["details"]["award_count"], 0
        )
        self.assertIsNone(
            self._task(task_status, "group_summary")["last_failure"]["count"]
        )

    async def test_super_admin_cost_digest_target_never_appears_in_group_view(self) -> None:
        await self._seed_both_groups()
        async with self.session_factory() as session:
            session.add(
                WeeklyReportPost(
                    target_id=0,
                    week_key="2099-W45",
                    message_id=612347,
                )
            )
            await session.commit()
        for group_id in (GROUP_A, GROUP_B):
            response = await self._get(group_id)
            self.assertEqual(response.status, 200)
            text = await response.text()
            self.assertNotIn("2099-W45", text)
            self.assertNotIn("612347", text)

    # ------------------------------------------------- 占位 ≠ 成功 / 水位 ≠ 成功

    async def test_weekly_report_placeholder_is_not_a_success(self) -> None:
        await self._seed_both_groups()
        async with self.session_factory() as session:
            session.add(
                WeeklyReportPost(
                    target_id=GROUP_A,
                    week_key="2026-W41",
                    message_id=0,
                )
            )
            await session.commit()
        task = self._task(await self._task_status(GROUP_A), "weekly_report")
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(task["last_success"]["reason"], "placeholder_only")
        self.assertIn("2026-W41", task["details"]["recent_placeholder_week_keys"])
        self.assertEqual(task["details"]["recent_delivered_week_keys"], [])

    async def test_weekly_report_delivered_row_reports_no_completion_time(self) -> None:
        """``created_at`` 只是登记时刻；本行没有 ``delivered_at``，不许当成完成时间。"""

        await self._seed_both_groups()
        registered_at = datetime(2026, 10, 12, 9, 0, 5)
        async with self.session_factory() as session:
            session.add(
                WeeklyReportPost(
                    target_id=GROUP_A,
                    week_key="2026-W41",
                    message_id=9876,
                    created_at=registered_at,
                )
            )
            await session.commit()
        task = self._task(await self._task_status(GROUP_A), "weekly_report")
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(task["last_success"]["basis"], "weekly_report_posts.message_id")
        self.assertEqual(task["last_success"]["reason"], "delivery_time_not_recorded")
        self.assertEqual(task["details"]["recent_delivered_week_keys"], ["2026-W41"])
        self.assertEqual(task["details"]["week_registered_at"], _iso(registered_at))

    async def test_scheduled_message_watermark_is_not_a_success(self) -> None:
        await self._seed_both_groups()
        anchored_at = now_shanghai_naive().replace(microsecond=0)
        async with self.session_factory() as session:
            session.add(
                ScheduledMessage(
                    group_id=GROUP_A,
                    text="每日公告",
                    schedule_type="daily",
                    schedule_time="23:59",
                    enabled=True,
                    last_run_at=anchored_at,
                    last_message_id=0,
                )
            )
            await session.commit()

        task = self._task(await self._task_status(GROUP_A), "scheduled_messages")
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(task["last_success"]["reason"], "no_delivery_evidence")
        self.assertIsNone(task["last_success"]["basis"])
        # 水位本身仍以「水位」名义可见，并明确标注它不是投递证据。
        self.assertEqual(task["details"]["schedule_watermark_at"], _iso(anchored_at))
        watermark_meaning = task["details"]["schedule_watermark_meaning"]
        self.assertIn("水位", watermark_meaning)
        self.assertIn("创建", watermark_meaning)

    async def test_scheduled_message_delivery_message_id_reports_no_time(self) -> None:
        await self._seed_both_groups()
        anchored_at = now_shanghai_naive().replace(microsecond=0)
        async with self.session_factory() as session:
            session.add(
                ScheduledMessage(
                    group_id=GROUP_A,
                    text="每日公告",
                    schedule_type="daily",
                    schedule_time="23:59",
                    enabled=True,
                    last_run_at=anchored_at,
                    last_message_id=4321,
                )
            )
            await session.commit()
        response = await self._get(GROUP_A)
        task = self._task((await response.json())["task_status"], "scheduled_messages")
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(task["last_success"]["basis"], "scheduled_messages.last_message_id")
        self.assertEqual(task["last_success"]["reason"], "delivery_time_not_recorded")
        self.assertTrue(task["details"]["last_delivery_message_present"])
        # 原始 message_id 不外泄。
        self.assertNotIn("4321", await response.text())

    async def test_checkin_placeholder_and_legacy_rows_are_not_successes(self) -> None:
        await self._seed_both_groups()
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=GROUP_A,
                    slot_key="2099-01-01:9",
                    message_id=0,
                    delivered_at=None,
                )
            )
            await session.commit()
        task = self._task(await self._task_status(GROUP_A), "checkin_reminder")
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(task["last_success"]["reason"], "placeholder_only")
        self.assertEqual(task["details"]["placeholder_count"], 1)
        self.assertEqual(task["details"]["delivered_count"], 0)

        # 旧台账：已投递但 delivered_at 为空（B-27 之前的数据）→ 只承认「已投递」，
        # 完成时间未知。
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=GROUP_A,
                    slot_key="2099-01-01:12",
                    message_id=61234,
                    delivered_at=None,
                )
            )
            await session.commit()
        task = self._task(await self._task_status(GROUP_A), "checkin_reminder")
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(task["last_success"]["basis"], "checkin_reminder_posts.message_id")
        self.assertEqual(task["last_success"]["reason"], "delivery_time_not_recorded")
        self.assertEqual(task["details"]["delivered_count"], 1)

    async def test_checkin_delivered_at_is_the_preferred_success_evidence(self) -> None:
        await self._seed_both_groups()
        delivered_at = datetime(2099, 1, 1, 9, 3, 7)
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=GROUP_A,
                    slot_key="2099-01-01:9",
                    message_id=61235,
                    delivered_at=delivered_at,
                )
            )
            await session.commit()
        task = self._task(await self._task_status(GROUP_A), "checkin_reminder")
        self.assertEqual(task["last_success"]["at"], _iso(delivered_at))
        self.assertEqual(
            task["last_success"]["basis"], "checkin_reminder_posts.delivered_at"
        )
        self.assertIsNone(task["last_success"]["reason"])

    # ------------------------------------------------------- 摘要元数据与退避

    async def test_group_summary_returns_metadata_only(self) -> None:
        await self._seed_both_groups()
        generated_at = datetime(2026, 10, 5, 4, 30, 0)
        failed_at = datetime(2026, 10, 5, 7, 15, 0)
        backoff_until = datetime(2026, 10, 5, 8, 0, 0)
        async with self.session_factory() as session:
            session.add(
                GroupSummary(
                    group_id=GROUP_A,
                    summary="高度机密的摘要正文",
                    version=3,
                    covered_count=42,
                    source_revision=7,
                    generated_at=generated_at,
                )
            )
            session.add(
                GroupSummaryFailureState(
                    group_id=GROUP_A,
                    failure_count=2,
                    backoff_until=backoff_until,
                    updated_at=failed_at,
                )
            )
            await session.commit()

        response = await self._get(GROUP_A)
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertNotIn("高度机密的摘要正文", text)
        task = self._task(json.loads(text)["task_status"], "group_summary")

        self.assertEqual(task["trigger"], "event_driven")
        self.assertEqual(task["last_success"]["at"], _iso(generated_at))
        self.assertEqual(task["last_success"]["basis"], "group_summaries.generated_at")
        self.assertEqual(task["last_failure"]["at"], _iso(failed_at))
        self.assertEqual(task["last_failure"]["count"], 2)
        self.assertEqual(task["last_failure"]["backoff_until"], _iso(backoff_until))
        # 这是「当前退避状态」，不是完整失败史 —— 必须显式声明。
        self.assertFalse(task["last_failure"]["history_complete"])
        self.assertEqual(task["details"]["summary_version"], 3)
        self.assertEqual(task["details"]["covered_count"], 42)
        self.assertIsNone(task["next_run"]["at"])
        self.assertEqual(task["next_run"]["reason"], "event_driven")

    # --------------------------------------------------------- 活跃奖励过滤

    async def test_activity_award_counts_only_weekly_activity_rows(self) -> None:
        await self._seed_both_groups()
        awarded_at = datetime(2026, 10, 5, 9, 0, 1)
        async with self.session_factory() as session:
            session.add(
                MemberPointAward(
                    group_id=GROUP_A,
                    user_id=8123456,
                    points=25,
                    reason="weekly_activity",
                    ref="weekly-activity:2026-W40:8123456",
                    created_at=awarded_at,
                )
            )
            # 不是周活跃：reason 不同。
            session.add(
                MemberPointAward(
                    group_id=GROUP_A,
                    user_id=8123456,
                    points=5,
                    reason="checkin",
                    ref="checkin:2026-10-01",
                )
            )
            # 不是周活跃：reason 像、但 ref 前缀不同（伪 idempotency key）。
            session.add(
                MemberPointAward(
                    group_id=GROUP_A,
                    user_id=8123456,
                    points=7,
                    reason="weekly_activity",
                    ref="other-reason:2026-W40:8123456",
                )
            )
            await session.commit()

        response = await self._get(GROUP_A)
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertNotIn("8123456", text)
        task = self._task(json.loads(text)["task_status"], "activity_award")

        self.assertEqual(task["details"]["award_count"], 1)
        self.assertEqual(task["details"]["total_points"], 25)
        self.assertEqual(task["details"]["latest_week_key"], "2026-W40")
        self.assertEqual(task["details"]["latest_award_at"], _iso(awarded_at))
        self.assertEqual(task["last_success"]["at"], _iso(awarded_at))
        self.assertEqual(
            task["last_success"]["basis"], "member_point_awards.created_at"
        )
        self.assertIn("reason", task["unknowns"][0]) if task["unknowns"] else None

    # ------------------------------------------------------------ 商店到期

    async def test_shop_expire_reports_counts_without_a_success_claim(self) -> None:
        await self._seed_both_groups()
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            session.add(
                MemberEntitlement(
                    group_id=GROUP_A,
                    user_id=8123456,
                    kind="tag",
                    payload="会员",
                    expires_at=now - timedelta(hours=1),
                )
            )
            session.add(
                MemberEntitlement(
                    group_id=GROUP_A,
                    user_id=8123456,
                    kind="pin",
                    payload="61234",
                    expires_at=now + timedelta(hours=1),
                )
            )
            await session.commit()

        task = self._task(await self._task_status(GROUP_A), "shop_expire")
        self.assertEqual(task["details"]["due_count"], 1)
        self.assertEqual(task["details"]["active_count"], 1)
        self.assertIsNone(task["next_run"]["at"])
        self.assertEqual(task["next_run"]["reason"], "runtime_not_observed")
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(task["last_success"]["reason"], "no_success_ledger")
        self.assertEqual(
            task["source"]["runner_task_name"], "shop-expiry-runner"
        )

    async def test_shop_expire_no_due_rows_is_not_a_success(self) -> None:
        await self._seed_both_groups()
        task = self._task(await self._task_status(GROUP_A), "shop_expire")
        self.assertEqual(task["details"]["due_count"], 0)
        self.assertEqual(task["details"]["active_count"], 0)
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(task["last_success"]["reason"], "no_success_ledger")

    # -------------------------------------------------- 重试待办 ≠ 已重试

    async def test_pending_retry_is_not_reported_as_retried_or_successful(self) -> None:
        await self._seed_both_groups()
        retry_at = datetime(2099, 5, 5, 12, 0, 0)
        async with self.session_factory() as session:
            entry = ScheduledMessage(
                group_id=GROUP_A,
                text="每日公告",
                schedule_type="daily",
                schedule_time="23:59",
                enabled=True,
            )
            session.add(entry)
            await session.flush()
            session.add(
                ScheduledMessageOccurrence(
                    scheduled_message_id=entry.id,
                    occurrence_at=datetime(2099, 5, 5, 23, 59, 0),
                    attempts=2,
                    next_attempt_at=retry_at,
                    last_error="telegram exploded with a secret token",
                )
            )
            await session.commit()

        response = await self._get(GROUP_A)
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertNotIn("secret token", text)
        self.assertNotIn("last_error", text)
        task = self._task(json.loads(text)["task_status"], "scheduled_messages")

        self.assertEqual(task["last_failure"]["pending_retry_at"], _iso(retry_at))
        self.assertEqual(task["last_failure"]["count"], 1)
        self.assertEqual(task["last_failure"]["kind"], "delivery_attempt_failed")
        self.assertEqual(task["details"]["retry_pending_occurrence_count"], 1)
        # 退避到期不是「已经重试过」，更不是成功。
        self.assertIsNone(task["last_success"]["at"])

    # --------------------------------------------------------------- 只读性

    async def test_endpoint_is_read_only_and_does_not_touch_telegram(self) -> None:
        await self._seed_both_groups()
        now = now_shanghai_naive()
        async with self.session_factory() as session:
            entry = ScheduledMessage(
                group_id=GROUP_A,
                text="每日公告",
                schedule_type="daily",
                schedule_time="00:00",
                enabled=True,
                last_run_at=now - timedelta(days=3),
            )
            session.add(entry)
            await session.flush()
            session.add(
                ScheduledMessageOccurrence(
                    scheduled_message_id=entry.id,
                    occurrence_at=now - timedelta(days=1),
                    attempts=1,
                    next_attempt_at=now + timedelta(minutes=5),
                    last_error="boom",
                )
            )
            session.add(GroupSummary(group_id=GROUP_A, summary="s", version=1))
            session.add(
                GroupSummaryFailureState(
                    group_id=GROUP_A,
                    failure_count=1,
                    backoff_until=now + timedelta(minutes=10),
                )
            )
            session.add(
                CheckinReminderPost(
                    group_id=GROUP_A, slot_key="2099-01-01:9", message_id=0
                )
            )
            session.add(
                WeeklyReportPost(target_id=GROUP_A, week_key="2099-W01", message_id=1)
            )
            session.add(
                MemberPointAward(
                    group_id=GROUP_A,
                    user_id=8123456,
                    points=25,
                    reason="weekly_activity",
                    ref="weekly-activity:2099-W01:8123456",
                )
            )
            session.add(
                MemberEntitlement(
                    group_id=GROUP_A,
                    user_id=8123456,
                    kind="tag",
                    payload="会员",
                    expires_at=now - timedelta(minutes=1),
                )
            )
            await session.commit()

        before = await self._snapshot()
        for _ in range(2):
            response = await self._get(GROUP_A)
            self.assertEqual(response.status, 200)
        after = await self._snapshot()
        self.assertEqual(before, after)

        for name in (
            "send_message",
            "delete_message",
            "pin_chat_message",
            "unpin_chat_message",
            "send_chat_action",
        ):
            mock = getattr(self.bot, name)
            self.assertEqual(mock.await_count, 0, name)

        # 该到期的 occurrence 没有被消费/删除，也没有被「认领」（租约保持原样）。
        async with self.session_factory() as session:
            rows = (await session.scalars(select(ScheduledMessageOccurrence))).all()
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0].lease_until)

    async def test_aggregation_does_not_swallow_storage_failures(self) -> None:
        """DB 不可用时必须抛出（由 API 层映射成明确错误），不许返回「全绿」。"""

        class _BrokenSessionFactory:
            def __call__(self):
                raise RuntimeError("database is unavailable")

        with self.assertRaises(Exception):
            await collect_operations_task_status(
                _BrokenSessionFactory(),
                group_id=GROUP_A,
                now=now_shanghai_naive(),
            )


class OperationsTaskStatusFullFactTests(_OperationsStatusTestCase):
    """R3/R4 返工：**展示窗口只能限制列表，不能限制事实**。

    父代理独立验收用真实 HTTP + 临时 DB 证明：此前「最近 N 条」的展示窗口被当成了
    整个任务的真相来源 —— 窗口外的成功被误报成 `placeholder_only`、第 21 条定时
    消息的启用状态/投递证据/水位整体消失、`due_now` 被一个未来的计划盖住。这一组
    用例全部按「全群台账」断言，窗口内外的差别只体现在列表长度上。
    """

    async def test_checkin_success_outside_the_recent_window_is_still_success(self) -> None:
        await self._seed_both_groups()
        delivered_at = datetime(2026, 1, 1, 9, 3, 7)
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=GROUP_A,
                    slot_key="2026-01-01:9",
                    message_id=61001,
                    delivered_at=delivered_at,
                    created_at=datetime(2026, 1, 1, 9, 3, 8),
                )
            )
            # 11 条更晚的占位行：按 created_at 倒序正好把那条真正的成功挤出展示窗口。
            for day in range(1, 12):
                session.add(
                    CheckinReminderPost(
                        group_id=GROUP_A,
                        slot_key=f"2026-02-{day:02d}:9",
                        message_id=0,
                        delivered_at=None,
                        created_at=datetime(2026, 2, day, 9, 0, 1),
                    )
                )
            await session.commit()

        task = self._task(await self._task_status(GROUP_A), "checkin_reminder")
        details = task["details"]
        self.assertEqual(details["delivered_count"], 1)
        self.assertEqual(details["placeholder_count"], 11)
        self.assertEqual(details["total_count"], 12)
        self.assertTrue(details["recent_posts_truncated"])
        self.assertEqual(len(details["recent_posts"]), 10)
        # 那条成功确实不在展示窗口里（否则这条用例证明不了任何东西）。
        self.assertNotIn(
            _iso(delivered_at),
            [row["delivered_at"] for row in details["recent_posts"]],
        )
        # 但成功事实来自全群台账，不受窗口影响。
        self.assertEqual(task["last_success"]["at"], _iso(delivered_at))
        self.assertEqual(
            task["last_success"]["basis"], "checkin_reminder_posts.delivered_at"
        )
        self.assertIsNone(task["last_success"]["reason"])
        self.assertEqual(details["latest_delivered_at"], _iso(delivered_at))
        self.assertEqual(details["delivered_with_time_count"], 1)
        self.assertEqual(details["delivered_missing_time_count"], 0)

    async def test_checkin_legacy_delivery_without_time_is_unknown_from_full_ledger(
        self,
    ) -> None:
        """旧台账：已投递但没有完成时刻 —— 全群取证据，只承认「已投递」。"""

        await self._seed_both_groups()
        async with self.session_factory() as session:
            session.add(
                CheckinReminderPost(
                    group_id=GROUP_A,
                    slot_key="2026-01-01:9",
                    message_id=61002,
                    delivered_at=None,
                    created_at=datetime(2026, 1, 1, 9, 3, 8),
                )
            )
            for day in range(1, 12):
                session.add(
                    CheckinReminderPost(
                        group_id=GROUP_A,
                        slot_key=f"2026-02-{day:02d}:9",
                        message_id=0,
                        delivered_at=None,
                        created_at=datetime(2026, 2, day, 9, 0, 1),
                    )
                )
            await session.commit()

        task = self._task(await self._task_status(GROUP_A), "checkin_reminder")
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(
            task["last_success"]["basis"], "checkin_reminder_posts.message_id"
        )
        self.assertEqual(task["last_success"]["reason"], "delivery_time_not_recorded")
        self.assertEqual(task["details"]["delivered_count"], 1)
        self.assertEqual(task["details"]["delivered_missing_time_count"], 1)

    async def test_weekly_success_outside_the_recent_window_is_still_success(self) -> None:
        await self._seed_both_groups()
        registered_at = datetime(2026, 1, 5, 9, 0, 5)
        async with self.session_factory() as session:
            session.add(
                WeeklyReportPost(
                    target_id=GROUP_A,
                    week_key="2026-W10",
                    message_id=9876,
                    created_at=registered_at,
                )
            )
            for week in range(41, 52):
                session.add(
                    WeeklyReportPost(
                        target_id=GROUP_A,
                        week_key=f"2026-W{week}",
                        message_id=0,
                        created_at=datetime(2026, 10, 12, 9, 0, 1),
                    )
                )
            await session.commit()

        task = self._task(await self._task_status(GROUP_A), "weekly_report")
        details = task["details"]
        self.assertEqual(details["delivered_count"], 1)
        self.assertEqual(details["placeholder_count"], 11)
        self.assertEqual(details["total_count"], 12)
        self.assertTrue(details["recent_rows_truncated"])
        # 展示窗口里一条已投递的都没有 —— 这正是此前的误报来源。
        self.assertEqual(details.get("recent_delivered_week_keys"), [])
        self.assertEqual(len(details.get("recent_placeholder_week_keys") or []), 10)
        # 而成功事实取自全群台账。
        self.assertIsNone(task["last_success"]["at"])
        self.assertEqual(task["last_success"]["basis"], "weekly_report_posts.message_id")
        self.assertEqual(task["last_success"]["reason"], "delivery_time_not_recorded")
        self.assertEqual(details["latest_delivered_week_key"], "2026-W10")
        self.assertEqual(details["week_registered_at"], _iso(registered_at))

    async def test_scheduled_message_facts_survive_the_entry_display_window(self) -> None:
        """第 21 条定时消息的证据/水位/计划不能被 20 条展示上限吃掉。"""

        await self._seed_both_groups()
        watermark = now_shanghai_naive().replace(microsecond=0)
        async with self.session_factory() as session:
            for index in range(20):
                session.add(
                    ScheduledMessage(
                        group_id=GROUP_A,
                        text=f"停用公告 {index}",
                        schedule_type="daily",
                        schedule_time="00:00",
                        enabled=False,
                        last_run_at=None,
                        last_message_id=0,
                    )
                )
            session.add(
                ScheduledMessage(
                    group_id=GROUP_A,
                    text="启用的公告",
                    schedule_type="daily",
                    schedule_time="00:00",
                    enabled=True,
                    last_run_at=watermark,
                    last_message_id=4321,
                )
            )
            await session.commit()

        task = self._task(await self._task_status(GROUP_A), "scheduled_messages")
        details = task["details"]
        self.assertEqual(details.get("entry_count"), 21)
        self.assertEqual(details.get("entries_returned"), 20)
        self.assertIs(details.get("entries_truncated"), True)
        self.assertEqual(len(details["entries"]), 20)
        # 全群投递证据与水位都不在展示窗口里，但必须被算出来。
        self.assertEqual(details.get("delivery_message_count"), 1)
        self.assertIs(details.get("last_delivery_message_present"), True)
        self.assertEqual(
            task["last_success"]["basis"], "scheduled_messages.last_message_id"
        )
        self.assertEqual(details.get("schedule_watermark_at"), _iso(watermark))
        # 唯一一条启用的配置在窗口外，但全群的「下一次计划」仍然来自它。
        self.assertEqual(task["next_run"].get("reason"), "planned_by_scheduler")
        self.assertIsNotNone(task["next_run"].get("at"))

    async def test_due_now_entry_is_not_hidden_by_a_future_plan(self) -> None:
        """R4：到期待处理与未来计划必须分开展示，不能被未来计划盖住。"""

        await self._seed_both_groups()
        today = now_shanghai_naive().replace(microsecond=0)
        tomorrow_midnight = datetime.combine(
            today.date() + timedelta(days=1), datetime.min.time()
        )
        async with self.session_factory() as session:
            # A：今天已经跑过 → 计划落在明天 00:00（未来计划）。
            session.add(
                ScheduledMessage(
                    group_id=GROUP_A,
                    text="已跑过的公告",
                    schedule_type="daily",
                    schedule_time="00:00",
                    enabled=True,
                    last_run_at=today.replace(hour=0, minute=0, second=5),
                )
            )
            # B：最后一次是昨天 → 今天 00:00 这一期还没投递（现在就该跑）。
            session.add(
                ScheduledMessage(
                    group_id=GROUP_A,
                    text="待投递的公告",
                    schedule_type="daily",
                    schedule_time="00:00",
                    enabled=True,
                    last_run_at=today.replace(hour=0, minute=0, second=5)
                    - timedelta(days=1),
                )
            )
            await session.commit()

        task = self._task(await self._task_status(GROUP_A), "scheduled_messages")
        details = task["details"]
        self.assertEqual(details.get("due_now_pending_count"), 1)
        self.assertEqual(task["next_run"].get("reason"), "due_now_pending_delivery")
        self.assertIsNone(task["next_run"].get("at"))
        # 未来计划独立可见，绝不冒充「全群下一次执行」。
        self.assertEqual(details.get("earliest_planned_at"), _iso(tomorrow_midnight))

    async def test_occurrence_facts_are_aggregated_without_a_row_window(self) -> None:
        """occurrence 只做 SQL 汇总：计数/最早重试/最大尝试次数都不受上限影响。"""

        await self._seed_both_groups()
        base = datetime(2099, 5, 5, 12, 0, 0)
        async with self.session_factory() as session:
            entry = ScheduledMessage(
                group_id=GROUP_A,
                text="每日公告",
                schedule_type="daily",
                schedule_time="23:59",
                enabled=True,
            )
            session.add(entry)
            await session.flush()
            for index in range(1, 26):
                session.add(
                    ScheduledMessageOccurrence(
                        scheduled_message_id=entry.id,
                        occurrence_at=datetime(2099, 4, 1) + timedelta(days=index),
                        attempts=index,
                        next_attempt_at=base + timedelta(minutes=index),
                        lease_until=(
                            base + timedelta(minutes=index)
                            if index <= 3
                            else None
                        ),
                        last_error=f"boom {index}",
                    )
                )
            await session.commit()

        response = await self._get(GROUP_A)
        self.assertEqual(response.status, 200)
        text = await response.text()
        self.assertNotIn("boom", text)
        self.assertNotIn("last_error", text)
        task = self._task(json.loads(text)["task_status"], "scheduled_messages")
        details = task["details"]
        self.assertEqual(details["occurrence_attempt_rows"], 25)
        self.assertEqual(details["retry_pending_occurrence_count"], 25)
        self.assertEqual(details["in_flight_occurrence_count"], 3)
        self.assertEqual(task["last_failure"]["count"], 25)
        self.assertEqual(task["last_failure"]["attempts_max"], 25)
        self.assertEqual(
            task["last_failure"]["pending_retry_at"], _iso(base + timedelta(minutes=1))
        )

    async def test_failure_count_units_are_labelled(self) -> None:
        """R4：待重试 occurrence 条数 != 失败次数；摘要的 failure_count 才是次数。"""

        await self._seed_both_groups()
        backoff_until = datetime(2099, 6, 1, 12, 0, 0)
        async with self.session_factory() as session:
            session.add(GroupSummary(group_id=GROUP_A, summary="s", version=1))
            session.add(
                GroupSummaryFailureState(
                    group_id=GROUP_A,
                    failure_count=3,
                    backoff_until=backoff_until,
                )
            )
            await session.commit()

        task_status = await self._task_status(GROUP_A)
        summary = self._task(task_status, "group_summary")
        self.assertEqual(summary["last_failure"]["count"], 3)
        self.assertEqual(
            summary["last_failure"].get("count_meaning"),
            "group_summary_cumulative_failures",
        )

        checkin = self._task(task_status, "checkin_reminder")
        self.assertIsNone(checkin["last_failure"]["count"])
        self.assertIsNone(checkin["last_failure"].get("count_meaning"))
        self.assertEqual(checkin["last_failure"]["reason"], "not_recorded")

    async def test_due_now_is_counted_even_when_a_retry_is_already_due(self) -> None:
        """R4-2：计划到期与重试队列是两个维度；到期配置不得被重试分支吞掉。"""

        await self._seed_both_groups()
        now = now_shanghai_naive().replace(microsecond=0)
        retry_at = now - timedelta(minutes=1)
        async with self.session_factory() as session:
            # 另一条配置只有未来计划：它必须独立可见，但绝不能冒充「全群下一次执行」。
            session.add(
                ScheduledMessage(
                    group_id=GROUP_A,
                    text="hourly",
                    schedule_type="interval",
                    interval_minutes=60,
                    enabled=True,
                    last_run_at=now,
                )
            )
            due_entry = ScheduledMessage(
                group_id=GROUP_A,
                text="daily",
                schedule_type="daily",
                schedule_time="00:00",
                enabled=True,
                last_run_at=now - timedelta(days=2),
            )
            session.add(due_entry)
            await session.flush()
            session.add(
                ScheduledMessageOccurrence(
                    scheduled_message_id=due_entry.id,
                    occurrence_at=now - timedelta(days=1),
                    attempts=1,
                    next_attempt_at=retry_at,
                    last_error="synthetic failure",
                )
            )
            await session.commit()

        task = self._task(await self._task_status(GROUP_A), "scheduled_messages")
        details = task["details"]
        # 到期配置 + 已到期重试同时成立：两个维度各自计数，互不吞并。
        self.assertEqual(details["due_now_pending_count"], 1)
        self.assertEqual(details["retry_due_occurrence_count"], 1)
        self.assertEqual(details["retry_backoff_occurrence_count"], 0)
        self.assertEqual(details["in_flight_occurrence_count"], 0)
        # 当前到期待处理优先；未来计划仍然独立可见。
        self.assertEqual(task["next_run"]["reason"], "due_now_pending_delivery")
        self.assertIsNone(task["next_run"]["at"])
        self.assertEqual(
            details["earliest_planned_at"], _iso(now + timedelta(minutes=60))
        )
        # 「已安排重试」既不是已重试，更不是成功。
        self.assertEqual(task["last_failure"]["retry_state"], "retry_due")
        self.assertEqual(task["last_failure"]["pending_retry_at"], _iso(retry_at))
        self.assertIsNone(task["last_success"]["at"])
        states = {entry["id"]: entry["state"] for entry in details["entries"]}
        self.assertEqual(states[due_entry.id], "due_now_pending_delivery")

    async def test_retry_states_are_counted_separately_and_never_executed(
        self,
    ) -> None:
        """R4：重试三态（已到期 / 未来退避 / 活动租约）分别计数，过期租约不算在途。"""

        await self._seed_both_groups()
        now = now_shanghai_naive().replace(microsecond=0)

        def _entry(text: str) -> ScheduledMessage:
            return ScheduledMessage(
                group_id=GROUP_A,
                text=text,
                schedule_type="interval",
                interval_minutes=60,
                enabled=True,
                last_run_at=now,
            )

        due_entry = _entry("retry due")
        backoff_entry = _entry("retry backoff")
        leased_entry = _entry("retry leased")
        expired_entry = _entry("lease expired")
        async with self.session_factory() as session:
            session.add_all([due_entry, backoff_entry, leased_entry, expired_entry])
            await session.flush()
            session.add_all(
                [
                    ScheduledMessageOccurrence(
                        scheduled_message_id=due_entry.id,
                        occurrence_at=now - timedelta(hours=2),
                        attempts=1,
                        next_attempt_at=now - timedelta(minutes=5),
                        last_error="synthetic failure",
                    ),
                    ScheduledMessageOccurrence(
                        scheduled_message_id=backoff_entry.id,
                        occurrence_at=now - timedelta(hours=2),
                        attempts=2,
                        next_attempt_at=now + timedelta(minutes=30),
                    ),
                    ScheduledMessageOccurrence(
                        scheduled_message_id=leased_entry.id,
                        occurrence_at=now - timedelta(hours=2),
                        attempts=3,
                        next_attempt_at=now + timedelta(minutes=10),
                        lease_until=now + timedelta(minutes=10),
                    ),
                    ScheduledMessageOccurrence(
                        scheduled_message_id=expired_entry.id,
                        occurrence_at=now - timedelta(hours=2),
                        attempts=4,
                        next_attempt_at=now + timedelta(minutes=20),
                        lease_until=now - timedelta(minutes=1),
                    ),
                ]
            )
            await session.commit()

        task = self._task(await self._task_status(GROUP_A), "scheduled_messages")
        details = task["details"]
        self.assertEqual(details["retry_pending_occurrence_count"], 4)
        self.assertEqual(details["retry_due_occurrence_count"], 1)
        # 未来退避 + 过期租约（崩溃残留）都只是「等下次退避」，谁也不在处理中。
        self.assertEqual(details["retry_backoff_occurrence_count"], 2)
        self.assertEqual(details["in_flight_occurrence_count"], 1)
        self.assertEqual(details["due_now_pending_count"], 0)
        self.assertEqual(task["last_failure"]["retry_state"], "retry_due")
        # 三态都不等于「已经重试过」或「成功」。
        self.assertIsNone(task["last_success"]["at"])
        states = {entry["id"]: entry["state"] for entry in details["entries"]}
        self.assertEqual(states[due_entry.id], "retry_due")
        self.assertEqual(states[backoff_entry.id], "pending_retry")
        self.assertEqual(states[leased_entry.id], "retry_in_flight")
        self.assertEqual(states[expired_entry.id], "pending_retry")

    async def test_entry_stream_never_pulls_message_bodies_into_python(self) -> None:
        """R3 机械收口：正文只取 SQL 侧存在性，语句与绑定参数里都不出现正文。"""

        await self._seed_both_groups()
        marker = "SYNTHETIC-BODY-4c1f"
        async with self.session_factory() as session:
            session.add_all(
                [
                    ScheduledMessage(
                        group_id=GROUP_A,
                        text=marker,
                        schedule_type="daily",
                        schedule_time="23:59",
                        enabled=True,
                    ),
                    ScheduledMessage(
                        group_id=GROUP_A,
                        text=f"{marker}-2",
                        schedule_type="daily",
                        schedule_time="23:58",
                        enabled=True,
                    ),
                ]
            )
            await session.commit()

        statements: list[tuple[str, str]] = []
        execution_options: list[dict] = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append((statement, repr(parameters)))
            execution_options.append(dict(context.execution_options or {}))

        event.listen(self.engine.sync_engine, "before_cursor_execute", _record)
        try:
            task = self._task(await self._task_status(GROUP_A), "scheduled_messages")
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", _record)

        self.assertEqual(task["details"]["entry_count"], 2)
        entry_statements = [
            statement
            for statement, _ in statements
            if "scheduled_messages." in statement and "SELECT" in statement.upper()
        ]
        self.assertTrue(entry_statements)
        # 正文存在性在 SQL 侧判定，而不是把正文取回 Python 再判空。
        self.assertTrue(
            any("length(trim(coalesce(" in statement for statement in entry_statements),
            entry_statements,
        )
        # 正文既不在语句里，也不作为绑定参数回到 Python。
        for statement, parameters in statements:
            self.assertNotIn(marker, statement)
            self.assertNotIn(marker, parameters)
        # 逐行流式读取必须显式声明批大小（有界内存）。
        self.assertTrue(
            any("yield_per" in options for options in execution_options),
            execution_options,
        )


class ScheduledMessageNextPlanTests(unittest.TestCase):
    """纯投影计算：与既有 scheduler 同口径，且不消费 occurrence、不改水位。"""

    @staticmethod
    def _entry(**overrides) -> ScheduledMessage:
        values = {
            "id": 1,
            "group_id": GROUP_A,
            "text": "公告",
            "schedule_type": "daily",
            "schedule_time": "09:00",
            "interval_minutes": 60,
            "enabled": True,
            "last_run_at": None,
            "created_at": datetime(2026, 10, 5, 0, 0, 0),
        }
        values.update(overrides)
        entry = ScheduledMessage(**values)
        return entry

    def test_daily_before_the_scheduled_time_plans_today(self) -> None:
        now = datetime(2026, 10, 6, 8, 0, 0)
        next_at, reason = scheduled_message_next_plan(self._entry(), now=now)
        self.assertEqual(next_at, datetime(2026, 10, 6, 9, 0, 0))
        self.assertEqual(reason, "planned_by_scheduler")

    def test_daily_after_the_scheduled_time_and_not_yet_run_is_due_now(self) -> None:
        now = datetime(2026, 10, 6, 9, 30, 0)
        next_at, reason = scheduled_message_next_plan(self._entry(), now=now)
        self.assertIsNone(next_at)
        self.assertEqual(reason, "due_now_pending_delivery")

    def test_daily_already_run_today_plans_tomorrow(self) -> None:
        now = datetime(2026, 10, 6, 9, 30, 0)
        entry = self._entry(last_run_at=datetime(2026, 10, 6, 9, 0, 3))
        next_at, reason = scheduled_message_next_plan(entry, now=now)
        self.assertEqual(next_at, datetime(2026, 10, 7, 9, 0, 0))
        self.assertEqual(reason, "planned_by_scheduler")

    def test_daily_ran_yesterday_and_now_is_before_the_time_plans_today(self) -> None:
        now = datetime(2026, 10, 6, 8, 0, 0)
        entry = self._entry(last_run_at=datetime(2026, 10, 5, 9, 0, 3))
        next_at, reason = scheduled_message_next_plan(entry, now=now)
        self.assertEqual(next_at, datetime(2026, 10, 6, 9, 0, 0))
        self.assertEqual(reason, "planned_by_scheduler")

    def test_interval_uses_last_run_or_created_at_anchor(self) -> None:
        now = datetime(2026, 10, 6, 10, 0, 0)
        entry = self._entry(
            schedule_type="interval",
            interval_minutes=90,
            created_at=datetime(2026, 10, 6, 9, 0, 0),
        )
        next_at, reason = scheduled_message_next_plan(entry, now=now)
        self.assertEqual(next_at, datetime(2026, 10, 6, 10, 30, 0))
        self.assertEqual(reason, "planned_by_scheduler")

    def test_interval_overdue_is_due_now_not_a_fabricated_plan(self) -> None:
        now = datetime(2026, 10, 6, 10, 0, 0)
        entry = self._entry(
            schedule_type="interval",
            interval_minutes=30,
            created_at=datetime(2026, 10, 6, 9, 0, 0),
        )
        next_at, reason = scheduled_message_next_plan(entry, now=now)
        self.assertIsNone(next_at)
        self.assertEqual(reason, "due_now_pending_delivery")

    def test_interval_without_any_persisted_clock_is_unknown(self) -> None:
        now = datetime(2026, 10, 6, 10, 0, 0)
        entry = self._entry(
            schedule_type="interval",
            last_run_at=None,
            created_at=None,
        )
        next_at, reason = scheduled_message_next_plan(entry, now=now)
        self.assertIsNone(next_at)
        self.assertEqual(reason, "no_persisted_clock")

    def test_disabled_entry_has_no_plan(self) -> None:
        now = datetime(2026, 10, 6, 8, 0, 0)
        next_at, reason = scheduled_message_next_plan(
            self._entry(enabled=False), now=now
        )
        self.assertIsNone(next_at)
        self.assertEqual(reason, "disabled")

    def test_empty_text_entry_has_no_plan(self) -> None:
        now = datetime(2026, 10, 6, 8, 0, 0)
        next_at, reason = scheduled_message_next_plan(
            self._entry(text="   "), now=now
        )
        self.assertIsNone(next_at)
        self.assertEqual(reason, "not_configured")

    def test_invalid_schedule_time_has_no_plan(self) -> None:
        now = datetime(2026, 10, 6, 8, 0, 0)
        next_at, reason = scheduled_message_next_plan(
            self._entry(schedule_time="25:99"), now=now
        )
        self.assertIsNone(next_at)
        self.assertEqual(reason, "invalid_schedule_time")

    def test_projection_is_pure_and_never_mutates_the_entry(self) -> None:
        now = datetime(2026, 10, 6, 8, 0, 0)
        entry = self._entry(last_run_at=datetime(2026, 10, 4, 9, 0, 0))
        before = (
            entry.last_run_at,
            entry.enabled,
            entry.schedule_time,
            entry.interval_minutes,
        )
        first = scheduled_message_next_plan(entry, now=now)
        second = scheduled_message_next_plan(entry, now=now)
        self.assertEqual(first, second)
        self.assertEqual(
            before,
            (
                entry.last_run_at,
                entry.enabled,
                entry.schedule_time,
                entry.interval_minutes,
            ),
        )

    def test_projection_agrees_with_the_scheduler_on_due_ness(self) -> None:
        """A4：同一个 ``now`` 下，本投影的「现在该跑」判定必须与 scheduler 一致。"""

        cases = (
            (self._entry(), datetime(2026, 10, 6, 8, 0, 0)),
            (self._entry(), datetime(2026, 10, 6, 9, 0, 0)),
            (self._entry(), datetime(2026, 10, 6, 9, 30, 0)),
            (
                self._entry(last_run_at=datetime(2026, 10, 6, 9, 0, 3)),
                datetime(2026, 10, 6, 9, 30, 0),
            ),
            (
                self._entry(last_run_at=datetime(2026, 10, 5, 9, 0, 3)),
                datetime(2026, 10, 6, 8, 0, 0),
            ),
            (
                self._entry(
                    schedule_type="interval",
                    interval_minutes=90,
                    created_at=datetime(2026, 10, 6, 9, 0, 0),
                ),
                datetime(2026, 10, 6, 10, 0, 0),
            ),
            (
                self._entry(
                    schedule_type="interval",
                    interval_minutes=30,
                    created_at=datetime(2026, 10, 6, 9, 0, 0),
                ),
                datetime(2026, 10, 6, 10, 0, 0),
            ),
            (self._entry(enabled=False), datetime(2026, 10, 6, 10, 0, 0)),
            (self._entry(text=""), datetime(2026, 10, 6, 10, 0, 0)),
            (self._entry(schedule_time="25:99"), datetime(2026, 10, 6, 10, 0, 0)),
        )
        for entry, now in cases:
            with self.subTest(now=now, entry=entry):
                planned_at, reason = scheduled_message_next_plan(entry, now=now)
                scheduler_says_due = (
                    scheduled_message_occurrence_at(entry, now=now) is not None
                )
                self.assertEqual(
                    reason == "due_now_pending_delivery",
                    scheduler_says_due,
                )
                if planned_at is not None:
                    self.assertGreater(planned_at, now)



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
