"""「广告经引用/转发再传播 → 被引用的当事人一并处罚」的行为与保护测试。

广告主把广告正文留在被引用的那条消息里，转发者自己的正文只有 "v"/"+1"：
只有转发者被处理显然不够。这里锁定：命中 ban 规则 + 高置信度时原作者也被
处置（沿用 record_violation + begin_moderation_challenge 那条既有链路），
以及所有保护条件（真实用户、管理员/群主/豁免、幂等、追溯时长、警示式引用、
运行时开关）。所有 Telegram / LLM 调用都是 mock。
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers.group import on_group_message
from bot.services.moderation import ModerationVerdict
from bot.utils.timezone import now_shanghai_naive

QUOTED_AUTHOR_ID = 99
FORWARDER_ID = 42
QUOTED_MESSAGE_ID = 555
FORWARDER_MESSAGE_ID = 777
GROUP_ID = -10001


def _settings(**moderation_overrides) -> SimpleNamespace:
    moderation = SimpleNamespace(
        enabled=True,
        warn_threshold=3,
        high_confidence_threshold=0.9,
        nsfw_image_guard_enabled=True,
        punish_quoted_author_enabled=True,
        quoted_author_max_age_seconds=7 * 24 * 60 * 60,
    )
    for key, value in moderation_overrides.items():
        setattr(moderation, key, value)
    return SimpleNamespace(
        super_admin_id=1,
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


def _quoted_message(
    *,
    user_id: int = QUOTED_AUTHOR_ID,
    message_id: int = QUOTED_MESSAGE_ID,
    text: str = "探花招募族 加V 私聊",
    sent_at=None,
    is_bot: bool = False,
    sender_chat=None,
    forward_origin=None,
) -> SimpleNamespace:
    quoted = SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(
            id=user_id,
            is_bot=is_bot,
            username="adposter",
            full_name="Ad Poster",
        ),
        sender_chat=sender_chat,
        forward_origin=forward_origin,
        text=text,
        date=sent_at if sent_at is not None else now_shanghai_naive(),
        delete=AsyncMock(),
    )
    return quoted


def _message(
    *,
    text: str = "v",
    quoted: SimpleNamespace | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(
        message_id=FORWARDER_MESSAGE_ID,
        chat=SimpleNamespace(
            id=GROUP_ID,
            type="supergroup",
            title="test",
            ban=AsyncMock(),
        ),
        from_user=SimpleNamespace(
            id=FORWARDER_ID,
            is_bot=False,
            username="forwarder",
            full_name="Forwarder",
        ),
        sender_chat=None,
        text=text,
        delete=AsyncMock(),
        bot=SimpleNamespace(
            me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1)),
            ban_chat_member=AsyncMock(return_value=True),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")),
        ),
    )
    if quoted is not None:
        message.reply_to_message = quoted
    return message


class _ViolationStore:
    """模拟 violations 的 (group_id, source_message_id) 唯一幂等键。"""

    def __init__(self) -> None:
        self._seen: set[tuple[int, int]] = set()
        self.calls: list[tuple[int, str, int, int | None]] = []

    async def record(
        self,
        session,
        group_id,
        user_id,
        text,
        action,
        rule=None,
        *,
        source_message_id: int | None = None,
        confidence=None,
        verdict_reason="",
    ):
        normalized = int(source_message_id or 0)
        key = (int(group_id), normalized)
        created = key not in self._seen
        self._seen.add(key)
        self.calls.append((int(user_id), str(action), normalized, rule))
        violation = SimpleNamespace(
            id=len(self._seen),
            notice_sent_at=None,
            action_taken=str(action),
            warning_count=None,
        )
        setattr(violation, "_source_event_created", created)
        return violation

    def users(self) -> list[int]:
        return [call[0] for call in self.calls]


class QuotedAuthorPunishmentTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        store: _ViolationStore,
        *,
        message: SimpleNamespace | None = None,
        settings: SimpleNamespace | None = None,
        verdict: ModerationVerdict | None = None,
        challenge_ready: bool = True,
        begin: AsyncMock | None = None,
        **extra_patches,
    ) -> SimpleNamespace:
        message = message if message is not None else _message()
        settings = settings if settings is not None else _settings()
        rule = SimpleNamespace(id=6, action="ban", rule_type="regex", pattern="招募|加V")
        verdict = verdict or ModerationVerdict(
            violated=True,
            reason="引用内容为招募广告",
            rule=rule,
            conclusive=True,
            confidence=1.0,
            match_source="quote",
        )
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
        begin = begin or AsyncMock(return_value=True)
        group_settings = {"mute_all_replies": True}

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
                new=AsyncMock(return_value=group_settings),
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
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.ModerationService", return_value=moderation),
            patch(
                "bot.handlers.group.moderation_challenge_ready",
                return_value=challenge_ready,
            ),
            patch("bot.handlers.group.begin_moderation_challenge", new=begin),
            patch(
                "bot.handlers.group._send_moderation_notice_once_locked",
                new=AsyncMock(return_value=True),
            ),
        ]
        patches.extend(extra_patches.values())
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            await on_group_message(message, session=session, settings=settings)
        return message

    @staticmethod
    def _challenged_users(begin: AsyncMock) -> list[int]:
        return [call.kwargs["user_id"] for call in begin.await_args_list]

    async def test_high_confidence_ban_on_quoted_ad_punishes_original_author(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)

        await self._run(store, message=_message(text="v", quoted=quoted), begin=begin)

        # 原作者：删其消息 + 记违规（source_message_id = 被引用消息 id）+ 质询
        quoted.delete.assert_awaited_once()
        self.assertIn(QUOTED_AUTHOR_ID, store.users())
        quoted_violation = next(
            call for call in store.calls if call[0] == QUOTED_AUTHOR_ID
        )
        self.assertEqual(quoted_violation[1], "challenge")
        self.assertEqual(quoted_violation[2], QUOTED_MESSAGE_ID)
        self.assertEqual(self._challenged_users(begin)[0], QUOTED_AUTHOR_ID)
        # 转发者照旧被处置
        self.assertIn(FORWARDER_ID, store.users())

    async def test_duplicate_quote_of_same_message_punishes_author_once(self) -> None:
        store = _ViolationStore()
        first = _message(text="v", quoted=_quoted_message(message_id=QUOTED_MESSAGE_ID))
        second = _message(text="+1", quoted=_quoted_message(message_id=QUOTED_MESSAGE_ID))
        begin = AsyncMock(return_value=True)

        await self._run(store, message=first, begin=begin)
        await self._run(store, message=second, begin=begin)

        self.assertEqual(
            [call for call in store.calls if call[0] == QUOTED_AUTHOR_ID].__len__(),
            2,  # record_violation 被调用两次，但第二次是"已存在的幂等事件"
        )
        self.assertEqual(self._challenged_users(begin).count(QUOTED_AUTHOR_ID), 1)
        first.reply_to_message.delete.assert_awaited_once()
        second.reply_to_message.delete.assert_not_awaited()
        # 转发者两次都被处置
        self.assertEqual(store.users().count(FORWARDER_ID), 2)

    async def test_quoted_message_older_than_threshold_is_not_punished(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message(sent_at=now_shanghai_naive() - timedelta(days=8))
        begin = AsyncMock(return_value=True)

        await self._run(store, message=_message(text="v", quoted=quoted), begin=begin)

        self.assertNotIn(QUOTED_AUTHOR_ID, store.users())
        quoted.delete.assert_not_awaited()
        self.assertIn(FORWARDER_ID, store.users())

    async def test_quoted_message_within_threshold_is_punished(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message(sent_at=now_shanghai_naive() - timedelta(days=6))
        begin = AsyncMock(return_value=True)

        await self._run(store, message=_message(text="v", quoted=quoted), begin=begin)

        self.assertIn(QUOTED_AUTHOR_ID, store.users())

    async def test_owner_quoted_author_is_skipped(self) -> None:
        store = _ViolationStore()
        settings = _settings()
        settings.super_admin_id = QUOTED_AUTHOR_ID
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)

        await self._run(
            store,
            message=_message(text="v", quoted=quoted),
            settings=settings,
            begin=begin,
        )

        self.assertNotIn(QUOTED_AUTHOR_ID, store.users())
        quoted.delete.assert_not_awaited()
        self.assertIn(FORWARDER_ID, store.users())

    async def test_exempt_quoted_author_is_skipped(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)

        async def claim(session, *, group_id, user_id, verdict):
            return int(user_id) != QUOTED_AUTHOR_ID

        await self._run(
            store,
            message=_message(text="v", quoted=quoted),
            begin=begin,
            claim=patch(
                "bot.handlers.group._claim_current_moderation_verdict",
                new=AsyncMock(side_effect=claim),
            ),
        )

        self.assertNotIn(QUOTED_AUTHOR_ID, store.users())
        quoted.delete.assert_not_awaited()

    async def test_tg_admin_quoted_author_is_skipped(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)

        async def admin_lookup(candidate):
            return int(getattr(getattr(candidate, "from_user", None), "id", 0)) == (
                QUOTED_AUTHOR_ID
            )

        await self._run(
            store,
            message=_message(text="v", quoted=quoted),
            begin=begin,
            admin=patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(side_effect=admin_lookup),
            ),
        )

        self.assertNotIn(QUOTED_AUTHOR_ID, store.users())
        quoted.delete.assert_not_awaited()

    async def test_warning_style_quote_punishes_neither_party(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)
        message = _message(text="别信这种骗子，假的，大家举报他", quoted=quoted)

        await self._run(store, message=message, begin=begin)

        self.assertEqual(store.calls, [])
        quoted.delete.assert_not_awaited()
        message.delete.assert_not_awaited()
        begin.assert_not_awaited()

    async def test_warning_reason_from_verdict_also_skips(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)
        rule = SimpleNamespace(id=6, action="ban", rule_type="regex", pattern="招募|加V")
        verdict = ModerationVerdict(
            violated=True,
            reason="群友在提醒大家小心骗子",
            rule=rule,
            conclusive=True,
            confidence=1.0,
            match_source="quote",
        )

        await self._run(
            store,
            message=_message(text="v", quoted=quoted),
            verdict=verdict,
            begin=begin,
        )

        self.assertEqual(store.calls, [])
        begin.assert_not_awaited()

    async def test_feature_switch_off_keeps_previous_behaviour(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)

        await self._run(
            store,
            message=_message(text="v", quoted=quoted),
            settings=_settings(punish_quoted_author_enabled=False),
            begin=begin,
        )

        self.assertNotIn(QUOTED_AUTHOR_ID, store.users())
        quoted.delete.assert_not_awaited()
        self.assertEqual(store.users(), [FORWARDER_ID])

    async def test_own_text_hit_does_not_punish_quoted_author(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)
        rule = SimpleNamespace(id=6, action="ban", rule_type="regex", pattern="招募|加V")
        verdict = ModerationVerdict(
            violated=True,
            reason="转发者自己发广告",
            rule=rule,
            conclusive=True,
            confidence=1.0,
            match_source="own",
        )

        await self._run(
            store,
            message=_message(text="招募 加V", quoted=quoted),
            verdict=verdict,
            begin=begin,
        )

        self.assertNotIn(QUOTED_AUTHOR_ID, store.users())
        quoted.delete.assert_not_awaited()
        self.assertIn(FORWARDER_ID, store.users())

    async def test_challenge_unavailable_still_deletes_and_records(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)

        await self._run(
            store,
            message=_message(text="v", quoted=quoted),
            challenge_ready=False,
            begin=begin,
        )

        quoted.delete.assert_awaited_once()
        self.assertIn(QUOTED_AUTHOR_ID, store.users())
        self.assertNotIn(QUOTED_AUTHOR_ID, self._challenged_users(begin))

    async def test_non_user_quoted_authors_are_skipped(self) -> None:
        cases = {
            "channel": _quoted_message(sender_chat=SimpleNamespace(id=-100999)),
            "bot": _quoted_message(is_bot=True),
            "forwarded": _quoted_message(
                forward_origin=SimpleNamespace(type="user")
            ),
            "self_reply": _quoted_message(user_id=FORWARDER_ID),
            "bot_itself": _quoted_message(user_id=1),
        }
        for label, quoted in cases.items():
            with self.subTest(label=label):
                store = _ViolationStore()
                begin = AsyncMock(return_value=True)
                await self._run(
                    store,
                    message=_message(text="v", quoted=quoted),
                    begin=begin,
                )
                self.assertNotIn(QUOTED_AUTHOR_ID, store.users())
                quoted.delete.assert_not_awaited()

    async def test_low_confidence_ban_hit_does_not_punish_quoted_author(self) -> None:
        """置信度没到 high_confidence_threshold 时不触发（复核路径也不触发）。"""

        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)
        rule = SimpleNamespace(id=6, action="ban", rule_type="llm", pattern="禁止广告")
        marginal = ModerationVerdict(
            violated=True,
            reason="疑似广告",
            rule=rule,
            conclusive=True,
            confidence=0.7,
            match_source="semantic",
        )

        await self._run(
            store,
            message=_message(text="v", quoted=quoted),
            verdict=marginal,
            challenge_ready=False,
            begin=begin,
        )

        self.assertNotIn(QUOTED_AUTHOR_ID, store.users())
        quoted.delete.assert_not_awaited()

    async def test_non_ban_rule_hit_does_not_punish_quoted_author(self) -> None:
        store = _ViolationStore()
        quoted = _quoted_message()
        begin = AsyncMock(return_value=True)
        rule = SimpleNamespace(id=5, action="delete", rule_type="regex", pattern="秒杀")
        verdict = ModerationVerdict(
            violated=True,
            reason="引用内容含广告词",
            rule=rule,
            conclusive=True,
            confidence=1.0,
            match_source="quote",
        )

        await self._run(
            store,
            message=_message(text="v", quoted=quoted),
            verdict=verdict,
            challenge_ready=False,
            begin=begin,
        )

        self.assertNotIn(QUOTED_AUTHOR_ID, store.users())
        quoted.delete.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
