"""修复批 P1-3 / F-032：``quality_report`` 的窗口查询加索引 + 限制取数。

报告原文（``AUDIT-C`` C1-F-032）::

    # bot/services/quality_report.py:194-202（修前）
    confidence_rows = await session.execute(
        select(Violation.confidence).where(
            Violation.group_id == gid, Violation.created_at >= utc_since))
    ...
    for (value,) in confidence_rows.all():
    # bot/db/models.py:538-545  violations 只有两个索引，没有 created_at

即「每群 8 次窗口查询、无 ``created_at`` 索引、整列拉进 Python」。

修法（**只做最小优化，不重写报表**）：

1. ``(group_id, created_at)`` 索引 —— ORM 定义 + SQLite 启动时的幂等升级语句
   （和 F-030 同理：``create_all`` 不会给已存在的表补索引）。
2. 5 个计数（总数 / 无置信度 / 高置信 / 边缘 / 自信）改成 SQL 侧一次聚合。
3. ``repeat_members`` 改成对 ``HAVING count() > 1`` 子查询计数，行数不再随窗口增长。

区间分布（``confidence_bands``）仍留在 Python：``confidence_band`` 的边界是按相邻
band 的中点动态算出来的，``CONFIDENCE_BANDS`` 又可被配置改写，SQL 里复刻不了——
这一处是刻意的取舍。本文件钉住「加了索引与聚合之后**输出逐个等价**」。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta

from sqlalchemy import text

from bot.db.engine import init_db
from bot.db.models import Group, ModerationRule, Violation
from bot.services.quality_report import MARGINAL_CONFIDENCE, collect_quality
from bot.utils.timezone import now_shanghai_naive

#: violations.created_at 是 **UTC 朴素时间**（quality_report 模块 docstring），
#: 而 _utc_since() 算的是「本地时间 - 8 小时」，两者同口径。
def _utc_ago(**kwargs: float) -> "object":
    return now_shanghai_naive() - timedelta(hours=8) - timedelta(**kwargs)


class QualityReportWindowIndexTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_fresh_database_has_the_window_index_and_uses_it(self) -> None:
        async with self.session_factory() as session:
            index_names = {
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT name FROM sqlite_master "
                            "WHERE type='index' AND tbl_name='violations'"
                        )
                    )
                ).all()
            }
            plan = (
                await session.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM violations "
                        "WHERE group_id = -100 AND created_at >= '2026-01-01'"
                    )
                )
            ).all()
        self.assertIn("ix_violations_group_created_at", index_names)
        # 窗口统计不能再扫整张 violations 表。
        self.assertTrue(
            any("ix_violations_group_created_at" in str(row[-1]) for row in plan),
            plan,
        )

    async def test_legacy_database_without_the_index_is_upgraded(self) -> None:
        """``create_all`` 不会给已存在的表补索引，启动路径必须自己补。"""

        await self.engine.dispose()
        connection = sqlite3.connect(self._db_path)
        connection.execute("DROP INDEX IF EXISTS ix_violations_group_created_at")
        connection.commit()
        connection.close()

        self.engine, factory = await init_db(f"sqlite+aiosqlite:///{self._db_path}")
        async with factory() as session:
            names = {
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT name FROM sqlite_master WHERE type='index' "
                            "AND name='ix_violations_group_created_at'"
                        )
                    )
                ).all()
            }
        self.assertEqual(names, {"ix_violations_group_created_at"})


class QualityReportAggregationEquivalenceTests(unittest.IsolatedAsyncioTestCase):
    #: 9 个值 × 3 轮 = 27 条窗口内命中，其中 9 条 confidence 为 NULL。
    CONFIDENCES: tuple = (
        None,
        0.0,
        0.1,
        MARGINAL_CONFIDENCE - 0.01,
        MARGINAL_CONFIDENCE,
        0.5,
        0.9,
        0.99,
        1.0,
    )

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=-100, title="群", settings={}))
            session.add(
                ModerationRule(id=1, group_id=-100, rule_type="keyword", pattern="广告")
            )
            for index, confidence in enumerate(self.CONFIDENCES * 3):
                session.add(
                    Violation(
                        group_id=-100,
                        user_id=1000 + (index % 4),
                        source_message_id=index + 1,
                        confidence=confidence,
                        action_taken="ban",
                        created_at=_utc_ago(hours=index),
                    )
                )
            # 窗口外的老数据：必须被排除。
            for index in range(5):
                session.add(
                    Violation(
                        group_id=-100,
                        user_id=2000 + index,
                        source_message_id=10_000 + index,
                        confidence=0.99,
                        action_taken="ban",
                        created_at=_utc_ago(days=400),
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

    async def _collect(self, **kwargs: object):
        async with self.session_factory() as session:
            return await collect_quality(session, group_id=-100, **kwargs)

    async def test_counts_match_the_plain_python_scan(self) -> None:
        quality = await self._collect(days=7)
        # 9 个值各 3 次：None ×3、< 0.9 ×12、>= 0.9 ×12（MARGINAL = 0.9）。
        self.assertEqual(quality.total, 27)
        self.assertEqual(quality.no_confidence, 3)
        self.assertEqual(quality.marginal, 12)
        self.assertEqual(quality.confident, 12)
        self.assertEqual(quality.confident + quality.marginal, 24)
        self.assertEqual(sum(quality.confidence_bands.values()), 24)
        # 默认阈值 = MARGINAL_CONFIDENCE = 0.9。
        self.assertEqual(quality.high_confidence_hits, 12)
        # 4 个 user 各命中 6~7 次 → 4 个都是重复成员。
        self.assertEqual(quality.members, 4)
        self.assertEqual(quality.repeat_members, 4)

    async def test_high_confidence_band_uses_the_runtime_threshold(self) -> None:
        strict = await self._collect(days=7, high_threshold=0.95)
        loose = await self._collect(days=7, high_threshold=0.0)
        self.assertGreater(loose.high_confidence_hits, strict.high_confidence_hits)
        # threshold=0 时窗口内所有非 NULL 置信度都算高置信。
        self.assertEqual(loose.high_confidence_hits, 24)
        # threshold=0.95 只剩 0.99 与 1.0，各 3 条。
        self.assertEqual(strict.high_confidence_hits, 6)
        # 0.9 也算高置信（>= 阈值）。
        self.assertEqual((await self._collect(days=7, high_threshold=0.9)).high_confidence_hits, 12)

    async def test_narrow_window_excludes_older_rows(self) -> None:
        quality = await self._collect(days=1)
        # 只剩 created_at 在最近 24 小时内的命中。
        self.assertLess(quality.total, 27)
        self.assertGreater(quality.total, 0)

    async def test_group_without_rows_is_still_all_zero(self) -> None:
        async with self.session_factory() as session:
            quality = await collect_quality(session, group_id=-999)
        self.assertEqual(quality.total, 0)
        self.assertEqual(quality.members, 0)
        self.assertEqual(quality.repeat_members, 0)
        self.assertEqual(quality.high_confidence_hits, 0)
        self.assertEqual(sum(quality.confidence_bands.values()), 0)

    async def test_the_counter_queries_are_index_ranged_not_table_scans(self) -> None:
        """聚合后的窗口查询走 (group_id, created_at) 索引，而不是全表扫。"""

        statements = (
            "SELECT COUNT(*) FROM violations "
            "WHERE group_id = -100 AND created_at >= '2026-01-01'",
            "SELECT COUNT(*) FROM violations WHERE group_id = -100 "
            "AND created_at >= '2026-01-01' AND (confidence IS NULL)",
            "SELECT COUNT(*) FROM (SELECT user_id FROM violations "
            "WHERE group_id = -100 AND created_at >= '2026-01-01' "
            "GROUP BY user_id HAVING COUNT(*) > 1)",
        )
        async with self.session_factory() as session:
            plans = []
            for statement in statements:
                rows = (
                    await session.execute(text(f"EXPLAIN QUERY PLAN {statement}"))
                ).all()
                plans.append([str(row[-1]) for row in rows])
        for statement, plan in zip(statements, plans):
            self.assertTrue(
                any("ix_violations_group_created_at" in line for line in plan),
                f"{statement}\n{plan}",
            )

    def test_the_python_side_row_pulls_are_limited_to_the_band_breakdown(self) -> None:
        """计数不再逐行进 Python；只有区间分布仍需逐行（见模块 docstring）。"""

        import inspect

        from bot.services import quality_report

        source = inspect.getsource(quality_report.collect_quality)
        window = source.split("utc_since = _utc_since(window)", 1)[1]
        # 逐行循环只应剩下区间分布那一处。
        self.assertEqual(window.count("for (value,) in"), 1)
        self.assertIn("func.sum(", window)
        self.assertIn(".having(func.count() > 1)", window)
