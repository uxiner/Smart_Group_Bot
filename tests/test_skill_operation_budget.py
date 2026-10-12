"""技能入口的功能操作额度（正式回归）。

走真实 ``SkillService._run_tool`` 与真实 worker（闸门 + 许可），只把外部技能
替换成记录供应商调用的替身；**不** mock ``consume_operation`` 或预算 service。

覆盖：两个技能 0 额度被拒且外部 run 0 次；1 额度时第一通过、第二拒；
disabled / unconfigured 外部仍可用且不计数；跨群与私聊隔离；外部调用失败
仍计一次；并发 limit=2 只放 2 个真实外部调用；SQL 计数精确。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import GroupBudgetDay
from bot.services import group_budget, pricing
from bot.services import usage_scope as scopes
from bot.services.skills.base import Skill, SkillContext, SkillRunResult
from bot.services.skills.service import SkillService

GROUP_A = -1001234567890
PRIVATE_CHAT = 556677


class OuterSkill(Skill):
    """记录外部供应商调用次数的替身；不联网。"""

    name = "outer"
    calls: list[tuple[str, int]]

    def __init__(self, calls: list[tuple[str, int]], *, name: str = "outer",
                 fail: bool = False):
        self.calls = calls
        self.name = name
        self._fail = fail

    async def run(self, arguments, context):  # noqa: ANN001
        self.calls.append((self.name, int(context.chat_id or 0)))
        if self._fail:
            return SkillRunResult(ok=False, skill=self.name, summary="外部失败")
        return SkillRunResult(ok=True, skill=self.name, summary="ok")


class SkillOperationBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="slot-a", model="m", currency="USD",
                    input_per_million="3", output_per_million="15",
                    cache_read_per_million="1", cache_write_per_million="4",
                ),
            ),
        )
        self.service = group_budget.shared_service(
            self.session_factory, lambda: self.catalog
        )
        self.service.invalidate()
        self.calls: list[tuple[str, int]] = []
        # llm 不参与这条路径：工具直接调用，不经过模型轮次。
        self.skill_service = SkillService(llm=None)

    async def asyncTearDown(self) -> None:
        group_budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _context(self, chat_id: int) -> SkillContext:
        return SkillContext(chat_id=chat_id, session_factory=self.session_factory)

    def _skills(self, *, fail: bool = False) -> dict:
        return {
            name: OuterSkill(self.calls, name=name, fail=fail)
            for name in ("websearch", "doubao_tts", "vote_ban")
        }

    async def _run(self, name: str, chat_id: int, *, fail: bool = False):
        skills = self._skills(fail=fail)
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
        ) if chat_id < 0 else scopes.usage_scope(
            kind=scopes.SCOPE_PRIVATE, purpose="optional"
        ):
            return await self.skill_service._run_tool(
                name=name, arguments={}, context=self._context(chat_id),
                skills=skills,
            )

    async def _save(self, *, enabled=True, operation_limits=None, **kw):
        await self.service.save_budget(
            GROUP_A, expected_revision=kw.pop("revision", 0), enabled=enabled,
            currency="USD", operation_limits=operation_limits, **kw
        )
        self.service.invalidate()

    async def _operations(self, group_id=GROUP_A) -> dict:
        async with self.session_factory() as session:
            row = (
                await session.scalars(
                    select(GroupBudgetDay).where(GroupBudgetDay.group_id == group_id)
                )
            ).first()
        return dict((row.used_operations or {}) if row else {})

    # ------------------------------------------------------------------
    async def test_zero_limit_denies_both_skills_without_calling_out(self) -> None:
        await self._save(operation_limits={"search": 0, "tts": 0})
        for name, feature in (("websearch", "search"), ("doubao_tts", "tts")):
            result = await self._run(name, GROUP_A)
            self.assertFalse(result.ok, name)
            self.assertEqual(result.error, "operation_budget_denied", name)
        self.assertEqual(self.calls, [], "被拒的技能仍然调用了外部服务")
        self.assertEqual(await self._operations(), {})

    async def test_first_passes_second_denied(self) -> None:
        await self._save(operation_limits={"search": 1, "tts": 1})
        first = await self._run("websearch", GROUP_A)
        self.assertTrue(first.ok, first.error)
        second = await self._run("websearch", GROUP_A)
        self.assertFalse(second.ok)
        self.assertEqual(second.error, "operation_budget_denied")
        self.assertEqual(len(self.calls), 1, self.calls)
        self.assertEqual(await self._operations(), {"search": 1})

    async def test_failing_external_call_still_counts_once(self) -> None:
        """已准入后外部失败也算一次 —— 未知结果的调用退款等于送免费。"""

        await self._save(operation_limits={"search": 1})
        result = await self._run("websearch", GROUP_A, fail=True)
        self.assertFalse(result.ok)
        self.assertEqual(len(self.calls), 1, self.calls)
        self.assertEqual(await self._operations(), {"search": 1})
        again = await self._run("websearch", GROUP_A)
        self.assertEqual(again.error, "operation_budget_denied")

    async def test_disabled_or_unconfigured_keeps_the_feature_usable(self) -> None:
        await self._save(enabled=False, operation_limits={"search": 0})
        result = await self._run("websearch", GROUP_A)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(await self._operations(), {})

        await self._save(revision=1, operation_limits={"tts": 1})
        unconfigured = await self._run("websearch", GROUP_A)
        self.assertTrue(unconfigured.ok, unconfigured.error)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(await self._operations(), {})

    async def test_private_chat_is_not_charged_to_a_group(self) -> None:
        await self._save(operation_limits={"search": 0})
        result = await self._run("websearch", PRIVATE_CHAT)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(await self._operations(), {})

    async def test_groups_are_isolated(self) -> None:
        other = -1005550001111
        await self._save(operation_limits={"search": 0})
        await self.service.save_budget(
            other, expected_revision=0, enabled=True, currency="USD",
            operation_limits={"search": 5},
        )
        self.service.invalidate()
        self.assertFalse((await self._run("websearch", GROUP_A)).ok)
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=other, purpose="optional"
        ):
            result = await self.skill_service._run_tool(
                name="websearch", arguments={}, context=self._context(other),
                skills=self._skills(),
            )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(await self._operations(GROUP_A), {})
        self.assertEqual(await self._operations(other), {"search": 1})

    async def test_concurrent_limit_two_admits_exactly_two_external_calls(self) -> None:
        await self._save(operation_limits={"search": 2})
        results = await asyncio.gather(
            *(self._run("websearch", GROUP_A) for _ in range(6)),
            return_exceptions=True,
        )
        self.assertEqual(
            [repr(x) for x in results if isinstance(x, BaseException)], [],
            f"并发执行抛了异常：{results!r}",
        )
        ok = [r for r in results if r.ok]
        denied = [r for r in results if not r.ok]
        self.assertEqual(len(ok), 2, f"放行数不是 2：{results!r}")
        self.assertEqual(len(denied), 4)
        for item in denied:
            self.assertEqual(item.error, "operation_budget_denied", item.error)
        self.assertEqual(len(self.calls), 2, self.calls)
        self.assertEqual(await self._operations(), {"search": 2})

    async def test_unmapped_safety_skill_still_runs_with_zero_operation_limit(self) -> None:
        """安全正控：未映射的技能（vote_ban）真实走 _run_tool，不受可选额度影响。"""

        await self._save(operation_limits={"search": 0, "tts": 0})
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
        ):
            result = await self.skill_service._run_tool(
                name="vote_ban", arguments={}, context=self._context(GROUP_A),
                skills=self._skills(),
            )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(
            [name for name, _ in self.calls], ["vote_ban"], self.calls
        )
        self.assertEqual(await self._operations(), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
