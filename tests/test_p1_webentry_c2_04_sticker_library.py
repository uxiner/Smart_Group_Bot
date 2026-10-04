"""补测试缺口 C2-04：``bot/services/sticker_library.py``（454 行）补真实 DB 用例。

``AUDIT-C`` C2-04 记录该模块在整个测试套件中**从未执行过一次真实代码**::

    tests/test_nsfw_image_guard.py:414   patch("bot.handlers.group.sticker_library",
                                      SimpleNamespace(learn_from_message=AsyncMock(...)))
    tests/test_send_sticker_skill.py:48  patch("...send_sticker.sticker_library.pick_sticker")
    tests/test_runtime_prompt_contexts.py:99 patch.object(sticker_library, "total_sent_count", new=AsyncMock(...))

真实实现含**文件系统读取与 JSON 解析**（``_legacy_path`` / ``_ensure_legacy_import``
读 ``memory/stickers/{group_id}.json``）、**剪枝**（``_trim_group_records``，
``_MAX_STICKERS_PER_GROUP = 100``）、**评分排序**，全部 0 覆盖。而
``bot/handlers/group.py`` **每条群消息**都调 ``learn_from_message``。

本文件走**真 SQLite**（真 ``init_db`` + 真 ``StickerLibraryRecord`` 行）+ 真实临时
``legacy_dir``，只把 ``Message`` 替身成 ``SimpleNamespace``（消息对象本就不是被测
对象）。按报告建议覆盖：旧 JSON 脏数据导入、剪枝边界、评分排序、并发写同一 group。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import func, select

from bot.db.engine import init_db
from bot.db.models import StickerLibraryRecord
from bot.services import sticker_library as sticker_module
from bot.services.sticker_library import StickerLibrary


def _sticker_message(file_id: str, emoji: str = "", set_name: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        sticker=SimpleNamespace(
            file_id=file_id,
            emoji=emoji,
            set_name=set_name,
        )
    )


class StickerLibraryLegacyImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self._legacy_dir = Path(tempfile.mkdtemp())
        self.library = StickerLibrary(legacy_dir=self._legacy_dir)
        self.group_id = -100123

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass
        try:
            self._legacy_dir and os.rmdir(self._legacy_dir)
        except OSError:
            pass

    def _write_legacy(self, group_id: int, payload: object) -> None:
        path = os.path.join(self._legacy_dir, f"{group_id}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

    async def _rows(self, group_id: int | None = None) -> list[StickerLibraryRecord]:
        async with self.session_factory() as session:
            stmt = select(StickerLibraryRecord).where(
                StickerLibraryRecord.group_id
                == (self.group_id if group_id is None else group_id)
            )
            return list((await session.execute(stmt)).scalars().all())

    async def test_legacy_json_is_imported_into_the_database(self) -> None:
        self._write_legacy(
            self.group_id,
            {
                "stickers": [
                    {
                        "file_id": "AAA",
                        "emoji": "😀",
                        "set_name": "经典",
                        "description": "笑脸",
                        "aliases": ["大笑", "  ", "haha"],
                        "seen_count": 3,
                        "sent_count": 2,
                        "created_at": "2026-01-02T03:04:05+00:00",
                    },
                    {
                        "file_id": "BBB",
                        "emoji": "😭",
                        "set_name": "",
                        "description": "哭脸",
                        "aliases": "不是列表",
                    },
                ]
            },
        )
        async with self.session_factory() as session:
            await self.library._ensure_legacy_import(session, self.group_id)
            await session.commit()
            rows = list(
                (
                    await session.execute(
                        select(StickerLibraryRecord).order_by(
                            StickerLibraryRecord.file_id
                        )
                    )
                )
                .scalars()
                .all()
            )

        self.assertEqual([row.file_id for row in rows], ["AAA", "BBB"])
        first = rows[0]
        self.assertEqual(first.emoji, "😀")
        self.assertEqual(first.set_name, "经典")
        self.assertEqual(first.description, "笑脸")
        # 空别名被丢掉，非列表的 aliases 退化成空列表。
        self.assertEqual(first.aliases, ["大笑", "haha"])
        self.assertEqual(first.seen_count, 3)
        self.assertEqual(first.sent_count, 2)
        self.assertEqual(first.source, "legacy_json")
        # SQLite 的 DateTime 列不带 tzinfo：+00:00 解析后按 UTC 朴素时间落库。
        self.assertEqual(first.created_at, datetime(2026, 1, 2, 3, 4, 5))
        self.assertIsNone(first.created_at.tzinfo)
        self.assertEqual(rows[1].aliases, [])

    async def test_duplicate_file_ids_in_one_legacy_file_are_collapsed(self) -> None:
        self._write_legacy(
            self.group_id,
            {
                "stickers": [
                    {"file_id": "AAA", "description": "第一份"},
                    {"file_id": "AAA", "description": "重复"},
                    {"file_id": "", "description": "没有 file_id"},
                    "不是字典",
                ]
            },
        )
        async with self.session_factory() as session:
            await self.library._ensure_legacy_import(session, self.group_id)
            await session.commit()

        rows = await self._rows()
        self.assertEqual([row.file_id for row in rows], ["AAA"])
        self.assertEqual(rows[0].description, "第一份")

    async def test_import_is_skipped_when_rows_already_exist(self) -> None:
        async with self.session_factory() as session:
            session.add(
                StickerLibraryRecord(
                    group_id=self.group_id,
                    file_id="EXISTING",
                    description="已在库里",
                )
            )
            await session.commit()
        # 旧文件里有别的东西，但不能被导进来（否则会重复）。
        self._write_legacy(
            self.group_id, {"stickers": [{"file_id": "FROM-FILE", "description": "x"}]}
        )
        async with self.session_factory() as session:
            await self.library._ensure_legacy_import(session, self.group_id)
            await session.commit()

        rows = await self._rows()
        self.assertEqual([row.file_id for row in rows], ["EXISTING"])

    async def test_corrupt_legacy_json_is_survived_and_marked_checked(self) -> None:
        path = os.path.join(self._legacy_dir, f"{self.group_id}.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json at all")
        async with self.session_factory() as session:
            await self.library._ensure_legacy_import(session, self.group_id)
            await session.commit()

        self.assertEqual(await self._rows(), [])
        # 标记成已检查 → 同一次运行里不会反复读盘。
        self.assertIn(self.group_id, self.library._checked_groups)

    async def test_non_dict_legacy_payload_is_ignored(self) -> None:
        self._write_legacy(self.group_id, ["not", "a", "dict"])
        async with self.session_factory() as session:
            await self.library._ensure_legacy_import(session, self.group_id)
            await session.commit()
        self.assertEqual(await self._rows(), [])

    async def test_missing_legacy_file_is_a_no_op(self) -> None:
        async with self.session_factory() as session:
            await self.library._ensure_legacy_import(session, -999)
            await session.commit()
        self.assertIn(-999, self.library._checked_groups)
        self.assertEqual(await self._rows(-999), [])

    async def test_legacy_import_is_trimmed_to_the_per_group_ceiling(self) -> None:
        ceiling = sticker_module._MAX_STICKERS_PER_GROUP
        self.assertEqual(ceiling, 100)
        self._write_legacy(
            self.group_id,
            {
                "stickers": [
                    {
                        "file_id": f"F{index:04d}",
                        "seen_count": index,
                        "last_seen_at": (
                            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index)
                        ).isoformat(),
                    }
                    for index in range(ceiling + 25)
                ]
            },
        )
        async with self.session_factory() as session:
            await self.library._ensure_legacy_import(session, self.group_id)
            await session.commit()

        rows = await self._rows()
        self.assertEqual(len(rows), ceiling)
        kept = {row.file_id for row in rows}
        # 按 last_seen_at 倒序保留：最新的 F0124..F0000 在内，最旧的 25 个被剪掉。
        self.assertNotIn("F0000", kept)
        self.assertIn(f"F{ceiling + 24:04d}", kept)


class StickerLibraryLearnAndPickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self._legacy_dir = Path(tempfile.mkdtemp())
        self.library = StickerLibrary(legacy_dir=self._legacy_dir)
        self.group_id = -100123

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass
        try:
            os.rmdir(self._legacy_dir)
        except OSError:
            pass

    async def _count(self) -> int:
        async with self.session_factory() as session:
            return int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(StickerLibraryRecord)
                        .where(StickerLibraryRecord.group_id == self.group_id)
                    )
                ).scalar_one()
            )

    async def test_learn_from_message_inserts_then_increments(self) -> None:
        async with self.session_factory() as session:
            first = await self.library.learn_from_message(
                session,
                self.group_id,
                _sticker_message("AAA", emoji="😀", set_name="经典"),
                vision_description="一个笑脸",
            )
            await session.commit()
            second = await self.library.learn_from_message(
                session,
                self.group_id,
                _sticker_message("AAA", emoji="😀", set_name="经典"),
            )
            await session.commit()

        self.assertEqual(first["file_id"], "AAA")
        self.assertEqual(first["seen_count"], 1)
        self.assertEqual(first["description"], "一个笑脸")
        self.assertEqual(second["seen_count"], 2)
        self.assertEqual(await self._count(), 1)

    async def test_learn_from_message_ignores_non_sticker_messages(self) -> None:
        async with self.session_factory() as session:
            self.assertIsNone(
                await self.library.learn_from_message(
                    session, self.group_id, SimpleNamespace(sticker=None)
                )
            )
            self.assertIsNone(
                await self.library.learn_from_message(
                    session, self.group_id, _sticker_message("   ")
                )
            )
        self.assertEqual(await self._count(), 0)

    async def test_description_change_moves_the_old_text_into_aliases(self) -> None:
        async with self.session_factory() as session:
            await self.library.learn_from_message(
                session, self.group_id, _sticker_message("AAA"), vision_description="旧描述"
            )
            # 会话工厂是 autoflush=False，模块内部那些 SELECT 看不到待写行；
            # 生产里每条消息各自提交，这里显式 flush 保持同一形状。
            await session.flush()
            await self.library.learn_from_message(
                session, self.group_id, _sticker_message("AAA"), vision_description="新描述"
            )
            await session.commit()
            row = (
                await session.execute(
                    select(StickerLibraryRecord).where(
                        StickerLibraryRecord.file_id == "AAA"
                    )
                )
            ).scalar_one()

        self.assertEqual(row.description, "新描述")
        self.assertEqual(row.aliases, ["旧描述"])

    async def test_invalid_vision_markers_fall_back_to_a_generated_description(self) -> None:
        async with self.session_factory() as session:
            await self.library.learn_from_message(
                session,
                self.group_id,
                _sticker_message("AAA", emoji="😀", set_name="经典"),
                vision_description="NO_VALID_IMAGE_CONTENT",
            )
            await session.commit()
            row = (
                await session.execute(
                    select(StickerLibraryRecord).where(
                        StickerLibraryRecord.file_id == "AAA"
                    )
                )
            ).scalar_one()
        self.assertEqual(row.description, "贴纸 😀（来自 经典）")

    async def test_learn_stops_growing_the_group_at_the_ceiling(self) -> None:
        ceiling = sticker_module._MAX_STICKERS_PER_GROUP
        async with self.session_factory() as session:
            for index in range(ceiling):
                await self.library.learn_from_message(
                    session, self.group_id, _sticker_message(f"F{index:04d}")
                )
                await session.flush()  # autoflush=False
            await session.commit()
            overflow = await self.library.learn_from_message(
                session, self.group_id, _sticker_message("OVERFLOW")
            )
            await session.commit()

        self.assertIsNone(overflow)
        self.assertEqual(await self._count(), ceiling)

    async def test_mark_sent_counts_and_total_sent_count_aggregates(self) -> None:
        async with self.session_factory() as session:
            await self.library.mark_sent(session, self.group_id, "AAA")
            await session.flush()  # autoflush=False，见上
            await self.library.mark_sent(session, self.group_id, "AAA")
            await session.flush()
            await self.library.mark_sent(session, self.group_id, "BBB")
            await session.commit()
            total = await self.library.total_sent_count(session, self.group_id)

        self.assertEqual(total, 3)

    async def test_mark_sent_of_an_unknown_sticker_creates_a_default_row(self) -> None:
        async with self.session_factory() as session:
            await self.library.mark_sent(session, self.group_id, "NEW")
            await session.commit()
            row = (
                await session.execute(
                    select(StickerLibraryRecord).where(
                        StickerLibraryRecord.file_id == "NEW"
                    )
                )
            ).scalar_one()
        self.assertEqual(row.description, "默认贴纸")
        self.assertEqual(row.source, "skill_send")
        self.assertEqual(row.sent_count, 1)

    async def test_pick_sticker_prefers_a_text_match(self) -> None:
        async with self.session_factory() as session:
            await self.library.learn_from_message(
                session, self.group_id, _sticker_message("CAT"), vision_description="一只小猫"
            )
            await self.library.learn_from_message(
                session, self.group_id, _sticker_message("DOG"), vision_description="一只小狗"
            )
            await session.commit()
            pick = await self.library.pick_sticker(
                session, self.group_id, query="小猫"
            )
        self.assertEqual(pick.file_id, "CAT")
        self.assertEqual(pick.source, "library_match")
        self.assertGreaterEqual(pick.score, 25)

    async def test_pick_sticker_falls_back_to_recent_then_pool_then_empty(self) -> None:
        async with self.session_factory() as session:
            empty = await self.library.pick_sticker(session, self.group_id)
            pooled = await self.library.pick_sticker(
                session, self.group_id, fallback_pool=["POOL-1", "  ", "POOL-2"]
            )
            self.assertEqual(empty.file_id, "")
            self.assertEqual(empty.source, "none")
            self.assertEqual(pooled.source, "fallback_pool")
            self.assertIn(pooled.file_id, {"POOL-1", "POOL-2"})

            await self.library.learn_from_message(
                session, self.group_id, _sticker_message("AAA")
            )
            await session.commit()
            recent = await self.library.pick_sticker(
                session, self.group_id, query="完全对不上的查询串"
            )
        self.assertEqual(recent.file_id, "AAA")
        self.assertEqual(recent.source, "library_recent")

    async def test_list_candidates_orders_by_recency_and_caps_the_db_portion(
        self,
    ) -> None:
        async with self.session_factory() as session:
            for index in range(6):
                await self.library.learn_from_message(
                    session,
                    self.group_id,
                    _sticker_message(f"F{index:02d}"),
                )
                await session.flush()  # autoflush=False
            await session.commit()
            candidates = await self.library.list_candidates(
                session,
                self.group_id,
                limit=3,
                fallback_pool=["F05", "POOL-1"],
            )
            db_only = await self.library.list_candidates(
                session, self.group_id, limit=3
            )

        # DB 部分严格按 limit 截断，按最近使用倒序（id 越大越新）。
        self.assertEqual([row["file_id"] for row in db_only], ["F05", "F04", "F03"])
        # fallback_pool 里与 DB 重复的 F05 被 used 集合挡住，POOL-1 接在后面。
        self.assertEqual(
            [row["file_id"] for row in candidates],
            ["F05", "F04", "F03", "POOL-1"],
        )
        self.assertEqual(candidates[-1]["source"], "fallback_pool")
        self.assertEqual(candidates[0]["source"], "group_message")

    async def test_list_candidates_clamps_the_limit(self) -> None:
        async with self.session_factory() as session:
            self.assertEqual(
                await self.library.list_candidates(session, self.group_id, limit=0), []
            )
            self.assertEqual(
                await self.library.list_candidates(session, self.group_id, limit=9999),
                [],
            )

    async def test_concurrent_learns_for_the_same_group_all_land(self) -> None:
        """``_checked_groups`` 是进程级的一次性门闩：并发首次导入不能互相踩。"""

        async def one(index: int) -> None:
            async with self.session_factory() as session:
                await self.library.learn_from_message(
                    session, self.group_id, _sticker_message(f"C{index:02d}")
                )
                await session.commit()

        await asyncio.gather(*(one(index) for index in range(8)))
        self.assertEqual(await self._count(), 8)
        # 门闩仍然只标记一次。
        self.assertIn(self.group_id, self.library._checked_groups)

    async def test_legacy_dir_defaults_to_the_repository_memory_folder(self) -> None:
        default_library = StickerLibrary()
        self.assertEqual(
            default_library.legacy_dir,
            sticker_module._LEGACY_STICKER_DIR,
        )
        with patch.object(sticker_module, "_LEGACY_STICKER_DIR", Path("/nope")):
            self.assertEqual(StickerLibrary().legacy_dir, Path("/nope"))
