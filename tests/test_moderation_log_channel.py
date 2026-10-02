"""审核命中证据 → 频道 + 「人工放行 / 放行收回」两个按钮。

覆盖：

- 普通成员命中并被处置 → 频道收到一条证据卡（标题「审核命中 · 证据」），
  且**没有**发给最高管理员的私聊；卡片按钮载荷带 ``violations.id``；
- 群管理员命中 → 频道卡片标题保持「管理员违规 · 证据」；
- 频道未启用（``log_channel_enabled=false`` 或未配置 ``log_channel_id``）→
  回到私聊老路径：普通成员不发、管理员发私聊；
- 非最高管理员点按钮 → ``callback.answer("无权限", show_alert=True)``，
  不改状态、不调 Telegram。
- 人工放行（``mrev:rel:``）→ ``review_state='released'`` + 调用解禁流程 +
  频道新发一条以「🟢 人工放行 · 待调整规则」开头的交接消息（带 mention 实体）；
- 未放行就收回（``mrev:rev:``）→ 只弹提示、状态不变；
- 放行后收回 → ``review_state='revoked'`` + 「🔴 放行收回 · 无需调整」交接消息。

所有 Telegram / LLM / DB 调用都是替身，不触网、不落库。
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import unittest

from bot.handlers import group
from bot.services.moderation import ModerationVerdict
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -1001234567890
CHANNEL_ID = -1004337744233
OWNER_ID = 1
ADMIN_ID = 7
MEMBER_ID = 42
MESSAGE_ID = 777
SUPER_ADMIN_ID = 1

CARD_HTML = (
    "<b>审核命中 · 证据</b>\n\n"
    "<blockquote><b>对象</b>　Member @member（id:42）</blockquote>\n\n"
    "<blockquote><b>身份</b>　TG 群管理员/群主</blockquote>\n\n"
    "<blockquote><b>群组</b>　测试群（id:-1001234567890）</blockquote>\n\n"
    "<blockquote><b>命中规则</b>　#5（正则） 秒杀|优惠券</blockquote>\n\n"
    "<blockquote><b>动作</b>　delete</blockquote>\n\n"
    "<blockquote><b>置信度</b>　0.97</blockquote>\n\n"
    "<blockquote><b>判定理由</b>　命中正则规则</blockquote>\n\n"
    "<blockquote><b>送审原文</b>　秒杀 优惠券 包邮</blockquote>\n\n"
    '<blockquote><b>消息回链</b>　<a href="https://t.me/c/1234567890/777">点此查看</a></blockquote>'
)


def _settings(**moderation_overrides) -> SimpleNamespace:
    moderation = SimpleNamespace(
        enabled=True,
        warn_threshold=3,
        high_confidence_threshold=0.9,
        challenge_timeout_seconds=600,
        bot_screening_enabled=True,
        bot_screening_message_count=5,
        nsfw_image_guard_enabled=True,
        punish_quoted_author_enabled=True,
        quoted_author_max_age_seconds=7 * 24 * 60 * 60,
        admin_moderation_enabled=True,
        admin_alert_super_admin_enabled=True,
        log_channel_enabled=True,
        log_channel_id=CHANNEL_ID,
    )
    for key, value in moderation_overrides.items():
        setattr(moderation, key, value)
    return SimpleNamespace(
        super_admin_id=SUPER_ADMIN_ID,
        bot=SimpleNamespace(
            main_model="",
            decision_model="",
            compress_model="",
            moderation_model="",
            vision_model="",
            embed_model="",
            max_context_tokens=0,
            auto_delete_seconds=0,
        ),
        moderation=moderation,
        skill_sticker_file_ids="",
    )


def _message(
    *,
    user_id: int = MEMBER_ID,
    text: str = "秒杀 优惠券 包邮",
    username: str = "member",
    full_name: str = "Member",
) -> SimpleNamespace:
    bot = SimpleNamespace(
        me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1)),
        ban_chat_member=AsyncMock(return_value=True),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=9001)),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=9002)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=9003)),
    )
    message = SimpleNamespace(
        message_id=MESSAGE_ID,
        date=now_shanghai_naive(),
        chat=SimpleNamespace(
            id=GROUP_ID,
            type="supergroup",
            title="测试群",
            username=None,
            ban=AsyncMock(),
        ),
        from_user=SimpleNamespace(
            id=user_id, is_bot=False, username=username, full_name=full_name
        ),
        sender_chat=None,
        text=text,
        caption=None,
        photo=None,
        document=None,
        delete=AsyncMock(),
        answer=AsyncMock(
            return_value=SimpleNamespace(
                chat=SimpleNamespace(id=GROUP_ID), message_id=9100
            )
        ),
        bot=bot,
    )
    return message


class _ViolationStore:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, int | None]] = []

    async def record(
        self,
        session,
        group_id,
        user_id,
        text,
        action,
        rule=None,
        *,
        source_message_id=None,
        confidence=None,
        verdict_reason="",
    ):
        self.calls.append((int(user_id), str(action), source_message_id))
        violation = SimpleNamespace(
            id=len(self.calls),
            notice_sent_at=None,
            action_taken=str(action),
            warning_count=None,
        )
        setattr(violation, "_source_event_created", True)
        return violation


def _verdict(
    *,
    violated: bool = True,
    action: str = "delete",
    rule_id: int = 5,
    rule_type: str = "regex",
    pattern: str = "秒杀|优惠券",
    confidence: float = 0.97,
    reason: str = "命中正则规则",
    conclusive: bool = True,
) -> ModerationVerdict:
    rule = SimpleNamespace(id=rule_id, action=action, rule_type=rule_type, pattern=pattern)
    return ModerationVerdict(
        violated=violated,
        reason=reason if violated else "",
        rule=rule if violated else None,
        conclusive=conclusive,
        confidence=confidence if violated else 0.0,
    )


async def _run_group(
    store: _ViolationStore,
    *,
    message: SimpleNamespace,
    settings: SimpleNamespace,
    verdict: ModerationVerdict,
    tg_admin: bool = False,
    super_admin: bool = False,
) -> SimpleNamespace:
    """把一条群消息喂给 ``on_group_message``（全部 Telegram/LLM 打桩）。"""

    sender_id = int(getattr(message.from_user, "id", 0) or 0)
    session = SimpleNamespace(
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        delete=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
    )
    moderation = SimpleNamespace(
        is_user_exempt=AsyncMock(return_value=False),
        evaluate=AsyncMock(return_value=verdict),
        is_high_confidence=(
            lambda candidate: bool(candidate.conclusive)
            and float(candidate.confidence) >= 0.9
        ),
        record_violation=AsyncMock(side_effect=store.record),
        add_warning=AsyncMock(return_value=(1, False)),
    )
    patches = [
        patch(
            "bot.handlers.group.ensure_group_authorized",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "bot.handlers.group._fresh_group_authorized_for_moderation",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "bot.handlers.group._claim_current_moderation_verdict",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "bot.handlers.group._record_group_activity_cas",
            new=AsyncMock(return_value={"mute_all_replies": True}),
        ),
        patch(
            "bot.handlers.group.extract_message_text",
            return_value=(str(getattr(message, "text", "") or ""), "text"),
        ),
        patch(
            "bot.handlers.group._append_image_context",
            new=AsyncMock(return_value=(str(getattr(message, "text", "") or ""), "")),
        ),
        patch(
            "bot.handlers.group._build_reply_context_for_llm",
            new=AsyncMock(return_value=""),
        ),
        patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
        patch(
            "bot.handlers.group.build_moderation_context",
            new=AsyncMock(return_value=([], False)),
        ),
        patch(
            "bot.handlers.group.is_super_admin_user_id",
            new=lambda user_id, _settings=None: bool(super_admin)
            and int(user_id or 0) == sender_id,
        ),
        patch(
            "bot.handlers.group._is_user_admin_cached",
            new=AsyncMock(return_value=tg_admin),
        ),
        patch("bot.handlers.group.LLMService", return_value=object()),
        patch("bot.handlers.group.ModerationService", return_value=moderation),
        patch(
            "bot.handlers.group.moderation_challenge_ready", return_value=True
        ),
        patch(
            "bot.handlers.group.answer_with_auto_delete",
            new=AsyncMock(return_value=True),
        ),
    ]
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        await group.on_group_message(message, session=session, settings=settings)
    message._session = session
    message._moderation = moderation
    return message


def _violation(**overrides) -> SimpleNamespace:
    base = dict(
        id=99,
        group_id=GROUP_ID,
        user_id=MEMBER_ID,
        review_state="none",
        reviewed_by=None,
        reviewed_at=None,
        action_taken="delete",
        confidence=0.97,
        verdict_reason="命中正则规则",
        message_text="秒杀 优惠券 包邮",
        source_message_id=MESSAGE_ID,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _callback(*, data: str, operator_id: int, html_text: str | None = CARD_HTML):
    answered: list[tuple[str, bool]] = []

    async def answer(text: str = "", **kwargs):
        answered.append((str(text), bool(kwargs.get("show_alert"))))
        return True

    callback = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=operator_id, username="owner", full_name="Owner"),
        message=SimpleNamespace(
            message_id=4321,
            chat=SimpleNamespace(id=CHANNEL_ID, type="channel", title="审核日志"),
            html_text=html_text,
        ),
        bot=SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1)),
            edit_message_text=AsyncMock(return_value=True),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=5555)),
        ),
        answer=AsyncMock(side_effect=answer),
    )
    callback.answered = answered
    return callback


def _review_session(
    violation: SimpleNamespace, *, exempt: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        get=AsyncMock(return_value=violation),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalar_one_or_none=lambda: (object() if exempt else None)
            )
        ),
    )


class LogChannelEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        group._ADMIN_ALERT_STATE.clear()

    def tearDown(self) -> None:
        group._ADMIN_ALERT_STATE.clear()

    async def test_member_hit_posts_evidence_card_to_channel_and_never_dms(self) -> None:
        store = _ViolationStore()
        message = await _run_group(
            store,
            message=_message(user_id=MEMBER_ID),
            settings=_settings(),
            verdict=_verdict(action="delete"),
        )

        message.bot.send_message.assert_awaited()
        kwargs = message.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], CHANNEL_ID)
        self.assertIn("审核命中 · 证据", kwargs["text"])
        self.assertIn("秒杀 优惠券 包邮", kwargs["text"])
        # 绝不能私聊最高管理员
        for call in message.bot.send_message.await_args_list:
            self.assertNotEqual(call.kwargs.get("chat_id"), SUPER_ADMIN_ID)
        # 违规记录照旧
        self.assertEqual(store.calls, [(MEMBER_ID, "delete", MESSAGE_ID)])

    async def test_channel_evidence_buttons_carry_violation_id(self) -> None:
        store = _ViolationStore()
        message = await _run_group(
            store,
            message=_message(user_id=MEMBER_ID),
            settings=_settings(),
            verdict=_verdict(action="delete"),
        )

        keyboard = message.bot.send_message.await_args.kwargs["reply_markup"]
        row = keyboard.inline_keyboard[0]
        self.assertEqual(len(row), 2)
        self.assertEqual(row[0].text, "人工放行")
        self.assertEqual(row[1].text, "放行收回")
        # violation id 是 1（第一条 record）
        self.assertEqual(row[0].callback_data, "mrev:rel:1")
        self.assertEqual(row[1].callback_data, "mrev:rev:1")

    async def test_admin_hit_channel_card_keeps_admin_title(self) -> None:
        store = _ViolationStore()
        message = await _run_group(
            store,
            message=_message(user_id=ADMIN_ID, username="admin"),
            settings=_settings(),
            verdict=_verdict(action="delete"),
            tg_admin=True,
        )

        kwargs = message.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], CHANNEL_ID)
        self.assertIn("管理员违规 · 证据", kwargs["text"])
        for call in message.bot.send_message.await_args_list:
            self.assertNotEqual(call.kwargs.get("chat_id"), SUPER_ADMIN_ID)

    async def test_ban_challenge_hit_posts_channel_evidence(self) -> None:
        store = _ViolationStore()
        begin = AsyncMock(return_value=True)
        with patch("bot.handlers.group.begin_moderation_challenge", new=begin):
            message = await _run_group(
                store,
                message=_message(user_id=MEMBER_ID),
                settings=_settings(),
                verdict=_verdict(action="ban", rule_id=6, pattern="招募探花"),
            )

        begin.assert_awaited_once()
        self.assertEqual(store.calls[0][1], "challenge")
        kwargs = message.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], CHANNEL_ID)
        self.assertIn("审核命中 · 证据", kwargs["text"])

    async def test_channel_posts_every_hit_without_aggregation(self) -> None:
        """频道取代私聊后，同一人连续命中不再被 10 分钟聚合抑制：每条都单独发。"""

        store = _ViolationStore()
        message = _message(user_id=MEMBER_ID)
        for _ in range(7):
            message = await _run_group(
                store,
                message=message,
                settings=_settings(),
                verdict=_verdict(action="delete"),
            )

        self.assertEqual(message.bot.send_message.await_count, 7)
        for call in message.bot.send_message.await_args_list:
            self.assertEqual(call.kwargs.get("chat_id"), CHANNEL_ID)

    async def test_channel_disabled_falls_back_to_private_for_admin(self) -> None:
        store = _ViolationStore()
        message = await _run_group(
            store,
            message=_message(user_id=ADMIN_ID, username="admin"),
            settings=_settings(log_channel_enabled=False),
            verdict=_verdict(action="delete"),
            tg_admin=True,
        )

        kwargs = message.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], SUPER_ADMIN_ID)

    async def test_channel_disabled_sends_nothing_for_member(self) -> None:
        store = _ViolationStore()
        message = await _run_group(
            store,
            message=_message(user_id=MEMBER_ID),
            settings=_settings(log_channel_enabled=False),
            verdict=_verdict(action="delete"),
        )

        message.bot.send_message.assert_not_awaited()


class ReviewCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_super_admin_click_is_denied_without_side_effects(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        callback = _callback(data="mrev:rel:99", operator_id=ADMIN_ID)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "无权限")
        self.assertTrue(callback.answered[-1][1])
        session.get.assert_not_awaited()
        session.commit.assert_not_awaited()
        callback.bot.edit_message_text.assert_not_awaited()
        callback.bot.send_message.assert_not_awaited()
        self.assertEqual(violation.review_state, "none")

    async def test_release_sets_state_calls_unban_and_posts_handover(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        callback = _callback(data="mrev:rel:99", operator_id=SUPER_ADMIN_ID)
        recovery = SimpleNamespace(verification_id=5, group_id=GROUP_ID, user_id=MEMBER_ID)
        lease = AsyncMock(return_value=recovery)
        activate = Mock()
        release = AsyncMock(return_value=True)

        with (
            patch("bot.handlers.group.lease_join_verification_for_unban", new=lease),
            patch("bot.handlers.group.activate_manual_unban_recovery", new=activate),
            patch(
                "bot.handlers.group.release_moderation_restriction_after_exemption",
                new=release,
            ),
        ):
            await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(violation.review_state, "released")
        self.assertEqual(violation.reviewed_by, SUPER_ADMIN_ID)
        self.assertIsNotNone(violation.reviewed_at)
        session.commit.assert_awaited()
        lease.assert_awaited_once()
        activate.assert_called_once()
        release.assert_awaited_once()

        # 频道新发的交接消息
        handover = callback.bot.send_message.await_args
        self.assertEqual(handover.kwargs["chat_id"], CHANNEL_ID)
        first_line = handover.kwargs["text"].splitlines()[0]
        self.assertEqual(first_line, "🟢 人工放行 · 待调整规则")
        self.assertIn("case：99", handover.kwargs["text"])
        self.assertIn("@Ming_GPT_bot", handover.kwargs["text"])
        entities = handover.kwargs["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, "mention")
        # mention 覆盖的正是 @Ming_GPT_bot（Telegram 用 UTF-16 偏移）
        text = handover.kwargs["text"]
        utf16 = text.encode("utf-16-le")
        start = entities[0].offset * 2
        end = start + entities[0].length * 2
        self.assertEqual(utf16[start:end].decode("utf-16-le"), "@Ming_GPT_bot")
        # 状态行写回频道卡片
        edited = callback.bot.edit_message_text.await_args.kwargs
        self.assertEqual(edited["chat_id"], CHANNEL_ID)
        self.assertIn("🟢 已人工放行 · 已解除该成员限制 · 待规则调整", edited["text"])
        self.assertEqual(callback.answered[-1][0], "已放行，正在交接给规则调整")

    async def test_release_absent_recovery_is_not_an_error(self) -> None:
        violation = _violation(id=100, review_state="none")
        session = _review_session(violation)
        callback = _callback(data="mrev:rel:100", operator_id=SUPER_ADMIN_ID)
        lease = AsyncMock(return_value=None)
        release = AsyncMock(return_value=True)

        with (
            patch("bot.handlers.group.lease_join_verification_for_unban", new=lease),
            patch(
                "bot.handlers.group.release_moderation_restriction_after_exemption",
                new=release,
            ),
        ):
            await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(violation.review_state, "released")
        release.assert_not_awaited()
        self.assertEqual(callback.answered[-1][0], "已放行，正在交接给规则调整")

    async def test_revoke_without_release_alerts_and_changes_nothing(self) -> None:
        violation = _violation(review_state="none")
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "这条没有放行过，不需要调整")
        self.assertTrue(callback.answered[-1][1])
        self.assertEqual(violation.review_state, "none")
        session.commit.assert_not_awaited()
        callback.bot.send_message.assert_not_awaited()
        callback.bot.edit_message_text.assert_not_awaited()

    async def test_revoke_after_release_sets_revoked_and_posts_handover(self) -> None:
        violation = _violation(review_state="released", action_taken="delete")
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(violation.review_state, "revoked")
        self.assertEqual(violation.reviewed_by, SUPER_ADMIN_ID)
        session.commit.assert_awaited()
        handover = callback.bot.send_message.await_args
        self.assertEqual(
            handover.kwargs["text"].splitlines()[0], "🔴 放行收回 · 无需调整"
        )
        # delete 处置没有限制可恢复：交接消息里写明
        self.assertIn("限制恢复：该次处置未禁言，无可恢复", handover.kwargs["text"])
        edited = callback.bot.edit_message_text.await_args.kwargs
        self.assertEqual(edited["chat_id"], CHANNEL_ID)
        self.assertIn("🔴 已收回放行申请 · 规则无需调整 · 限制恢复", edited["text"])
        self.assertIn("该次处置未禁言，无可恢复", edited["text"])
        self.assertEqual(
            callback.answered[-1][0], "已收回放行申请，正在恢复该成员的原始处置"
        )

    async def test_revoke_reapplies_challenge_mute_and_challenge(self) -> None:
        violation = _violation(review_state="released", action_taken="challenge")
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)
        begin = AsyncMock(return_value=True)

        with (
            patch("bot.handlers.group.moderation_challenge_ready", return_value=True),
            patch("bot.handlers.group.begin_moderation_challenge", new=begin),
        ):
            await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(violation.review_state, "revoked")
        begin.assert_awaited_once()
        self.assertEqual(begin.await_args.kwargs["rule_action"], "ban")
        self.assertEqual(begin.await_args.kwargs["user_id"], MEMBER_ID)
        handover_text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("限制恢复：已重新禁言并重新发起质询", handover_text)

    async def test_revoke_challenge_falls_back_to_mute_when_unavailable(self) -> None:
        violation = _violation(review_state="released", action_taken="challenge")
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)
        begin = AsyncMock(return_value=False)
        restrict = AsyncMock(return_value=True)

        with (
            patch("bot.handlers.group.moderation_challenge_ready", return_value=True),
            patch("bot.handlers.group.begin_moderation_challenge", new=begin),
            patch("bot.handlers.group.restrict_new_member", new=restrict),
        ):
            await group.on_review_action(callback, _settings(), session=session)

        begin.assert_awaited_once()
        restrict.assert_awaited_once()
        self.assertEqual(violation.review_state, "revoked")
        handover_text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("限制恢复：已重新禁言（质询创建失败，退化为仅禁言）", handover_text)

    async def test_revoke_challenge_without_provider_only_remutes(self) -> None:
        violation = _violation(review_state="released", action_taken="challenge")
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)
        begin = AsyncMock(return_value=True)
        restrict = AsyncMock(return_value=True)

        with (
            patch("bot.handlers.group.moderation_challenge_ready", return_value=False),
            patch("bot.handlers.group.begin_moderation_challenge", new=begin),
            patch("bot.handlers.group.restrict_new_member", new=restrict),
        ):
            await group.on_review_action(callback, _settings(), session=session)

        begin.assert_not_awaited()
        restrict.assert_awaited_once()
        handover_text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("限制恢复：已重新禁言（质询未配置，未重新发起质询）", handover_text)

    async def test_revoke_ban_case_rebans(self) -> None:
        violation = _violation(review_state="released", action_taken="ban_applied")
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)
        ban = AsyncMock(return_value=True)

        with patch("bot.handlers.group.ban_member", new=ban):
            await group.on_review_action(callback, _settings(), session=session)

        ban.assert_awaited_once()
        handover_text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("限制恢复：已重新封禁", handover_text)

    async def test_revoke_ban_failure_is_surfaced_in_channel(self) -> None:
        violation = _violation(review_state="released", action_taken="ban_applied")
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)
        ban = AsyncMock(side_effect=RuntimeError("telegram down"))

        with patch("bot.handlers.group.ban_member", new=ban):
            await group.on_review_action(callback, _settings(), session=session)

        # 恢复失败必须可见，且不向上抛异常
        self.assertEqual(violation.review_state, "revoked")
        handover_text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("限制恢复：恢复限制失败", handover_text)

    async def test_revoke_skips_manually_exempt_user(self) -> None:
        violation = _violation(review_state="released", action_taken="ban_applied")
        session = _review_session(violation, exempt=True)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)
        ban = AsyncMock(return_value=True)

        with patch("bot.handlers.group.ban_member", new=ban):
            await group.on_review_action(callback, _settings(), session=session)

        ban.assert_not_awaited()
        handover_text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("限制恢复：该用户当前在手动豁免名单，跳过限制恢复", handover_text)

    async def test_revoke_skips_super_admin_owner(self) -> None:
        """owner 全豁免：即便原始处置是 ban，收回时也不对他施加限制。"""

        violation = _violation(
            review_state="released", action_taken="ban_applied", user_id=SUPER_ADMIN_ID
        )
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)
        ban = AsyncMock(return_value=True)

        with patch("bot.handlers.group.ban_member", new=ban):
            await group.on_review_action(callback, _settings(), session=session)

        ban.assert_not_awaited()
        self.assertEqual(violation.review_state, "revoked")
        handover_text = callback.bot.send_message.await_args.kwargs["text"]
        self.assertIn("限制恢复：该用户是最高管理员，完全豁免，不施加限制", handover_text)

    async def test_revoke_review_state_none_is_treated_as_not_released(self) -> None:
        violation = _violation(review_state=None)
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "这条没有放行过，不需要调整")
        session.commit.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
