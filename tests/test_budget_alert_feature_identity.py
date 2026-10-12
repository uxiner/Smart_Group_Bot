"""告警事件的 feature 身份与**真实旧库迁移**（正式回归）。

被测的是 schema/身份这一层：唯一键里有没有 feature、老库能不能被真实
``init_db`` 升上去且不丢数据、同群同日同类型同档的两个 feature 能不能并存、
长 feature 会不会被截断导致身份冲突。

旧表是**手工建出来的**（旧 schema + 旧四列唯一索引），不是靠 ORM metadata —
只有这样才能证明迁移真的做了事。四种状态（pending / sending / sent / unknown）
都 seed 一条，迁移后逐字段读回。

不 mock 数据库：所有读写走真实 SQL 与项目自己的插入路径。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest

from sqlalchemy import select, text

from bot.db.engine import init_db
from bot.db.models import BudgetAlertEvent
from bot.services import budget_alerts, group_budget
from bot.utils.timezone import now_shanghai_naive

GROUP = -1001234567890

_OLD_SCHEMA = """
CREATE TABLE budget_alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id BIGINT NOT NULL,
    budget_date VARCHAR(10) NOT NULL,
    limit_kind VARCHAR(16) NOT NULL,
    threshold_kind VARCHAR(8) NOT NULL,
    threshold_value BIGINT,
    used_value BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(8) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner VARCHAR(32) NOT NULL DEFAULT '',
    lease_expires_at DATETIME,
    next_attempt_at DATETIME,
    summary VARCHAR(300) NOT NULL DEFAULT '',
    last_error VARCHAR(200) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    sent_at DATETIME
);
CREATE UNIQUE INDEX ux_budget_alert_daily_threshold
    ON budget_alert_events (group_id, budget_date, limit_kind, threshold_kind);
