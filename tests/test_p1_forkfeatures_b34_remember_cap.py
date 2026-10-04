"""修复批 P1-2 / B-34：``remember`` 工具的每日上限不能被单个成员吃光。

复现的原缺陷（``AUDIT-B`` B-34）：

``skills/remember.py`` 的计数维度是 ``(scope, scope_id)``——**没有主语**。
群聊里 ``scope_id`` 就是群 id，所以任何一个**普通成员**都能在一天内把整群的
``memory_tool_daily_cap``（默认 30）写满，群里其他人当天再也记不住任何事。

修法（refs 建议）：**只加 per-subject 上限，不动 group 总上限**——管理员统一帮大家
记的用法不变。私聊作用域本来就「一个人一个额度」，口径保持原样。

断言：同一个人写满个人上限后被拦下；换成**另一个人**（同群）仍能继续写。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import UserFact
from bot.services import long_term_memory as ltm
from bot.services.skills.base import SkillContext
from bot.services.skills.remember import RememberSkill

GROUP_ID = -100777
ALICE = 924
BOB = 925
NOW = datetime(2026, 10, 3, 21, 40, 0)


def _settings(**overrides) -> SimpleNamespace:
    bot = SimpleNamespace(
        memory_facts_enabled=True,
        memory_tool_enabled=True,
        memory_tool_daily_cap=30,
        memory_event_ttl_days=30,
    )
    for key, value in overrides.items():
        setattr(bot, key, value)
    return SimpleNamespace(bot=bot)


class SubjectDailyCapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _context(self, user_id: int) -> SkillContext:
        return SkillContext(
            session_factory=self.session_factory,
            chat_id=GROUP_ID,
            sender_user_id=user_id,
            current_user_text="我喜欢喝咖啡",
        )

    async def _facts(self) -> list[UserFact]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(select(UserFact).order_by(UserFact.id.asc()))
            ).scalars()
            return list(rows)

    async def test_count_tool_facts_today_can_filter_by_subject(self) -> None:
        async with self.session_factory() as session:
            for index in range(3):
                await ltm.record_fact(
                    session,
                    scope=ltm.SCOPE_GROUP,
                    scope_id=GROUP_ID,
                    subject_user_id=ALICE,
                    fact_text=f"甲事实{index}",
                    category=ltm.CATEGORY_PREFERENCE,
                    confidence=70,
                    source_kind=ltm.SOURCE_TOOL,
                    evidence_excerpt="我喜欢喝咖啡",
                )
            await ltm.record_fact(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=BOB,
                fact_text="乙事实",
                category=ltm.CATEGORY_PREFERENCE,
                confidence=70,
                source_kind=ltm.SOURCE_TOOL,
                evidence_excerpt="我喜欢喝咖啡",
            )
            await session.commit()
            self.assertEqual(
                await ltm.count_tool_facts_today(
                    session, scope=ltm.SCOPE_GROUP, scope_id=GROUP_ID
                ),
                4,
            )
            self.assertEqual(
                await ltm.count_tool_facts_today(
                    session,
                    scope=ltm.SCOPE_GROUP,
                    scope_id=GROUP_ID,
                    subject_user_id=ALICE,
                ),
                3,
            )
            self.assertEqual(
                await ltm.count_tool_facts_today(
                    session,
                    scope=ltm.SCOPE_GROUP,
                    scope_id=GROUP_ID,
                    subject_user_id=BOB,
                ),
                1,
            )

    async def test_one_member_cannot_exhaust_the_group_quota(self) -> None:
        skill = RememberSkill(_settings())
        cap = ltm.TOOL_SUBJECT_DAILY_CAP
        context = self._context(ALICE)
        for index in range(cap):
            result = await skill.run({"fact": f"甲的第{index}条稳定事实"}, context)
            self.assertEqual(result.summary, "已记住", f"第{index}条应当写进去")

        with self.assertLogs("bot.services.skills.remember", level="INFO") as captured:
            blocked = await skill.run({"fact": "甲的又一条稳定事实"}, context)
        self.assertEqual(blocked.payload, {"skipped": True, "reason": "daily_cap"})
        self.assertTrue(
            any("本人" in line for line in captured.output),
            f"必须留下「按本人计数」的日志，实际={captured.output}",
        )
        self.assertEqual(len(await self._facts()), cap)

        # 关键：同群**另一个人**不受影响，仍能写（整群总额度没被吃光）。
        other = await skill.run({"fact": "乙的一条稳定事实"}, self._context(BOB))
        self.assertEqual(other.summary, "已记住")
        facts = await self._facts()
        self.assertEqual(len(facts), cap + 1)
        self.assertIn(
            BOB, {fact.subject_user_id for fact in facts}, "乙的事实必须真的落库"
        )

    async def test_private_scope_keeps_the_original_single_quota(self) -> None:
        """私聊里 ``scope_id == sender_id``，口径不变：不叠加 per-subject 闸门。"""

        skill = RememberSkill(_settings(memory_tool_daily_cap=2))
        context = SkillContext(
            session_factory=self.session_factory,
            chat_id=ALICE,
            sender_user_id=ALICE,
            current_user_text="我喜欢喝咖啡",
        )
        for index in range(2):
            result = await skill.run({"fact": f"私聊第{index}条稳定事实"}, context)
            self.assertEqual(result.summary, "已记住")
        blocked = await skill.run({"fact": "私聊第三条稳定事实"}, context)
        self.assertEqual(blocked.payload, {"skipped": True, "reason": "daily_cap"})
        self.assertEqual(len(await self._facts()), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
