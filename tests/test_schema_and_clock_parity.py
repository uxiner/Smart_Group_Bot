"""F-060 / F-050：schema 漂移与时间口径一致性。

F-060：``violations.verdict_reason`` 的 ORM 定义与迁移 DDL 必须一致。

审查结论：ORM 声明 NOT NULL + ``server_default=""``，而迁移 DDL 是
``verdict_reason VARCHAR(120) DEFAULT ''``（可空）——读/插入行为等价，但新库
（create_all）与升级库（ALTER TABLE ADD COLUMN）会长出两套 schema，未来做严格
校验或 schema 对比就会暴露差异。

修好之后的口径：两边统一为**可空 + 库级默认 ''**（SQLite 无法给已存在的表加
NOT NULL，要做到 NOT NULL 只能整表重建，对纯观测列不划算）。

F-050：四张新台账表的 ``created_at`` 统一为本地（Asia/Shanghai）时钟。

审查结论：``member_checkins`` / ``member_point_spends`` 只有
``server_default=func.now()``（SQLite CURRENT_TIMESTAMP = UTC），而
``member_entitlements`` / ``member_point_awards`` 是 ``default=now_shanghai_naive``。
现在只有 award 参与分桶，暂时自洽；但任何未来对另外两张表的报表都会差 8 小时。

修好之后的口径：Python 侧 default 统一为 ``now_shanghai_naive``（server_default 只
作裸 SQL 兜底）；扣分走的 Core ``INSERT ... SELECT`` 显式带上本地 created_at；
老库里由 CURRENT_TIMESTAMP 写入的存量行用 ``PRAGMA user_version`` 独立版本号
**一次性** +8 小时，重跑不会再加一次。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from sqlalchemy import select, text

from bot.db.engine import _SQLITE_SCHEMA_VERSION, init_db
from bot.db.models import (
    Group,
    MemberCheckin,
    MemberEntitlement,
    MemberPointAward,
    MemberPointSpend,
    MessageVector,
    Violation,
)
from bot.services.checkin import spend_points
from bot.utils.timezone import now_shanghai_naive


async def _verdict_reason_column(path: str) -> tuple:
    engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
    try:
        async with session_factory() as session:
            rows = (
                await session.execute(text("PRAGMA table_info(violations)"))
            ).all()
    finally:
        await engine.dispose()
    for row in rows:
        if row[1] == "verdict_reason":
            return tuple(row)
    raise AssertionError("verdict_reason column missing")


class VerdictReasonSchemaDriftTests(unittest.IsolatedAsyncioTestCase):
    def test_orm_declares_the_column_nullable_with_a_server_default(self) -> None:
        column = Violation.__table__.c.verdict_reason

        self.assertTrue(column.nullable)
        self.assertIsNotNone(column.server_default)
        self.assertEqual(column.server_default.arg, "")

    async def test_upgraded_and_fresh_schemas_agree(self) -> None:
        fresh_fd, fresh_path = tempfile.mkstemp(suffix=".db")
        os.close(fresh_fd)
        legacy_fd, legacy_path = tempfile.mkstemp(suffix=".db")
        os.close(legacy_fd)
        try:
            fresh = await _verdict_reason_column(fresh_path)

            # 造一个"中间版本"的老库：先按当前定义建库，再把这列删掉，模拟
            # 由 _sqlite_ensure_column 补列的那条升级路径。
            engine, _session_factory = await init_db(
                f"sqlite+aiosqlite:///{legacy_path}"
            )
            await engine.dispose()
            connection = sqlite3.connect(legacy_path)
            connection.execute("ALTER TABLE violations DROP COLUMN verdict_reason")
            connection.commit()
            connection.close()

            upgraded = await _verdict_reason_column(legacy_path)
        finally:
            for path in (fresh_path, legacy_path):
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.remove(path + suffix)
                    except OSError:
                        pass

        # (type, notnull, dflt_value) 必须逐字段一致；cid 会因 ALTER 追加而不同，
        # 列顺序不影响读写语义，所以不比较。
        self.assertEqual(fresh[2:5], upgraded[2:5])
        self.assertEqual(fresh[2:5], ("VARCHAR(120)", 0, "''"))

    async def test_raw_sql_insert_without_the_column_gets_the_empty_default(
        self,
    ) -> None:
        """可空 + 库级默认：裸 SQL 省略该列时拿 ''，不是 NULL 也不是报错。"""

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
        try:
            async with session_factory() as session:
                session.add(Group(id=-100, title="t", settings={}))
                await session.commit()
                await session.execute(
                    text(
                        "INSERT INTO violations "
                        "(group_id, user_id, action_taken, message_text, created_at) "
                        "VALUES (-100, 7, 'warn', 'x', CURRENT_TIMESTAMP)"
                    )
                )
                await session.commit()
                value = (
                    await session.execute(
                        text("SELECT verdict_reason FROM violations")
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

        self.assertEqual(value, "")


LEDGER_MODELS = (
    MemberCheckin,
    MemberPointSpend,
    MemberEntitlement,
    MemberPointAward,
)


class LedgerClockParityTests(unittest.IsolatedAsyncioTestCase):
    def test_all_four_ledger_tables_default_to_the_local_clock(self) -> None:
        for model in LEDGER_MODELS:
            with self.subTest(model=model.__name__):
                column = model.__table__.c.created_at
                self.assertIsNotNone(
                    column.default, f"{model.__name__}.created_at 缺少 Python 侧默认值"
                )
                # ColumnDefault.arg 可以接受一个 ExecutionContext；对零参可调用对象
                # SQLAlchemy 会自己包一层，所以这里传 None 是标准调用方式。
                value = column.default.arg(None)
                self.assertIsInstance(value, datetime)
                self.assertIsNone(value.tzinfo)
                self.assertLess(
                    abs((value - now_shanghai_naive()).total_seconds()),
                    60.0,
                    f"{model.__name__}.created_at 的默认值不是本地时钟",
                )

    async def test_checkin_insert_uses_the_local_clock(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
        try:
            async with session_factory() as session:
                session.add(
                    MemberCheckin(
                        group_id=-100,
                        user_id=7,
                        checkin_date="2026-01-01",
                        points=3,
                        display_name="甲",
                    )
                )
                await session.commit()
            async with session_factory() as session:
                value = (
                    await session.execute(select(MemberCheckin.created_at))
                ).scalar_one()
        finally:
            await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

        self.assertLess(
            abs((value - now_shanghai_naive()).total_seconds()),
            120.0,
            "签到行的 created_at 必须是本地时间",
        )

    async def test_point_spend_insert_uses_the_local_clock(self) -> None:
        """扣分走 Core INSERT ... SELECT，Python 默认值不会自动生效，必须显式带上。"""

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
        try:
            async with session_factory() as session:
                session.add(
                    MemberCheckin(
                        group_id=-100,
                        user_id=7,
                        checkin_date="2026-01-01",
                        points=5,
                        display_name="甲",
                    )
                )
                await session.commit()
            async with session_factory() as session:
                spent = await spend_points(
                    session,
                    group_id=-100,
                    user_id=7,
                    points=2,
                    reason="moderation_challenge",
                    ref="challenge:1",
                )
                await session.commit()
            self.assertTrue(spent)
            async with session_factory() as session:
                value = (
                    await session.execute(select(MemberPointSpend.created_at))
                ).scalar_one()
        finally:
            await engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

        self.assertLess(
            abs((value - now_shanghai_naive()).total_seconds()),
            120.0,
            "消费行的 created_at 必须是本地时间",
        )


class LegacyLedgerClockMigrationTests(unittest.IsolatedAsyncioTestCase):
    """老库（user_version=1，生产现值）启动后口径一致，且只迁移一次。"""

    async def test_legacy_rows_shift_exactly_once(self) -> None:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
            async with session_factory() as session:
                session.add(
                    MessageVector(
                        group_id=-100,
                        message_id="-100:1",
                        role="user",
                        content="x",
                        created_at=datetime(2026, 1, 1, 8, 30, 0),
                    )
                )
                await session.commit()
            await engine.dispose()

            connection = sqlite3.connect(path)
            try:
                # 生产库现在的值：v1（message_vectors 时间戳迁移已完成）。
                connection.execute("PRAGMA user_version = 1")
                connection.execute(
                    "INSERT INTO member_checkins "
                    "(group_id, user_id, checkin_date, points, display_name, created_at) "
                    "VALUES (-100, 7, '2026-01-01', 1, '', '2026-01-01 00:30:00')"
                )
                connection.commit()
            finally:
                connection.close()

            engine, _session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
            await engine.dispose()
            first = self._raw_timestamps(path)

            # 第二次启动：v2 迁移已经跑过，任何一行都不许再 +8 小时。
            engine, _session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
            await engine.dispose()
            second = self._raw_timestamps(path)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(path + suffix)
                except OSError:
                    pass

        # message_vectors 已经是本地时间：v1 迁移不许重跑
        self.assertEqual(first["message_vectors"], "2026-01-01 08:30:00")
        # 台账存量行（UTC）被 +8 小时成本地
        self.assertEqual(first["member_checkins"], "2026-01-01 08:30:00")
        # 幂等：再启动一次不变
        self.assertEqual(second, first)
        self.assertEqual(second["user_version"], _SQLITE_SCHEMA_VERSION)

    @staticmethod
    def _raw_timestamps(path: str) -> dict:
        connection = sqlite3.connect(path)
        try:
            return {
                "message_vectors": connection.execute(
                    "SELECT created_at FROM message_vectors"
                ).fetchone()[0],
                "member_checkins": connection.execute(
                    "SELECT created_at FROM member_checkins"
                ).fetchone()[0],
                "user_version": connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0],
            }
        finally:
            connection.close()
