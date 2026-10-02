"""审核质询卡与「复核 / 开始验证」按钮的回归测试。

按钮设计（2026-09-29 定型）：卡片只有两行——第 1 行是成员自己的
「复核 / 开始验证」，第 2 行是「管理员通过 | 管理员拒绝」。
点第 1 行先让审核模型重判那条消息：判为正常就直接放行；仍判违规（或模型
不表态）就回到原有质询流程，送本人去做人机验证。管理员那条路不受影响。
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.db.engine import init_db
from bot.db.models import Group, Violation
from bot.handlers import membership
from bot.services.join_verification import (
    VERIFICATION_CALLBACK_APPEAL,
    VERIFICATION_CALLBACK_APPROVE,
    VERIFICATION_CALLBACK_REJECT,
    VERIFICATION_CALLBACK_SPEND,
    VERIFICATION_CALLBACK_START,
    VERIFICATION_KIND_JOIN,
    VERIFICATION_KIND_MODERATION,
    build_group_prompt_keyboard,
    build_moderation_prompt_text,
    build_verification_callback_data,
    get_join_verification,
    parse_verification_callback_data,
    upsert_join_verification,
)
from bot.utils.timezone import now_shanghai_naive

APPEAL_TEXT = "优惠券 加V 领取"


def _settings(**overrides):
    from bot.config import Settings

    settings = Settings(_env_file=None)
    settings.moderation.enabled = True
    settings.join_verification_enabled = True
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _verdict(*, violated, conclusive, confidence=0.0, reason="测试结论"):
    return SimpleNamespace(
        violated=violated,
        conclusive=conclusive,
        confidence=confidence,
        reason=reason,
    )


class _DbTestCase(unittest.IsolatedAsyncioTestCase):
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

    async def _add_record(
        self,
        *,
        user_id: int,
        message_id: int,
        kind: str = VERIFICATION_KIND_MODERATION,
        reason: str = "疑似命中广告规则",
    ) -> None:
        async with self.session_factory() as session:
            await upsert_join_verification(
                session,
                group_id=-100,
                user_id=user_id,
                deadline_at=now_shanghai_naive() + timedelta(minutes=5),
                kind=kind,
                reason=reason,
                ban_on_timeout=True,
                display_name=f"用户{user_id}",
                prompt_message_id=message_id,
            )
            await session.commit()

    async def _add_violation(self, *, user_id: int, text: str = APPEAL_TEXT) -> None:
        async with self.session_factory() as session:
            # violations.group_id → groups.id、rule_id → moderation_rules.id
            # 两个外键都要满足：先补 Group 行，规则留空（被测代码不读它）
            if await session.get(Group, -100) is None:
                session.add(Group(id=-100, title="测试群"))
            session.add(
                Violation(
                    group_id=-100,
                    user_id=user_id,
                    rule_id=None,
                    message_text=text,
                    action_taken="challenge",
                )
            )
            await session.commit()

    async def _record(self, user_id: int):
        async with self.session_factory() as session:
            return await get_join_verification(session, -100, user_id)


def _callback(*, action: str, target_user_id: int, operator_id: int, message_id: int):
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=555)),
        edit_message_reply_markup=AsyncMock(),
        edit_message_text=AsyncMock(
            return_value=SimpleNamespace(
                message_id=message_id, chat=SimpleNamespace(id=-100)
            )
        ),
        delete_message=AsyncMock(return_value=True),
        me=AsyncMock(return_value=SimpleNamespace(username="my_bot")),
    )
    return SimpleNamespace(
        data=build_verification_callback_data(action, target_user_id),
        from_user=SimpleNamespace(id=operator_id),
        message=SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=-100, type="supergroup"),
        ),
        bot=bot,
        answer=AsyncMock(),
    )


class ChallengeKeyboardTests(unittest.TestCase):
    def test_card_has_exactly_two_rows(self) -> None:
        for appeal in (False, True):
            keyboard = build_group_prompt_keyboard(123456, appeal=appeal)
            self.assertEqual(len(keyboard.inline_keyboard), 2, appeal)
            self.assertEqual(
                [button.text for button in keyboard.inline_keyboard[1]],
                ["管理员通过", "管理员拒绝"],
            )

    def test_moderation_card_offers_the_combined_recheck_button(self) -> None:
        keyboard = build_group_prompt_keyboard(123456, appeal=True)

        member_row = keyboard.inline_keyboard[0]
        self.assertEqual(len(member_row), 1)
        self.assertEqual(member_row[0].text, "复核 / 开始验证")
        self.assertEqual(member_row[0].callback_data, "jv:p:123456")
        # 管理员那一行照旧，可点击
        self.assertEqual(
            keyboard.inline_keyboard[1][0].callback_data,
            build_verification_callback_data(VERIFICATION_CALLBACK_APPROVE, 123456),
        )
        self.assertEqual(
            keyboard.inline_keyboard[1][1].callback_data,
            build_verification_callback_data(VERIFICATION_CALLBACK_REJECT, 123456),
        )

    def test_affordable_members_get_the_skip_row_on_top(self) -> None:
        keyboard = build_group_prompt_keyboard(123456, appeal=True, spend_points=2)

        self.assertEqual(len(keyboard.inline_keyboard), 3)
        row = keyboard.inline_keyboard[0]
        self.assertEqual(row[0].text, "消耗 2 积分免除质询")
        self.assertEqual(row[0].callback_data, "jv:s:123456")
        # 原有的复核按钮与管理员行都还在，只是往下挪了一行
        self.assertEqual(keyboard.inline_keyboard[1][0].text, "复核 / 开始验证")
        self.assertEqual(keyboard.inline_keyboard[2][0].callback_data, "jv:a:123456")

    def test_other_challenges_keep_the_plain_start_button(self) -> None:
        keyboard = build_group_prompt_keyboard(123456)

        self.assertEqual(keyboard.inline_keyboard[0][0].text, "开始验证")
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "jv:v:123456")

    def test_recheck_action_round_trips_through_the_callback_parser(self) -> None:
        self.assertEqual(
            parse_verification_callback_data(
                build_verification_callback_data(VERIFICATION_CALLBACK_APPEAL, 42)
            ),
            (VERIFICATION_CALLBACK_APPEAL, 42),
        )
        self.assertIsNone(parse_verification_callback_data("jv:z:42"))

    def test_moderation_card_explains_the_button(self) -> None:
        text = build_moderation_prompt_text(
            user_id=7,
            display_name="张三",
            reason="疑似广告",
            timeout_seconds=600,
        )
        self.assertIn("<b>消息审查验证 · 待完成</b>", text)
        self.assertIn("复核 / 开始验证", text)
        self.assertIn("已暂停发言", text)


class ModerationRecheckCallbackTests(_DbTestCase):
    async def _click(
        self,
        callback,
        *,
        verdict,
        summary="模型结论",
        settings=None,
    ):
        restorer = AsyncMock(return_value=True)
        with (
            patch.object(
                membership,
                "_review_moderation_appeal",
                new=AsyncMock(return_value=(verdict, summary)),
            ),
            patch.object(membership, "restore_member_permissions", new=restorer),
            patch.object(
                membership, "schedule_message_auto_delete_durable", new=AsyncMock()
            ),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=settings or _settings(),
                )
        return restorer

    def _click_for(self, user_id: int, message_id: int):
        return _callback(
            action=VERIFICATION_CALLBACK_APPEAL,
            target_user_id=user_id,
            operator_id=user_id,
            message_id=message_id,
        )

    async def test_only_the_challenged_member_can_press_it(self) -> None:
        await self._add_record(user_id=930, message_id=830)
        callback = _callback(
            action=VERIFICATION_CALLBACK_APPEAL,
            target_user_id=930,
            operator_id=999,
            message_id=830,
        )

        restorer = await self._click(
            callback, verdict=_verdict(violated=False, conclusive=True)
        )

        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("本人", callback.answer.await_args.args[0])
        restorer.assert_not_awaited()
        self.assertIsNotNone(await self._record(930))

    async def test_clean_recheck_releases_the_member_without_an_admin(self) -> None:
        await self._add_record(user_id=933, message_id=833)
        await self._add_violation(user_id=933)
        callback = self._click_for(933, 833)

        restorer = await self._click(
            callback,
            verdict=_verdict(violated=False, conclusive=True, reason="正常聊天"),
            summary="模型二次复核判「未违规」｜正常聊天",
        )

        restorer.assert_awaited_once_with(callback.bot, -100, 933)
        self.assertIsNone(await self._record(933))
        self.assertEqual(
            callback.answer.await_args.args[0], "复核为正常消息，已恢复发言权限"
        )
        edited = callback.bot.edit_message_text.await_args.kwargs
        self.assertIn("复核为正常消息", edited["text"])

    async def test_confirmed_ad_keeps_the_challenge_and_verification_open(self) -> None:
        await self._add_record(user_id=931, message_id=831)
        await self._add_violation(user_id=931)
        callback = self._click_for(931, 831)

        restorer = await self._click(
            callback,
            verdict=_verdict(violated=True, conclusive=True, confidence=0.93),
            summary="模型二次复核仍判「违规」，置信 0.93｜推销",
        )

        # 不放行：本人还得去做人机验证
        restorer.assert_not_awaited()
        record = await self._record(931)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "pending")
        # 仍命中时把本人送进验证（deep link），不是弹提示
        self.assertIn("t.me/", callback.answer.await_args.kwargs["url"])

    async def test_inconclusive_recheck_keeps_the_challenge(self) -> None:
        await self._add_record(user_id=932, message_id=832)
        await self._add_violation(user_id=932)
        callback = self._click_for(932, 832)

        restorer = await self._click(
            callback,
            verdict=_verdict(violated=False, conclusive=False),
            summary="模型二次复核没有给出明确结论，请管理员人工判断",
        )

        # 空回复/不可解析按"未违规"处理，但绝不能因此直接放人
        restorer.assert_not_awaited()
        self.assertIsNotNone(await self._record(932))
        self.assertIn("t.me/", callback.answer.await_args.kwargs["url"])

    async def test_repeat_offender_in_the_same_day_must_verify(self) -> None:
        await self._add_record(user_id=934, message_id=834)
        await self._add_violation(user_id=934)
        await self._add_violation(user_id=934, text="今天第二次被命中")
        callback = self._click_for(934, 834)

        restorer = await self._click(
            callback,
            verdict=_verdict(violated=False, conclusive=True),
            summary="模型二次复核判「未违规」",
        )

        restorer.assert_not_awaited()
        self.assertIsNotNone(await self._record(934))
        self.assertIn("t.me/", callback.answer.await_args.kwargs["url"])

    async def test_missing_original_text_must_verify(self) -> None:
        await self._add_record(user_id=935, message_id=835)
        callback = self._click_for(935, 835)

        restorer = await self._click(
            callback,
            verdict=None,
            summary="找不到原始消息文本，无法自动复核",
        )

        restorer.assert_not_awaited()
        self.assertIsNotNone(await self._record(935))
        self.assertIn("t.me/", callback.answer.await_args.kwargs["url"])

    async def test_non_moderation_challenge_refuses_the_recheck(self) -> None:
        await self._add_record(user_id=936, message_id=836, kind=VERIFICATION_KIND_JOIN)
        callback = self._click_for(936, 836)

        restorer = await self._click(
            callback, verdict=_verdict(violated=False, conclusive=True)
        )

        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("不支持复核", callback.answer.await_args.args[0])
        restorer.assert_not_awaited()

    async def test_admin_approval_still_releases_after_a_kept_challenge(self) -> None:
        await self._add_record(user_id=942, message_id=842)
        await self._add_violation(user_id=942)
        await self._click(
            self._click_for(942, 842),
            verdict=_verdict(violated=True, conclusive=True),
            summary="模型二次复核仍判「违规」",
        )

        admin_callback = _callback(
            action=VERIFICATION_CALLBACK_APPROVE,
            target_user_id=942,
            operator_id=777,
            message_id=842,
        )
        restorer = AsyncMock(return_value=True)
        with (
            patch.object(
                membership, "is_group_admin_or_higher", new=AsyncMock(return_value=True)
            ),
            patch.object(membership, "restore_member_permissions", new=restorer),
            patch.object(
                membership, "schedule_message_auto_delete_durable", new=AsyncMock()
            ),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    admin_callback, session=session, settings=_settings()
                )

        restorer.assert_awaited_once_with(admin_callback.bot, -100, 942)
        self.assertIsNone(await self._record(942))


class ModerationPointsSkipTests(_DbTestCase):
    """「消耗 N 积分免除质询」：扣分 + 放行；分不够就不能走这条。"""

    async def _seed_points(self, user_id: int, days: int) -> None:
        from bot.services.checkin import record_checkin

        for offset in range(days, 0, -1):
            async with self.session_factory() as session:
                await record_checkin(
                    session,
                    group_id=-100,
                    user_id=user_id,
                    now=datetime(2026, 9, 29, 12, 0, 0) - timedelta(days=offset),
                )
                await session.commit()

    async def _balance(self, user_id: int) -> tuple[int, int]:
        from bot.services.checkin import summarize

        async with self.session_factory() as session:
            outcome = await summarize(
                session,
                group_id=-100,
                user_id=user_id,
                now=datetime(2026, 9, 29, 12, 0, 0),
            )
        return outcome.available_points, outcome.spent_points

    async def _click_spend(self, callback):
        restorer = AsyncMock(return_value=True)
        with (
            patch.object(membership, "restore_member_permissions", new=restorer),
            patch.object(
                membership, "schedule_message_auto_delete_durable", new=AsyncMock()
            ),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback, session=session, settings=_settings()
                )
        return restorer

    async def test_skip_costs_two_points_and_releases_the_member(self) -> None:
        await self._add_record(user_id=960, message_id=860)
        await self._seed_points(960, days=2)  # +1 +2 = 3 分
        callback = _callback(
            action=VERIFICATION_CALLBACK_SPEND,
            target_user_id=960,
            operator_id=960,
            message_id=860,
        )

        restorer = await self._click_spend(callback)

        restorer.assert_awaited_once_with(callback.bot, -100, 960)
        self.assertIsNone(await self._record(960), "质询已被免除")
        self.assertEqual(await self._balance(960), (1, 2), "扣了 2 分，余 1 分")
        edited = callback.bot.edit_message_text.await_args.kwargs["text"]
        self.assertIn("消耗 2 积分免除质询", edited)
        self.assertEqual(callback.answer.await_args.args[0], "已消耗 2 积分，质询已免除")

    async def test_not_enough_points_keeps_the_challenge_and_charges_nothing(self) -> None:
        await self._add_record(user_id=961, message_id=861)
        await self._seed_points(961, days=1)  # 只有 1 分
        callback = _callback(
            action=VERIFICATION_CALLBACK_SPEND,
            target_user_id=961,
            operator_id=961,
            message_id=861,
        )

        restorer = await self._click_spend(callback)

        restorer.assert_not_awaited()
        self.assertIsNotNone(await self._record(961), "质询还得照常走")
        self.assertEqual(await self._balance(961), (1, 0), "一分都不能扣")
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("积分不足", callback.answer.await_args.args[0])

    async def test_only_the_challenged_member_can_spend(self) -> None:
        await self._add_record(user_id=962, message_id=862)
        await self._seed_points(962, days=3)
        callback = _callback(
            action=VERIFICATION_CALLBACK_SPEND,
            target_user_id=962,
            operator_id=999,  # 别人想替他被扣分
            message_id=862,
        )

        restorer = await self._click_spend(callback)

        restorer.assert_not_awaited()
        self.assertEqual(await self._balance(962), (6, 0))
        self.assertIn("本人", callback.answer.await_args.args[0])

    async def test_a_second_click_cannot_charge_twice(self) -> None:
        await self._add_record(user_id=963, message_id=863)
        await self._seed_points(963, days=3)  # 6 分
        first = _callback(
            action=VERIFICATION_CALLBACK_SPEND,
            target_user_id=963,
            operator_id=963,
            message_id=863,
        )
        await self._click_spend(first)

        second = _callback(
            action=VERIFICATION_CALLBACK_SPEND,
            target_user_id=963,
            operator_id=963,
            message_id=863,
        )
        restorer = await self._click_spend(second)

        restorer.assert_not_awaited()
        self.assertEqual(await self._balance(963), (4, 2), "只扣一次")

    async def test_skip_is_unavailable_on_non_moderation_challenges(self) -> None:
        await self._add_record(user_id=964, message_id=864, kind=VERIFICATION_KIND_JOIN)
        await self._seed_points(964, days=3)
        callback = _callback(
            action=VERIFICATION_CALLBACK_SPEND,
            target_user_id=964,
            operator_id=964,
            message_id=864,
        )

        restorer = await self._click_spend(callback)

        restorer.assert_not_awaited()
        self.assertEqual(await self._balance(964), (6, 0))
        self.assertIn("不支持", callback.answer.await_args.args[0])

    # --- F-010：扣分已提交但没放行时必须退款 ---------------------------------

    async def _point_refs(self, user_id: int) -> tuple[list[str], list[str]]:
        """(消费 ref, 退款 ref)，按插入顺序。"""

        from sqlalchemy import select

        from bot.db.models import MemberPointAward, MemberPointSpend

        async with self.session_factory() as session:
            spend_rows = await session.execute(
                select(MemberPointSpend.ref)
                .where(
                    MemberPointSpend.group_id == -100,
                    MemberPointSpend.user_id == user_id,
                )
                .order_by(MemberPointSpend.id)
            )
            award_rows = await session.execute(
                select(MemberPointAward.ref)
                .where(
                    MemberPointAward.group_id == -100,
                    MemberPointAward.user_id == user_id,
                )
                .order_by(MemberPointAward.id)
            )
        spends = [str(ref) for ref in spend_rows.scalars().all() if ref]
        awards = [
            str(ref)
            for ref in award_rows.scalars().all()
            if ref and str(ref).startswith("shop-refund:")
        ]
        return spends, awards

    async def _ban_user(self, user_id: int) -> None:
        from bot.db.models import UserWarning

        async with self.session_factory() as session:
            session.add(
                UserWarning(
                    group_id=-100,
                    user_id=user_id,
                    count=3,
                    is_banned=True,
                )
            )
            await session.commit()

    async def test_refused_release_refunds_the_points(self) -> None:
        """F-010：目标已被封禁 → 放行链路只 ack 不动作，扣掉的 2 分必须退回。"""

        await self._add_record(user_id=965, message_id=865)
        await self._seed_points(965, days=2)  # +1 +2 = 3 分
        await self._ban_user(965)
        callback = _callback(
            action=VERIFICATION_CALLBACK_SPEND,
            target_user_id=965,
            operator_id=965,
            message_id=865,
        )

        restorer = await self._click_spend(callback)

        restorer.assert_not_awaited()
        record = await self._record(965)
        self.assertIsNotNone(record, "放行没发生，质询记录还在")
        self.assertEqual(await self._balance(965), (3, 2), "扣了 2 分，又退了 2 分")
        spends, refunds = await self._point_refs(965)
        self.assertEqual(spends, [f"challenge:{int(record.id)}"])
        self.assertEqual(refunds, [f"shop-refund:challenge:{int(record.id)}"])
        self.assertIn("已被封禁", callback.answer.await_args.args[0])

    async def test_a_retry_after_a_refund_can_charge_again(self) -> None:
        """退款后同一个 ref 不能再扣（唯一索引），所以下一次点击要换 attempt。"""

        await self._add_record(user_id=967, message_id=867)
        await self._seed_points(967, days=2)
        await self._ban_user(967)

        async def click() -> AsyncMock:
            return await self._click_spend(
                _callback(
                    action=VERIFICATION_CALLBACK_SPEND,
                    target_user_id=967,
                    operator_id=967,
                    message_id=867,
                )
            )

        await click()
        await click()

        record = await self._record(967)
        self.assertIsNotNone(record)
        record_id = int(record.id)
        spends, refunds = await self._point_refs(967)
        self.assertEqual(
            spends,
            [f"challenge:{record_id}", f"challenge:{record_id}#1"],
            "第二次点击换了一个幂等键，否则退款后永远扣不动",
        )
        self.assertEqual(
            refunds,
            [
                f"shop-refund:challenge:{record_id}",
                f"shop-refund:challenge:{record_id}#1",
            ],
        )
        self.assertEqual(
            await self._balance(967),
            (3, 4),
            "两次扣分两次退款，净值不变",
        )

    async def test_successful_release_does_not_refund(self) -> None:
        """放行真的发生了就不退款：否则等于白送一次免除。"""

        await self._add_record(user_id=968, message_id=868)
        await self._seed_points(968, days=3)  # 6 分
        callback = _callback(
            action=VERIFICATION_CALLBACK_SPEND,
            target_user_id=968,
            operator_id=968,
            message_id=868,
        )

        restorer = await self._click_spend(callback)

        restorer.assert_awaited_once()
        self.assertIsNone(await self._record(968))
        self.assertEqual(await self._balance(968), (4, 2))
        _spends, refunds = await self._point_refs(968)
        self.assertEqual(refunds, [])


class AppealRecheckDeferralTests(_DbTestCase):
    """F-023：申诉复核不再占着 HIGH 更新通道内联跑模型，并且有按人冷却。

    ``jv:p:<uid>`` 走的是 HIGH 更新通道（与 chat_member / 入群验证共用 4 个
    worker），旧实现在 handler 里内联 await 整次模型复核，而且维持原判时质询仍是
    pending，连点没有上限。现在复核提交到 policy lane 的后台任务，并加 60 秒冷却。
    """

    def setUp(self) -> None:
        membership._APPEAL_RECHECK_AT.clear()
        self.addCleanup(membership._APPEAL_RECHECK_AT.clear)

    def _click_for(self, user_id: int, message_id: int):
        return _callback(
            action=VERIFICATION_CALLBACK_APPEAL,
            target_user_id=user_id,
            operator_id=user_id,
            message_id=message_id,
        )

    @staticmethod
    def _submission(**kwargs):
        from bot.services.privileged_tasks import PrivilegedTaskSubmission

        return PrivilegedTaskSubmission(
            accepted=True,
            created=True,
            job_id="job-1",
            lane=str(kwargs.get("lane", "")),
            queue_depth=0,
        )

    async def test_the_model_review_is_not_run_inline_in_the_update_worker(self) -> None:
        await self._add_record(user_id=970, message_id=870)
        await self._add_violation(user_id=970)
        callback = self._click_for(970, 870)
        captured: dict = {}

        def _submit(**kwargs):
            captured.update(kwargs)
            return self._submission(**kwargs)

        review = AsyncMock(
            return_value=(_verdict(violated=True, conclusive=True), "仍判违规")
        )
        with (
            patch.object(membership, "submit_privileged_task", side_effect=_submit),
            patch.object(membership, "_review_moderation_appeal", new=review),
        ):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                    session_factory=self.session_factory,
                )
            # handler 已经返回，模型复核还没有被内联调用
            review.assert_not_awaited()

        self.assertEqual(captured.get("lane"), "policy")
        self.assertEqual(captured.get("key"), "verification-appeal:-100:970")
        # 点击的人立刻拿到回执，不用干等模型
        self.assertIn("复核", callback.answer.await_args.args[0])
        # 冷却位已经占住
        self.assertGreater(membership._appeal_recheck_remaining(-100, 970), 0)

    async def test_repeat_clicks_inside_the_cooldown_start_no_second_review(self) -> None:
        await self._add_record(user_id=971, message_id=871)
        callback = self._click_for(971, 871)
        submissions: list = []

        def _submit(**kwargs):
            submissions.append(kwargs)
            return self._submission(**kwargs)

        with patch.object(membership, "submit_privileged_task", side_effect=_submit):
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                    session_factory=self.session_factory,
                )
            self.assertEqual(len(submissions), 1)
            callback.answer.reset_mock()
            async with self.session_factory() as session:
                await membership.on_verification_callback(
                    callback,
                    session=session,
                    settings=_settings(),
                    session_factory=self.session_factory,
                )

        self.assertEqual(len(submissions), 1, "冷却期内不能再提交一次复核")
        self.assertTrue(callback.answer.await_args.kwargs["show_alert"])
        self.assertIn("复核", callback.answer.await_args.args[0])

    async def test_deferred_kept_outcome_posts_a_one_click_verification_entry(
        self,
    ) -> None:
        await self._add_record(user_id=972, message_id=872)
        await self._add_violation(user_id=972)
        callback = self._click_for(972, 872)

        with patch.object(
            membership,
            "_review_moderation_appeal",
            new=AsyncMock(
                return_value=(
                    _verdict(violated=True, conclusive=True),
                    "模型二次复核仍判「违规」",
                )
            ),
        ):
            async with self.session_factory() as session:
                await membership._run_verification_appeal_recheck(
                    callback,
                    session,
                    _settings(),
                    group_id=-100,
                    target_user_id=972,
                    display_name="用户972",
                    session_factory=self.session_factory,
                    deferred=True,
                )

        sent = callback.bot.send_message.await_args.kwargs
        self.assertEqual(sent["chat_id"], -100)
        button = sent["reply_markup"].inline_keyboard[0][0]
        self.assertIn("t.me/", button.url)
        self.assertIn("仍判违规", sent["text"])
        # 维持原判：质询必须还在，不能悄悄放人
        record = await self._record(972)
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "pending")

    async def test_the_deferred_review_still_routes_a_cleared_member_to_approve(
        self,
    ) -> None:
        """挪到后台不能改变判定口径：清白消息照样走管理员「通过」那条放行链路。"""

        await self._add_record(user_id=973, message_id=873)
        await self._add_violation(user_id=973)
        callback = self._click_for(973, 873)
        approve = AsyncMock()

        with (
            patch.object(
                membership,
                "_review_moderation_appeal",
                new=AsyncMock(
                    return_value=(_verdict(violated=False, conclusive=True), "未违规")
                ),
            ),
            patch.object(
                membership, "_handle_verification_admin_callback", new=approve
            ),
        ):
            async with self.session_factory() as session:
                await membership._run_verification_appeal_recheck(
                    callback,
                    session,
                    _settings(),
                    group_id=-100,
                    target_user_id=973,
                    display_name="用户973",
                    session_factory=self.session_factory,
                    deferred=True,
                )

        approve.assert_awaited_once()
        kwargs = approve.await_args.kwargs
        self.assertEqual(kwargs["action"], VERIFICATION_CALLBACK_APPROVE)
        self.assertTrue(kwargs["system_override"])
        self.assertEqual(kwargs["target_user_id"], 973)


if __name__ == "__main__":
    unittest.main()
