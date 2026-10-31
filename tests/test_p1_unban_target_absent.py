"""解封清理 P1：目标本就不在群里 → 解封已完成，而不是每 15 分钟重试一次。

真机上 Telegram 对「不在该群的人」返回的不是 ``status="left"``，而是一个 Bad Request：
``member not found``（等价语义 ``USER_NOT_FOUND`` / ``PARTICIPANT_ID_INVALID``）。修复前
``_telegram_ban_failure_result`` 把它当普通失败（``ok=False`` / ``retryable=True``）：

* ``_ensure_kick_unbanned_result`` 打满 3 轮 ``unban_chat_member`` 重试；
* 每轮失败后 ``_recover_unbanning`` 走 ``_defer_terminal_record``，把
  ``join_verifications.status='unbanning'`` 的行按 ``RECOVERY_RETRY_SECONDS``（15 分钟）
  再排一次，永远删不掉——而这个人本来就不在群里，没有任何东西需要解封。

修复：``_telegram_ban_failure_result`` 给这类错误打 ``target_absent`` 标记，unban 清理
（``_ensure_kick_unbanned_result`` 与随后的权限恢复）看到它就直接按「已完成」收尾。
封禁（ban enforcement）路径的 ``ok`` / ``retryable`` 分类保持不变，其它 BadRequest
（如 ``CHAT_NOT_FOUND``、``not enough rights``）也仍然走原来的分类。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest

from bot.db.engine import init_db
from bot.services.join_verification import (
    JoinVerificationSweeper,
    VERIFICATION_STATUS_UNBANNING,
    _ensure_kick_unbanned_result,
    _telegram_ban_failure_result,
    ban_member_result,
    get_join_verification,
    lease_join_verification_for_unban,
    telegram_ban_failure_is_deterministic,
    telegram_target_is_absent_error,
)
from bot.utils.timezone import now_shanghai_naive

# 真机里「目标根本不在这个群」的三种等价说法。
_ABSENT_MESSAGES = (
    "Bad Request: member not found",
    "Bad Request: USER_NOT_FOUND",
    "Bad Request: PARTICIPANT_ID_INVALID",
    "Bad Request: participant not found",
)

# 普通 BadRequest：既不是「目标不在群里」，也不该被重新分类。
_ORDINARY_MESSAGES = (
    "Bad Request: message to edit not found",
    "Bad Request: chat member status is not changed",
    "Bad Request: FLOOD_WAIT_3",
)


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


class TargetAbsentClassificationTests(unittest.TestCase):
    """``_telegram_ban_failure_result`` 只给「目标不在群里」这一个新标记。"""

    def test_absent_markers_are_flagged(self) -> None:
        for message in _ABSENT_MESSAGES:
            with self.subTest(message=message):
                exc = _bad_request(message)
                self.assertTrue(telegram_target_is_absent_error(exc))
                failure = _telegram_ban_failure_result(exc)
                self.assertTrue(failure.target_absent)
                # 只加标记，不放宽分类：失败结果的 ok/retryable 保持原义，
                # 封禁路径读到的就是这两个字段。
                self.assertFalse(failure.ok)
                self.assertTrue(failure.retryable)
                self.assertFalse(failure.group_unreachable)
                self.assertFalse(failure.operator_action_required)

    def test_ordinary_rejections_are_not_flagged(self) -> None:
        for message in _ORDINARY_MESSAGES:
            with self.subTest(message=message):
                exc = _bad_request(message)
                failure = _telegram_ban_failure_result(exc)
                self.assertFalse(failure.target_absent)
                self.assertFalse(failure.ok)
                self.assertTrue(failure.retryable)

    def test_group_errors_keep_their_existing_classification(self) -> None:
        """``chat not found`` 仍然只是「群不可达」，不会被当成目标缺失。"""

        exc = _bad_request("Bad Request: CHAT_NOT_FOUND")
        failure = _telegram_ban_failure_result(exc)
        self.assertFalse(failure.target_absent)
        self.assertTrue(failure.group_unreachable)
        self.assertFalse(failure.retryable)

        rights = _telegram_ban_failure_result(
            _bad_request("Bad Request: not enough rights to restrict chat member")
        )
        self.assertFalse(rights.target_absent)
        self.assertTrue(rights.operator_action_required)
        self.assertFalse(rights.retryable)

    def test_ban_enforcement_semantics_are_untouched(self) -> None:
        """封禁路径：``member not found`` 仍是「未确认的终态、可重试」。"""

        for message in _ABSENT_MESSAGES:
            with self.subTest(message=message):
                self.assertFalse(telegram_ban_failure_is_deterministic(_bad_request(message)))


class UnbanCleanupTests(unittest.IsolatedAsyncioTestCase):
    """解封循环：目标缺失一次就收尾，其它错误仍然按原样重试。"""

    async def test_member_not_found_finishes_after_a_single_attempt(self) -> None:
        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: member not found")
            ),
            get_chat_member=AsyncMock(),
            ban_chat_member=AsyncMock(),
        )
        with patch(
            "bot.services.join_verification.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await _ensure_kick_unbanned_result(bot, -100, 71)

        self.assertTrue(result.ok)
        self.assertFalse(result.retryable)
        # 一次就够：既不重试 3 轮，也不再额外探一次成员的远程状态。
        self.assertEqual(bot.unban_chat_member.await_count, 1)
        bot.get_chat_member.assert_not_awaited()

    async def test_membership_probe_absence_also_finishes(self) -> None:
        """unban 响应丢失后，探针说「不在群里」同样等于已解封。"""

        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(side_effect=RuntimeError("response lost")),
            get_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: member not found")
            ),
            ban_chat_member=AsyncMock(),
        )
        with patch(
            "bot.services.join_verification.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await _ensure_kick_unbanned_result(bot, -100, 72)

        self.assertTrue(result.ok)
        self.assertEqual(bot.unban_chat_member.await_count, 1)

    async def test_other_bad_request_still_retries_three_times(self) -> None:
        for message in _ORDINARY_MESSAGES:
            with self.subTest(message=message):
                bot = SimpleNamespace(
                    unban_chat_member=AsyncMock(side_effect=_bad_request(message)),
                    get_chat_member=AsyncMock(side_effect=_bad_request(message)),
                    ban_chat_member=AsyncMock(),
                )
                with patch(
                    "bot.services.join_verification.asyncio.sleep",
                    new=AsyncMock(),
                ):
                    result = await _ensure_kick_unbanned_result(bot, -100, 73)

                self.assertFalse(result.ok)
                self.assertTrue(result.retryable)
                self.assertEqual(bot.unban_chat_member.await_count, 3)

    async def test_unreachable_group_stays_single_attempt(self) -> None:
        bot = SimpleNamespace(
            unban_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: CHAT_NOT_FOUND")
            ),
            get_chat_member=AsyncMock(),
            ban_chat_member=AsyncMock(),
        )
        with patch(
            "bot.services.join_verification.asyncio.sleep",
            new=AsyncMock(),
        ):
            result = await _ensure_kick_unbanned_result(bot, -100, 74)

        self.assertFalse(result.ok)
        self.assertTrue(result.group_unreachable)
        self.assertEqual(bot.unban_chat_member.await_count, 1)

    async def test_ban_enforcement_result_is_unchanged(self) -> None:
        """封禁路径不受影响：``final_banned`` 仍不确认、``retryable`` 仍为真。"""

        bot = SimpleNamespace(
            ban_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: member not found")
            ),
            get_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: member not found")
            ),
        )

        result = await ban_member_result(bot, -100, 75)
        self.assertIsNone(result.final_banned)
        self.assertTrue(result.retryable)
        self.assertFalse(result.group_unreachable)
        self.assertFalse(result.operator_action_required)


class SweeperUnbanningTests(unittest.IsolatedAsyncioTestCase):
    """端到端：``status='unbanning'`` 的行在目标缺失时终结，而不是 15 分钟后重排。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        from bot.services.authz import authorize_group

        async with self.session_factory() as session:
            await authorize_group(session, -100, 1)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _lease_unban(self, user_id: int) -> None:
        async with self.session_factory() as session:
            recovery = await lease_join_verification_for_unban(
                session,
                -100,
                user_id,
                now=now_shanghai_naive() - timedelta(minutes=3),
            )
            self.assertIsNotNone(recovery)
            await session.commit()

    @staticmethod
    def _bot(**overrides: object) -> SimpleNamespace:
        defaults: dict[str, object] = {
            "unban_chat_member": AsyncMock(return_value=True),
            "ban_chat_member": AsyncMock(return_value=True),
            "restrict_chat_member": AsyncMock(return_value=True),
            "get_chat_member": AsyncMock(return_value=SimpleNamespace(status="left")),
            "delete_message": AsyncMock(return_value=True),
            "edit_message_text": AsyncMock(return_value=True),
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    async def test_unbanning_row_is_deleted_when_target_is_not_in_chat(self) -> None:
        await self._lease_unban(961)
        bot = self._bot(
            unban_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: member not found")
            ),
            restrict_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: member not found")
            ),
            get_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: member not found")
            ),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
        )

        self.assertEqual(await sweeper.sweep_once(), 1)
        # 一次解封尝试即收尾，row 被删除（等价于不需要再重试）。
        self.assertEqual(bot.unban_chat_member.await_count, 1)
        async with self.session_factory() as session:
            self.assertIsNone(await get_join_verification(session, -100, 961))

    async def test_unbanning_row_keeps_retrying_on_an_ordinary_bad_request(self) -> None:
        await self._lease_unban(962)
        bot = self._bot(
            unban_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: chat member status is not changed")
            ),
            get_chat_member=AsyncMock(
                side_effect=_bad_request("Bad Request: chat member status is not changed")
            ),
        )
        sweeper = JoinVerificationSweeper(
            bot=bot,
            session_factory=self.session_factory,
        )

        self.assertEqual(await sweeper.sweep_once(), 1)
        self.assertEqual(bot.unban_chat_member.await_count, 3)
        async with self.session_factory() as session:
            record = await get_join_verification(session, -100, 962)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, VERIFICATION_STATUS_UNBANNING)
            # 仍然按原行为回队：至少 10 分钟后才再试（实际是 15 分钟）。
            self.assertGreater(
                record.lease_until,
                now_shanghai_naive() + timedelta(minutes=10),
            )
