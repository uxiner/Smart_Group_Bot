"""用量归属与按群成本台账（Task 4）。

被钉死的不变量：

* **归属必须来自上下文而不是猜**：A 群与 B 群并发调用同一 stage，台账里必须
  分成两行，且互不串号；system / private 同 stage 也各自独立。
* **``group_id=0`` 不能冒充群**：非群归属在库里就是 0，而 ``kind='group'``
  的行永远不允许 0。
* **token 上下文在 finally 复位**：异常与取消之后不许泄漏给下一个任务。
* **一次真实发出 = 一个 attempt**：重试/回退各算各的；发送前取消**不**记。
* **usage 缺失 = unknown，不是 0**：没有 usage 的失败/超时必须落
  ``usage_unknown=1`` 而不是伪造零费用。
* **落盘失败并回内存**：遥测丢一次不等于额度可以复原。
* **迟到结果去重**：同一 attempt 的结果记两次只算一次。
* **旧口径不变**：``llm_usage_daily`` 仍是「成功调用数」，新表是明细，
  两者不重叠、不互相翻倍。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from sqlalchemy import func, select

from bot.db.engine import init_db
from bot.db.models import LlmCostDaily, LlmUsageDaily
from bot.services import group_costs
from bot.services import llm_metrics
from bot.services import usage_scope as scopes

GROUP_A = -100900000001
GROUP_B = -100900000002
USER_ID = 7


class UsageScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        # 先 reset（它会连会话工厂一起清掉）再 configure：反过来的话这一轮
        # 所有 flush 都会因为"没注入会话工厂"静默返回 0。
        group_costs.reset()
        group_costs.configure(self.session_factory)
        llm_metrics.reset()
        llm_metrics.configure(self.session_factory)

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
        async with self.session_factory() as session:
            return list((await session.scalars(select(LlmCostDaily))).all())

    async def _row_for(self, **filters) -> LlmCostDaily | None:
        async with self.session_factory() as session:
            stmt = select(LlmCostDaily)
            for key, value in filters.items():
                stmt = stmt.where(getattr(LlmCostDaily, key) == value)
            return (await session.scalars(stmt)).first()

    # ------------------------------------------------------------------
    # 上下文本身
    # ------------------------------------------------------------------
    async def test_default_scope_is_system_not_some_group(self) -> None:
        self.assertEqual(scopes.current_usage_scope(), scopes.SYSTEM_SCOPE)
        self.assertIsNone(scopes.current_usage_scope().group_id)

    async def test_scope_requires_a_real_group_id(self) -> None:
        with self.assertRaises(scopes.ScopeInvalid):
            with scopes.usage_scope(kind="group", group_id=0):
                pass
        with self.assertRaises(scopes.ScopeInvalid):
            with scopes.usage_scope(kind="group"):
                pass
        with self.assertRaises(scopes.ScopeInvalid):
            with scopes.usage_scope(kind="group", group_id=12345):
                pass

    async def test_unknown_scope_kind_is_rejected(self) -> None:
        with self.assertRaises(scopes.ScopeInvalid):
            with scopes.usage_scope(kind="whatever", group_id=GROUP_A):
                pass

    async def test_purpose_must_be_a_known_role(self) -> None:
        with self.assertRaises(scopes.ScopeInvalid):
            with scopes.usage_scope(
                kind="group", group_id=GROUP_A, purpose="background"
            ):
                pass

    async def test_scope_resets_after_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with scopes.usage_scope(
                kind="group", group_id=GROUP_A, purpose="optional"
            ):
                assert scopes.current_usage_scope().group_id == GROUP_A
                raise RuntimeError("boom")
        self.assertEqual(scopes.current_usage_scope(), scopes.SYSTEM_SCOPE)

    async def test_scope_resets_after_cancellation(self) -> None:
        async def worker() -> None:
            with scopes.usage_scope(
                kind="group", group_id=GROUP_A, purpose="optional"
            ):
                await asyncio.sleep(10)

        task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(scopes.current_usage_scope(), scopes.SYSTEM_SCOPE)

    async def test_nested_scopes_restore_the_outer_one(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A):
            with scopes.usage_scope(kind="group", group_id=GROUP_B):
                self.assertEqual(scopes.current_usage_scope().group_id, GROUP_B)
            self.assertEqual(scopes.current_usage_scope().group_id, GROUP_A)
        self.assertEqual(scopes.current_usage_scope(), scopes.SYSTEM_SCOPE)

    async def test_background_private_user_defaults_to_anonymous_aggregate(self) -> None:
        with scopes.usage_scope(kind="private_user", user_id=USER_ID):
            current = scopes.current_usage_scope()
        self.assertEqual(current.kind, "private_user")
        self.assertEqual(current.bucket, "anonymous")
        self.assertNotIn(str(USER_ID), str(current))

    async def test_safety_and_optional_are_distinct_even_for_one_stage(self) -> None:
        with scopes.usage_scope(
            kind="group", group_id=GROUP_A, purpose="safety", stage="vision"
        ):
            safety = scopes.current_usage_scope()
        with scopes.usage_scope(
            kind="group", group_id=GROUP_A, purpose="optional", stage="vision"
        ):
            optional = scopes.current_usage_scope()
        self.assertNotEqual(safety.purpose, optional.purpose)
        self.assertEqual(safety.stage, optional.stage)

    async def test_moderation_stage_defaults_to_safety_not_optional(self) -> None:
        # 归属在 update 入口建立时还不知道这次调什么，所以用途在那里**未定**；
        # 真正的判定点是每次 attempt 拿到 stage 的那一刻。
        with scopes.stage_scope("moderation", group_id=GROUP_A):
            current = scopes.current_usage_scope()
            self.assertEqual(current.purpose, "")
            self.assertEqual(
                scopes.effective_purpose(current, current.stage), scopes.PURPOSE_SAFETY
            )
        with scopes.stage_scope("synopsis", group_id=GROUP_A):
            self.assertEqual(
                scopes.effective_purpose(
                    scopes.current_usage_scope(), "synopsis"
                ),
                scopes.PURPOSE_OPTIONAL,
            )
        with scopes.stage_scope("group_summary", group_id=GROUP_A):
            self.assertEqual(
                scopes.effective_purpose(
                    scopes.current_usage_scope(), "group_summary"
                ),
                scopes.PURPOSE_BACKGROUND_OPTIONAL,
            )

    async def test_an_explicit_purpose_always_wins_over_the_stage(self) -> None:
        with scopes.stage_scope("moderation", group_id=GROUP_A):
            current = scopes.with_purpose(scopes.current_usage_scope(), "optional")
            self.assertEqual(
                scopes.effective_purpose(current, current.stage), scopes.PURPOSE_OPTIONAL
            )

    # ------------------------------------------------------------------
    # 台账
    # ------------------------------------------------------------------
    async def test_concurrent_group_scopes_never_cross_over(self) -> None:
        async def emit(group_id: int, tag: str) -> None:
            for index in range(20):
                with scopes.usage_scope(
                    kind="group", group_id=group_id, purpose="optional"
                ):
                    attempt = group_costs.record_attempt(
                        stage=f"main-{tag}",
                        provider="fixture",
                        model=f"model-{tag}",
                    )
                    group_costs.record_result(
                        attempt_id=attempt,
                        usage={
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "cached_tokens": 40,
                        },
                    )

        await asyncio.gather(emit(GROUP_A, "a"), emit(GROUP_B, "b"))
        await group_costs.flush(force=True)

        rows = await self._rows()
        self.assertEqual(len(rows), 2)
        by_group = {int(row.group_id): row for row in rows}
        self.assertEqual(set(by_group), {GROUP_A, GROUP_B})
        for row in rows:
            self.assertEqual(int(row.attempts), 20)
            self.assertEqual(int(row.successes), 20)
            self.assertEqual(int(row.prompt_tokens), 2000)
            self.assertEqual(int(row.output_tokens), 400)
            self.assertEqual(int(row.cached_tokens), 800)
            self.assertEqual(str(row.model), f"model-{str(row.stage)[-1]}")
            self.assertEqual(int(row.usage_unknown), 0)

    async def test_system_and_private_do_not_collapse_into_one_row(self) -> None:
        for kind in ("system", "private"):
            with scopes.usage_scope(kind=kind, purpose="optional"):
                attempt = group_costs.record_attempt(
                    stage="main", provider="fixture", model="m"
                )
                group_costs.record_result(attempt_id=attempt, usage={})
        await group_costs.flush(force=True)
        rows = await self._rows()
        self.assertEqual({str(row.scope) for row in rows}, {"system", "private"})
        for row in rows:
            self.assertEqual(int(row.group_id), 0)

    async def test_missing_usage_is_unknown_never_fake_zero(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(
                stage="skill", provider="fixture", model="m"
            )
            group_costs.record_result(attempt_id=attempt, usage=None, failed=True)
        await group_costs.flush(force=True)
        row = await self._row_for(stage="skill")
        self.assertEqual(int(row.attempts), 1)
        self.assertEqual(int(row.failures), 1)
        self.assertEqual(int(row.usage_unknown), 1)
        self.assertIsNone(row.prompt_tokens)
        self.assertIsNone(row.output_tokens)

    async def test_usage_present_on_a_parse_error_still_counts_tokens(self) -> None:
        """解析失败但**带 usage** 的响应是真花了 token，必须记，不能当 unknown。"""

        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="safety"):
            attempt = group_costs.record_attempt(
                stage="moderation", provider="fixture", model="m"
            )
            group_costs.record_result(
                attempt_id=attempt,
                usage={"prompt_tokens": 500, "completion_tokens": 30},
                parse_error=True,
            )
        await group_costs.flush(force=True)
        row = await self._row_for(stage="moderation")
        self.assertEqual(int(row.prompt_tokens), 500)
        self.assertEqual(int(row.usage_unknown), 0)
        self.assertEqual(int(row.parse_errors), 1)

    async def test_cancelled_before_send_is_not_an_attempt(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            group_costs.cancel_before_send(
                stage="skill", provider="fixture", model="m"
            )
        await group_costs.flush(force=True)
        self.assertEqual(await self._rows(), [])

    async def test_late_result_for_one_attempt_is_counted_once(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model="m"
            )
            usage = {"prompt_tokens": 10, "completion_tokens": 2}
            group_costs.record_result(attempt_id=attempt, usage=usage)
            group_costs.record_result(attempt_id=attempt, usage=usage)
            group_costs.record_result(attempt_id=attempt, usage=usage)
        await group_costs.flush(force=True)
        row = await self._row_for(stage="main")
        self.assertEqual(int(row.attempts), 1)
        self.assertEqual(int(row.successes), 1)
        self.assertEqual(int(row.prompt_tokens), 10)

    async def test_timeout_and_failure_are_separate_buckets(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            for _ in range(3):
                group_costs.record_result(
                    attempt_id=group_costs.record_attempt(
                        stage="main", provider="fixture", model="m"
                    ),
                    usage=None,
                    failed=True,
                )
            for _ in range(2):
                group_costs.record_result(
                    attempt_id=group_costs.record_attempt(
                        stage="main", provider="fixture", model="m"
                    ),
                    usage=None,
                    timed_out=True,
                )
        await group_costs.flush(force=True)
        row = await self._row_for(stage="main")
        self.assertEqual(int(row.attempts), 5)
        self.assertEqual(int(row.failures), 3)
        self.assertEqual(int(row.timeouts), 2)
        self.assertEqual(int(row.usage_unknown), 5)

    async def test_flush_failure_returns_the_batch_and_retries(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            group_costs.record_result(
                attempt_id=group_costs.record_attempt(
                    stage="main", provider="fixture", model="m"
                ),
                usage={"prompt_tokens": 7},
            )

        original = self.session_factory

        def broken_factory(*args, **kwargs):
            raise RuntimeError("database is gone")

        group_costs.configure(broken_factory)  # type: ignore[arg-type]
        try:
            self.assertEqual(await group_costs.flush(force=True), 0)
        finally:
            group_costs.configure(original)

        # 遥测丢了这一轮不等于账没了：下一轮必须把它补上。
        self.assertEqual(await group_costs.flush(force=True), 1)
        row = await self._row_for(stage="main")
        self.assertIsNotNone(row)
        self.assertEqual(int(row.prompt_tokens), 7)

    async def test_concurrent_flush_does_not_duplicate(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            for _ in range(5):
                group_costs.record_result(
                    attempt_id=group_costs.record_attempt(
                        stage="main", provider="fixture", model="m"
                    ),
                    usage={"prompt_tokens": 3},
                )
        await asyncio.gather(
            *(group_costs.flush(force=True) for _ in range(6))
        )
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(LlmCostDaily).where(LlmCostDaily.stage == "main")
                )
            ).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0].attempts), 5)
        self.assertEqual(int(rows[0].prompt_tokens), 15)

    async def test_cancelling_a_flush_keeps_the_counters(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            group_costs.record_result(
                attempt_id=group_costs.record_attempt(
                    stage="main", provider="fixture", model="m"
                ),
                usage={"prompt_tokens": 4},
            )
        task = asyncio.create_task(group_costs.flush(force=True))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await group_costs.flush(force=True)
        row = await self._row_for(stage="main")
        self.assertIsNotNone(row)
        self.assertEqual(int(row.prompt_tokens), 4)

    async def test_group_scope_rows_never_carry_group_id_zero(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            group_costs.record_result(
                attempt_id=group_costs.record_attempt(
                    stage="main", provider="fixture", model="m"
                ),
                usage={"prompt_tokens": 1},
            )
        await group_costs.flush(force=True)
        async with self.session_factory() as session:
            bogus = int(
                await session.scalar(
                    select(func.count())
                    .select_from(LlmCostDaily)
                    .where(LlmCostDaily.scope == "group")
                    .where(LlmCostDaily.group_id == 0)
                )
                or 0
            )
        self.assertEqual(bogus, 0)

    async def test_legacy_table_keeps_its_success_only_semantics(self) -> None:
        """新表不改动旧表口径：``llm_usage_daily.calls`` 仍只记成功响应。"""

        llm_metrics.record_usage(
            "main", {"prompt_tokens": 10, "completion_tokens": 2}
        )
        llm_metrics.record("main", failures=1)
        llm_metrics.record("main", timeouts=1)
        await llm_metrics.flush(force=True)
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            for _ in range(3):
                group_costs.record_result(
                    attempt_id=group_costs.record_attempt(
                        stage="main", provider="fixture", model="m"
                    ),
                    usage={"prompt_tokens": 10, "completion_tokens": 2},
                )
        await group_costs.flush(force=True)

        async with self.session_factory() as session:
            legacy = (
                await session.scalars(
                    select(LlmUsageDaily).where(LlmUsageDaily.stage == "main")
                )
            ).first()
            detail = (
                await session.scalars(
                    select(LlmCostDaily).where(LlmCostDaily.stage == "main")
                )
            ).first()
        self.assertEqual(int(legacy.calls), 1)
        self.assertEqual(int(legacy.failures), 1)
        self.assertEqual(int(legacy.timeouts), 1)
        self.assertEqual(int(detail.attempts), 3)
        self.assertEqual(int(detail.successes), 3)
        # 两张表口径不同：旧表是「成功调用数」，新表是「明细」。
        self.assertNotEqual(int(legacy.calls), int(detail.attempts))

    async def test_empty_response_with_usage_is_both_empty_and_billed(self) -> None:
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="safety"):
            group_costs.record_result(
                attempt_id=group_costs.record_attempt(
                    stage="moderation", provider="fixture", model="m"
                ),
                usage={"prompt_tokens": 12, "completion_tokens": 0},
                empty=True,
            )
        await group_costs.flush(force=True)
        row = await self._row_for(stage="moderation")
        self.assertEqual(int(row.empty_responses), 1)
        self.assertEqual(int(row.prompt_tokens), 12)
        self.assertEqual(int(row.usage_unknown), 0)

    async def test_snapshot_does_not_carry_another_group(self) -> None:
        for group_id in (GROUP_A, GROUP_B):
            with scopes.usage_scope(
                kind="group", group_id=group_id, purpose="optional"
            ):
                group_costs.record_result(
                    attempt_id=group_costs.record_attempt(
                        stage="main", provider="fixture", model="m"
                    ),
                    usage={"prompt_tokens": 5},
                )
        keys = list(group_costs.snapshot())
        self.assertEqual(len(keys), 2)
        self.assertEqual({key[2] for key in keys}, {GROUP_A, GROUP_B})
        # 落盘后内存必须清空：留在内存里的数字会被下一轮连同别的群的数字一起
        # 再并一次，看起来像「同一个桶里多算了别人的」。
        await group_costs.flush(force=True)
        self.assertEqual(group_costs.snapshot(), {})
        with scopes.usage_scope(kind="group", group_id=GROUP_A, purpose="optional"):
            group_costs.record_result(
                attempt_id=group_costs.record_attempt(
                    stage="main", provider="fixture", model="m"
                ),
                usage={"prompt_tokens": 5},
            )
        post = group_costs.snapshot()
        self.assertEqual(len(post), 1)
        self.assertEqual(next(iter(post))[2], GROUP_A)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()