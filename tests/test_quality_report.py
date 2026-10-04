"""审核质量报表：命中构成、边缘判定、被改判放行的（误伤）条数。

这张报表要能回答一个具体问题：**机器人这些天判得准不准**。
所以测的不是"函数能跑"，而是数字对不对：窗口边界、置信度分档、放行计数、时区。
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import timedelta
from html.parser import HTMLParser
from types import SimpleNamespace

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import (
    BanAuditEvent,
    Group,
    GroupMessageArchive,
    JoinVerification,
    MemberCheckin,
    ModerationRule,
    Violation,
)
from bot.services.ban_audit import record_ban_event
from bot.services.quality_report import (
    collect_activity,
    collect_quality,
    render_group_quality,
    render_quality_report,
)
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -100

#: 报表正文只允许这两种标签；多出来的就是不受信任文本被拼了进来。
_ALLOWED_TAGS = {"b", "blockquote"}
_BARE_AMPERSAND_RE = re.compile(r"&(?![a-zA-Z#][a-zA-Z0-9]*;)")


def _html_tags(text: str) -> set[str]:
    """渲染结果里出现的标签名集合（Telegram 只认自己那几种）。"""

    found: set[str] = set()

    class _Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):  # noqa: ANN001 - 覆写标准签名
            found.add(tag)

    _Collector().feed(text)
    return found


def _violation(
    *,
    user_id: int,
    action: str = "challenge",
    confidence: float | None = None,
    reason: str = "",
    age_days: float = 0,
    rule_id: int | None = None,
) -> Violation:
    created = now_shanghai_naive() - timedelta(hours=8) - timedelta(days=age_days)
    return Violation(
        group_id=GROUP_ID,
        user_id=user_id,
        rule_id=rule_id,
        message_text="测试消息",
        action_taken=action,
        confidence=confidence,
        verdict_reason=reason,
        created_at=created,
    )


class QualityReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        from bot.services.authz import authorize_group

        async with self.session_factory() as session:
            session.add(Group(id=GROUP_ID, title="测试群", settings={}))
            await authorize_group(session, GROUP_ID, 1)
            session.add(
                ModerationRule(
                    id=1,
                    group_id=GROUP_ID,
                    rule_type="llm",
                    pattern="禁止发布广告、推销与引流：商品或服务推销",
                    action="ban",
                    enabled=True,
                )
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _seed(self, *objects: object) -> None:
        async with self.session_factory() as session:
            for obj in objects:
                session.add(obj)
            await session.commit()

    async def test_confidence_is_split_into_marginal_and_confident(self) -> None:
        await self._seed(
            _violation(user_id=1, confidence=0.70, reason="疑似引流", rule_id=1),
            _violation(user_id=1, confidence=0.85, rule_id=1),
            _violation(user_id=2, confidence=0.95, rule_id=1),
            _violation(user_id=3),  # 历史行：没有置信度
        )

        async with self.session_factory() as session:
            quality = await collect_quality(session, group_id=GROUP_ID, days=7)

        self.assertEqual(quality.total, 4)
        self.assertEqual(quality.marginal, 2, "0.70/0.85 都算边缘判定")
        self.assertEqual(quality.confident, 1)
        self.assertEqual(quality.no_confidence, 1)
        self.assertEqual(quality.members, 3)
        self.assertEqual(quality.repeat_members, 1, "user 1 命中两次")
        self.assertEqual(quality.by_rule[0][0].startswith("[llm]"), True)
        self.assertEqual(quality.top_reasons[0], ("疑似引流", 1))

    async def test_cleared_events_drive_the_false_positive_rate(self) -> None:
        await self._seed(
            _violation(user_id=1, confidence=0.7),
            _violation(user_id=2, confidence=0.8),
            _violation(user_id=3, confidence=0.95),
            _violation(user_id=4, confidence=0.95),
        )
        async with self.session_factory() as session:
            await record_ban_event(
                session,
                group_id=GROUP_ID,
                target_user_id=1,
                action="clear",
                source="appeal_recheck",
                outcome="cleared",
                reason="复核判为正常消息",
                reference_type="violation",
                reference_id=1,
            )
            await record_ban_event(
                session,
                group_id=GROUP_ID,
                target_user_id=2,
                action="clear",
                source="admin_review",
                outcome="cleared",
                reason="管理员判定放行",
            )
            # 入群资料拦截不属于消息级误伤，不能混进误伤率
            await record_ban_event(
                session,
                group_id=GROUP_ID,
                target_user_id=9,
                action="ban",
                source="profile_screening",
                outcome="banned",
                reason="简介引流",
            )
            await session.commit()

        async with self.session_factory() as session:
            quality = await collect_quality(session, group_id=GROUP_ID, days=7)

        self.assertEqual(quality.cleared_appeal, 1)
        self.assertEqual(quality.cleared_admin, 1)
        self.assertEqual(quality.cleared, 2)
        self.assertEqual(quality.join_bans, 1)
        self.assertAlmostEqual(quality.false_positive_rate, 0.5)
        text = render_quality_report(quality)
        self.assertIn("误伤率 50%", text)
        self.assertIn("模型复核 1｜管理员 1", text)

    async def test_rows_outside_the_window_are_ignored(self) -> None:
        await self._seed(
            _violation(user_id=1, confidence=0.95, age_days=0),
            _violation(user_id=2, confidence=0.95, age_days=20),
        )

        async with self.session_factory() as session:
            week = await collect_quality(session, group_id=GROUP_ID, days=7)
            month = await collect_quality(session, group_id=GROUP_ID, days=30)

        self.assertEqual(week.total, 1)
        self.assertEqual(month.total, 2)

    async def test_empty_window_is_stated_plainly(self) -> None:
        async with self.session_factory() as session:
            quality = await collect_quality(session, group_id=GROUP_ID, days=7)

        text = render_quality_report(quality)
        self.assertIn("本期没有任何审核命中", text)
        self.assertIsNone(quality.false_positive_rate)

    async def test_pending_moderation_challenges_are_counted(self) -> None:
        await self._seed(
            JoinVerification(
                group_id=GROUP_ID,
                user_id=5,
                kind="moderation",
                status="pending",
                reason="疑似广告",
                deadline_at=now_shanghai_naive() + timedelta(minutes=10),
            ),
            JoinVerification(
                group_id=GROUP_ID,
                user_id=6,
                kind="moderation",
                status="released",
                deadline_at=now_shanghai_naive() + timedelta(minutes=10),
            ),
            JoinVerification(
                group_id=GROUP_ID,
                user_id=7,
                kind="join",
                status="pending",
                deadline_at=now_shanghai_naive() + timedelta(minutes=10),
            ),
        )

        async with self.session_factory() as session:
            quality = await collect_quality(session, group_id=GROUP_ID, days=7)

        self.assertEqual(quality.pending_challenges, 1, "只算审核类、且还在 pending 的")

    async def test_activity_counts_messages_senders_and_points(self) -> None:
        now = now_shanghai_naive()
        today = now.date()
        await self._seed(
            GroupMessageArchive(
                group_id=GROUP_ID,
                message_key="k1",
                telegram_message_id=1,
                sender_id=11,
                sender_display_name="甲",
                content="你好",
                sent_at=now - timedelta(hours=2),
            ),
            GroupMessageArchive(
                group_id=GROUP_ID,
                message_key="k2",
                telegram_message_id=2,
                sender_id=12,
                sender_display_name="乙",
                content="在吗",
                sent_at=now - timedelta(hours=1),
            ),
            MemberCheckin(
                group_id=GROUP_ID,
                user_id=11,
                checkin_date=today.isoformat(),
                points=3,
                display_name="甲",
            ),
            MemberCheckin(
                group_id=GROUP_ID,
                user_id=12,
                checkin_date=today.isoformat(),
                points=1,
                display_name="乙",
            ),
        )

        async with self.session_factory() as session:
            activity = await collect_activity(session, group_id=GROUP_ID, days=7)

        self.assertEqual(activity.messages, 2)
        self.assertEqual(activity.senders, 2)
        self.assertEqual(activity.checkin_count, 2)
        self.assertEqual(activity.points_awarded, 4)
        self.assertEqual(activity.top_members[0], ("甲", 3))

    async def test_render_group_quality_reports_both_sections(self) -> None:
        await self._seed(_violation(user_id=1, confidence=0.7, reason="疑似引流"))

        async with self.session_factory() as session:
            text = await render_group_quality(session, group_id=GROUP_ID, days=7)

        self.assertIn("审核质量 · 近 7 天", text)
        self.assertIn("<blockquote expandable>", text, "管理命令靠 blockquote 保持排版")
        self.assertIn("群活跃", text)

    async def test_untrusted_text_is_html_escaped_before_rendering(self) -> None:
        """D3-14：管理员自由输入 / LLM 生成 / 成员自填含裸 ``<``、``&`` 时整条报表发不出去。

        Telegram ``parse_mode=HTML`` 遇到裸标签/裸 ``&`` 直接 400，而
        ``admin.py`` 的 ``try`` 只包渲染不包发送，所以整条 ``/modstats`` 会静默消失。
        """

        await self._seed(
            # 管理员自由输入的匹配模式
            ModerationRule(
                id=2,
                group_id=GROUP_ID,
                rule_type="regex",
                pattern="<script>广告</script>&",
                action="ban",
                enabled=True,
            ),
            # LLM 生成的判定理由（照抄 cost_report._esc docstring 里那个坑）
            _violation(user_id=1, confidence=0.7, reason="置信<0.9 & 判定", rule_id=2),
            # 成员自填的签到昵称
            MemberCheckin(
                group_id=GROUP_ID,
                user_id=11,
                checkin_date=now_shanghai_naive().date().isoformat(),
                points=3,
                display_name="<b>evil</b>&co",
            ),
        )

        async with self.session_factory() as session:
            text = await render_group_quality(session, group_id=GROUP_ID, days=7)

        # 三类文本都还在（转义只改显示，不改统计口径与内容）
        self.assertIn("[regex]", text)
        self.assertIn("置信", text)
        self.assertIn("evil", text)
        # 但必须已经转义成实体，而不是把裸标签交给 Telegram 解析
        self.assertNotIn("<script>", text, "ModerationRule.pattern 没转义")
        self.assertNotIn("<b>evil", text, "MemberCheckin.display_name 没转义")
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("置信&lt;0.9 &amp; 判定", text)
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;&amp;co", text)
        # 整条正文必须是 Telegram 能解析的 HTML
        self.assertEqual(_html_tags(text), _ALLOWED_TAGS, f"正文里混进了外来标签：{text}")
        self.assertEqual(_BARE_AMPERSAND_RE.findall(text), [], f"正文里有裸 & ：{text}")

    async def test_record_violation_persists_confidence_and_reason(self) -> None:
        """新列要真的被写进去，否则报表永远只有 NULL。"""

        from bot.config import Settings
        from bot.services.moderation import ModerationService

        service = ModerationService(SimpleNamespace(), SimpleNamespace())
        async with self.session_factory() as session:
            rule = (
                await session.execute(select(ModerationRule).where(ModerationRule.id == 1))
            ).scalar_one()
            await service.record_violation(
                session,
                GROUP_ID,
                77,
                "先加我白名单",
                "challenge",
                rule,
                confidence=0.75,
                verdict_reason="疑似引流",
            )
            await session.commit()
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Violation).where(Violation.user_id == 77)
                )
            ).scalar_one()

        self.assertAlmostEqual(row.confidence, 0.75)
        self.assertEqual(row.verdict_reason, "疑似引流")

if __name__ == "__main__":
    unittest.main()
