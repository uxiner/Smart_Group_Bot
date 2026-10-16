"""第二期：素材类型分派发送（贴纸 / 图片 / 动图）与安全闸门。"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import MediaIngestLog, StickerLibraryRecord
from bot.services.skills import send_meme as send_meme_module
from bot.services.skills.base import SkillContext
from bot.services.skills.send_meme import SendMemeSkill
from bot.services.skills.send_sticker import SendStickerSkill
from bot.services.sticker_library import (
    MEDIA_TYPE_PHOTO,
    StickerLibrary,
)

GROUP_ID = -1001234567890


def _record(
    file_id: str,
    *,
    media_type: str,
    description: str,
    aliases: list[str] | None = None,
    mime_type: str = "",
) -> StickerLibraryRecord:
    return StickerLibraryRecord(
        group_id=GROUP_ID,
        file_id=file_id,
        media_type=media_type,
        mime_type=mime_type,
        description=description,
        aliases=aliases or [],
        emoji="",
        set_name="",
        seen_count=1,
        sent_count=0,
        source="group_message",
        file_unique_id=f"uq-{file_id}",
    )


class MediaDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        root = (Path.cwd() / "data" / "_test_tmp").resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = (root / f"media-delivery-{uuid4().hex}").resolve()
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tmp / "library.db"

        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path.as_posix()}")
        async with self.engine.begin() as conn:
            await conn.run_sync(StickerLibraryRecord.__table__.create)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.library = StickerLibrary(legacy_dir=self.tmp / "no-legacy")

        async with self.session_factory() as session:
            session.add_all(
                [
                    _record(
                        "photo-1",
                        media_type="photo",
                        description="一只橘猫坐在键盘上 | 情绪=无语 | 场景=吐槽 | 梗=橘猫打字",
                    ),
                    _record(
                        "anim-1",
                        media_type="animation",
                        description="小狗原地转圈 | 情绪=开心 | 场景=庆祝 | 梗=转圈狗",
                        mime_type="video/mp4",
                    ),
                    _record(
                        "stk-1",
                        media_type="sticker",
                        description="熊猫头咧嘴大笑 | 情绪=开心 | 场景=庆祝 | 梗=熊猫头",
                    ),
                ]
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def _session(self):
        return self.session_factory()

    def _context(self, session, **overrides) -> SkillContext:
        kwargs: dict = {
            "session": session,
            "session_factory": self.session_factory,
            "message": SimpleNamespace(chat=SimpleNamespace(id=GROUP_ID)),
            "current_user_text": "哈哈太开心了",
        }
        kwargs.update(overrides)
        return SkillContext(**kwargs)

    async def test_pick_sticker_filters_by_media_type(self) -> None:
        async with self._session() as session:
            photo = await self.library.pick_sticker(
                session, GROUP_ID, query="吐槽", media_types=("photo",)
            )
            animation = await self.library.pick_sticker(
                session, GROUP_ID, query="庆祝", media_types=("animation",)
            )
            sticker = await self.library.pick_sticker(
                session, GROUP_ID, query="庆祝", media_types=("sticker",)
            )

        self.assertEqual(photo.file_id, "photo-1")
        self.assertEqual(photo.media_type, "photo")
        self.assertEqual(animation.file_id, "anim-1")
        self.assertEqual(animation.media_type, "animation")
        self.assertEqual(sticker.file_id, "stk-1")
        self.assertEqual(sticker.media_type, "sticker")

    async def test_send_meme_dispatches_photo_and_animation_to_correct_helper(self) -> None:
        dispatch = AsyncMock()
        async with self._session() as session:
            with (
                patch.object(
                    send_meme_module.sticker_library, "pick_sticker", new=AsyncMock()
                ) as picker,
                patch(
                    "bot.services.skills.send_meme.send_material_with_auto_delete",
                    new=dispatch,
                ),
            ):
                from bot.services.sticker_library import StickerPick

                picker.return_value = StickerPick(
                    file_id="photo-1",
                    source="library_match",
                    description="橘猫打字",
                    media_type="photo",
                )
                result = await SendMemeSkill().run(
                    {"query": "吐槽", "media_type": "photo"},
                    self._context(session),
                )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(dispatch.await_args.kwargs["media_type"], "photo")
        self.assertEqual(dispatch.await_args.kwargs["file_id"], "photo-1")
        self.assertEqual(result.payload["media_type"], "photo")

    async def test_send_meme_rejects_unknown_media_type(self) -> None:
        dispatch = AsyncMock()
        async with self._session() as session:
            with patch(
                "bot.services.skills.send_meme.send_material_with_auto_delete",
                new=dispatch,
            ):
                result = await SendMemeSkill().run(
                    {"media_file_id": "photo-1", "media_type": "video"},
                    self._context(session),
                )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_media_type")
        dispatch.assert_not_awaited()

    async def test_send_meme_rejects_explicit_file_id_outside_group_library(self) -> None:
        dispatch = AsyncMock()
        async with self._session() as session:
            with patch(
                "bot.services.skills.send_meme.send_material_with_auto_delete",
                new=dispatch,
            ):
                result = await SendMemeSkill().run(
                    {"media_file_id": "https://evil.example/x.png"},
                    self._context(session),
                )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "material_not_owned")
        dispatch.assert_not_awaited()

    async def test_send_meme_rejects_wrong_media_type_for_known_material(self) -> None:
        dispatch = AsyncMock()
        async with self._session() as session:
            with patch(
                "bot.services.skills.send_meme.send_material_with_auto_delete",
                new=dispatch,
            ):
                result = await SendMemeSkill().run(
                    {"media_file_id": "photo-1", "media_type": "animation"},
                    self._context(session),
                )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "media_type_mismatch")
        dispatch.assert_not_awaited()

    async def test_send_meme_semantic_miss_sends_nothing(self) -> None:
        dispatch = AsyncMock()
        async with self._session() as session:
            with patch(
                "bot.services.skills.send_meme.send_material_with_auto_delete",
                new=dispatch,
            ):
                result = await SendMemeSkill().run(
                    {"query": "请检查数据库连接失败的原因并给出修复方案"},
                    self._context(
                        session,
                        current_user_text="请检查数据库连接失败的原因并给出修复方案",
                    ),
                )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_material")
        dispatch.assert_not_awaited()

    async def test_send_meme_bookkeeping_failure_does_not_resend(self) -> None:
        dispatch = AsyncMock()
        events: list[str] = []

        async def mark_then_fail(*_args, **_kwargs) -> None:
            events.append("mark")
            raise RuntimeError("db is locked")

        async with self._session() as session:
            with (
                patch(
                    "bot.services.skills.send_meme.send_material_with_auto_delete",
                    new=dispatch,
                ),
                patch.object(
                    send_meme_module.sticker_library,
                    "mark_sent",
                    new=AsyncMock(side_effect=mark_then_fail),
                ),
            ):
                result = await SendMemeSkill().run(
                    {"media_file_id": "photo-1", "media_type": "photo"},
                    self._context(session),
                )

        self.assertTrue(result.ok, result.error)
        self.assertEqual(events, ["mark"])
        self.assertEqual(dispatch.await_count, 1)

    async def test_send_sticker_never_sends_photo_or_animation(self) -> None:
        multi_type_dispatch = AsyncMock()
        sticker_dispatch = AsyncMock()
        picker = AsyncMock()
        async with self._session() as session:
            with (
                patch(
                    "bot.services.skills.send_sticker.sticker_library.pick_sticker",
                    new=picker,
                ),
                patch(
                    "bot.services.skills.send_sticker.send_sticker_with_auto_delete",
                    new=sticker_dispatch,
                ),
                patch(
                    "bot.services.skills.send_meme.send_material_with_auto_delete",
                    new=multi_type_dispatch,
                ),
            ):
                from bot.services.sticker_library import StickerPick

                picker.return_value = StickerPick(
                    file_id="stk-1", source="library_match", media_type="sticker"
                )
                sticker_result = await SendStickerSkill().run(
                    {"query": "庆祝"},
                    self._context(session),
                )

        # The sticker skill hard-restricts the candidate pool to stickers, so the
        # lookup itself can never return a photo/animation row.
        self.assertEqual(picker.await_args.kwargs["media_types"], ("sticker",))
        # It sends through the sticker API only and never routes a photo/animation
        # through the multi-type sender.
        self.assertTrue(sticker_result.ok, sticker_result.error)
        self.assertTrue(sticker_dispatch.await_count == 1)
        self.assertEqual(sticker_dispatch.await_args.kwargs["sticker"], "stk-1")
        multi_type_dispatch.assert_not_awaited()

    async def test_send_sticker_cannot_reach_a_photo_row_with_real_lookup(self) -> None:
        # Real (unpatched) lookup: only a photo row matches "吐槽", so the sticker
        # skill must find nothing rather than sending the photo as a sticker.
        sticker_dispatch = AsyncMock()
        async with self._session() as session:
            with patch(
                "bot.services.skills.send_sticker.send_sticker_with_auto_delete",
                new=sticker_dispatch,
            ):
                result = await SendStickerSkill().run(
                    {"query": "吐槽", "file_id": ""},
                    self._context(session, current_user_text="吐槽"),
                )

        sticker_dispatch.assert_not_awaited()
        self.assertFalse(result.ok)

    async def test_lookup_owned_material_is_group_scoped(self) -> None:
        async with self._session() as session:
            owned = await self.library.lookup_owned_material(session, GROUP_ID, "photo-1")
            foreign = await self.library.lookup_owned_material(session, GROUP_ID, "nope")

        self.assertIsNotNone(owned)
        self.assertEqual(owned.media_type, "photo")
        self.assertIsNone(foreign)


class _MissResult:
    """A SELECT result that pretends the row does not exist yet."""

    def scalar_one_or_none(self):
        return None


class MaterialUpsertRaceTests(unittest.IsolatedAsyncioTestCase):
    """并发入库：同一张图只能有一行，且不能被写两次。"""

    async def asyncSetUp(self) -> None:
        root = (Path.cwd() / "data" / "_test_tmp").resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.tmp = (root / f"media-race-{uuid4().hex}").resolve()
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tmp / "library.db"

        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path.as_posix()}")
        async with self.engine.begin() as conn:
            await conn.run_sync(
                StickerLibraryRecord.__table__.create,
            )
            await conn.run_sync(MediaIngestLog.__table__.create)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.library = StickerLibrary(legacy_dir=self.tmp / "no-legacy")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _count_rows(self, group_id: int, file_id: str) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM sticker_library "
                    "WHERE group_id = :g AND file_id = :f"
                ),
                {"g": group_id, "f": file_id},
            )
            return int(result.scalar())

    async def test_same_image_with_two_file_ids_stays_one_row(self) -> None:
        # Telegram gives one photo several sizes: different file_id, same
        # file_unique_id. Both must collapse onto the row we already own.
        async with self.session_factory() as session:
            first = await self.library.upsert_material(
                session,
                GROUP_ID,
                file_id="photo-small",
                raw_description="一只橘猫坐在键盘上 | 情绪=无语 | 场景=吐槽",
                media_type="photo",
                mime_type="image/jpeg",
                width=320,
                height=240,
                file_unique_id="uq-shared-photo",
            )
            await session.commit()

        async with self.session_factory() as session:
            second = await self.library.upsert_material(
                session,
                GROUP_ID,
                file_id="photo-large",
                raw_description="一只橘猫坐在键盘上 | 情绪=无语 | 场景=吐槽",
                media_type="photo",
                mime_type="image/jpeg",
                width=1280,
                height=960,
                file_unique_id="uq-shared-photo",
            )
            await session.commit()

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        # The row is reused, not duplicated.
        self.assertEqual(second["file_id"], "photo-small")
        self.assertEqual(second["file_unique_id"], "uq-shared-photo")
        self.assertEqual(second["seen_count"], 2)
        self.assertEqual(await self._count_rows(GROUP_ID, "photo-small"), 1)
        self.assertEqual(await self._count_rows(GROUP_ID, "photo-large"), 0)

    async def test_lost_insert_race_refreshes_the_winner_row(self) -> None:
        # Another worker commits the same file_id while our worker is still
        # between its SELECT and its INSERT. Simulated deterministically: the
        # row exists, but our two lookup SELECTs lie that it does not, so the
        # INSERT really collides on the unique index.
        async with self.session_factory() as session:
            session.add(
                _record(
                    "race-1",
                    media_type="sticker",
                    description="先到者的描述 | 情绪=开心 | 场景=庆祝",
                )
            )
            await session.commit()

        async with self.session_factory() as session:
            # Unrelated pending work from the caller: the savepoint rollback
            # must not take it down with it.
            session.add(
                _record("pending-1", media_type="sticker", description="无关的待写记录")
            )

            real_execute = session.execute
            misses = {"left": 2}

            async def _execute(stmt, *args, **kwargs):
                if misses["left"] > 0:
                    misses["left"] -= 1
                    return _MissResult()
                return await real_execute(stmt, *args, **kwargs)

            with patch.object(session, "execute", new=_execute):
                row = await self.library.upsert_material(
                    session,
                    GROUP_ID,
                    file_id="race-1",
                    raw_description="后到者的描述 | 情绪=无语 | 场景=吐槽",
                    media_type="sticker",
                    file_unique_id="uq-race-1",
                )

            # The session must still be usable after losing the race.
            await session.commit()

        self.assertIsNotNone(row)
        self.assertEqual(row["file_id"], "race-1")
        # Losing the race refreshes the winner instead of raising.
        self.assertEqual(row["seen_count"], 2)
        self.assertEqual(await self._count_rows(GROUP_ID, "race-1"), 1)
        # The caller's own pending row survived the savepoint rollback.
        self.assertEqual(await self._count_rows(GROUP_ID, "pending-1"), 1)

    async def test_claim_ingest_is_first_writer_wins(self) -> None:
        async with self.session_factory() as session:
            self.assertFalse(
                await self.library.has_ingested(session, GROUP_ID, "uq-claim")
            )
            first = await self.library.claim_ingest(
                session,
                GROUP_ID,
                "uq-claim",
                media_type=MEDIA_TYPE_PHOTO,
                telegram_message_id=42,
                status="ok",
            )
            await session.commit()

        async with self.session_factory() as session:
            self.assertTrue(
                await self.library.has_ingested(session, GROUP_ID, "uq-claim")
            )
            second = await self.library.claim_ingest(
                session,
                GROUP_ID,
                "uq-claim",
                media_type=MEDIA_TYPE_PHOTO,
                telegram_message_id=42,
                status="ok",
            )
            await session.commit()

        self.assertTrue(first)
        self.assertFalse(second)

    async def test_has_ingested_refuses_to_claim_without_a_stable_id(self) -> None:
        async with self.session_factory() as session:
            # No stable identifier: we must not claim "already processed", or the
            # material would be silently dropped forever.
            self.assertFalse(await self.library.has_ingested(session, GROUP_ID, ""))

