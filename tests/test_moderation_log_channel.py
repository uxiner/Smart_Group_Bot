"""审核命中证据 → 频道 + 「人工放行 / 确认封禁」两个按钮（都需按两次）。

覆盖：

- 普通成员命中并被处置 → 频道收到一条证据卡（标题「审核命中 · 证据」），
  且**没有**发给最高管理员的私聊；卡片按钮载荷带 ``violations.id``；
- 群管理员命中 → 频道卡片标题保持「管理员违规 · 证据」；
- 频道未启用（``log_channel_enabled=false`` 或未配置 ``log_channel_id``）→
  回到私聊老路径：普通成员不发、管理员发私聊；
- 权限：只有审核日志频道的管理员（``get_chat_member`` 返回 administrator/creator）
  或 ``super_admin_id`` 可点；普通频道订阅者/其他人 → 「仅频道管理员可操作」，
  不改状态、不发频道消息；取频道信息失败 → 拒绝并提示稍后重试。
- 双击确认状态机（``pending_action`` / ``pending_at`` 落库）：
  * 第一次点「人工放行」只 arm（追加 ⏳ 待确认状态行、``answer('再按一次确认')``），
    review_state 不变、**不执行任何处置**；
  * 第二次点同一个按钮、且在 ``review_confirm_seconds`` 窗口内 → 执行；
  * 窗口过期 → 重新算第一次，仍需按两次；
  * 切换按钮（先放行再封禁）→ 改为该动作的 pending，仍需它自己的第二次点击；
  * 已 released/banned → 「已经处理过了」，不再执行；
- 人工放行执行 → ``review_state='released'`` + 解禁流程 + 「🟢 人工放行 · 待调整规则」
  交接消息（带 mention 实体、写明已删除的群内消息不补回）；
- 确认封禁执行 → 复用 ``_perform_group_ban``（拒绝质询直接封禁）+ ``review_state='banned'``
  + 「🔴 确认封禁 · 判定准确」交接消息；owner/本群管理员被拒；执行失败写进状态行与交接消息；
- 旧回调 ``mrev:rev:`` → 只提示「按钮已更新，请使用新版按钮」，不执行任何动作、状态不变。

所有 Telegram / LLM / DB 调用都是替身，不触网、不落库。
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import unittest

from bot.db.models import AuthorizedGroup
from bot.handlers import group
from bot.services.moderation import ModerationVerdict
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -1001234567890
CHANNEL_ID = -1000000000001
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
        review_confirm_seconds=300,
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
        pending_action=None,
        pending_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _callback(
    *,
    data: str,
    operator_id: int,
    html_text: str | None = CARD_HTML,
    channel_admin_status: str | None = "administrator",
    channel_lookup_error: Exception | None = None,
):
    answered: list[tuple[str, bool]] = []

    async def answer(text: str = "", **kwargs):
        answered.append((str(text), bool(kwargs.get("show_alert"))))
        return True

    if channel_lookup_error is not None:
        get_chat_member = AsyncMock(side_effect=channel_lookup_error)
    else:
        get_chat_member = AsyncMock(
            return_value=SimpleNamespace(status=channel_admin_status)
        )

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
            get_chat_member=get_chat_member,
            edit_message_text=AsyncMock(return_value=True),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=5555)),
        ),
        answer=AsyncMock(side_effect=answer),
    )
    callback.answered = answered
    return callback


def _review_session(
    violation: SimpleNamespace,
    *,
    exempt: bool = False,
    group_authorized: bool = True,
) -> SimpleNamespace:
    # `on_review_action` 现在会复验群授权（`is_group_authorized`），它和取 Violation
    # 走同一个 `session.get`，所以这里按模型分流；`group_authorized=False` 用来
    # 覆盖「群已被取消授权」那条路径（见 tests/test_p0_security_b07_*）。
    async def get(model, ident):
        if model is AuthorizedGroup:
            return SimpleNamespace(id=int(ident), bot_present=bool(group_authorized))
        return violation

    return SimpleNamespace(
        get=AsyncMock(side_effect=get),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=Mock(),
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
        self.assertEqual(row[1].text, "确认封禁")
        # violation id 是 1（第一条 record）
        self.assertEqual(row[0].callback_data, "mrev:rel:1")
        self.assertEqual(row[1].callback_data, "mrev:ban:1")

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
                verdict=_verdict(action="ban", rule_id=6, pattern="兼职招募"),
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
    def setUp(self) -> None:
        group._ADMIN_ALERT_STATE.clear()

    def tearDown(self) -> None:
        group._ADMIN_ALERT_STATE.clear()

    # ---- 权限：只有频道管理员或最高管理员可点 ---------------------------
    async def test_deauthorized_group_blocks_review_action(self) -> None:
        """B-07：群被取消授权后，频道管理员不能再对它执行审核处置。"""

        violation = _violation()
        session = _review_session(violation, group_authorized=False)
        callback = _callback(
            data="mrev:ban:99", operator_id=ADMIN_ID, channel_admin_status="administrator"
        )

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "当前群组未授权，不能执行审核操作")
        self.assertTrue(callback.answered[-1][1])
        self.assertIsNone(violation.pending_action)
        self.assertEqual(violation.review_state, "none")
        callback.bot.edit_message_text.assert_not_awaited()

    async def test_non_channel_member_click_is_denied_without_side_effects(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        callback = _callback(
            data="mrev:rel:99", operator_id=ADMIN_ID, channel_admin_status="member"
        )

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "仅频道管理员可操作")
        self.assertTrue(callback.answered[-1][1])
        session.get.assert_not_awaited()
        session.commit.assert_not_awaited()
        callback.bot.edit_message_text.assert_not_awaited()
        callback.bot.send_message.assert_not_awaited()
        self.assertEqual(violation.review_state, "none")

    async def test_channel_admin_lookup_failure_denies(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        callback = _callback(
            data="mrev:rel:99",
            operator_id=ADMIN_ID,
            channel_lookup_error=RuntimeError("flood"),
        )

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(
            callback.answered[-1][0], "暂时无法确认频道管理员身份，请稍后重试"
        )
        self.assertTrue(callback.answered[-1][1])
        session.get.assert_not_awaited()
        self.assertEqual(violation.review_state, "none")

    async def test_channel_admin_can_click_and_arms(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        callback = _callback(
            data="mrev:rel:99",
            operator_id=ADMIN_ID,
            channel_admin_status="administrator",
        )

        await group.on_review_action(callback, _settings(), session=session)

        # 频道管理员不是超管，但允许操作：第一次点击只 arm。
        self.assertEqual(callback.answered[-1][0], "再按一次确认")
        self.assertEqual(violation.pending_action, "rel")
        self.assertEqual(violation.review_state, "none")

    # ---- 人工放行：双击才生效 ------------------------------------------
    async def test_release_requires_two_clicks(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        lease = AsyncMock(
            return_value=SimpleNamespace(
                verification_id=5, group_id=GROUP_ID, user_id=MEMBER_ID
            )
        )
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
            # 第一次点击：只 arm，不执行任何处置、不改 review_state
            first = _callback(data="mrev:rel:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(first, _settings(), session=session)
            self.assertEqual(first.answered[-1][0], "再按一次确认")
            self.assertFalse(first.answered[-1][1])
            self.assertEqual(violation.review_state, "none")
            self.assertEqual(violation.pending_action, "rel")
            self.assertIsNotNone(violation.pending_at)
            lease.assert_not_awaited()
            release.assert_not_awaited()
            first.bot.send_message.assert_not_awaited()
            pending_edit = first.bot.edit_message_text.await_args.kwargs
            self.assertIn("⏳ 待确认：再按一次「人工放行」", pending_edit["text"])

            # 第二次点击同一个按钮（窗口内）：执行
            second = _callback(data="mrev:rel:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(second, _settings(), session=session)

        self.assertEqual(violation.review_state, "released")
        self.assertEqual(violation.reviewed_by, SUPER_ADMIN_ID)
        self.assertIsNotNone(violation.reviewed_at)
        self.assertIsNone(violation.pending_action)
        self.assertIsNone(violation.pending_at)
        lease.assert_awaited_once()
        activate.assert_called_once()
        release.assert_awaited_once()
        # 不写永久豁免行
        session.add.assert_not_called()

        handover = second.bot.send_message.await_args
        self.assertEqual(handover.kwargs["chat_id"], CHANNEL_ID)
        self.assertEqual(
            handover.kwargs["text"].splitlines()[0], "🟢 人工放行 · 待调整规则"
        )
        self.assertIn("case：99", handover.kwargs["text"])
        self.assertIn("已删除的群内消息不补回", handover.kwargs["text"])
        self.assertIn("@your_bot", handover.kwargs["text"])
        entities = handover.kwargs["entities"]
        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, "mention")
        text = handover.kwargs["text"]
        utf16 = text.encode("utf-16-le")
        start = entities[0].offset * 2
        end = start + entities[0].length * 2
        self.assertEqual(utf16[start:end].decode("utf-16-le"), "@your_bot")
        edited = second.bot.edit_message_text.await_args.kwargs
        self.assertIn("🟢 已人工放行 · 已解除该成员限制 · 待规则调整", edited["text"])
        self.assertEqual(second.answered[-1][0], "已放行，正在交接给规则调整")

    async def test_release_absent_recovery_is_not_an_error(self) -> None:
        violation = _violation(id=100)
        session = _review_session(violation)
        lease = AsyncMock(return_value=None)
        release = AsyncMock(return_value=True)

        with (
            patch("bot.handlers.group.lease_join_verification_for_unban", new=lease),
            patch(
                "bot.handlers.group.release_moderation_restriction_after_exemption",
                new=release,
            ),
        ):
            await group.on_review_action(
                _callback(data="mrev:rel:100", operator_id=SUPER_ADMIN_ID),
                _settings(),
                session=session,
            )
            second = _callback(data="mrev:rel:100", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(second, _settings(), session=session)

        self.assertEqual(violation.review_state, "released")
        release.assert_not_awaited()
        self.assertEqual(second.answered[-1][0], "已放行，正在交接给规则调整")

    async def test_release_out_of_window_requires_two_fresh_clicks(self) -> None:
        violation = _violation(
            pending_action="rel",
            pending_at=now_shanghai_naive() - timedelta(seconds=400),
        )
        session = _review_session(violation)
        lease = AsyncMock(
            return_value=SimpleNamespace(
                verification_id=5, group_id=GROUP_ID, user_id=MEMBER_ID
            )
        )

        with (
            patch(
                "bot.handlers.group.lease_join_verification_for_unban", new=lease
            ),
            patch(
                "bot.handlers.group.activate_manual_unban_recovery", new=Mock()
            ),
        ):
            # 窗口已过期：这一下只当「第一次」，重新 arm、不执行
            first = _callback(data="mrev:rel:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(first, _settings(), session=session)
            self.assertEqual(first.answered[-1][0], "再按一次确认")
            self.assertEqual(violation.review_state, "none")
            lease.assert_not_awaited()
            # 紧接着同键第二次 → 执行
            second = _callback(data="mrev:rel:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(second, _settings(), session=session)

        self.assertEqual(violation.review_state, "released")
        lease.assert_awaited_once()

    async def test_switching_button_rearms_the_other_action(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        ban = AsyncMock(return_value="<b>本群封禁完成</b>")
        rejection = AsyncMock(return_value="")

        with (
            patch("bot.handlers.admin._ban_target_rejection", new=rejection),
            patch("bot.handlers.admin._perform_group_ban", new=ban),
        ):
            # 先点「人工放行」→ arm rel（不执行）
            await group.on_review_action(
                _callback(data="mrev:rel:99", operator_id=SUPER_ADMIN_ID),
                _settings(),
                session=session,
            )
            self.assertEqual(violation.pending_action, "rel")
            # 再点「确认封禁」→ 改为 ban 的 pending，仍不执行
            switch = _callback(data="mrev:ban:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(switch, _settings(), session=session)
            self.assertEqual(switch.answered[-1][0], "再按一次确认")
            self.assertEqual(violation.review_state, "none")
            self.assertEqual(violation.pending_action, "ban")
            ban.assert_not_awaited()
            # 再点「确认封禁」第二次 → 执行
            second = _callback(data="mrev:ban:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(second, _settings(), session=session)

        self.assertEqual(violation.review_state, "banned")
        ban.assert_awaited_once()

    # ---- 确认封禁：双击 + 复用拒绝质询路径 ------------------------------
    async def test_ban_first_click_only_arms(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        ban = AsyncMock(return_value="<b>本群封禁完成</b>")

        with patch("bot.handlers.admin._perform_group_ban", new=ban):
            callback = _callback(data="mrev:ban:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "再按一次确认")
        self.assertEqual(violation.pending_action, "ban")
        self.assertEqual(violation.review_state, "none")
        ban.assert_not_awaited()
        callback.bot.send_message.assert_not_awaited()

    async def test_confirm_ban_executes_via_rejection_path(self) -> None:
        violation = _violation(pending_action="ban", pending_at=now_shanghai_naive())
        session = _review_session(violation)
        ban = AsyncMock(return_value="<b>本群封禁完成</b>")
        rejection = AsyncMock(return_value="")

        with (
            patch("bot.handlers.admin._ban_target_rejection", new=rejection),
            patch("bot.handlers.admin._perform_group_ban", new=ban),
        ):
            callback = _callback(data="mrev:ban:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(violation.review_state, "banned")
        self.assertEqual(violation.reviewed_by, SUPER_ADMIN_ID)
        self.assertIsNotNone(violation.reviewed_at)
        self.assertIsNone(violation.pending_action)
        # 复用 /ban 的底层实现封禁该 case 的当事人（并作废其质询）
        ban.assert_awaited_once()
        self.assertEqual(ban.await_args.kwargs["target_id"], MEMBER_ID)
        edited = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("🔴 确认封禁 · 判定准确，已立即封禁", edited["text"])
        handover = callback.bot.send_message.await_args
        self.assertEqual(
            handover.kwargs["text"].splitlines()[0], "🔴 确认封禁 · 判定准确"
        )
        self.assertIn("作废该成员的质询资格", handover.kwargs["text"])
        self.assertEqual(handover.kwargs["entities"][0].type, "mention")
        self.assertEqual(callback.answered[-1][0], "已确认封禁，判定准确")

    async def test_confirm_ban_owner_or_group_admin_is_refused(self) -> None:
        violation = _violation(pending_action="ban", pending_at=now_shanghai_naive())
        session = _review_session(violation)
        ban = AsyncMock(return_value="<b>本群封禁完成</b>")
        rejection = AsyncMock(return_value="不能封禁最高管理员。")

        with (
            patch("bot.handlers.admin._ban_target_rejection", new=rejection),
            patch("bot.handlers.admin._perform_group_ban", new=ban),
        ):
            callback = _callback(data="mrev:ban:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(callback, _settings(), session=session)

        ban.assert_not_awaited()
        self.assertEqual(violation.review_state, "none")
        self.assertIsNone(violation.pending_action)
        self.assertEqual(callback.answered[-1][0], "不能封禁最高管理员。")
        self.assertTrue(callback.answered[-1][1])
        edited = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("确认封禁被拒绝", edited["text"])

    async def test_confirm_ban_failure_is_surfaced(self) -> None:
        violation = _violation(pending_action="ban", pending_at=now_shanghai_naive())
        session = _review_session(violation)
        ban = AsyncMock(
            return_value=(
                "<b>本群封禁未完成</b>\n\n<blockquote>"
                "错误详情\nTelegram 群内封禁结果未确认</blockquote>"
            )
        )
        rejection = AsyncMock(return_value="")

        with (
            patch("bot.handlers.admin._ban_target_rejection", new=rejection),
            patch("bot.handlers.admin._perform_group_ban", new=ban),
        ):
            callback = _callback(data="mrev:ban:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(callback, _settings(), session=session)

        # 失败绝不静默：review_state 保持原值（不谎报），pending 已清空
        self.assertEqual(violation.review_state, "none")
        self.assertIsNone(violation.pending_action)
        edited = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("但封禁未完成", edited["text"])
        handover = callback.bot.send_message.await_args
        self.assertIn("封禁未完成", handover.kwargs["text"])
        self.assertEqual(
            handover.kwargs["text"].splitlines()[0], "🔴 确认封禁 · 判定准确"
        )
        self.assertTrue(callback.answered[-1][1])

    async def test_confirm_ban_exception_is_surfaced(self) -> None:
        violation = _violation(pending_action="ban", pending_at=now_shanghai_naive())
        session = _review_session(violation)
        ban = AsyncMock(side_effect=RuntimeError("PARTICIPANT_ID_INVALID"))
        rejection = AsyncMock(return_value="")

        with (
            patch("bot.handlers.admin._ban_target_rejection", new=rejection),
            patch("bot.handlers.admin._perform_group_ban", new=ban),
        ):
            callback = _callback(data="mrev:ban:99", operator_id=SUPER_ADMIN_ID)
            await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(violation.review_state, "none")
        handover = callback.bot.send_message.await_args
        self.assertIn("PARTICIPANT_ID_INVALID", handover.kwargs["text"])
        edited = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("封禁未完成", edited["text"])

    # ---- 终态 & 旧版按钮 -----------------------------------------------
    async def test_already_released_alerts_and_does_nothing(self) -> None:
        violation = _violation(review_state="released")
        session = _review_session(violation)
        callback = _callback(data="mrev:rel:99", operator_id=SUPER_ADMIN_ID)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "已经处理过了")
        self.assertTrue(callback.answered[-1][1])
        session.commit.assert_not_awaited()
        callback.bot.send_message.assert_not_awaited()

    async def test_already_banned_alerts_and_does_nothing(self) -> None:
        violation = _violation(review_state="banned")
        session = _review_session(violation)
        callback = _callback(data="mrev:ban:99", operator_id=SUPER_ADMIN_ID)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "已经处理过了")
        self.assertTrue(callback.answered[-1][1])
        session.commit.assert_not_awaited()

    async def test_legacy_rev_button_is_a_noop(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        callback = _callback(data="mrev:rev:99", operator_id=SUPER_ADMIN_ID)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "按钮已更新，请使用新版按钮")
        self.assertFalse(callback.answered[-1][1])
        session.get.assert_not_awaited()
        session.commit.assert_not_awaited()
        callback.bot.send_message.assert_not_awaited()
        callback.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(violation.review_state, "none")

    async def test_unknown_action_is_rejected(self) -> None:
        violation = _violation()
        session = _review_session(violation)
        callback = _callback(data="mrev:xyz:99", operator_id=SUPER_ADMIN_ID)

        await group.on_review_action(callback, _settings(), session=session)

        self.assertEqual(callback.answered[-1][0], "不支持的审核操作")
        session.get.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
