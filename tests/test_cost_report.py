"""成本与健康看板：累加器、落盘、报表口径。

重点不是"函数能跑"，而是**数字对不对**：累加有没有漏、落盘会不会重复计、
窗口边界对不对、缓存率和异常计数算得对不对。这些数字是要拿来做决策的
（要不要动 decision 那条链路），算错比没有更糟。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from bot.db.engine import init_db
from bot.db.models import LlmUsageDaily
from bot.services import llm_metrics
from bot.services.cost_report import (
    collect_cost,
    cost_digest_text,
    render_cost_report,
)
from bot.services.llm import LLMService
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -100
_REPO_ROOT = Path(__file__).resolve().parents[1]


class UsageAccumulatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        llm_metrics.reset()
        llm_metrics.configure(self.session_factory)

    async def asyncTearDown(self) -> None:
        llm_metrics.reset()
        await self.engine.dispose()
        for path in (self._db_path, f"{self._db_path}-wal", f"{self._db_path}-shm"):
            if os.path.exists(path):
                os.unlink(path)

    async def _rows(self) -> list[LlmUsageDaily]:
        async with self.session_factory() as session:
            return list((await session.execute(select(LlmUsageDaily))).scalars())

    async def test_record_accumulates_and_ignores_unknown_fields(self) -> None:
        llm_metrics.record("decision", calls=1, prompt_tokens=100, 未知字段=5)
        llm_metrics.record("decision", calls=2, prompt_tokens=50, timeouts=1)
        # 非整数值不能把统计打挂
        llm_metrics.record("decision", calls="坏值", prompt_tokens=7)  # type: ignore[arg-type]

        snap = llm_metrics.snapshot()
        key = (now_shanghai_naive().date().isoformat(), "decision")
        self.assertEqual(snap[key]["calls"], 3)
        self.assertEqual(snap[key]["prompt_tokens"], 157)
        self.assertEqual(snap[key]["timeouts"], 1)
        self.assertNotIn("未知字段", snap[key])

    async def test_record_usage_reads_gateway_field_names(self) -> None:
        usage = SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=20,
            cached_tokens=600,
            cache_creation_input_tokens=128,
            completion_thinking_tokens=0,
        )
        llm_metrics.record_usage("moderation", usage)
        (_, vals), = llm_metrics.snapshot().items()
        self.assertEqual(vals["calls"], 1)
        self.assertEqual(vals["prompt_tokens"], 1000)
        self.assertEqual(vals["output_tokens"], 20)
        self.assertEqual(vals["cached_tokens"], 600)
        self.assertEqual(vals["cache_write_tokens"], 128)
        # 计数为 0 的字段不落内存（累加器只存非零值），读的时候按 0 处理
        self.assertEqual(vals.get("thinking_tokens", 0), 0)
        self.assertEqual(vals.get("timeouts", 0), 0)

    async def test_flush_upserts_without_double_counting(self) -> None:
        llm_metrics.record("decision", calls=2, prompt_tokens=300)
        self.assertEqual(await llm_metrics.flush(force=True), 1)
        llm_metrics.record("decision", calls=1, prompt_tokens=100)
        self.assertEqual(await llm_metrics.flush(force=True), 1)
        # 没有新计数时不应再写
        self.assertEqual(await llm_metrics.flush(force=True), 0)

        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].calls, 3)
        self.assertEqual(rows[0].prompt_tokens, 400)

    async def test_flush_keeps_counters_when_write_fails(self) -> None:
        llm_metrics.record("skill", calls=1, prompt_tokens=999)

        class _Boom:
            def __call__(self):  # pragma: no cover - 仅用于触发失败
                raise RuntimeError("db down")

        llm_metrics.configure(_Boom())
        self.assertEqual(await llm_metrics.flush(force=True), 0)
        llm_metrics.configure(self.session_factory)
        snap = llm_metrics.snapshot()
        (_, vals), = snap.items()
        self.assertEqual(vals["prompt_tokens"], 999)  # 失败后计数没丢

    async def test_two_stages_stay_separate(self) -> None:
        llm_metrics.record("decision", calls=1, prompt_tokens=10)
        llm_metrics.record("moderation", calls=1, prompt_tokens=20)
        await llm_metrics.flush(force=True)
        rows = await self._rows()
        self.assertEqual({r.stage for r in rows}, {"decision", "moderation"})
        self.assertEqual({r.usage_date for r in rows}, {now_shanghai_naive().date().isoformat()})

    async def test_scheduled_flush_task_is_strongly_referenced(self) -> None:
        """事件循环只持弱引用：没有模块级强引用，落盘任务可能被 GC 掉。"""

        self.assertIsNone(llm_metrics._flush_task)
        llm_metrics.record("decision", calls=1, prompt_tokens=42)

        task = llm_metrics._flush_task
        self.assertIsNotNone(task, "record 排出的落盘任务没有被任何地方引用")
        self.assertFalse(task.done())
        await task
        await asyncio.sleep(0)  # 让 done_callback 跑完

        self.assertIsNone(llm_metrics._flush_task, "完成后必须放掉引用，否则再也排不出下一轮")
        rows = await self._rows()
        self.assertEqual([(r.stage, r.calls, r.prompt_tokens) for r in rows], [("decision", 1, 42)])

    async def test_shutdown_flushes_the_tail_batch(self) -> None:
        """关停链必须 flush(force=True)，否则重启丢掉最后不足 60 秒的计数。"""

        llm_metrics.record("decision", calls=2, prompt_tokens=7)
        self.assertEqual(await llm_metrics.flush(force=True), 1)
        self.assertEqual(await llm_metrics.flush(force=True), 0)
        self.assertEqual([(r.calls, r.prompt_tokens) for r in await self._rows()], [(2, 7)])

    def test_ordered_shutdown_flushes_before_disposing_the_engine(self) -> None:
        source = (_REPO_ROOT / "bot" / "__main__.py").read_text(encoding="utf-8")

        self.assertTrue(
            "llm_metrics.flush(force=True)" in source,
            "关停链里没有 llm_metrics.flush(force=True)：重启会丢掉最后不足 60 秒的用量",
        )
        self.assertLess(
            source.index("llm_metrics.flush(force=True)"),
            source.rindex("engine.dispose()"),
            "用量 flush 必须排在关停链末尾的 engine.dispose() 之前",
        )

    def test_window_start_includes_today(self) -> None:
        today = now_shanghai_naive().date()
        self.assertEqual(llm_metrics.window_start(1, today=today), today.isoformat())
        self.assertEqual(
            llm_metrics.window_start(7, today=today),
            (today - timedelta(days=6)).isoformat(),
        )


class CoerceUsageTests(unittest.TestCase):
    """网关字段名有 OpenAI / Anthropic / DeepSeek 三套写法，都要认。"""

    def test_openai_nested_details(self) -> None:
        usage = LLMService._coerce_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 64},
                "completion_tokens_details": {"reasoning_tokens": 12},
            }
        )
        self.assertIsNotNone(usage)
        self.assertEqual(usage.cached_tokens, 64)
        self.assertEqual(usage.thinking_tokens, 12)

    def test_anthropic_style_names(self) -> None:
        usage = LLMService._coerce_usage(
            {
                "input_tokens": 200,
                "output_tokens": 9,
                "cache_read_input_tokens": 128,
                "cache_creation_input_tokens": 32,
            }
        )
        self.assertIsNotNone(usage)
        self.assertEqual(usage.prompt_tokens, 200)
        self.assertEqual(usage.cached_tokens, 128)
        self.assertEqual(usage.cache_write_tokens, 32)

    def test_gateway_thinking_field(self) -> None:
        usage = LLMService._coerce_usage(
            {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "completion_thinking_tokens": 7,
            }
        )
        self.assertEqual(usage.thinking_tokens, 7)

    def test_all_zero_returns_none(self) -> None:
        self.assertIsNone(LLMService._coerce_usage({"prompt_tokens": 0, "completion_tokens": 0}))


class CostReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for path in (self._db_path, f"{self._db_path}-wal", f"{self._db_path}-shm"):
            if os.path.exists(path):
                os.unlink(path)

    async def _seed(self, rows: list[dict[str, object]]) -> None:
        async with self.session_factory() as session:
            for row in rows:
                session.add(LlmUsageDaily(**row))  # type: ignore[arg-type]
            await session.commit()

    def _day(self, offset: int) -> str:
        return (now_shanghai_naive().date() - timedelta(days=offset)).isoformat()

    async def test_empty_window_says_so(self) -> None:
        async with self.session_factory() as session:
            report = await collect_cost(session, days=7)
        self.assertFalse(report.has_data)
        text = render_cost_report(report)
        self.assertIn("还没有用量记录", text)
        self.assertIn("成本与健康 · 近 7 天", text)

    async def test_totals_cache_rate_and_anomalies(self) -> None:
        await self._seed(
            [
                dict(usage_date=self._day(0), stage="decision", calls=10, prompt_tokens=1000,
                     cached_tokens=400, output_tokens=5, timeouts=2),
                dict(usage_date=self._day(1), stage="decision", calls=5, prompt_tokens=500,
                     cached_tokens=100, output_tokens=3, timeouts=1),
                dict(usage_date=self._day(1), stage="moderation", calls=4, prompt_tokens=200,
                     empty_responses=1, parse_errors=1),
                dict(usage_date=self._day(30), stage="skill", calls=99, prompt_tokens=999999),
            ]
        )
        async with self.session_factory() as session:
            report = await collect_cost(session, days=7)
        total = report.totals
        self.assertEqual(total.calls, 19)               # 窗口外的 99 次不能算进来
        self.assertEqual(total.prompt_tokens, 1700)
        self.assertEqual(total.cached_tokens, 500)
        self.assertEqual(total.timeouts, 3)
        self.assertEqual(total.empty_responses, 1)
        self.assertEqual(total.parse_errors, 1)
        self.assertAlmostEqual(report.cache_rate, 500 / 1700, places=4)
        # 阶段按 prompt token 降序
        self.assertEqual([s.stage for s in report.stages], ["decision", "moderation"])

        text = render_cost_report(report)
        self.assertIn("调用 <b>19</b>", text)
        self.assertIn("缓存命中 <b>29%</b>", text)
        self.assertIn("超时 <b>3</b>", text)
        self.assertIn("空响应 <b>1</b>", text)
        # 阶段占比：decision 1500/1700 ≈ 88%
        self.assertIn("decision 1.5K（88%）", text)

    async def test_zero_cache_hint_appears(self) -> None:
        await self._seed(
            [
                dict(usage_date=self._day(0), stage="decision", calls=1, prompt_tokens=200_000,
                     cached_tokens=0),
            ]
        )
        async with self.session_factory() as session:
            report = await collect_cost(session, days=7)
        self.assertIn("前缀没有被复用", render_cost_report(report))

    async def test_thinking_tokens_warn_when_nonzero(self) -> None:
        await self._seed(
            [
                dict(usage_date=self._day(0), stage="main", calls=1, prompt_tokens=100,
                     thinking_tokens=42),
            ]
        )
        async with self.session_factory() as session:
            report = await collect_cost(session, days=7)
        text = render_cost_report(report)
        self.assertIn("思考 token <b>42</b>", text)
        self.assertIn("关闭思考的配置没生效", text)

    async def test_thinking_tokens_silent_when_zero(self) -> None:
        await self._seed(
            [dict(usage_date=self._day(0), stage="main", calls=1, prompt_tokens=100)]
        )
        async with self.session_factory() as session:
            report = await collect_cost(session, days=7)
        text = render_cost_report(report)
        self.assertIn("思考 token 0", text)
        self.assertNotIn("关闭思考的配置没生效", text)

    async def test_bad_counter_value_drops_only_that_field(self) -> None:
        """坏值不能把整次计数扔掉：calls 被丢，prompt_tokens 必须还在。"""
        llm_metrics.reset()
        llm_metrics.configure(self.session_factory)
        llm_metrics.record("decision", calls="坏值", prompt_tokens=7)  # type: ignore[arg-type]
        (_, vals), = llm_metrics.snapshot().items()
        self.assertNotIn("calls", vals)
        self.assertEqual(vals["prompt_tokens"], 7)
        llm_metrics.reset()

    async def test_stage_names_are_html_escaped(self) -> None:
        await self._seed(
            [dict(usage_date=self._day(0), stage="<b>坏</b>", calls=1, prompt_tokens=10)]
        )
        async with self.session_factory() as session:
            report = await collect_cost(session, days=7)
        # 报表本身是 HTML：正文里透传的标签会破坏 Telegram 解析
        self.assertIn("&lt;b&gt;坏&lt;/b&gt;", render_cost_report(report))

    async def test_digest_is_plain_text(self) -> None:
        await self._seed(
            [
                dict(usage_date=self._day(0), stage="decision", calls=3, prompt_tokens=3000,
                     cached_tokens=300, timeouts=1, empty_responses=2),
            ]
        )
        async with self.session_factory() as session:
            report = await collect_cost(session, days=7)
        digest = cost_digest_text(report)
        self.assertIn("【成本与健康 · 近 7 天】", digest)
        self.assertIn("调用 3 次", digest)
        self.assertIn("缓存命中 10%", digest)
        self.assertIn("超时 1", digest)
        self.assertNotIn("<b>", digest)
