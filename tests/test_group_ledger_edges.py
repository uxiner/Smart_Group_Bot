"""用量台账的边界（第四阶段返工）。

这些全部是"看起来有数字、其实账是错的"那一类：

* 只覆盖 stage/purpose 时**不能**丢掉已有的群归属；
* 上限淘汰**不得**删除已发生但未结算的尝试；
* 畸形/部分 usage 不得伪造 0，也不得把整次尝试丢掉；
* 请求成功与"上游有没有报 usage"是两个维度；
* 留存清理**不得**删掉尚未落盘的计数；
* 提交成功之后被取消**不得**把已经写进库的那批再并回内存；
* 迟到结果必须归到**发送那天**。
"""

from __future__ import annotations

import asyncio
import math
import os
import tempfile
import unittest

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import LlmCostDaily
from bot.services import group_costs
from bot.services import usage_scope as scopes

GROUP_A = -100900000001
GROUP_B = -100900000002


class LedgerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        group_costs.reset()
        group_costs.configure(self.session_factory)

    async def asyncTearDown(self) -> None:
        await group_costs.flush(force=True)
        group_costs.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _rows(self) -> list[LlmCostDaily]:
        await group_costs.flush(force=True)
        async with self.session_factory() as session:
            return list((await session.scalars(select(LlmCostDaily))).all())

    def _snapshot_totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for key, values in group_costs.snapshot().items():
            for name, value in values.items():
                totals[name] = totals.get(name, 0) + int(value or 0)
        return totals

    # ------------------------------------------------------------------
    # P4-1 只覆盖 stage/purpose 不得丢掉群归属
    # ------------------------------------------------------------------
    async def test_stage_only_override_keeps_the_group(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(stage="vision", purpose="safety")
            self.assertNotEqual(attempt, "", "record_attempt 不该静默失败")
            group_costs.record_result(
                attempt_id=attempt, usage={"prompt_tokens": 5, "completion_tokens": 1}
            )
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0].group_id), GROUP_A)
        self.assertEqual(str(rows[0].scope), "group")
        self.assertEqual(str(rows[0].stage), "vision")
        self.assertEqual(str(rows[0].purpose), scopes.PURPOSE_SAFETY)
        self.assertEqual(int(rows[0].attempts), 1)

    async def test_stage_only_override_keeps_private_user_kind(self) -> None:
        with scopes.usage_scope(kind="private_user", user_id=7):
            attempt = group_costs.record_attempt(stage="memory_extract")
            group_costs.record_result(attempt_id=attempt, usage={"prompt_tokens": 2})
        rows = await self._rows()
        self.assertEqual(str(rows[0].scope), "private_user")
        self.assertEqual(int(rows[0].group_id), 0)

    async def test_explicit_group_id_cannot_silently_flip_system_to_group(self) -> None:
        """当前上下文是 system 时，显式传 group_id 不该把它变成某个群。"""

        with scopes.usage_scope(kind="system"):
            attempt = group_costs.record_attempt(stage="main", group_id=GROUP_A)
            group_costs.record_result(attempt_id=attempt, usage={"prompt_tokens": 3})
        rows = await self._rows()
        self.assertEqual(str(rows[0].scope), "system")
        self.assertEqual(int(rows[0].group_id), 0)

    async def test_result_without_attempt_snapshot_uses_current_scope(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            group_costs.record_result(
                attempt_id="", stage="manual", provider="fixture", model="manual-m",
                usage={"prompt_tokens": 9},
            )
        row = await self._one_row("manual-m")
        self.assertEqual(int(row.group_id), GROUP_A)
        self.assertEqual(int(row.attempts), 1)

    # ------------------------------------------------------------------
    # P4-2 上限不得吃掉已发生但未结算的尝试
    # ------------------------------------------------------------------
    async def test_inflight_attempts_are_never_evicted_by_the_cap(self) -> None:
        original = group_costs._MAX_ATTEMPTS_TRACKED
        group_costs._MAX_ATTEMPTS_TRACKED = 3
        try:
            attempts = []
            with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
                for index in range(5):
                    attempts.append(
                        group_costs.record_attempt(
                            stage="skill", provider="fixture", model=f"m{index}"
                        )
                    )
                self.assertEqual(group_costs.inflight_attempts(), 5)
                for attempt in attempts:
                    group_costs.record_result(
                        attempt_id=attempt, usage={"prompt_tokens": 1}
                    )
        finally:
            group_costs._MAX_ATTEMPTS_TRACKED = original
        rows = await self._rows()
        self.assertEqual(sum(int(row.attempts) for row in rows), 5)
        self.assertEqual(sum(int(row.prompt_tokens or 0) for row in rows), 5)
        self.assertEqual(group_costs.inflight_attempts(), 0)

    async def test_record_attempt_alone_is_visible_as_inflight(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            group_costs.record_attempt(stage="skill", provider="fixture", model="m")
            self.assertEqual(group_costs.inflight_attempts(), 1)
            # 未结算的尝试**不**进台账：它还没发生 token 花费。
            self.assertEqual(self._snapshot_totals().get("attempts", 0), 0)

    # ------------------------------------------------------------------
    # P4-3 部分 / 畸形 usage
    # ------------------------------------------------------------------
    async def test_partial_usage_does_not_fabricate_zero_coverage(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(
                stage="skill", provider="fixture", model="m"
            )
            group_costs.record_result(
                attempt_id=attempt, usage={"prompt_tokens": 12}
            )
        rows = await self._rows()
        row = rows[0]
        self.assertEqual(int(row.prompt_tokens), 12)
        self.assertIsNone(
            row.output_tokens, "上游没报的字段不能写成 0"
        )
        self.assertIsNone(row.cached_tokens)
        self.assertIsNone(row.cache_write_tokens)
        self.assertGreaterEqual(int(row.usage_unknown), 0)

    async def _one_row(self, model: str) -> LlmCostDaily:
        """按模型取本轮那一行：不能用 rows[0]，因为同表里还有前一个子用例的行。"""

        rows = await self._rows()
        matched = [row for row in rows if str(row.model) == model]
        self.assertEqual(len(matched), 1, f"{model}: {[r.model for r in rows]}")
        return matched[0]

    async def test_negative_and_non_finite_tokens_are_rejected(self) -> None:
        for index, bad in enumerate((-10, math.inf, -math.inf, math.nan)):
            with self.subTest(bad=bad):
                model = f"bad-{index}"
                with scopes.usage_scope(
                    kind="group", group_id=GROUP_A, purpose="optional"
                ):
                    attempt = group_costs.record_attempt(
                        stage="skill", provider="fixture", model=model
                    )
                    group_costs.record_result(
                        attempt_id=attempt, usage={"prompt_tokens": bad}
                    )
                row = await self._one_row(model)
                self.assertIsNone(row.prompt_tokens, f"{bad} 不得进账")
                self.assertEqual(int(row.usage_unknown), 1)
                self.assertEqual(int(row.attempts), 1)

    async def test_float_token_values_are_rejected(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(
                stage="skill", provider="fixture", model="floaty"
            )
            group_costs.record_result(
                attempt_id=attempt, usage={"prompt_tokens": 12.5}
            )
        row = await self._one_row("floaty")
        self.assertIsNone(row.prompt_tokens)
        self.assertEqual(int(row.usage_unknown), 1)
        self.assertEqual(int(row.attempts), 1)

    async def test_other_valid_fields_survive_a_bad_one(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(
                stage="skill", provider="fixture", model="mixed"
            )
            group_costs.record_result(
                attempt_id=attempt,
                usage={"prompt_tokens": -1, "completion_tokens": 7},
            )
        row = await self._one_row("mixed")
        self.assertIsNone(row.prompt_tokens, "坏字段必须留空，不能进账")
        self.assertEqual(int(row.output_tokens), 7, "好字段必须保住")
        self.assertEqual(int(row.attempts), 1)

    async def test_bool_tokens_are_rejected(self) -> None:
        group_costs.reset()
        group_costs.configure(self.session_factory)
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(
                stage="skill", provider="fixture", model="m"
            )
            group_costs.record_result(attempt_id=attempt, usage={"prompt_tokens": True})
        row = (await self._rows())[0]
        self.assertIsNone(row.prompt_tokens)
        self.assertEqual(int(row.usage_unknown), 1)

    # ------------------------------------------------------------------
    # P4-4 成功与 usage 未知是两个维度
    # ------------------------------------------------------------------
    async def test_success_without_usage_is_still_a_success(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model="m"
            )
            group_costs.record_result(attempt_id=attempt, usage=None, success=True)
        row = (await self._rows())[0]
        self.assertEqual(int(row.successes), 1)
        self.assertEqual(int(row.usage_unknown), 1)
        self.assertIsNone(row.prompt_tokens)

    async def test_empty_response_with_usage_is_empty_and_billed(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="safety"):
            attempt = group_costs.record_attempt(
                stage="moderation", provider="fixture", model="m"
            )
            group_costs.record_result(
                attempt_id=attempt,
                usage={"prompt_tokens": 12, "completion_tokens": 0},
                empty=True,
            )
        row = (await self._rows())[0]
        self.assertEqual(int(row.empty_responses), 1)
        self.assertEqual(int(row.prompt_tokens), 12)
        self.assertEqual(int(row.usage_unknown), 0)

    # ------------------------------------------------------------------
    # P4-6 留存清理不得删掉未落盘批次
    # ------------------------------------------------------------------
    async def test_prune_never_drops_unflushed_recent_buckets(self) -> None:
        """昨天的计数还没落盘时，今天的批量统计不得把它清掉。"""

        original_today = group_costs._today
        day = ["2026-10-07"]
        group_costs._today = lambda: day[0]
        try:
            with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
                attempt = group_costs.record_attempt(
                    stage="yesterday", provider="fixture", model="m"
                )
                group_costs.record_result(
                    attempt_id=attempt, usage={"prompt_tokens": 1}
                )
            # 跨到"今天"并塞满超过阈值的不同模型，触发 _prune_locked。
            day[0] = "2026-10-08"
            with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
                for index in range(300):
                    attempt = group_costs.record_attempt(
                        stage="today", provider="fixture", model=f"m{index}"
                    )
                    group_costs.record_result(
                        attempt_id=attempt, usage={"prompt_tokens": 1}
                    )
            totals = self._snapshot_totals()
            self.assertEqual(
                totals.get("attempts"), 301, "昨天的未落盘计数被留存清理删掉了"
            )
        finally:
            group_costs._today = original_today
        rows = await self._rows()
        self.assertEqual(sum(int(row.attempts) for row in rows), 301)

    # ------------------------------------------------------------------
    # P4-7 flush 提交/取消界线
    # ------------------------------------------------------------------
    async def test_cancel_after_commit_does_not_double_count(self) -> None:
        """真实 session、真实 commit 完成、``__aexit__`` 之后才抛 CancelledError。"""

        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model="m"
            )
            group_costs.record_result(
                attempt_id=attempt, usage={"prompt_tokens": 1}
            )

        original_factory = group_costs._session_factory

        class _CancelAfterCommitSession:
            """真实会话、真实 commit，``__aexit__`` 之后才抛 CancelledError。

            这是最危险的位置：数据已经在库里了，任何"并回内存"都会造成双倍。
            """

            def __init__(self, session):
                self._inner = session

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def __aenter__(self):
                return await self._inner.__aenter__()

            async def __aexit__(self, exc_type, exc, tb):
                await self._inner.__aexit__(exc_type, exc, tb)
                raise asyncio.CancelledError()

        class _CancelAfterCommitFactory:
            def __init__(self, inner):
                self._inner = inner

            def __call__(self, *args, **kwargs):
                return _CancelAfterCommitSession(self._inner())

        group_costs._session_factory = _CancelAfterCommitFactory(original_factory)
        try:
            with self.assertRaises(asyncio.CancelledError):
                await group_costs.flush(force=True)
        finally:
            group_costs._session_factory = original_factory

        group_costs.configure(self.session_factory)
        await group_costs.flush(force=True)
        rows = await self._rows()
        self.assertEqual(sum(int(row.attempts) for row in rows), 1)
        self.assertEqual(sum(int(row.prompt_tokens or 0) for row in rows), 1)

    async def test_failure_before_commit_is_still_recoverable(self) -> None:
        """真正没写进去的必须能补记（这一侧不能因为上面的修正而被牺牲）。"""

        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model="m"
            )
            group_costs.record_result(
                attempt_id=attempt, usage={"prompt_tokens": 4}
            )

        class _BrokenSession:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("database is gone")

        original = group_costs._session_factory
        group_costs._session_factory = _BrokenSession()
        try:
            self.assertEqual(await group_costs.flush(force=True), 0)
        finally:
            group_costs._session_factory = original
        group_costs.configure(self.session_factory)
        self.assertEqual(await group_costs.flush(force=True), 1)
        rows = await self._rows()
        self.assertEqual(int(rows[0].prompt_tokens), 4)

    # ------------------------------------------------------------------
    # P4-8 usage_scope 标识也必须严格
    # ------------------------------------------------------------------
    async def test_usage_scope_rejects_float_group_ids(self) -> None:
        for bad in (-100900000002.75, -100900000002.0, 1e3, True):
            with self.subTest(bad=bad):
                with self.assertRaises(scopes.ScopeInvalid):
                    with scopes.usage_scope(kind="group", group_id=bad):
                        pass  # pragma: no cover

    async def test_usage_scope_accepts_a_full_decimal_string(self) -> None:
        with scopes.usage_scope(kind="group", group_id=str(GROUP_A)):
            self.assertEqual(scopes.current_usage_scope().group_id, GROUP_A)

    # ------------------------------------------------------------------
    # 迟到结果归到发送那天
    # ------------------------------------------------------------------
    async def test_late_result_lands_on_the_send_day(self) -> None:
        original_today = group_costs._today
        day = ["2026-10-07"]
        group_costs._today = lambda: day[0]
        try:
            with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
                attempt = group_costs.record_attempt(
                    stage="main", provider="fixture", model="m"
                )
            # 跨过午夜才回来。
            day[0] = "2026-10-08"
            group_costs.record_result(
                attempt_id=attempt, usage={"prompt_tokens": 6}
            )
        finally:
            group_costs._today = original_today
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].usage_date), "2026-10-07")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()