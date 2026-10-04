"""修复批 P1-2 / B-06：人工放行与永久豁免共用同一条解禁序列（别再"照抄"）。

复现的原缺陷（``AUDIT-B`` B-06）：

``_release_member_restriction_for_review``（人工放行）是
``_moderation_add_permanent_exemption``（永久豁免）的**照抄**——docstring 自己写着
"照抄"。两份实现已经在**失败处理**上分叉：

* 永久豁免：``release_moderation_restriction_after_exemption`` 返回假 → 提示
  「旧限制正在由恢复任务继续校准」并 ``request_current_update_retry()``（持久化重投递补偿）；
* 人工放行：同一个调用返回假 → 只 ``return bool(released)``，**不请求重投递**。

修法（refs 明确要求"抽一个公共函数，两处共用；把 request_current_update_retry()
提到共用层"，**不要**重写成一个巨型函数）：新增
``_lease_and_release_restriction`` + ``_UnbanRecoveryOutcome``，两条路径共用同一段
lease → commit → activate → release 序列。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.exc import IntegrityError

from bot.handlers import group as group_handler

GROUP_ID = -100777
USER_ID = 4242


def _violation() -> SimpleNamespace:
    return SimpleNamespace(id=99, group_id=GROUP_ID, user_id=USER_ID)


def _callback() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(send_message=AsyncMock()),
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )


def _session() -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


class SharedReleaseHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_release_does_not_request_retry(self) -> None:
        activated: list[object] = []
        with patch.object(
            group_handler, "lease_join_verification_for_unban", new=AsyncMock(return_value="R")
        ) as lease, patch.object(
            group_handler,
            "activate_manual_unban_recovery",
            new=lambda recovery: activated.append(recovery),
        ), patch.object(
            group_handler,
            "release_moderation_restriction_after_exemption",
            new=AsyncMock(return_value=True),
        ) as release, patch.object(
            group_handler, "request_current_update_retry"
        ) as retry:
            outcome = await group_handler._lease_and_release_restriction(
                bot="BOT", session=_session(), group_id=GROUP_ID, user_id=USER_ID, prefix="t"
            )
        self.assertTrue(outcome.needs_work)
        self.assertTrue(outcome.released)
        self.assertFalse(outcome.retry_requested)
        lease.assert_awaited_once()
        self.assertEqual(activated, ["R"], "恢复工单必须交给后台继续校准")
        release.assert_awaited_once()
        retry.assert_not_called()

    async def test_failed_release_requests_a_persistent_redelivery(self) -> None:
        """B-06 的核心：Telegram 没解干净时，**共用层**统一请求重投递。"""

        with patch.object(
            group_handler, "lease_join_verification_for_unban", new=AsyncMock(return_value="R")
        ), patch.object(
            group_handler, "activate_manual_unban_recovery", new=lambda recovery: None
        ), patch.object(
            group_handler,
            "release_moderation_restriction_after_exemption",
            new=AsyncMock(return_value=False),
        ), patch.object(
            group_handler, "request_current_update_retry"
        ) as retry:
            outcome = await group_handler._lease_and_release_restriction(
                bot="BOT", session=_session(), group_id=GROUP_ID, user_id=USER_ID, prefix="t"
            )
        self.assertTrue(outcome.needs_work)
        self.assertFalse(outcome.released)
        self.assertTrue(outcome.retry_requested)
        self.assertEqual(retry.call_count, 1)

    async def test_no_recovery_means_nothing_to_do(self) -> None:
        with patch.object(
            group_handler, "lease_join_verification_for_unban", new=AsyncMock(return_value=None)
        ), patch.object(group_handler, "request_current_update_retry") as retry:
            outcome = await group_handler._lease_and_release_restriction(
                bot="BOT", session=_session(), group_id=GROUP_ID, user_id=USER_ID, prefix="t"
            )
        self.assertFalse(outcome.needs_work)
        self.assertTrue(outcome.released, "没有待处理限制 = 视为已解禁")
        retry.assert_not_called()

    async def test_integrity_error_is_translated_only_when_asked(self) -> None:
        session = _session()
        session.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dup")))
        with patch.object(
            group_handler, "lease_join_verification_for_unban", new=AsyncMock(return_value="R")
        ):
            with self.assertRaises(RuntimeError):
                await group_handler._lease_and_release_restriction(
                    bot="BOT", session=session, group_id=GROUP_ID, user_id=USER_ID, prefix="t"
                )
            with self.assertRaises(IntegrityError):
                await group_handler._lease_and_release_restriction(
                    bot="BOT",
                    session=session,
                    group_id=GROUP_ID,
                    user_id=USER_ID,
                    prefix="t",
                    raise_on_integrity=True,
                )


class BothPathsShareTheHelperTests(unittest.IsolatedAsyncioTestCase):
    """结构断言：两条路径都必须走共用层（防止再次"照抄"漂移）。"""

    def test_neither_flow_reimplements_the_release_sequence(self) -> None:
        source = Path(group_handler.__file__).read_text(encoding="utf-8")
        for name in (
            "_moderation_add_permanent_exemption",
            "_release_member_restriction_for_review",
        ):
            with self.subTest(function=name):
                start = source.index(f"async def {name}(")
                end = source.index("\nasync def ", start + 10)
                body = source[start:end]
                self.assertIn(
                    "_lease_and_release_restriction(",
                    body,
                    f"{name} 必须调用共用解禁层",
                )
                for duplicated in (
                    "lease_join_verification_for_unban(",
                    "activate_manual_unban_recovery(",
                    "release_moderation_restriction_after_exemption(",
                ):
                    self.assertNotIn(
                        duplicated,
                        body,
                        f"{name} 不得再自己实现 {duplicated}（B-06 漂移的根因）",
                    )

    async def test_review_release_requests_redelivery_on_unban_failure(self) -> None:
        """端到端：人工放行的解禁失败现在会请求重投递（旧实现不会）。"""

        with patch.object(
            group_handler, "lease_join_verification_for_unban", new=AsyncMock(return_value="R")
        ), patch.object(
            group_handler, "activate_manual_unban_recovery", new=lambda recovery: None
        ), patch.object(
            group_handler,
            "release_moderation_restriction_after_exemption",
            new=AsyncMock(return_value=False),
        ), patch.object(
            group_handler, "request_current_update_retry"
        ) as retry:
            released = await group_handler._release_member_restriction_for_review(
                bot="BOT", session=_session(), violation=_violation()
            )
        self.assertFalse(released)
        self.assertEqual(retry.call_count, 1)

    async def test_review_release_with_no_recovery_is_not_an_error(self) -> None:
        with patch.object(
            group_handler, "lease_join_verification_for_unban", new=AsyncMock(return_value=None)
        ):
            released = await group_handler._release_member_restriction_for_review(
                bot="BOT", session=_session(), violation=_violation()
            )
        self.assertTrue(released)

    async def test_exemption_still_answers_and_mentions_recalibration(self) -> None:
        callback = _callback()
        with patch.object(
            group_handler, "lease_join_verification_for_unban", new=AsyncMock(return_value="R")
        ), patch.object(
            group_handler, "activate_manual_unban_recovery", new=lambda recovery: None
        ), patch.object(
            group_handler,
            "release_moderation_restriction_after_exemption",
            new=AsyncMock(return_value=False),
        ), patch.object(group_handler, "request_current_update_retry") as retry:
            outcome = await group_handler._moderation_add_permanent_exemption(
                callback, _session(), _violation()
            )
        self.assertEqual(outcome, "exempt")
        self.assertEqual(retry.call_count, 1)
        text = callback.answer.await_args.args[0]
        self.assertIn("已永久豁免", text)
        self.assertIn("恢复任务继续校准", text)

    async def test_exemption_without_recovery_returns_retry(self) -> None:
        callback = _callback()
        with patch.object(
            group_handler, "lease_join_verification_for_unban", new=AsyncMock(return_value=None)
        ):
            outcome = await group_handler._moderation_add_permanent_exemption(
                callback, _session(), _violation()
            )
        self.assertEqual(outcome, "retry")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
