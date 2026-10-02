"""F-060：``violations.verdict_reason`` 的 ORM 定义与迁移 DDL 必须一致。

审查结论：ORM 声明 NOT NULL + ``server_default=""``，而迁移 DDL 是
``verdict_reason VARCHAR(120) DEFAULT ''``（可空）——读/插入行为等价，但新库
（create_all）与升级库（ALTER TABLE ADD COLUMN）会长出两套 schema，未来做严格
校验或 schema 对比就会暴露差异。

修好之后的口径：两边统一为**可空 + 库级默认 ''**（SQLite 无法给已存在的表加
NOT NULL，要做到 NOT NULL 只能整表重建，对纯观测列不划算）。本文件锁定：
ORM 可空、库级默认存在、升级库与全新库的该列定义逐字段一致、裸 SQL 省略该列时
拿到 '' 而不是报错。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from sqlalchemy import text

from bot.db.engine import init_db
from bot.db.models import Group, Violation


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
