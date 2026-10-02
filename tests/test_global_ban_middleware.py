"""F-056：全局封禁中间件删除违规消息失败时不许静默 pass。

审查结论：``except Exception: pass`` 会让被封禁成员的消息静默留在群里，运维无法
判断「为什么执法看起来生效了但消息还在」。

修好之后的口径：删除失败必须留下 WARNING 日志（含群、用户、消息 id 与异常），
删除成功则保持安静。所有 Telegram / DB 调用都是 mock。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.middlewares.global_ban import GlobalBanEnforcementMiddleware

CHAT_ID = -10077
USER_ID = 4242
MESSAGE_ID = 555


def _event(*, delete) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=CHAT_ID, type="supergroup", title="群"),
        from_user=SimpleNamespace(id=USER_ID, full_name="被封禁用户"),
        sender_chat=None,
        message_id=MESSAGE_ID,
        bot=SimpleNamespace(),
        delete=delete,
    )


class GlobalBanDeleteLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, event):
        middleware = GlobalBanEnforcementMiddleware(session_factory=object())
        handler = AsyncMock(return_value="handled")

        patches = [
            patch.object(
                GlobalBanEnforcementMiddleware,
                "_lookup_ban_policy",
                new=AsyncMock(return_value=(True, True)),
            ),
            patch(
                "bot.middlewares.global_ban.retract_removed_member_residue",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "bot.middlewares.global_ban.enforce_ban_with_policy_reconciliation_result",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        final_banned=False,
                        retryable=False,
                        group_unreachable=False,
                        operator_action_required=False,
                    )
                ),
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        return await middleware(handler, event, {})

    async def test_delete_failure_leaves_a_warning_log(self) -> None:
        event = _event(delete=AsyncMock(side_effect=RuntimeError("message can't be deleted")))

        with self.assertLogs("bot.middlewares.global_ban", level="WARNING") as logs:
            result = await self._run(event)

        self.assertIsNone(result)
        event.delete.assert_awaited_once()
        joined = "\n".join(logs.output)
        self.assertIn("message stays visible", joined)
        self.assertIn(str(USER_ID), joined)
        self.assertIn(str(MESSAGE_ID), joined)
        self.assertIn("message can't be deleted", joined)

    async def test_successful_delete_stays_quiet(self) -> None:
        event = _event(delete=AsyncMock(return_value=True))

        with self.assertNoLogs("bot.middlewares.global_ban", level="WARNING"):
            result = await self._run(event)

        self.assertIsNone(result)
        event.delete.assert_awaited_once()

    async def test_a_normal_member_is_never_deleted(self) -> None:
        event = _event(delete=AsyncMock(return_value=True))
        middleware = GlobalBanEnforcementMiddleware(session_factory=object())
        handler = AsyncMock(return_value="handled")

        with patch.object(
            GlobalBanEnforcementMiddleware,
            "_lookup_ban_policy",
            new=AsyncMock(return_value=(False, True)),
        ):
            result = await middleware(handler, event, {})

        self.assertEqual(result, "handled")
        event.delete.assert_not_awaited()
