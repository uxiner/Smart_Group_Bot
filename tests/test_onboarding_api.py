"""新群接入 API（Task 2）：Telegram 实时身份核验 + 原子批准 + 角色矩阵。

这一层钉死的是**放权边界**，不是功能：

* **群管理员申请 ≠ 授权**：Telegram ADMIN 提交后只有一条 pending，
  ``authorized_groups`` 一行都不许多；只有超管 approve 才开通。
* **普通用户申请 = 403**：非群管理员拿到 403，且同样不产生任何授权。
* **401 优先**：过期 / 伪造 initData 在任何 Telegram 调用之前就被拒。
* **不可核验 ≠ 通过**：Telegram 超时 / 报错时**拒绝**（明确状态），绝不按
  本地表离线放权。
* **核验不持写锁**：注入一个慢核验时，另一条独立连接仍能写库。
* **approve 后 Telegram 降级不得授权**：approve 也要复核申请人仍是管理员。
* **群隔离**：A 群的申请人读不到 B 群的任何东西。
* **入口不给特权**：申请入口不套 ``privileged_request_scope``（那是超管级资源
  优先级），并对每人每分钟限次（失败也计）。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Admin, AuthorizedGroup, GroupOnboardingRequest
from sqlalchemy import select
from bot.services import group_onboarding as onboarding
from bot.services.request_priority import ExecutionPriority
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.verify_web import VerifyWebServer

BOT_TOKEN = "42:TEST_TOKEN"
SUPER_ADMIN_ID = 42
GROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009876543210
APPLICANT_ID = 7
MEMBER_ID = 99


def _signed_init_data(
    user_id: int,
    *,
    auth_date: int | None = None,
    bot_token: str = BOT_TOKEN,
) -> str:
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAF-onboarding-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "Owner"},
            separators=(",", ":"),
        ),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(
        secret, check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(pairs)


def _admin_member(user_id: int, status: str = "administrator") -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, is_bot=False, full_name=f"U{user_id}"),
        status=status,
    )


class _FakeTelegram:
    """可控的 Telegram 读侧替身（只提供本轮真正会调用的方法）。

    延迟走 ``asyncio.sleep``：用 ``time.sleep`` 会**堵住事件循环**，那样
    「核验不持写锁」那条用例根本测不到东西（并发写入那个协程根本没机会跑）。
    """

    BOT_ID = 424242

    def __init__(self) -> None:
        self.chat_types: dict[int, str] = {
            GROUP_ID: "supergroup",
            OTHER_GROUP_ID: "supergroup",
        }
        self.admins: dict[int, list[SimpleNamespace]] = {
            GROUP_ID: [_admin_member(APPLICANT_ID), _admin_member(SUPER_ADMIN_ID)],
            OTHER_GROUP_ID: [_admin_member(MEMBER_ID)],
        }
        self.bot_status: dict[int, str] = {
            GROUP_ID: "administrator",
            OTHER_GROUP_ID: "administrator",
        }
        self.bot_permissions: dict[int, dict[str, bool]] = {
            GROUP_ID: {
                "can_delete_messages": True,
                "can_restrict_members": True,
                "can_pin_messages": True,
                "can_invite_users": False,
                "can_manage_chat": False,
            },
            OTHER_GROUP_ID: {
                "can_delete_messages": True,
                "can_restrict_members": False,
                "can_pin_messages": True,
                "can_invite_users": False,
                "can_manage_chat": False,
            },
        }
        self.fail_chat = False
        self.fail_admins = False
        self.delay_seconds = 0.0
        self.chat_calls: list[int] = []
        self.admin_calls: list[tuple[int, int]] = []

    async def _maybe_delay(self) -> None:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)

    async def get_chat(self, chat_id: int) -> SimpleNamespace:
        self.chat_calls.append(int(chat_id))
        await self._maybe_delay()
        if self.fail_chat:
            raise RuntimeError("telegram transport unavailable")
        chat_type = self.chat_types.get(int(chat_id))
        if chat_type is None:
            raise RuntimeError("Bad Request: chat not found")
        return SimpleNamespace(id=int(chat_id), type=chat_type)

    async def get_chat_administrators(self, chat_id: int) -> list[SimpleNamespace]:
        self.admin_calls.append((int(chat_id), 0))
        await self._maybe_delay()
        if self.fail_admins:
            raise RuntimeError("telegram transport unavailable")
        return list(self.admins.get(int(chat_id), []))

    async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
        self.admin_calls.append((int(chat_id), int(user_id)))
        await self._maybe_delay()
        if self.fail_chat or self.fail_admins:
            raise RuntimeError("telegram transport unavailable")
        if int(user_id) == self.BOT_ID:
            status = self.bot_status.get(int(chat_id))
            if status is None:
                return SimpleNamespace(
                    status="left",
                    user=SimpleNamespace(id=self.BOT_ID, is_bot=True),
                )
            return SimpleNamespace(
                status=status,
                user=SimpleNamespace(id=self.BOT_ID, is_bot=True),
                **self.bot_permissions.get(int(chat_id), {}),
            )
        for member in self.admins.get(int(chat_id), []):
            if int(member.user.id) == int(user_id):
                return SimpleNamespace(status=member.status, user=member.user)
        return SimpleNamespace(
            status="member", user=SimpleNamespace(id=int(user_id), is_bot=False)
        )

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=self.BOT_ID, username="fixture_bot")


class OnboardingApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token=BOT_TOKEN,
            super_admin_id=SUPER_ADMIN_ID,
            config_master_key="onboarding-api-test-key",
        )
        self.settings.bot.token = BOT_TOKEN
        self.settings.miniapp_public_base_url = "https://bot.example.com"
        self.settings.miniapp_listen_host = "127.0.0.1"
        self.settings.miniapp_listen_port = 8481
        self.settings.join_verification_public_base_url = (
            self.settings.miniapp_public_base_url
        )
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-onboarding-api.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()
        # 两张进程内滑窗表是模块级的：不清会让上一个用例用掉的次数扣到下一个
        # 用例头上（限次用例尤其明显）。测试之间必须各自从零开始。
        from bot.web.auth import reset_init_data_replay_budget

        reset_init_data_replay_budget()
        onboarding.reset_rate_limit()
        self.telegram = _FakeTelegram()
        self.bot = SimpleNamespace(
            token=BOT_TOKEN,
            get_chat=self.telegram.get_chat,
            get_chat_administrators=self.telegram.get_chat_administrators,
            get_chat_member=self.telegram.get_chat_member,
            get_me=self.telegram.get_me,
            ban_chat_member=AsyncMock(),
            unban_chat_member=AsyncMock(),
            restrict_chat_member=AsyncMock(),
        )
        server = VerifyWebServer(
            bot=self.bot,
            settings=self.settings,
            session_factory=self.session_factory,
            runtime_config=self.manager,
        )
        self.app = server.build_app()
        self.client = TestClient(TestServer(self.app))
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
    def _headers(user_id: int = APPLICANT_ID, **kwargs: object) -> dict[str, str]:
        return {"Authorization": f"tma {_signed_init_data(user_id, **kwargs)}"}

    async def _authorized_rows(self, group_id: int = GROUP_ID) -> int:
        async with self.session_factory() as session:
            return int(
                await session.scalar(
                    select(AuthorizedGroup.__table__.c.group_id).where(
                        AuthorizedGroup.group_id == group_id
                    )
                )
                is not None
            )

    async def _pending_rows(self) -> list[GroupOnboardingRequest]:
        async with self.session_factory() as session:
            return list((await session.scalars(select(GroupOnboardingRequest))).all())

    async def _submit(self, user_id: int = APPLICANT_ID, **body) -> tuple[int, dict]:
        payload = {"group_id": GROUP_ID, **body}
        response = await self.client.post(
            "/api/v1/onboarding/requests",
            json=payload,
            headers=self._headers(user_id),
        )
        return response.status, await response.json()

    # ------------------------------------------------------------------
    # 认证：401 / 403
    # ------------------------------------------------------------------
    async def test_missing_and_forged_sessions_are_rejected_before_telegram(self) -> None:
        missing = await self.client.post(
            "/api/v1/onboarding/requests", json={"group_id": GROUP_ID}
        )
        self.assertEqual(missing.status, 401)

        forged = await self.client.post(
            "/api/v1/onboarding/requests",
            json={"group_id": GROUP_ID},
            headers=self._headers(bot_token="43:WRONG_TOKEN"),
        )
        self.assertEqual(forged.status, 401)

        expired = await self.client.post(
            "/api/v1/onboarding/requests",
            json={"group_id": GROUP_ID},
            headers=self._headers(auth_date=int(time.time()) - 601),
        )
        self.assertEqual(expired.status, 401)

        self.assertEqual(self.telegram.chat_calls, [])
        self.assertEqual(self.telegram.admin_calls, [])

    async def test_non_admin_member_cannot_submit(self) -> None:
        status, body = await self._submit(user_id=MEMBER_ID)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "not_group_administrator")
        self.assertEqual(await self._pending_rows(), [])
        self.assertFalse(await self._authorized_rows())

    async def test_admin_submit_creates_pending_without_authorizing(self) -> None:
        status, body = await self._submit(
            desired_features=["moderation"], preset_id="moderation_only"
        )
        self.assertEqual(status, 200)
        request_id = body["request"]["request_id"]
        self.assertEqual(body["request"]["status"], onboarding.STATUS_PENDING)
        self.assertEqual(body["request"]["group_id"], GROUP_ID)

        rows = await self._pending_rows()
        self.assertEqual([row.request_id for row in rows], [request_id])
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))
            self.assertEqual(int(await session.scalar(select(Admin.__table__.c.id).limit(1)) or 0), 0)

    async def test_repeated_submit_is_idempotent(self) -> None:
        first_status, first = await self._submit(desired_features=["moderation"])
        second_status, second = await self._submit(desired_features=["moderation"])
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(
            first["request"]["request_id"], second["request"]["request_id"]
        )
        self.assertFalse(second["created"])
        self.assertEqual(len(await self._pending_rows()), 1)

    async def test_float_group_id_is_rejected_before_any_lookup(self) -> None:
        """``-1001234567890.75`` 不是群 id 的一种写法：拒绝，不修好再用。"""

        for bad in (-1001234567890.75, -1001234567890.0, 1e3, "1e3", True):
            response = await self.client.post(
                "/api/v1/onboarding/requests",
                json={"group_id": bad},
                headers=self._headers(),
            )
            self.assertEqual(response.status, 400, bad)
        self.assertEqual(self.telegram.chat_calls, [])
        self.assertEqual(await self._pending_rows(), [])

    async def test_check_rejects_float_group_id_before_telegram(self) -> None:
        response = await self.client.post(
            "/api/v1/onboarding/check",
            json={"group_id": -1001234567890.75},
            headers=self._headers(),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.telegram.chat_calls, [])

    async def test_total_matches_the_returned_rows_for_every_status_spelling(
        self,
    ) -> None:
        """rows 与 total 必须来自同一个归一化，否则分页自相矛盾。"""

        request_id = await self._pending_request_id()
        for spelling in ("pending", "PENDING", " pending ", "PeNdInG"):
            response = await self.client.get(
                f"/api/v1/onboarding/requests?status={spelling.replace(' ', '%20')}",
                headers=self._headers(SUPER_ADMIN_ID),
            )
            self.assertEqual(response.status, 200, spelling)
            payload = await response.json()
            self.assertGreaterEqual(
                int(payload["total"]),
                len(payload["requests"]),
                spelling,
            )
            self.assertEqual(int(payload["total"]), 1, spelling)
            self.assertEqual(
                [row["request_id"] for row in payload["requests"]], [request_id]
            )
        # 非 pending 的状态必须真的是 0，而不是"过滤失效但 total 照抄"。
        for spelling in ("approved", "rejected", "cancelled"):
            response = await self.client.get(
                f"/api/v1/onboarding/requests?status={spelling}",
                headers=self._headers(SUPER_ADMIN_ID),
            )
            payload = await response.json()
            self.assertEqual(int(payload["total"]), 0, spelling)
            self.assertEqual(payload["requests"], [], spelling)

    async def test_second_applicant_gets_a_generic_conflict(self) -> None:
        """同群已有别人的 pending：409 + 无任何申请内容。"""

        request_id = await self._pending_request_id()
        # MEMBER_ID 也做成这个群的真实管理员：两人**都**有管理权，但管理权
        # 不等于有权读另一个人的接入申请。
        self.telegram.admins[GROUP_ID] = [
            _admin_member(APPLICANT_ID),
            _admin_member(MEMBER_ID),
            _admin_member(SUPER_ADMIN_ID),
        ]
        response = await self.client.post(
            "/api/v1/onboarding/requests",
            json={"group_id": GROUP_ID},
            headers=self._headers(MEMBER_ID),
        )
        self.assertEqual(response.status, 409)
        payload = await response.json()
        self.assertEqual(payload["error"]["code"], "already_pending")
        # 断言解码后的结构：中文错误文案会被 aiohttp 转义成 \uXXXX，
        # 直接对原始文本 grep 单个数字会命中转义序列本身（假阳性）。
        self.assertNotIn(request_id, json.dumps(payload))
        self.assertNotIn(str(APPLICANT_ID), payload["error"]["message"])
        self.assertNotIn(str(MEMBER_ID), payload["error"]["message"])
        self.assertNotIn("applicant", payload["error"]["message"])
        self.assertNotIn("request_id", json.dumps(payload))

    async def test_check_never_leaks_another_applicants_pending_request(self) -> None:
        """check 里的 open_request 同样不能成为侧信道。"""

        request_id = await self._pending_request_id()
        self.telegram.admins[GROUP_ID] = [
            _admin_member(APPLICANT_ID),
            _admin_member(MEMBER_ID),
            _admin_member(SUPER_ADMIN_ID),
        ]
        response = await self.client.post(
            "/api/v1/onboarding/check",
            json={"group_id": GROUP_ID},
            headers=self._headers(MEMBER_ID),
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertIsNone(payload["open_request"])
        self.assertTrue(payload["foreign_pending"])
        # group_id 里本来就含 "7"，所以只能断言"结构里没有别人的申请"。
        self.assertEqual(payload["group_id"], GROUP_ID)
        self.assertNotIn("request_id", json.dumps(payload))
        self.assertNotIn("applicant_id", json.dumps(payload))

    async def test_same_applicant_retry_is_still_idempotent_over_http(self) -> None:
        first_status, first = await self._submit(desired_features=["moderation"])
        second_status, second = await self._submit(desired_features=["moderation"])
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(
            first["request"]["request_id"], second["request"]["request_id"]
        )

    async def test_rate_limiter_memory_stays_bounded_under_a_flood(self) -> None:
        """十万次拒绝不得在内存里留下十万个时间戳。"""

        # 前 N 次真的打了 Telegram（每次都失败 → 503），之后被限次挡在
        # Telegram **之前**（429）。两者都要能看到，否则「超限仍在发网络请求」
        # 这种退化不会被发现。
        self.telegram.fail_admins = True
        allowed = 0
        for index in range(200):
            response = await self.client.post(
                "/api/v1/onboarding/requests",
                json={"group_id": GROUP_ID},
                headers=self._headers(),
            )
            if index < onboarding.RATE_LIMIT_MAX_CALLS:
                self.assertEqual(response.status, 503, index)
                allowed += 1
            else:
                self.assertEqual(response.status, 429, index)
        self.assertEqual(allowed, onboarding.RATE_LIMIT_MAX_CALLS)
        # Telegram 只被问满额度那几次；**之后**的请求在限次层就被挡下，
        # 一个群 id 刷不出额外的网络调用。
        self.assertEqual(
            len(self.telegram.chat_calls),
            onboarding.RATE_LIMIT_MAX_CALLS,
        )
        window = onboarding._RATE_WINDOWS.get(APPLICANT_ID)
        self.assertIsNotNone(window)
        self.assertLessEqual(
            len(window),
            onboarding.RATE_LIMIT_MAX_CALLS * 2,
            f"限次表里留下了 {len(window)} 条时间戳",
        )

    async def test_rate_limiter_keeps_other_users_windows_intact(self) -> None:
        """容量压力下清理**别的用户**的限次表等于给所有人开绕过口子。"""

        for _ in range(onboarding.RATE_LIMIT_MAX_CALLS):
            await self._submit(user_id=APPLICANT_ID)
        denied = await self._submit(user_id=APPLICANT_ID)
        self.assertEqual(denied[0], 429)
        # 另一位用户仍然只受自己的额度约束：他没有因为表满而被放行。
        # （他对该群的结果是 409 already_pending 而不是 429 —— 那是另一个
        #   正当的判定，与限次无关。）
        other_status, other_body = await self._submit(user_id=SUPER_ADMIN_ID)
        self.assertNotEqual(other_status, 429)
        self.assertEqual(other_status, 409)

    async def test_terminal_approval_replay_makes_no_telegram_call_and_no_grant(
        self,
    ) -> None:
        """已批准的重放：不做任何 Telegram 核验，也不写任何授权。"""

        request_id = await self._pending_request_id()
        first = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(first.status, 200)
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    AuthorizedGroup.__table__.delete().where(
                        AuthorizedGroup.group_id == GROUP_ID
                    )
                )
                await session.execute(
                    Admin.__table__.delete().where(Admin.group_id == GROUP_ID)
                )
        self.telegram.chat_calls.clear()
        self.telegram.admin_calls.clear()

        # Telegram 此刻完全不可用：重放也不该去问，更不该因此授权。
        self.telegram.fail_admins = True
        replay = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(replay.status, 200)
        self.assertEqual(self.telegram.admin_calls, [])
        payload = await replay.json()
        self.assertTrue(payload["already_decided"])
        self.assertFalse(payload["group_authorized"])
        self.assertFalse(payload["applicant_granted"])
        self.assertFalse(payload["current"]["authorized"])
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))
            self.assertEqual(
                int(await session.scalar(select(Admin.__table__.c.id).limit(1)) or 0), 0
            )

    async def test_approved_history_is_not_reported_as_currently_authorized(self) -> None:
        """历史「已批准」与当前「已开通」必须分开显示。"""

        request_id = await self._pending_request_id()
        await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    AuthorizedGroup.__table__.update()
                    .where(AuthorizedGroup.group_id == GROUP_ID)
                    .values(bot_present=False)
                )
        replay = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        payload = await replay.json()
        self.assertEqual(payload["request"]["status"], "approved")
        self.assertFalse(payload["current"]["authorized"])
        self.assertFalse(payload["current"]["bot_present"])

    async def test_unknown_group_id_is_not_guessed_locally(self) -> None:
        """群在不在只能问 Telegram；它的**确定**答复（没有这个群）是 404。"""

        response = await self.client.post(
            "/api/v1/onboarding/requests",
            json={"group_id": -1009999999999},
            headers=self._headers(),
        )
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["error"]["code"], "group_not_found")
        self.assertIn(-1009999999999, self.telegram.chat_calls)
        self.assertEqual(await self._pending_rows(), [])

    async def test_malformed_group_id_is_rejected_without_telegram_call(self) -> None:
        """用户 id / 0 / 非数字在**语法层**就拒，不该为它们打 Telegram。"""

        for bad in (0, 12345, "not-a-group"):
            response = await self.client.post(
                "/api/v1/onboarding/requests",
                json={"group_id": bad},
                headers=self._headers(),
            )
            self.assertEqual(response.status, 400, bad)
        self.assertEqual(self.telegram.chat_calls, [])
        self.assertEqual(await self._pending_rows(), [])

    async def test_private_chat_and_channel_are_rejected(self) -> None:
        for chat_type in ("private", "channel"):
            self.telegram.chat_types[GROUP_ID] = chat_type
            status, body = await self._submit()
            self.assertEqual(status, 400, chat_type)
            self.assertEqual(body["error"]["code"], "unsupported_chat_type", chat_type)
        self.assertEqual(await self._pending_rows(), [])

    async def test_group_without_the_bot_is_rejected(self) -> None:
        self.telegram.chat_types[GROUP_ID] = "supergroup"
        self.telegram.bot_status.pop(GROUP_ID, None)
        status, body = await self._submit()
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "bot_not_in_group")
        self.assertEqual(await self._pending_rows(), [])

    async def test_telegram_failure_is_unverifiable_not_approved(self) -> None:
        self.telegram.fail_admins = True
        status, body = await self._submit()
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "telegram_unverifiable")
        self.assertEqual(await self._pending_rows(), [])
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))

    async def test_invalid_body_is_rejected(self) -> None:
        response = await self.client.post(
            "/api/v1/onboarding/requests",
            json={"group_id": GROUP_ID, "desired_features": ["root-shell"]},
            headers=self._headers(),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(await self._pending_rows(), [])

    # ------------------------------------------------------------------
    # 入口不套特权调度 + 限次
    # ------------------------------------------------------------------
    async def test_application_entry_does_not_run_at_privileged_priority(self) -> None:
        """申请入口**不得**继承 ``privileged_request_scope``（超管级资源优先级）。"""

        from bot.services.request_priority import current_execution_priority

        observed: list[int] = []

        original = onboarding.inspect_group

        async def spy(*args, **kwargs):
            observed.append(int(current_execution_priority()))
            return await original(*args, **kwargs)

        onboarding.inspect_group = spy  # type: ignore[assignment]
        try:
            status, _ = await self._submit()
        finally:
            onboarding.inspect_group = original  # type: ignore[assignment]
        self.assertEqual(status, 200)
        self.assertEqual(observed, [int(ExecutionPriority.NORMAL)])

    async def test_rate_limit_counts_failures_too(self) -> None:
        self.telegram.fail_admins = True
        for _ in range(onboarding.RATE_LIMIT_MAX_CALLS):
            status, _ = await self._submit()
            self.assertEqual(status, 503)
        limited, body = await self._submit()
        self.assertEqual(limited, 429)
        self.assertEqual(body["error"]["code"], "rate_limited")

    async def test_rate_limit_is_per_user(self) -> None:
        for _ in range(onboarding.RATE_LIMIT_MAX_CALLS):
            await self._submit(user_id=APPLICANT_ID)
        other, _ = await self._submit(user_id=SUPER_ADMIN_ID)
        self.assertNotEqual(other, 429)

    async def test_rate_limiter_rejects_oversized_arguments(self) -> None:
        with self.assertRaises(onboarding.OnboardingInvalid):
            onboarding.consume_rate_limit(APPLICANT_ID, limit=0)
        with self.assertRaises(onboarding.OnboardingInvalid):
            onboarding.consume_rate_limit(APPLICANT_ID, limit=onboarding.RATE_LIMIT_MAX_CALLS + 1)

    # ------------------------------------------------------------------
    # 审理队列的角色矩阵
    # ------------------------------------------------------------------
    async def test_queue_is_scoped_per_actor_and_cannot_be_overridden(self) -> None:
        await self._submit()
        self.telegram.admins[OTHER_GROUP_ID] = [_admin_member(APPLICANT_ID)]
        await self._submit(group_id=OTHER_GROUP_ID)

        mine = await self.client.get(
            "/api/v1/onboarding/requests",
            headers=self._headers(APPLICANT_ID),
        )
        self.assertEqual(mine.status, 200)
        rows = (await mine.json())["requests"]
        self.assertEqual(len(rows), 2)

        # 请求体里的 actor_id 不能覆盖会话身份。
        injected = await self.client.get(
            "/api/v1/onboarding/requests?actor_id=12345",
            headers=self._headers(APPLICANT_ID),
        )
        self.assertEqual(injected.status, 200)
        self.assertEqual(
            [row["request_id"] for row in (await injected.json())["requests"]],
            [row["request_id"] for row in rows],
        )

        # 另一位群管理员只看得到自己提交的。
        other = await self.client.get(
            "/api/v1/onboarding/requests",
            headers=self._headers(MEMBER_ID),
        )
        self.assertEqual(other.status, 200)
        self.assertEqual((await other.json())["requests"], [])

    async def test_queue_is_paged_and_bounded(self) -> None:
        self.telegram.admins[GROUP_ID] = [_admin_member(APPLICANT_ID)]
        for index in range(3):
            group_id = GROUP_ID - index
            self.telegram.chat_types[group_id] = "supergroup"
            self.telegram.admins[group_id] = [_admin_member(APPLICANT_ID)]
            # bot 必须也在这些群里，否则核验会以 bot_not_in_group 拒掉，
            # 那测的就不是「分页」而是「核验」了。
            self.telegram.bot_status[group_id] = "administrator"
            self.telegram.bot_permissions[group_id] = {
                "can_delete_messages": True,
                "can_restrict_members": True,
            }
            await self._submit(group_id=group_id)

        too_big = await self.client.get(
            "/api/v1/onboarding/requests?limit=101",
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(too_big.status, 400)
        zero = await self.client.get(
            "/api/v1/onboarding/requests?limit=0",
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(zero.status, 400)

        paged = await self.client.get(
            "/api/v1/onboarding/requests?limit=2",
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(paged.status, 200)
        payload = await paged.json()
        self.assertEqual(len(payload["requests"]), 2)
        self.assertEqual(payload["total"], 3)

    # ------------------------------------------------------------------
    # 审批
    # ------------------------------------------------------------------
    async def _pending_request_id(self) -> str:
        _status, body = await self._submit(desired_features=["moderation"])
        return str(body["request"]["request_id"])

    async def test_ordinary_user_cannot_approve_or_reject(self) -> None:
        request_id = await self._pending_request_id()
        for suffix in ("approve", "reject"):
            response = await self.client.post(
                f"/api/v1/onboarding/requests/{request_id}/{suffix}",
                json={},
                headers=self._headers(APPLICANT_ID),
            )
            self.assertEqual(response.status, 403, suffix)
            self.assertEqual((await response.json())["error"]["code"], "super_admin_required")
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))
            self.assertEqual(
                int(await session.scalar(select(Admin.__table__.c.id).limit(1)) or 0), 0
            )

    async def test_super_admin_approve_grants_in_one_shot(self) -> None:
        request_id = await self._pending_request_id()
        response = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={"reason": "运营确认"},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["approved"])
        self.assertTrue(payload["group_authorized"])
        self.assertTrue(payload["applicant_granted"])
        self.assertFalse(payload["already_decided"])

        async with self.session_factory() as session:
            authorized = await session.get(AuthorizedGroup, GROUP_ID)
            self.assertIsNotNone(authorized)
            self.assertTrue(bool(authorized.bot_present))
            self.assertEqual(int(authorized.authorized_by), SUPER_ADMIN_ID)
            admin = await session.scalar(
                select(Admin.__table__).where(
                    Admin.group_id == GROUP_ID,
                    Admin.user_id == APPLICANT_ID,
                )
            )
            self.assertIsNotNone(admin)
            stored = await onboarding.find_request(session, request_id)
            self.assertEqual(stored.status, onboarding.STATUS_APPROVED)
            self.assertEqual(int(stored.reviewed_by), SUPER_ADMIN_ID)
            self.assertEqual(stored.reason, "运营确认")

    async def test_repeated_approve_is_idempotent(self) -> None:
        request_id = await self._pending_request_id()
        first = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        second = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(first.status, 200)
        self.assertEqual(second.status, 200)
        self.assertTrue((await first.json())["already_decided"] is False)
        self.assertTrue((await second.json())["already_decided"] is True)
        async with self.session_factory() as session:
            self.assertEqual(
                int(await session.scalar(select(Admin.__table__.c.id).limit(1)) or 0), 1
            )

    async def test_approve_refuses_when_applicant_is_no_longer_admin(self) -> None:
        request_id = await self._pending_request_id()
        self.telegram.admins[GROUP_ID] = [_admin_member(SUPER_ADMIN_ID)]
        response = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(
            (await response.json())["error"]["code"], "not_group_administrator"
        )
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))
            stored = await onboarding.find_request(session, request_id)
            self.assertEqual(stored.status, onboarding.STATUS_PENDING)

    async def test_approve_refuses_when_telegram_degrades(self) -> None:
        """approve 必须重新核验申请人：核验失败**不得**授权。"""

        request_id = await self._pending_request_id()
        self.telegram.fail_admins = True
        response = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "telegram_unverifiable")
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))
            self.assertEqual(
                int(await session.scalar(select(Admin.__table__.c.id).limit(1)) or 0), 0
            )

    async def test_reject_is_terminal_and_cannot_be_approved(self) -> None:
        request_id = await self._pending_request_id()
        rejected = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/reject",
            json={"reason": "暂不接入"},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(rejected.status, 200)
        later = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(later.status, 409)
        self.assertEqual((await later.json())["error"]["code"], "request_state_conflict")
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))

    async def test_cancel_by_applicant_and_by_stranger(self) -> None:
        request_id = await self._pending_request_id()
        stranger = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/cancel",
            json={},
            headers=self._headers(MEMBER_ID),
        )
        self.assertEqual(stranger.status, 403)

        own = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/cancel",
            json={},
            headers=self._headers(APPLICANT_ID),
        )
        self.assertEqual(own.status, 200)
        self.assertEqual((await own.json())["request"]["status"], onboarding.STATUS_CANCELLED)

        again = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/cancel",
            json={},
            headers=self._headers(APPLICANT_ID),
        )
        self.assertEqual(again.status, 409)

    async def test_unknown_request_id_is_404(self) -> None:
        response = await self.client.post(
            "/api/v1/onboarding/requests/does-not-exist/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        self.assertEqual(response.status, 404)

    # ------------------------------------------------------------------
    # 事务边界
    # ------------------------------------------------------------------
    async def test_slow_telegram_verification_does_not_hold_the_write_lock(self) -> None:
        """核验必须发生在写事务之外：慢核验期间另一条连接仍能写库。"""

        self.telegram.delay_seconds = 0.6
        write_error: list[BaseException] = []

        async def concurrent_writer() -> None:
            try:
                async with self.session_factory() as session:
                    async with session.begin():
                        session.add(
                            AuthorizedGroup(
                                group_id=OTHER_GROUP_ID,
                                authorized_by=SUPER_ADMIN_ID,
                            )
                        )
            except BaseException as exc:  # noqa: BLE001 - 原样交给断言
                write_error.append(exc)

        task = asyncio.create_task(self._submit())
        await asyncio.sleep(0.05)
        await asyncio.wait_for(concurrent_writer(), timeout=0.5)

        status, _ = await asyncio.wait_for(task, timeout=10.0)
        self.assertEqual(status, 200)
        self.assertEqual(write_error, [])
        async with self.session_factory() as session:
            self.assertIsNotNone(await session.get(AuthorizedGroup, OTHER_GROUP_ID))

    async def test_group_a_cannot_read_or_act_on_group_b(self) -> None:
        await self._submit(group_id=GROUP_ID)
        rows = await self._pending_rows()
        self.assertEqual(len(rows), 1)
        listing = await self.client.get(
            f"/api/v1/onboarding/requests?group_id={OTHER_GROUP_ID}",
            headers=self._headers(APPLICANT_ID),
        )
        # group_id 只是过滤器：申请人看不到别人群的申请，行数必须仍是自己的。
        self.assertEqual(listing.status, 200)
        self.assertEqual(len((await listing.json())["requests"]), 1)

        # 群 A 的申请人也不能借 cancel 动别人（他本来就不是别人的申请人）。
        response = await self.client.post(
            f"/api/v1/onboarding/requests/{rows[0].request_id}/cancel",
            json={},
            headers=self._headers(MEMBER_ID),
        )
        self.assertEqual(response.status, 403)

    async def test_response_never_leaks_api_keys_or_global_config(self) -> None:
        request_id = await self._pending_request_id()
        response = await self.client.post(
            f"/api/v1/onboarding/requests/{request_id}/approve",
            json={},
            headers=self._headers(SUPER_ADMIN_ID),
        )
        raw = await response.text()
        self.assertNotIn("api_key", raw.lower())
        self.assertNotIn("secret", raw.lower())
        self.assertNotIn(BOT_TOKEN, raw)
        settings_response = await self.client.get(
            "/api/v1/settings",
            headers=self._headers(APPLICANT_ID),
        )
        self.assertEqual(settings_response.status, 403)

    async def test_check_endpoint_reports_state_without_authorizing(self) -> None:
        response = await self.client.post(
            "/api/v1/onboarding/check",
            json={"group_id": GROUP_ID, "desired_features": ["moderation"]},
            headers=self._headers(APPLICANT_ID),
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertFalse(payload["authorized"])
        self.assertTrue(payload["applicant_is_administrator"])
        self.assertEqual(payload["chat_type"], "supergroup")
        self.assertTrue(payload["bot_present"])
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))

    async def _check(self, user_id: int = APPLICANT_ID, **body) -> tuple[int, dict]:
        response = await self.client.post(
            "/api/v1/onboarding/check",
            json={"group_id": GROUP_ID, **body},
            headers=self._headers(user_id),
        )
        return response.status, await response.json()

    async def test_check_fills_the_readiness_panel_without_writing_anything(
        self,
    ) -> None:
        """体检必须真的回一份可显示的就绪文档，而不是只有标题的空壳。

        父代理在真实浏览器里点「检查就绪」，页面只有标题、下面一条都没有：
        端点当时回的是身份/权限/授权字段，唯独没有 ``readiness``。这里把
        「回文档 / 回默认功能集合 / 零写入」三条一起钉住。
        """

        status, payload = await self._check()
        self.assertEqual(status, 200, payload)
        readiness = payload["readiness"]
        self.assertTrue(readiness["capabilities"], "就绪文档里一条能力都没有")
        self.assertEqual(
            readiness["selected_features"],
            list(onboarding.DEFAULT_READINESS_FEATURES),
        )
        self.assertFalse(readiness["remote_probe"])
        self.assertIn("不会真的调用任何模型或供应商", readiness["remote_probe_note"])
        # 认得出身份/权限/授权这几件事，面板才有可理解的诊断。
        self.assertEqual(readiness["chat_type"], "supergroup")
        self.assertTrue(readiness["bot_present"])
        self.assertIsNone(payload["preset_id"])
        for item in readiness["capabilities"]:
            self.assertTrue(item["capability"])
            self.assertIsInstance(item["detail"], str)
        # 一份诊断不是一次写：不授权、不建申请。
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))
        self.assertEqual(await self._pending_rows(), [])

    async def test_check_with_a_preset_diagnoses_that_presets_real_features(
        self,
    ) -> None:
        """选了预设就按**该预设真实需要的功能**体检 —— 不再把预设丢掉。"""

        definition = onboarding.preset_definition(onboarding.PRESET_COMPANION_ID)
        status, payload = await self._check(preset_id=onboarding.PRESET_COMPANION_ID)
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["preset_id"], onboarding.PRESET_COMPANION_ID)
        self.assertEqual(payload["requested_features"], list(definition.features))
        self.assertEqual(
            payload["readiness"]["selected_features"], list(definition.features)
        )
        # 预设并没被应用：体检不写库、不授权、不建申请。
        async with self.session_factory() as session:
            self.assertIsNone(await session.get(AuthorizedGroup, GROUP_ID))
        self.assertEqual(await self._pending_rows(), [])

    async def test_check_lets_explicit_features_win_over_the_preset(self) -> None:
        status, payload = await self._check(
            preset_id=onboarding.PRESET_COMMUNITY_ID,
            desired_features=["moderation"],
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["requested_features"], ["moderation"])
        self.assertEqual(payload["readiness"]["selected_features"], ["moderation"])

    async def test_check_leaks_no_readiness_when_the_caller_is_not_an_admin(
        self,
    ) -> None:
        status, payload = await self._check(user_id=MEMBER_ID)
        self.assertEqual(status, 403, payload)
        raw = json.dumps(payload)
        self.assertNotIn("readiness", raw)
        self.assertNotIn("capabilities", raw)

    async def test_check_rejects_an_unknown_preset_without_a_readiness_document(
        self,
    ) -> None:
        status, payload = await self._check(preset_id="enable-everything")
        self.assertEqual(status, 400, payload)
        self.assertNotIn("readiness", json.dumps(payload))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()