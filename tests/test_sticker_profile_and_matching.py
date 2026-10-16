from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base, StickerLibraryRecord
from bot.services.sticker_library import (
    StickerLibrary,
    StickerProfile,
    infer_profile,
    parse_profile,
    query_has_semantic_evidence,
    render_profile,
)


class StickerProfileRenderingTests(unittest.TestCase):
    def test_render_keeps_objective_description_when_nothing_inferred(self) -> None:
        text = render_profile(StickerProfile(base="一只黄色柴犬坐在沙发上"))
        self.assertIn("一只黄色柴犬坐在沙发上", text)
        self.assertNotIn("情绪=", text)
        self.assertNotIn("场景=", text)
        self.assertNotIn("梗=", text)

    def test_render_encodes_emotion_scene_and_meme(self) -> None:
        text = render_profile(
            StickerProfile(
                base="熊猫头咧嘴大笑",
                emotion="开心",
                scenes=("庆祝", "鼓励"),
                meme="熊猫头",
            )
        )
        self.assertTrue(text.startswith("熊猫头咧嘴大笑"))
        self.assertIn("情绪=开心", text)
        self.assertIn("场景=庆祝,鼓励", text)
        self.assertIn("梗=熊猫头", text)

    def test_parse_round_trips_structured_description(self) -> None:
        text = render_profile(
            StickerProfile(
                base="熊猫头咧嘴大笑",
                emotion="开心",
                scenes=("庆祝",),
                meme="熊猫头",
            )
        )
        profile = parse_profile(text)
        self.assertEqual(profile.base, "熊猫头咧嘴大笑")
        self.assertEqual(profile.emotion, "开心")
        self.assertEqual(profile.scenes, ("庆祝",))
        self.assertEqual(profile.meme, "熊猫头")

    def test_parse_legacy_plain_description_is_backward_compatible(self) -> None:
        profile = parse_profile("黑白熊猫头咧嘴大笑，适合庆祝胜利")
        self.assertEqual(profile.base, "黑白熊猫头咧嘴大笑，适合庆祝胜利")
        # Legacy rows carry no structured fields; nothing may be invented.
        self.assertEqual(profile.emotion, "")
        self.assertEqual(profile.scenes, ())
        self.assertEqual(profile.meme, "")


class InferProfileTests(unittest.TestCase):
    def test_infer_extracts_emotion_and_scene_from_vision_text(self) -> None:
        profile = infer_profile("画面是一只熊猫头咧嘴大笑，用来庆祝胜利")
        self.assertEqual(profile.emotion, "开心")
        self.assertIn("庆祝", profile.scenes)
        self.assertIn("熊猫头", profile.base)

    def test_infer_never_invents_meme_when_text_has_none(self) -> None:
        profile = infer_profile("一条蓝色小鱼游过珊瑚礁")
        self.assertEqual(profile.meme, "")
        self.assertEqual(profile.emotion, "")

    def test_infer_annotates_serious_vision_text_as_sad(self) -> None:
        profile = infer_profile("一个人低头叹气，对告别感到难过")
        self.assertEqual(profile.emotion, "难过")
        self.assertIn("告别", profile.scenes)


class QueryEvidenceGateTests(unittest.TestCase):
    def test_serious_long_text_is_not_sticker_query_material(self) -> None:
        self.assertFalse(
            query_has_semantic_evidence("请立刻修好支付接口的报错，用户无法下单")
        )

    def test_warning_text_is_not_sticker_query_material(self) -> None:
        self.assertFalse(query_has_semantic_evidence("警告：再刷屏就禁言处理"))

    def test_short_emotional_query_is_sticker_query_material(self) -> None:
        self.assertTrue(query_has_semantic_evidence("开心"))
        self.assertTrue(query_has_semantic_evidence("无语"))

    def test_technical_query_is_not_sticker_query_material(self) -> None:
        self.assertFalse(query_has_semantic_evidence("check the database connection"))
        self.assertFalse(query_has_semantic_evidence("帮我看看这个 exception 堆栈"))


class StickerLibraryMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all, tables=[StickerLibraryRecord.__table__]
            )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.library = StickerLibrary(legacy_dir=Path(self.db_path).parent / "no-legacy")

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self.db_path}{suffix}")
            except OSError:
                pass

    async def _seed(self, **kwargs) -> None:
        async with self.sessions() as session:
            session.add(StickerLibraryRecord(group_id=-100, **kwargs))
            await session.commit()

    async def test_structured_emotion_match_returns_library_match(self) -> None:
        await self._seed(
            file_id="celebrate",
            emoji="🎉",
            description="熊猫头咧嘴大笑 | 情绪=开心 | 场景=庆祝,鼓励",
            aliases=["开心", "庆祝"],
            seen_count=4,
        )
        async with self.sessions() as session:
            picked = await self.library.pick_sticker(session, -100, query="开心")

        self.assertEqual(picked.file_id, "celebrate")
        self.assertEqual(picked.source, "library_match")

    async def test_alias_only_match_returns_library_match(self) -> None:
        await self._seed(
            file_id="shrug",
            description="一个人摊手耸肩 | 情绪=无语 | 场景=无奈",
            aliases=["无语", "摊手"],
            seen_count=2,
        )
        async with self.sessions() as session:
            picked = await self.library.pick_sticker(session, -100, query="摊手")

        self.assertEqual(picked.file_id, "shrug")
        self.assertEqual(picked.source, "library_match")

    async def test_legacy_plain_record_is_still_matchable(self) -> None:
        await self._seed(
            file_id="legacy",
            description="黑白熊猫头咧嘴大笑，适合庆祝胜利",
            aliases=["开心", "庆祝"],
            seen_count=3,
        )
        async with self.sessions() as session:
            picked = await self.library.pick_sticker(session, -100, query="庆祝")

        self.assertEqual(picked.file_id, "legacy")
        self.assertEqual(picked.source, "library_match")

    async def test_serious_warning_query_never_randomly_sends(self) -> None:
        await self._seed(
            file_id="clap",
            description="鼓掌 | 情绪=开心 | 场景=庆祝",
            aliases=["鼓掌", "庆祝"],
            seen_count=9,
        )
        async with self.sessions() as session:
            picked = await self.library.pick_sticker(
                session, -100, query="警告：再刷屏就禁言处理，请立刻停止"
            )

        self.assertEqual(picked.file_id, "")
        self.assertEqual(picked.source, "no_semantic_match")

    async def test_technical_long_query_never_randomly_sends(self) -> None:
        await self._seed(
            file_id="popcorn",
            description="吃瓜看戏 | 情绪=开心 | 场景=围观",
            aliases=["吃瓜", "围观"],
            seen_count=7,
        )
        async with self.sessions() as session:
            picked = await self.library.pick_sticker(
                session, -100, query="请检查数据库连接失败的原因并给出修复方案"
            )

        self.assertEqual(picked.file_id, "")
        self.assertEqual(picked.source, "no_semantic_match")

    async def test_empty_query_uses_explicit_fallback_pool(self) -> None:
        await self._seed(file_id="unused", description="无关素材", seen_count=1)
        async with self.sessions() as session:
            picked = await self.library.pick_sticker(
                session, -100, fallback_pool=["configured-sticker"]
            )

        self.assertEqual(picked.file_id, "configured-sticker")
        self.assertEqual(picked.source, "fallback_pool")

    async def test_empty_query_without_pool_does_not_guess(self) -> None:
        await self._seed(file_id="unused", description="无关素材", seen_count=1)
        async with self.sessions() as session:
            picked = await self.library.pick_sticker(session, -100)

        self.assertEqual(picked.file_id, "")
        self.assertEqual(picked.source, "no_semantic_match")

    async def test_learn_from_message_stores_structured_description(self) -> None:
        from types import SimpleNamespace

        message = SimpleNamespace(
            sticker=SimpleNamespace(
                file_id="learned-1",
                emoji="😂",
                set_name="panda",
            )
        )
        async with self.sessions() as session:
            learned = await self.library.learn_from_message(
                session,
                -100,
                message,
                vision_description="熊猫头咧嘴大笑，用来庆祝胜利",
            )
            await session.commit()

        self.assertIsNotNone(learned)
        assert learned is not None
        self.assertIn("情绪=开心", learned["description"])
        self.assertIn("庆祝", learned["aliases"])

    async def test_learn_preserves_old_description_as_alias(self) -> None:
        from types import SimpleNamespace

        message = SimpleNamespace(
            sticker=SimpleNamespace(file_id="learned-2", emoji="😀", set_name=""),
        )
        async with self.sessions() as session:
            await self.library.learn_from_message(
                session, -100, message, vision_description="一只柴犬在笑"
            )
            await session.commit()
        async with self.sessions() as session:
            learned = await self.library.learn_from_message(
                session,
                -100,
                message,
                vision_description="一只柴犬在沙发上大笑，非常开心",
            )
            await session.commit()

        self.assertIsNotNone(learned)
        assert learned is not None
        self.assertIn("一只柴犬在笑", learned["aliases"])


if __name__ == "__main__":
    unittest.main()
