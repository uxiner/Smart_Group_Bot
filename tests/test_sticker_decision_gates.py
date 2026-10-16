from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.services.sticker_decision import StickerDecisionService, _RATIO_BASELINE
from bot.services.sticker_library import sticker_library

_GROUP_ID = 771001


class _CaptureGenerateLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0
        self.system = ""
        self.prompt = ""

    async def generate(self, system: str, prompt: str) -> str:
        self.calls += 1
        self.system = system
        self.prompt = prompt
        return self.response


class _StickerDecisionGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _RATIO_BASELINE[_GROUP_ID] = (0, 0)

    def tearDown(self) -> None:
        _RATIO_BASELINE.pop(_GROUP_ID, None)

    async def _decide(
        self,
        llm: _CaptureGenerateLLM,
        *,
        action: str = "casual",
        user_text: str = "来点气氛",
        assistant_reply: str = "这条我接一下",
        msg_type: str = "text",
        candidates: list[dict] | None = None,
    ):
        service = StickerDecisionService(llm)
        rows = candidates if candidates is not None else [
            {
                "file_id": "file_1",
                "emoji": "🙂",
                "set_name": "test",
                "description": "开心 | 情绪=开心 | 场景=庆祝",
                "aliases": ["开心"],
                "seen_count": 1,
                "sent_count": 0,
                "source": "learned",
            }
        ]
        with (
            patch.object(
                sticker_library, "total_sent_count", new=AsyncMock(return_value=0)
            ),
            patch.object(
                sticker_library, "list_candidates", new=AsyncMock(return_value=rows)
            ),
            patch.object(
                sticker_library,
                "pick_sticker",
                new=AsyncMock(return_value=SimpleNamespace(file_id="", source="no_semantic_match")),
            ),
        ):
            return await service.decide(
                session=SimpleNamespace(),
                group_id=_GROUP_ID,
                action=action,
                msg_type=msg_type,
                is_mentioned=True,
                is_reply_to_bot=False,
                user_text=user_text,
                assistant_reply=assistant_reply,
                reply_source="casual",
                assistant_reply_count=5,
            )

    async def test_skip_action_never_sends_even_when_model_says_send(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": true, "sticker_file_id": "file_1", "query": "开心", "reason": "celebrate"}'
        )
        result = await self._decide(llm, action="skip")
        self.assertFalse(result.send)
        self.assertEqual(llm.calls, 0)

    async def test_serious_warning_never_sends(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": true, "sticker_file_id": "file_1", "query": "开心", "reason": "celebrate"}'
        )
        result = await self._decide(
            llm,
            user_text="警告：再刷屏就禁言处理，请立刻停止",
            assistant_reply="收到，已记录这次警告。",
        )
        self.assertFalse(result.send)
        self.assertEqual(result.reason, "serious_context")
        self.assertEqual(llm.calls, 0)

    async def test_technical_query_never_sends(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": true, "sticker_file_id": "file_1", "query": "开心", "reason": "celebrate"}'
        )
        result = await self._decide(
            llm,
            user_text="请检查数据库连接失败的原因并给出修复方案",
            assistant_reply="连接池耗尽了，需要调大上限。",
        )
        self.assertFalse(result.send)
        self.assertEqual(result.reason, "serious_context")
        self.assertEqual(llm.calls, 0)

    async def test_long_informational_draft_never_sends(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": true, "sticker_file_id": "file_1", "query": "开心", "reason": "celebrate"}'
        )
        long_reply = "关于这个问题的完整说明如下：" + ("细节内容。" * 60)
        result = await self._decide(llm, assistant_reply=long_reply)
        self.assertFalse(result.send)
        self.assertEqual(result.reason, "long_reply")
        self.assertEqual(llm.calls, 0)

    async def test_empty_candidates_never_sends(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": true, "sticker_file_id": "ghost", "query": "开心", "reason": "celebrate"}'
        )
        result = await self._decide(llm, candidates=[])
        self.assertFalse(result.send)
        self.assertEqual(result.reason, "no_candidates")
        self.assertEqual(llm.calls, 0)

    async def test_model_file_id_outside_candidates_is_rejected(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": true, "sticker_file_id": "ghost", "query": "", "reason": "celebrate"}'
        )
        result = await self._decide(llm)
        self.assertFalse(result.send)

    async def test_semantic_miss_refuses_to_send(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": true, "sticker_file_id": "", "query": "庆祝", "reason": "celebrate"}'
        )
        result = await self._decide(llm)
        self.assertFalse(result.send)
        self.assertEqual(result.reason, "no_semantic_match")

    async def test_semantic_hit_sends(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": true, "sticker_file_id": "", "query": "开心", "reason": "celebrate"}'
        )
        service = StickerDecisionService(llm)
        with (
            patch.object(
                sticker_library, "total_sent_count", new=AsyncMock(return_value=0)
            ),
            patch.object(
                sticker_library,
                "list_candidates",
                new=AsyncMock(
                    return_value=[
                        {
                            "file_id": "file_1",
                            "emoji": "🙂",
                            "set_name": "test",
                            "description": "开心 | 情绪=开心 | 场景=庆祝",
                            "aliases": ["开心"],
                            "seen_count": 1,
                            "sent_count": 0,
                            "source": "learned",
                        }
                    ]
                ),
            ),
            patch.object(
                sticker_library,
                "pick_sticker",
                new=AsyncMock(
                    return_value=SimpleNamespace(file_id="file_1", source="library_match")
                ),
            ),
        ):
            result = await service.decide(
                session=SimpleNamespace(),
                group_id=_GROUP_ID,
                action="casual",
                msg_type="text",
                is_mentioned=True,
                is_reply_to_bot=False,
                user_text="今天好开心",
                assistant_reply="哈哈哈",
                reply_source="casual",
                assistant_reply_count=5,
            )
        self.assertTrue(result.send)
        self.assertEqual(result.sticker_file_id, "file_1")

    async def test_candidates_prompt_exposes_structured_fields(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": false, "sticker_file_id": "", "query": "", "reason": "skip"}'
        )
        await self._decide(llm)
        self.assertIn("情绪=开心", llm.prompt)
        self.assertIn("[STICKER_CANDIDATES]", llm.prompt)
        # 候选必须声明素材类型，否则决策无法区分贴纸与图片/动图。
        self.assertIn('"media_type": "sticker"', llm.prompt)

    async def test_candidates_prompt_states_photo_and_animation_media_type(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": false, "sticker_file_id": "", "query": "", "reason": "skip"}'
        )
        await self._decide(
            llm,
            candidates=[
                {
                    "file_id": "photo_1",
                    "description": "橘猫打字 | 情绪=无语 | 场景=吐槽",
                    "aliases": ["无语"],
                    "seen_count": 1,
                    "sent_count": 0,
                    "source": "group_media",
                    "media_type": "photo",
                },
                {
                    "file_id": "anim_1",
                    "description": "转圈狗 | 情绪=开心 | 场景=庆祝",
                    "aliases": ["开心"],
                    "seen_count": 1,
                    "sent_count": 0,
                    "source": "group_media",
                    "media_type": "animation",
                },
            ],
        )
        self.assertIn('"media_type": "photo"', llm.prompt)
        self.assertIn('"media_type": "animation"', llm.prompt)

    async def test_unknown_candidate_media_type_reads_as_sticker(self) -> None:
        llm = _CaptureGenerateLLM(
            '{"send": false, "sticker_file_id": "", "query": "", "reason": "skip"}'
        )
        await self._decide(
            llm,
            candidates=[
                {
                    "file_id": "legacy_1",
                    "description": "旧数据",
                    "aliases": [],
                    "seen_count": 1,
                    "sent_count": 0,
                    "source": "learned",
                    "media_type": "video",
                }
            ],
        )
        self.assertIn('"media_type": "sticker"', llm.prompt)


if __name__ == "__main__":
    unittest.main()
