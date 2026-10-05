"""修复批 P0-1 / B-07：``on_review_action`` 补群授权复验 + 按钮会话校验。

复现的原缺陷（``AUDIT-B`` B-07，并入 B-15）：同文件两个回调处理器对权限的处理
不一致——``on_moderation_action``（``group.py:3114-3140``）做了「群授权复验 + 群
管理员判定 + ``violation.group_id`` 必须等于当前群」三重检查；``on_review_action``
（人工放行 / **确认封禁**，执行封禁的那条）**一项都没有**：

* 群被取消授权（``is_group_authorized`` 为假）后，频道管理员仍能对**已退出**的群
  执行「确认封禁」；
* ``callback.message`` 不做 chat 校验，而 ``_edit_review_channel_status``
  （``group.py:2560``）直接用 ``callback.message.chat.id`` 作为编辑目标，缺失时
  回落到 ``_admin_log_channel_id(settings)``——即在别的会话里按了按钮会去编辑
  配置频道里 id 相同的消息。

操作者鉴权本身是合理的（``_review_operator_check``，fail-closed），所以这不是任意
用户可利用的越权；这里补的是**群授权**与**会话**这两重。

所有 Telegram / DB 调用都是替身，不触网、不落库。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot.db.models import AuthorizedGroup, Violation
from bot.handlers import group

GROUP_ID = -1001234567890
CHANNEL_ID = -1000000000001
OTHER_CHANNEL_ID = -1009999999999
SUPER_ADMIN_ID = 1
ADMIN_ID = 7
VIOLATION_ID = 99


def _settings(**moderation_overrides) -> SimpleNamespace:
    moderation = SimpleNamespace(
        enabled=True,
        log_channel_enabled=True,
        log_channel_id=CHANNEL_ID,
        review_confirm_seconds=300,
    )
    for key, value in moderation_overrides.items():
        setattr(moderation, key, value)
    return SimpleNamespace(super_admin_id=SUPER_ADMIN_ID, moderation=moderation)


def _violation(**overrides) -> SimpleNamespace:
    base = dict(
        id=VIOLATION_ID,
        group_id=GROUP_ID,
        user_id=42,
        review_state="none",
        reviewed_by=None,
        reviewed_at=None,
        action_taken="delete",
        confidence=0.97,
        verdict_reason="命中正则规则",
        message_text="秒杀 优惠券 包邮",
        source_message_id=777,
        pending_action=None,
        pending_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _callback(
    *,
    data: str = f"mrev:ban:{VIOLATION_ID}",
    operator_id: int = ADMIN_ID,
    chat_id: int = CHANNEL_ID,
    chat_type: str = "channel",
    message_missing: bool = False,
):
    answered: list[tuple[str, bool]] = []

    async def answer(text: str = "", **kwargs):
        answered.append((str(text), bool(kwargs.get("show_alert"))))
        return True

    message = None
    if not message_missing:
        message = SimpleNamespace(
            message_id=4321,
            chat=SimpleNamespace(id=chat_id, type=chat_type, title="审核日志"),
            html_text="<b>审核命中 · 证据</b>",
        )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=operator_id, username="admin", full_name="A"),
        message=message,
        bot=SimpleNamespace(
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")),
            edit_message_text=AsyncMock(),
            send_message=AsyncMock(),
        ),
        answered=answered,
        answer=answer,
    )


def _review_session(violation, *, group_authorized: bool = True) -> SimpleNamespace:
    """``is_group_authorized`` 走 ``session.get(AuthorizedGroup, …)``，与取 Violation
    同一个 ``get``，所以这里按模型分流。"""

    async def get(model, ident):
        if model is AuthorizedGroup:
            if group_authorized:
                return SimpleNamespace(id=int(ident), bot_present=True)
            return SimpleNamespace(id=int(ident), bot_present=False)
        if model is Violation:
            return violation
        return None

    return SimpleNamespace(
        get=AsyncMock(side_effect=get),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=Mock(),
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
        ),
    )


class ReviewActionAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_deauthorized_group_cannot_confirm_ban(self) -> None:
        """核心回归：群已被取消授权 → 「确认封禁」必须 fail-closed，不 arm、不执行。"""

        violation = _violation()
        session = _review_session(violation, group_authorized=False)
        callback = _callback()

        with patch("bot.handlers.admin._perform_group_ban", new=AsyncMock()) as ban:
            await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "当前群组未授权，不能执行审核操作")
        self.assertTrue(callback.answered[-1][1])
        # 不 arm、不改状态、不执行封禁。
        self.assertIsNone(violation.pending_action)
        self.assertEqual(violation.review_state, "none")
        ban.assert_not_awaited()
        callback.bot.edit_message_text.assert_not_awaited()

    async def test_deauthorized_group_cannot_release_either(self) -> None:
        violation = _violation()
        session = _review_session(violation, group_authorized=False)
        callback = _callback(data=f"mrev:rel:{VIOLATION_ID}")

        with patch("bot.handlers.admin._perform_group_ban", new=AsyncMock()) as ban:
            await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "当前群组未授权，不能执行审核操作")
        self.assertIsNone(violation.pending_action)
        ban.assert_not_awaited()

    async def test_authorized_group_still_arms(self) -> None:
        """不能把功能一起修没：授权正常的群第一次点击仍然只 arm。"""

        violation = _violation()
        session = _review_session(violation, group_authorized=True)
        callback = _callback()

        with patch("bot.handlers.admin._perform_group_ban", new=AsyncMock()) as ban:
            await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "再按一次确认")
        self.assertEqual(violation.pending_action, "ban")
        ban.assert_not_awaited()

    async def test_button_in_another_chat_is_rejected(self) -> None:
        """核心回归：按钮不在配置的审核日志频道 → 拒绝，且不去编辑配置频道的消息。"""

        violation = _violation()
        session = _review_session(violation, group_authorized=True)
        callback = _callback(chat_id=OTHER_CHANNEL_ID)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "该操作只能在审核日志频道中执行")
        self.assertTrue(callback.answered[-1][1])
        self.assertIsNone(violation.pending_action)
        callback.bot.edit_message_text.assert_not_awaited()

    async def test_button_in_a_group_chat_is_rejected(self) -> None:
        """在群里按（而不是在日志频道）同样拒绝。"""

        violation = _violation()
        session = _review_session(violation, group_authorized=True)
        callback = _callback(chat_id=GROUP_ID, chat_type="supergroup")

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "该操作只能在审核日志频道中执行")
        self.assertIsNone(violation.pending_action)

    async def test_missing_callback_message_is_rejected(self) -> None:
        """``callback.message`` 缺失时不再回落到配置频道去编辑消息。"""

        violation = _violation()
        session = _review_session(violation, group_authorized=True)
        callback = _callback(message_missing=True)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "该操作只能在审核日志频道中执行")
        self.assertIsNone(violation.pending_action)
        callback.bot.edit_message_text.assert_not_awaited()

    async def test_log_channel_unconfigured_is_rejected(self) -> None:
        """频道没配置时不存在「正确会话」，一律拒绝（fail-closed）。"""

        violation = _violation()
        session = _review_session(violation, group_authorized=True)
        callback = _callback()
        settings = _settings(log_channel_id=0)

        await group.on_review_action(callback, settings, session=session)

        self.assertEqual(callback.answered[-1][0], "该操作只能在审核日志频道中执行")
        self.assertIsNone(violation.pending_action)

    async def test_wrong_chat_is_rejected_before_operator_lookup(self) -> None:
        """会话不对时连频道管理员身份都不必去查。"""

        violation = _violation()
        session = _review_session(violation, group_authorized=True)
        callback = _callback(chat_id=OTHER_CHANNEL_ID)

        await group.on_review_action(callback, _settings(), session=session)

        callback.bot.get_chat_member.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
