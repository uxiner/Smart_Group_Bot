"""D 节：管理员不再豁免日常审核 + D2 管理员违规证据私聊最高管理员。

覆盖：

- 管理员/群主命中违规 → 删消息 + 群内 @警示 + 记违规(action_taken=delete)，
  **不质询、不封禁、不禁言、不累计警告**（因此不会被升级成 ban）；
- `moderation.admin_moderation_enabled=false` → 回到"整段跳过"的旧行为；
- 手动豁免名单（/aiexempt）仍然完全跳过；
- 普通成员的处置路径逐字不变（ban → 质询；delete → 删除 + 通知）；
- 管理员正常技术讨论（不违规）→ 什么都不做；
- 管理员 NSFW 图片 → 删图 + @警告，不质询不禁言；开关关闭 → 跳过；
- D2：管理员违规私聊最高管理员一次完整证据（对象/时间/规则/置信度/理由/原文/
  已执行/回链），带图附图片；私聊失败不影响群内处置；开关关闭不发；
  同一人 10 分钟内第 6 次改为一条汇总；普通成员违规不发。

所有 Telegram / LLM 调用都是 mock，不触网。
"""

from __future__ import annotations

import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers import group
from bot.services.moderation import ModerationVerdict
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -1001234567890
OWNER_ID = 1
ADMIN_ID = 7
MEMBER_ID = 42
MESSAGE_ID = 777
SUPER_ADMIN_ID = 1


async def _write_image(_path, *, destination) -> None:
    destination.write(b"\xff\xd8\xff\xe0nsfw")


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
    text: str = "技术讨论：丢包怎么排查",
    username: str = "member",
    full_name: str = "Member",
    photos: list | None = None,
) -> SimpleNamespace:
    bot = SimpleNamespace(
        me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1)),
        ban_chat_member=AsyncMock(return_value=True),
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=9001)),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=9002)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=9003)),
        get_file=AsyncMock(
            return_value=SimpleNamespace(file_path="photos/x.jpg", file_size=3)
        ),
        download_file=AsyncMock(side_effect=_write_image),
    )
    answered: list[str] = []

    async def answer(body, **_kwargs):
        answered.append(str(body))
        return SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_ID),
            message_id=9100,
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
            id=user_id,
            is_bot=False,
            username=username,
            full_name=full_name,
        ),
        sender_chat=None,
        text=text,
        caption=None,
        photo=photos,
        document=None,
        delete=AsyncMock(),
        answer=AsyncMock(side_effect=answer),
        bot=bot,
    )
    message.answered = answered
    return message


class _ViolationStore:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, int | None]] = []
        # True 表示"事件已存在"（Telegram 重投同一条消息）。
        self.duplicate_events = False

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
        setattr(violation, "_source_event_created", not self.duplicate_events)
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
    rule = SimpleNamespace(
        id=rule_id, action=action, rule_type=rule_type, pattern=pattern
    )
    return ModerationVerdict(
        violated=violated,
        reason=reason if violated else "",
        rule=rule if violated else None,
        conclusive=conclusive,
        confidence=confidence if violated else 0.0,
    )


class AdminModerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        group._ADMIN_ALERT_STATE.clear()

    def tearDown(self) -> None:
        group._ADMIN_ALERT_STATE.clear()

    async def _run(
        self,
        store: _ViolationStore,
        *,
        message: SimpleNamespace | None = None,
        settings: SimpleNamespace | None = None,
        verdict: ModerationVerdict | None = None,
        tg_admin: bool = False,
        super_admin: bool = False,
        manual_exempt: bool = False,
        challenge_ready: bool = True,
        begin: AsyncMock | None = None,
        send_side_effect=None,
    ) -> SimpleNamespace:
        message = message if message is not None else _message()
        settings = settings if settings is not None else _settings()
        sender_id = int(getattr(message.from_user, "id", 0) or 0)
        verdict = verdict if verdict is not None else _verdict()
        if send_side_effect is not None:
            message.bot.send_message = AsyncMock(side_effect=send_side_effect)
        session = SimpleNamespace(
            flush=AsyncMock(),
            commit=AsyncMock(),
            rollback=AsyncMock(),
            delete=AsyncMock(),
            execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=manual_exempt),
            evaluate=AsyncMock(return_value=verdict),
            is_high_confidence=(
                lambda candidate: bool(candidate.conclusive)
                and float(candidate.confidence) >= 0.9
            ),
            record_violation=AsyncMock(side_effect=store.record),
            add_warning=AsyncMock(return_value=(1, False)),
        )
        begin = begin or AsyncMock(return_value=True)
        notice = AsyncMock(return_value=True)

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
                new=AsyncMock(
                    return_value=(str(getattr(message, "text", "") or ""), "")
                ),
            ),
            patch(
                "bot.handlers.group._build_reply_context_for_llm",
                new=AsyncMock(return_value=""),
            ),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            # 两个身份判定分别 mock：is_super_admin_user_id（最高管理员）与
            # is_user_admin_cached（群管理员/群主）。
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
                "bot.handlers.group.moderation_challenge_ready",
                return_value=challenge_ready,
            ),
            patch("bot.handlers.group.begin_moderation_challenge", new=begin),
            patch("bot.handlers.group.answer_with_auto_delete", new=notice),
        ]
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            await group.on_group_message(message, session=session, settings=settings)
        message._notice_mock = notice
        message._moderation = moderation
        return message

    async def test_group_message_audit_declares_the_sender_for_admission(self) -> None:
        """F-021：群消息审核必须把发送者带进 ``evaluate``，整形闸才有 key。

        没有发送者就无法按 (群, 成员) 摊平连发成本——这条断言守住接线。
        """

        store = _ViolationStore()

        message = await self._run(store)

        self.assertEqual(
            message._moderation.evaluate.await_args.kwargs.get("sender_id"),
            int(message.from_user.id),
        )

    # ------------------------------------------------------------------ D

    async def test_admin_violation_deletes_warns_records_without_punishment(
        self,
    ) -> None:
        store = _ViolationStore()
        begin = AsyncMock(return_value=True)
        message = await self._run(
            store,
            message=_message(user_id=ADMIN_ID, username="admin"),
            tg_admin=True,
            super_admin=False,
            begin=begin,
        )

        message.delete.assert_awaited_once()
        message._notice_mock.assert_awaited_once()
        notice_text = message._notice_mock.await_args.args[1]
        self.assertIn("@admin", notice_text)
        self.assertIn("已删除违规消息", notice_text)
        # 记录违规，动作是 delete
        self.assertEqual(store.calls, [(ADMIN_ID, "delete", MESSAGE_ID)])
        # 不质询 / 不封禁 / 不禁言 / 不累计
        begin.assert_not_awaited()
        message.chat.ban.assert_not_awaited()
        message.bot.ban_chat_member.assert_not_awaited()

    async def test_super_admin_is_fully_exempt(self) -> None:
        """最高管理员（super admin）：不判定、不删、不警示、不质询、不记录、不私聊。

        即使他同时是群管理员（is_user_admin_cached=True），也仍然完全豁免。
        """

        store = _ViolationStore()
        begin = AsyncMock(return_value=True)
        message = await self._run(
            store,
            message=_message(user_id=OWNER_ID, username="owner"),
            verdict=_verdict(action="ban", rule_id=6, pattern="招募"),
            tg_admin=True,
            super_admin=True,
            begin=begin,
        )

        # 语义判定都不该跑（完全跳过），更不会有任何处置/记录/私聊
        self.assertEqual(store.calls, [])
        message.delete.assert_not_awaited()
        message._notice_mock.assert_not_awaited()
        message.answer.assert_not_awaited()
        message.bot.send_message.assert_not_awaited()
        message.chat.ban.assert_not_awaited()
        message.bot.ban_chat_member.assert_not_awaited()
        begin.assert_not_awaited()
        message._moderation.evaluate.assert_not_awaited()
        message._moderation.is_user_exempt.assert_not_awaited()

    async def test_admin_moderation_disabled_skips_entirely(self) -> None:
        store = _ViolationStore()
        begin = AsyncMock(return_value=True)
        message = await self._run(
            store,
            message=_message(user_id=ADMIN_ID),
            settings=_settings(admin_moderation_enabled=False),
            tg_admin=True,
            begin=begin,
        )

        message.delete.assert_not_awaited()
        message._notice_mock.assert_not_awaited()
        message.bot.send_message.assert_not_awaited()
        begin.assert_not_awaited()

    async def test_manually_exempt_admin_is_still_skipped(self) -> None:
        store = _ViolationStore()
        begin = AsyncMock(return_value=True)
        message = await self._run(
            store,
            message=_message(user_id=ADMIN_ID),
            tg_admin=True,
            manual_exempt=True,
            begin=begin,
        )

        message.delete.assert_not_awaited()
        message._notice_mock.assert_not_awaited()
        message.bot.send_message.assert_not_awaited()
        begin.assert_not_awaited()

    async def test_admin_clean_technical_talk_does_nothing(self) -> None:
        store = _ViolationStore()
        message = await self._run(
            store,
            message=_message(user_id=ADMIN_ID, text="先加我白名单，硬代码就行"),
            tg_admin=True,
            verdict=_verdict(violated=False),
        )

        message.delete.assert_not_awaited()
        message._notice_mock.assert_not_awaited()
        message.bot.send_message.assert_not_awaited()

    async def test_ordinary_member_ban_rule_still_gets_challenge(self) -> None:
        """普通成员路径逐字不变：ban 规则 → 质询。"""

        store = _ViolationStore()
        begin = AsyncMock(return_value=True)
        message = await self._run(
            store,
            message=_message(user_id=MEMBER_ID),
            verdict=_verdict(action="ban", rule_id=6, pattern="招募"),
            begin=begin,
        )

        message.delete.assert_awaited_once()
        self.assertEqual(begin.await_args.kwargs["user_id"], MEMBER_ID)
        self.assertEqual(store.calls[0][1], "challenge")
        # 普通成员不发 D2 私聊证据
        message.bot.send_message.assert_not_awaited()

    async def test_ordinary_member_delete_rule_is_unchanged(self) -> None:
        store = _ViolationStore()
        message = await self._run(
            store,
            message=_message(user_id=MEMBER_ID),
            verdict=_verdict(action="delete"),
        )

        message.delete.assert_awaited_once()
        message._notice_mock.assert_awaited_once()
        self.assertEqual(store.calls, [(MEMBER_ID, "delete", MESSAGE_ID)])
        message.bot.send_message.assert_not_awaited()

    # ----------------------------------------------------------------- D2

    async def test_admin_alert_contains_full_evidence(self) -> None:
        store = _ViolationStore()
        message = await self._run(
            store,
            message=_message(user_id=ADMIN_ID, username="admin", text="秒杀 优惠券 包邮"),
            tg_admin=True,
        )

        message.bot.send_message.assert_awaited_once()
        kwargs = message.bot.send_message.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], SUPER_ADMIN_ID)
        report = kwargs["text"]
        self.assertIn("id:7", report)
        self.assertIn("@admin", report)
        self.assertIn("TG 群管理员/群主", report)
        self.assertIn("#5", report)
        self.assertIn("正则", report)
        self.assertIn("0.97", report)
        self.assertIn("秒杀 优惠券 包邮", report)
        self.assertIn("delete", report)
        self.assertIn("已删消息", report)
        self.assertIn("已跳过质询", report)
        self.assertIn(f"https://t.me/c/1234567890/{MESSAGE_ID}", report)

    async def test_tg_admin_alert_marks_identity_and_never_self_reports(self) -> None:
        """报告只发给最高管理员；被处置的群管理员身份标注与 id 都在报告里。"""

        store = _ViolationStore()
        message = await self._run(
            store,
            message=_message(user_id=ADMIN_ID, username="admin"),
            tg_admin=True,
            super_admin=False,
        )

        report = message.bot.send_message.await_args.kwargs["text"]
        self.assertIn("TG 群管理员/群主", report)
        self.assertIn(f"id:{ADMIN_ID}", report)
        self.assertEqual(
            message.bot.send_message.await_args.kwargs["chat_id"], SUPER_ADMIN_ID
        )

    async def test_alert_attaches_photo_when_present(self) -> None:
        store = _ViolationStore()
        message = await self._run(
            store,
            message=_message(
                user_id=ADMIN_ID,
                username="admin",
                photos=[SimpleNamespace(file_id="photo-file-id", file_size=10)],
            ),
            tg_admin=True,
        )

        message.bot.send_photo.assert_awaited_once()
        self.assertEqual(
            message.bot.send_photo.await_args.kwargs["photo"], "photo-file-id"
        )
        self.assertEqual(
            message.bot.send_photo.await_args.kwargs["chat_id"], SUPER_ADMIN_ID
        )

    async def test_alert_failure_does_not_affect_group_action(self) -> None:
        store = _ViolationStore()
        message = await self._run(
            store,
            message=_message(user_id=ADMIN_ID),
            tg_admin=True,
            send_side_effect=RuntimeError("telegram down"),
        )

        message.delete.assert_awaited_once()
        message._notice_mock.assert_awaited_once()
        self.assertEqual(store.calls, [(ADMIN_ID, "delete", MESSAGE_ID)])

    async def test_alert_disabled_sends_nothing(self) -> None:
        store = _ViolationStore()
        message = await self._run(
            store,
            message=_message(user_id=ADMIN_ID),
            settings=_settings(admin_alert_super_admin_enabled=False),
            tg_admin=True,
        )

        message.bot.send_message.assert_not_awaited()
        # 群内处置照旧
        message.delete.assert_awaited_once()
        message._notice_mock.assert_awaited_once()

    async def test_sixth_alert_in_window_is_aggregated(self) -> None:
        store = _ViolationStore()
        message = _message(user_id=ADMIN_ID, username="admin")
        for _ in range(6):
            message = await self._run(store, message=message, tg_admin=True)

        # 前 5 次逐条，第 6 次是汇总
        self.assertEqual(message.bot.send_message.await_count, 6)
        summary = message.bot.send_message.await_args.kwargs["text"]
        self.assertIn("管理员违规 · 汇总", summary)
        self.assertIn("10 分钟内次数", summary)

        # 窗口内的第 7 次不再发（汇总之后静默，避免刷屏）
        message = await self._run(store, message=message, tg_admin=True)
        self.assertEqual(message.bot.send_message.await_count, 6)

    async def test_duplicate_delivery_does_not_alert_twice(self) -> None:
        """同一条消息被 Telegram 重投时不再重复私聊（事件已存在）。"""

        store = _ViolationStore()
        store.duplicate_events = True
        message = await self._run(
            store,
            message=_message(user_id=ADMIN_ID),
            tg_admin=True,
        )

        message.bot.send_message.assert_not_awaited()
        message.delete.assert_awaited_once()

    async def test_alert_window_resets_after_ten_minutes(self) -> None:
        store = _ViolationStore()
        message = _message(user_id=ADMIN_ID)
        for _ in range(6):
            message = await self._run(store, message=message, tg_admin=True)

        # 手动把窗口里的时间戳推旧：下一次恢复逐条提醒
        state = group._ADMIN_ALERT_STATE[(GROUP_ID, ADMIN_ID)]
        state.events.clear()
        state.events.append(time.monotonic() - 3600)
        state.summary_sent = True

        message = await self._run(store, message=message, tg_admin=True)
        report = message.bot.send_message.await_args.kwargs["text"]
        self.assertIn("管理员违规 · 证据", report)

    # ----------------------------------------------------------------- NSFW

    async def _run_nsfw(
        self,
        *,
        user_id: int,
        tg_admin: bool,
        settings: SimpleNamespace | None = None,
        **extra_patches,
    ) -> tuple[SimpleNamespace, AsyncMock, SimpleNamespace]:
        import bot.handlers.group as group_module

        message = _message(user_id=user_id)
        # 图片消息：aiogram 里 text 为空、只有 caption/photo，别让文本分支抢先。
        message.text = None
        message.caption = None
        message.photo = [SimpleNamespace(file_id="p1", file_size=3)]
        settings = settings or _settings()
        session = SimpleNamespace(
            flush=AsyncMock(),
            commit=AsyncMock(),
            rollback=AsyncMock(),
            delete=AsyncMock(),
            execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
        )
        moderation = SimpleNamespace(
            is_user_exempt=AsyncMock(return_value=False),
            record_violation=AsyncMock(
                return_value=SimpleNamespace(
                    id=1, notice_sent_at=None, _source_event_created=True
                )
            ),
        )
        challenge = AsyncMock(return_value=True)
        vision_text = "NSFW_YES 画面中出现裸露的性器官。"

        patches = [
            patch(
                "bot.handlers.group.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.group._fresh_group_authorized_for_moderation",
                new=AsyncMock(side_effect=[True, False]),
            ),
            patch(
                "bot.handlers.group._record_group_activity_cas",
                new=AsyncMock(return_value={"mute_all_replies": True}),
            ),
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=tg_admin),
            ),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
            patch(
                "bot.handlers.group.memory_holder",
                SimpleNamespace(get_optional=lambda: None),
            ),
            patch(
                "bot.handlers.group.LLMService",
                return_value=SimpleNamespace(
                    vision_describe=AsyncMock(return_value=vision_text)
                ),
            ),
            patch(
                "bot.handlers.group.ModerationService", return_value=moderation
            ),
            patch(
                "bot.handlers.group.build_moderation_context",
                new=AsyncMock(return_value=([], False)),
            ),
            patch(
                "bot.handlers.group.sticker_library",
                SimpleNamespace(learn_from_message=AsyncMock(return_value=None)),
            ),
            patch("bot.handlers.group.begin_moderation_challenge", new=challenge),
        ]
        patches.extend(extra_patches.values())
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            await group_module.on_group_message(
                message, session=session, settings=settings
            )
        return message, challenge, moderation

    async def test_admin_nsfw_image_is_deleted_and_warned_without_challenge(
        self,
    ) -> None:
        message, challenge, moderation = await self._run_nsfw(
            user_id=ADMIN_ID, tg_admin=True
        )

        message.delete.assert_awaited_once()
        message.answer.assert_awaited()
        challenge.assert_not_awaited()
        moderation.record_violation.assert_awaited_once()
        self.assertEqual(
            moderation.record_violation.await_args.args[4], "nsfw_image"
        )
        message.bot.send_message.assert_awaited_once()

    async def test_admin_nsfw_guard_disabled_keeps_exemption(self) -> None:
        message, challenge, moderation = await self._run_nsfw(
            user_id=ADMIN_ID,
            tg_admin=True,
            settings=_settings(admin_moderation_enabled=False),
        )

        message.delete.assert_not_awaited()
        moderation.record_violation.assert_not_awaited()
        challenge.assert_not_awaited()

    async def test_super_admin_nsfw_image_keeps_full_exemption(self) -> None:
        """最高管理员的 NSFW 图同样完全跳过：不删、不警告、不记录、不私聊。"""

        message, challenge, moderation = await self._run_nsfw(
            user_id=OWNER_ID, tg_admin=True
        )

        message.delete.assert_not_awaited()
        message.answer.assert_not_awaited()
        message.bot.send_message.assert_not_awaited()
        moderation.record_violation.assert_not_awaited()
        challenge.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