"""

# 旧唯一键是 (群,日,类型,档)，所以四条种子必须**额度类型互不相同**才插得进去 ——
# 这正是旧库的真实约束，不能绕过它去造数据。
_SEED = [
    # (limit_kind, status, attempts, lease_owner, has_sent_at)
    ("token", "pending", 0, "", False),
    ("attempt", "sending", 2, "worker-a", False),
    ("money", "sent", 1, "", True),
    ("operation", "unknown", 3, "", False),
]


class FeatureIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="sb-alert-feature-")
        self.addCleanup(self._td.cleanup)
        self.db_path = os.path.join(self._td.name, "old.db")
        self._build_old_schema()
        self.engine = None

    async def asyncTearDown(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _build_old_schema(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(_OLD_SCHEMA)
            now = now_shanghai_naive().strftime("%Y-%m-%d %H:%M:%S.%f")
            today = now_shanghai_naive().date().isoformat()
            for index, (kind, status, attempts, owner, has_sent) in enumerate(_SEED):
                conn.execute(
                    "INSERT INTO budget_alert_events ("
                    " group_id, budget_date, limit_kind, threshold_kind,"
                    " threshold_value, used_value, status, attempts, lease_owner,"
                    " lease_expires_at, next_attempt_at, summary, last_error,"
                    " created_at, updated_at, sent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        GROUP, today, kind, "warn" if index % 2 else "pause",
                        100 + index, index * 10, status, attempts, owner,
                        now if status == "sending" else None, now,
                        f"seed-{index}", "" if status != "failed" else "boom",
                        now, now, now if has_sent else None,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _raw(self, sql: str):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    async def _migrate(self):
        self.engine, factory = await init_db(f"sqlite+aiosqlite:///{self.db_path}")
        self.factory = factory
        return factory

    async def _rows(self) -> list:
        async with self.factory() as session:
            return list(
                (await session.scalars(
                    select(BudgetAlertEvent).order_by(BudgetAlertEvent.id)
                )).all()
            )

    # ------------------------------------------------------------------
    async def test_migration_upgrades_real_old_schema_and_keeps_every_row(self) -> None:
        before = [
            (r[0], r[1], r[2], r[3],
             None if r[4] is None else str(r[4]).split(".")[0],
             None if r[5] is None else str(r[5]).split(".")[0], r[6])
            for r in self._raw(
                "SELECT id, status, attempts, lease_owner, lease_expires_at,"
                " sent_at, summary FROM budget_alert_events ORDER BY id"
            )
        ]
        self.assertEqual(len(before), 4)
        self.assertEqual(
            [r[6] for r in before], [f"seed-{i}" for i in range(4)]
        )

        await self._migrate()

        rows = await self._rows()
        self.assertEqual(len(rows), 4, "迁移把旧事件弄丢了")
        def _as_text(value):
            # ORM 读回是秒级，raw sqlite 里还带着微秒 —— 比到秒即可。
            if value is None:
                return None
            text_value = str(value)
            return text_value.split(".")[0]

        after = [
            (r.id, str(r.status), int(r.attempts), str(r.lease_owner),
             _as_text(r.lease_expires_at), _as_text(r.sent_at), str(r.summary))
            for r in rows
        ]
        self.assertEqual(after, [tuple(r) for r in before], "旧行值被改动了")
        for row in rows:
            self.assertEqual(
                str(row.feature), "",
                "旧行迁移后 feature 必须为空串（其余额度类型没有 feature）",
            )
        self.assertEqual(
            sorted(str(r.status) for r in rows),
            ["pending", "sending", "sent", "unknown"],
        )

        # 索引真的重建了：包含 feature 列。
        index_sql = self._raw(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='ux_budget_alert_daily_threshold'"
        )[0][0]
        self.assertIn(
            "feature", index_sql,
            f"唯一索引没有 feature 列：{index_sql!r}",
        )

    async def test_second_migration_is_idempotent(self) -> None:
        await self._migrate()
        first = [
            (r.id, str(r.status), int(r.attempts), str(r.feature), str(r.summary))
            for r in await self._rows()
        ]
        await init_db(f"sqlite+aiosqlite:///{self.db_path}")
        second = [
            (r.id, str(r.status), int(r.attempts), str(r.feature), str(r.summary))
            for r in await self._rows()
        ]
        self.assertEqual(second, first, "重复迁移改变了数据")

    async def test_two_features_coexist_and_repeat_is_rejected(self) -> None:
        await self._migrate()
        async with self.factory() as session:
            await session.execute(
                text("DELETE FROM budget_alert_events")
            )
            await session.commit()

        group_budget.configure_alert_enqueuer(
            budget_alerts.enqueue_threshold_events
        )
        self.addCleanup(group_budget.configure_alert_enqueuer, None)
        self.addCleanup(group_budget.reset_shared_service)

        from bot.services import pricing

        catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="slot-a", model="m", currency="USD",
                    input_per_million="3", output_per_million="15",
                    cache_read_per_million="1", cache_write_per_million="4",
                ),
            ),
        )
        service = group_budget.shared_service(
            self.factory, lambda: catalog
        )
        self.addCleanup(group_budget.reset_shared_service)

        today = now_shanghai_naive().date().isoformat()
        warn_ratio, pause_ratio = 0.8, 1.0

        # 两次插入同群/同日/同类型/同档，但 feature 不同 -> 必须共存。
        for feature in ("tts", "search"):
            snapshot = budget_alerts.ThresholdSnapshot("operation", 5, 5)
            async with self.factory() as session:
                async with session.begin():
                    await budget_alerts._insert_event(
                        session,
                        group_id=GROUP,
                        budget_date=today,
                        snapshot=snapshot,
                        threshold_kind="pause",
                        threshold_value=5,
                        feature=feature,
                    )
        rows = await self._rows()
        self.assertEqual(
            sorted(r.feature for r in rows), ["search", "tts"],
            "两个 feature 没有共存",
        )
        self.assertTrue(all(str(r.limit_kind) == "operation" for r in rows))

        # 同 feature 重复 -> 唯一键冲突，不再插入。
        async with self.factory() as session:
            async with session.begin():
                again = await budget_alerts._insert_event(
                    session,
                    group_id=GROUP,
                    budget_date=today,
                    snapshot=budget_alerts.ThresholdSnapshot("operation", 6, 5),
                    threshold_kind="pause",
                    threshold_value=5,
                    feature="tts",
                )
        self.assertIsNone(again, "同 feature 重复插入没有被幂等拦下")
        self.assertEqual(len(await self._rows()), 2)
        self.assertLess(warn_ratio, pause_ratio)

    async def test_long_features_sharing_a_prefix_do_not_collide(self) -> None:
        """两个前若干字符相同的长 feature 必须各自独立，且**不被截断**。"""

        await self._migrate()
        async with self.factory() as session:
            await session.execute(text("DELETE FROM budget_alert_events"))
            await session.commit()
        long_a = "search_" + ("x" * 200)
        long_b = "search_" + ("y" * 200)
        common = "search_"
        self.assertTrue(long_a.startswith(common) and long_b.startswith(common))
        self.assertNotEqual(long_a, long_b)

        today = now_shanghai_naive().date().isoformat()
        for feature in (long_a, long_b):
            async with self.factory() as session:
                async with session.begin():
                    await budget_alerts._insert_event(
                        session,
                        group_id=GROUP,
                        budget_date=today,
                        snapshot=budget_alerts.ThresholdSnapshot(
                            "operation", 5, 5
                        ),
                        threshold_kind="pause",
                        threshold_value=5,
                        feature=feature,
                    )
        rows = await self._rows()
        self.assertEqual(len(rows), 2, "长 feature 被截断后互相碰撞了")
        self.assertEqual(
            sorted(r.feature for r in rows), sorted([long_a, long_b]),
            "feature 库被截断了",
        )
        for row in rows:
            self.assertEqual(len(str(row.feature)), 207)

    async def test_existing_attempt_events_keep_idempotency(self) -> None:
        """feature 为空串时，attempt/token/money 的幂等语义完全不变。"""

        await self._migrate()
        async with self.factory() as session:
            await session.execute(text("DELETE FROM budget_alert_events"))
            await session.commit()
        today = now_shanghai_naive().date().isoformat()
        snapshot = budget_alerts.ThresholdSnapshot("attempt", 5, 5)
        async with self.factory() as session:
            async with session.begin():
                first = await budget_alerts._insert_event(
                    session, group_id=GROUP, budget_date=today, snapshot=snapshot,
                    threshold_kind="warn", threshold_value=4,
                )
        async with self.factory() as session:
            async with session.begin():
                second = await budget_alerts._insert_event(
                    session, group_id=GROUP, budget_date=today, snapshot=snapshot,
                    threshold_kind="warn", threshold_value=4,
                )
        self.assertIsNotNone(first)
        self.assertIsNone(second, "空 feature 的旧类型幂等被破坏了")
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].feature), "")
        self.assertEqual(str(rows[0].limit_kind), "attempt")

        # 事件载荷与文本里没有 feature 的就不显示这一段。
        payload = budget_alerts._event_dict(rows[0])
        self.assertEqual(payload["feature"], "")
        self.assertNotIn("（）", budget_alerts._render(rows[0]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()