"""审核判定的可观测性：置信度与"累计命中次数"真的写进了 violations。

这张表要能回答两个运营问题：

1) **这次判定有多确定** —— ``moderation.high_confidence_threshold``（默认 0.9）
   调高或调低，会动到哪一批命中、会不会把大部分命中推进"不可用"区间；
2) **同一用户在本群本规则上第几次命中** —— ``moderation.warn_threshold``（默认 3）
   该不该调。

改之前这两个数字只活在内存里的 verdict 对象上，落库时丢了（历史行全 NULL），
所以阈值只能靠日志拍脑袋。这里把它们钉死，同时钉住"落库口径变了、判定口径不变"：

- LLM 判定 → 写该次判定的真实置信度（不是常量，更不是写死的 0.9）；
- 本地关键词/正则命中 → 没有模型置信度，写 NULL，不许拿 1.0 冒充；
- ``warning_count`` → 本群 + 本用户 + 本规则下的累计命中次数（含本次）；
- 阈值/质询/警告的判定继续读原来的 ``verdict.confidence``，一行代码都没改。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from bot.config import ModerationConfig
from bot.db.engine import init_db
from bot.db.models import Group, ModerationRule, Violation
from bot.handlers.group import _verdict_confidence
from bot.services.moderation import ModerationService, ModerationVerdict
from bot.services.quality_report import collect_quality, render_quality_report
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -100
OTHER_GROUP_ID = -200
LLM_RULE_ID = 1
REGEX_RULE_ID = 2
KEYWORD_RULE_ID = 3
SECOND_LLM_RULE_ID = 4


def _utc_now() -> object:
    """violations.created_at 是 UTC 朴素时间（报表按此比较）。"""

    return now_shanghai_naive() - timedelta(hours=8)


class ModerationMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self.path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=GROUP_ID, title="测试群", settings={}))
            session.add(Group(id=OTHER_GROUP_ID, title="另一个群", settings={}))
            session.add(
                ModerationRule(
                    id=LLM_RULE_ID,
                    group_id=GROUP_ID,
                    rule_type="llm",
                    pattern="禁止发布广告与引流",
                    action="warn",
                    enabled=True,
                )
            )
            session.add(
                ModerationRule(
                    id=REGEX_RULE_ID,
                    group_id=GROUP_ID,
                    rule_type="regex",
                    pattern="加我.*白名单",
                    action="warn",
                    enabled=True,
                )
            )
            session.add(
                ModerationRule(
                    id=KEYWORD_RULE_ID,
                    group_id=GROUP_ID,
                    rule_type="keyword",
                    pattern="免费节点",
                    action="warn",
                    enabled=True,
                )
            )
            session.add(
                ModerationRule(
                    id=SECOND_LLM_RULE_ID,
                    group_id=GROUP_ID,
                    rule_type="llm",
                    pattern="禁止刷屏",
                    action="warn",
                    enabled=True,
                )
            )
            await session.commit()
        self.service = ModerationService(
            ModerationConfig(warn_threshold=3),
            SimpleNamespace(),
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def _service(self, reply: str) -> ModerationService:
        async def fake_moderation(_system: str, _user: str) -> str:
            return reply

        return ModerationService(
            ModerationConfig(warn_threshold=3),
            SimpleNamespace(moderation=fake_moderation),
        )

    async def _rows(self) -> list[Violation]:
        async with self.session_factory() as session:
            result = await session.execute(select(Violation).order_by(Violation.id))
            return list(result.scalars().all())

    # ---------------------------------------------------------------- 置信度

    async def test_llm_verdict_persists_its_own_confidence_not_a_constant(self) -> None:
        """LLM 命中 → 库里是该次判定的真实置信度。"""

        scores = (0.62, 0.97)
        for index, (score, text) in enumerate(zip(scores, ("买我的服务", "再来一条广告"))):
            service = self._service(
                json.dumps(
                    {
                        "violated": True,
                        "confidence": score,
                        "rule_id": LLM_RULE_ID,
                        "reason": "广告",
                    }
                )
            )
            async with self.session_factory() as session:
                verdict = await service.evaluate(session, GROUP_ID, text)
                self.assertTrue(verdict.violated)
                self.assertFalse(verdict.deterministic)
                self.assertAlmostEqual(verdict.confidence, score)
                await service.record_violation(
                    session,
                    GROUP_ID,
                    42,
                    text,
                    "challenge",
                    verdict.rule,
                    source_message_id=5000 + index,
                    confidence=_verdict_confidence(verdict),
                    verdict_reason=verdict.reason,
                )
                await session.commit()

        rows = await self._rows()
        self.assertEqual([round(float(row.confidence), 3) for row in rows], [0.62, 0.97])
        # 两个值互不相同 → 落库的不是写死的常量（比如 0.9 或 1.0）。
        self.assertNotAlmostEqual(float(rows[0].confidence), 0.9)
        self.assertNotAlmostEqual(float(rows[0].confidence), float(rows[1].confidence))

    async def test_local_regex_and_keyword_hits_persist_null_confidence(self) -> None:
        """本地规则命中没有模型置信度：写 NULL，判定口径仍然是 1.0。"""

        service = self._service('{"violated": false}')
        for rule_id, text in (
            (REGEX_RULE_ID, "加我进白名单"),
            (KEYWORD_RULE_ID, "免费节点来啦"),
        ):
            async with self.session_factory() as session:
                verdict = await service.evaluate(session, GROUP_ID, text)
                self.assertTrue(verdict.violated, text)
                self.assertTrue(verdict.deterministic, text)
                # 判定口径没有变：确定性命中仍按 1.0 算高置信。
                self.assertEqual(verdict.confidence, 1.0)
                self.assertTrue(service.is_high_confidence(verdict))
                # 落库口径变了：NULL，而不是 1.0。
                self.assertIsNone(_verdict_confidence(verdict))
                row = await service.record_violation(
                    session,
                    GROUP_ID,
                    7,
                    text,
                    "warn",
                    verdict.rule,
                    source_message_id=6000 + rule_id,
                    confidence=_verdict_confidence(verdict),
                    verdict_reason=verdict.reason,
                )
                self.assertIsNone(row.confidence)
                await session.commit()

        rows = await self._rows()
        self.assertEqual([row.confidence for row in rows], [None, None])

    def test_persistence_change_leaves_the_threshold_decision_untouched(self) -> None:
        """只有落库口径变了：is_high_confidence 继续读 verdict.confidence。"""

        service = self.service
        high = ModerationVerdict(
            violated=True, reason="", rule=None, conclusive=True, confidence=0.95
        )
        marginal = ModerationVerdict(
            violated=True, reason="", rule=None, conclusive=True, confidence=0.70
        )
        inconclusive = ModerationVerdict(
            violated=True, reason="", rule=None, conclusive=False, confidence=1.0
        )
        regex = ModerationVerdict(
            violated=True,
            reason="命中正则规则",
            rule=SimpleNamespace(id=REGEX_RULE_ID),
            conclusive=True,
            confidence=1.0,
            deterministic=True,
        )

        self.assertTrue(service.is_high_confidence(high))
        self.assertFalse(service.is_high_confidence(marginal))
        self.assertFalse(service.is_high_confidence(inconclusive))
        self.assertTrue(service.is_high_confidence(regex), "正则命中仍是高置信")

        self.assertEqual(_verdict_confidence(high), 0.95)
        self.assertEqual(_verdict_confidence(marginal), 0.70)
        self.assertIsNone(_verdict_confidence(regex), "只有落库这一层被改成 NULL")
        self.assertIsNone(_verdict_confidence(None))

    # ------------------------------------------------------------ 累计告警次数

    async def test_warning_count_counts_hits_per_group_user_and_rule(self) -> None:
        """同一用户同群同规则连续命中 → 1、2、3；换群/换人/换规则各自重新数。"""

        async with self.session_factory() as session:
            rule_one = await session.get(ModerationRule, LLM_RULE_ID)
            rule_two = await session.get(ModerationRule, SECOND_LLM_RULE_ID)

            counts: list[int | None] = []
            for index in range(3):
                row = await service_record(
                    self.service, session, GROUP_ID, 42, f"同规则 {index}", rule_one
                )
                counts.append(row.warning_count)
            self.assertEqual(counts, [1, 2, 3])

            other_rule = await service_record(
                self.service, session, GROUP_ID, 42, "另一条规则", rule_two
            )
            other_user = await service_record(
                self.service, session, GROUP_ID, 43, "另一个用户", rule_one
            )
            other_group = await service_record(
                self.service, session, OTHER_GROUP_ID, 42, "另一个群", rule_one
            )
            unlabelled: list[int | None] = []
            for index in range(2):
                row = await service_record(
                    self.service, session, GROUP_ID, 44, f"未标注规则 {index}", None
                )
                unlabelled.append(row.warning_count)
            await session.commit()

        self.assertEqual(other_rule.warning_count, 1)
        self.assertEqual(other_user.warning_count, 1)
        self.assertEqual(other_group.warning_count, 1)
        self.assertEqual(unlabelled, [1, 2])

    async def test_warning_count_does_not_double_count_a_replayed_message(self) -> None:
        """同一条 Telegram 消息被重投 → 只有一行，次数不涨。"""

        async with self.session_factory() as session:
            rule_one = await session.get(ModerationRule, LLM_RULE_ID)
            first = await service_record(
                self.service,
                session,
                GROUP_ID,
                42,
                "同一条消息",
                rule_one,
                source_message_id=7001,
            )
            self.assertEqual(first.warning_count, 1)
            await session.commit()

        async with self.session_factory() as session:
            rule_one = await session.get(ModerationRule, LLM_RULE_ID)
            replay = await service_record(
                self.service,
                session,
                GROUP_ID,
                42,
                "同一条消息",
                rule_one,
                source_message_id=7001,
            )
            await session.commit()

        self.assertEqual(replay.warning_count, 1)
        async with self.session_factory() as session:
            total = (
                await session.execute(select(func.count()).select_from(Violation))
            ).scalar()
        self.assertEqual(total, 1)

    # ------------------------------------------------------- 汇总输出与库一致

    async def test_quality_summary_bands_agree_with_the_rows_in_the_database(
        self,
    ) -> None:
        """置信度分档 / 高置信占比：数字必须与库里的行对得上。"""

        scores = (0.42, 0.61, 0.75, 0.93, 0.99)
        async with self.session_factory() as session:
            for index, score in enumerate(scores):
                session.add(
                    Violation(
                        group_id=GROUP_ID,
                        user_id=100 + index,
                        rule_id=LLM_RULE_ID,
                        message_text="命中",
                        action_taken="challenge",
                        confidence=score,
                        verdict_reason="广告",
                        created_at=_utc_now(),
                    )
                )
            # 正则命中：没有置信度的那一类（新行就该长这样）
            session.add(
                Violation(
                    group_id=GROUP_ID,
                    user_id=200,
                    rule_id=REGEX_RULE_ID,
                    message_text="加我进白名单",
                    action_taken="warn",
                    confidence=None,
                    verdict_reason="命中正则规则",
                    created_at=_utc_now(),
                )
            )
            await session.commit()

        async with self.session_factory() as session:
            quality = await collect_quality(
                session, group_id=GROUP_ID, days=7, high_threshold=0.9
            )
            loose = await collect_quality(
                session, group_id=GROUP_ID, days=7, high_threshold=0.6
            )

        self.assertEqual(quality.total, 6)
        self.assertEqual(
            quality.confidence_bands,
            {"lt_0_5": 1, "0_5_0_7": 1, "0_7_0_9": 1, "ge_0_9": 2},
        )
        self.assertEqual(
            sum(quality.confidence_bands.values()) + quality.no_confidence,
            quality.total,
            "分档 + 无置信度必须覆盖全部命中",
        )
        self.assertEqual(quality.high_confidence_hits, 2)
        self.assertAlmostEqual(quality.high_confidence_ratio, 2 / 6)
        # 既有口径没被破坏
        self.assertEqual(
            (quality.marginal, quality.confident, quality.no_confidence), (3, 2, 1)
        )
        # 阈值是旋钮：调低到 0.6，落在"高置信"里的命中从 2 条变 4 条
        self.assertEqual(loose.high_confidence_hits, 4)

        text = render_quality_report(quality)
        self.assertIn("置信度分档：&lt;0.5 1｜0.5–0.7 1｜0.7–0.9 1｜≥0.9 2", text)
        self.assertIn("高于高置信阈值（0.9）：<b>2</b> 条，占全部命中 33%", text)
        # NULL 不等于"历史行"：正则命中本来就没有模型置信度，文案要说清楚
        self.assertIn("1 条命中没有模型置信度", text)

    async def test_quality_summary_stays_quiet_when_there_is_nothing_to_show(
        self,
    ) -> None:
        async with self.session_factory() as session:
            quality = await collect_quality(session, group_id=GROUP_ID, days=7)

        self.assertIsNone(quality.high_confidence_ratio)
        text = render_quality_report(quality)
        self.assertIn("本期没有任何审核命中", text)
        self.assertNotIn("置信度分档", text)


async def service_record(
    service: ModerationService,
    session: object,
    group_id: int,
    user_id: int,
    text: str,
    rule: ModerationRule | None,
    *,
    source_message_id: int | None = None,
) -> Violation:
    return await service.record_violation(
        session,  # type: ignore[arg-type]
        group_id,
        user_id,
        text,
        "warn",
        rule,
        source_message_id=source_message_id,
    )


if __name__ == "__main__":
    unittest.main()
