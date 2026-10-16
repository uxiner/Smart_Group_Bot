"""第二期：素材库 schema 启动迁移的向后兼容与幂等。"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from bot.db.engine import init_db
from bot.services.sticker_library import MEDIA_TYPE_STICKER, normalize_media_type

_LEGACY_STICKER_TABLE_SQL = """
CREATE TABLE sticker_library (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    group_id BIGINT NOT NULL,
    file_id VARCHAR(255) NOT NULL,
    emoji VARCHAR(32) NOT NULL DEFAULT '',
    set_name VARCHAR(255) NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    aliases JSON,
    seen_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    source VARCHAR(64) NOT NULL DEFAULT 'group_message',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_sent_at DATETIME
)
"""

_NEW_COLUMNS = ("media_type", "mime_type", "width", "height", "file_unique_id")


def _create_legacy_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(_LEGACY_STICKER_TABLE_SQL)
        cur.execute(
            "CREATE UNIQUE INDEX ix_sticker_group_file "
            "ON sticker_library (group_id, file_id)"
        )
        cur.execute(
            "INSERT INTO sticker_library "
            "(group_id, file_id, emoji, description, aliases, seen_count, sent_count, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                -100,
                "legacy-file-id",
                "🎉",
                "黑白熊猫头咧嘴大笑，适合庆祝胜利",
                '["开心", "庆祝"]',
                3,
                1,
                "group_message",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _table_columns(db_path: Path, table: str) -> dict[str, str]:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        return {str(row[1]): str(row[2]) for row in cur.fetchall()}
    finally:
        conn.close()


class StickerLibrarySchemaMigrationTests(unittest.IsolatedAsyncioTestCase):
    def _workspace_tmpdir(self) -> Path:
        root = (Path.cwd() / "data" / "_test_tmp").resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = (root / f"sticker-migration-{uuid4().hex}").resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _sqlite_url(db_path: Path) -> str:
        return f"sqlite+aiosqlite:///{db_path.resolve().as_posix()}"

    async def test_legacy_database_gains_media_columns_and_keeps_old_rows(self) -> None:
        db_path = self._workspace_tmpdir() / "legacy.db"
        _create_legacy_database(db_path)

        engine, session_factory = await init_db(self._sqlite_url(db_path))
        try:
            columns = _table_columns(db_path, "sticker_library")
            for column in _NEW_COLUMNS:
                self.assertIn(column, columns)
            self.assertIn("VARCHAR(16)", columns["media_type"].upper())
            self.assertIn("VARCHAR(255)", columns["file_unique_id"].upper())

            async with session_factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT file_id, media_type, mime_type, width, height, "
                            "file_unique_id, description FROM sticker_library "
                            "WHERE group_id = -100"
                        )
                    )
                ).one()
            # A pre-phase-2 row reads as a sticker with empty media metadata and
            # its original description untouched.
            self.assertEqual(row[0], "legacy-file-id")
            self.assertEqual(row[1], MEDIA_TYPE_STICKER)
            self.assertEqual(row[2], "")
            self.assertEqual(row[3], 0)
            self.assertEqual(row[4], 0)
            self.assertEqual(row[5], "")
            self.assertEqual(row[6], "黑白熊猫头咧嘴大笑，适合庆祝胜利")

            ingest_columns = _table_columns(db_path, "media_ingest_log")
            self.assertTrue(ingest_columns)
            self.assertIn("file_unique_id", ingest_columns)
            self.assertIn("status", ingest_columns)
        finally:
            await engine.dispose()

    async def test_repeated_startup_is_idempotent(self) -> None:
        db_path = self._workspace_tmpdir() / "restart.db"
        _create_legacy_database(db_path)

        engine, _sessions = await init_db(self._sqlite_url(db_path))
        await engine.dispose()
        engine, _sessions = await init_db(self._sqlite_url(db_path))
        try:
            columns = _table_columns(db_path, "sticker_library")
        finally:
            await engine.dispose()

        # Exactly one column per name: ALTER TABLE ADD COLUMN must not run twice
        # and there must be no duplicate/damaged columns after a restart.
        self.assertEqual(sorted(columns), sorted(set(columns)))
        for column in _NEW_COLUMNS:
            self.assertIn(column, columns)

        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sticker_library WHERE group_id = -100")
            self.assertEqual(int(cur.fetchone()[0]), 1)
            cur.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name = 'media_ingest_log'"
            )
            self.assertEqual(int(cur.fetchone()[0]), 1)
            cur.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
                "AND name = 'ix_media_ingest_group_unique'"
            )
            self.assertEqual(int(cur.fetchone()[0]), 1)
        finally:
            conn.close()

    async def test_media_type_normalization_never_guesses(self) -> None:
        self.assertEqual(normalize_media_type(None), MEDIA_TYPE_STICKER)
        self.assertEqual(normalize_media_type(""), MEDIA_TYPE_STICKER)
        self.assertEqual(normalize_media_type("video"), MEDIA_TYPE_STICKER)
        self.assertEqual(normalize_media_type("PHOTO"), "photo")
        self.assertEqual(normalize_media_type("animation"), "animation")
