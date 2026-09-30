"""审核上下文（C 方案）：让模型看到"这句话是在什么对话里说的"。

背景：孤立送审时，群里的日常话题词（存储/套餐/邀请/白名单/加我/丢包）会被读成引流，
真实事故里「先加我白名单」「先加我id 硬代码就行」都被判成广告并禁言了当事人。
这里测三件事：上下文怎么取、怎么渲染、有没有真的送进模型。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.db.engine import init_db
from bot.db.models import Group, GroupMessageArchive, ModerationRule
from bot.services.moderation_context import (
    MAX_CONTEXT_LINES,
    build_moderation_context,
    render_context_block,
)
from bot.utils.prompts import get_prompt


def _row(
    *,
    message_id: int,
    text: str,
    name: str = "群友",
    group_id: int = -100,
    sent_at: datetime | None = None,
    derived: str = "",
) -> GroupMessageArchive:
    return GroupMessageArchive(
        group_id=group_id,
        message_key=f"{group_id}:{message_id}",
        telegram_message_id=message_id,
        role="user",
        direction="inbound",
        sender_kind="user",
        sender_id=1000 + message_id,
        sender_username=f"u{message_id}",
        sender_display_name=name,
        message_type="text",
        content=text,
        raw_text=text,
        derived_text=derived,
        sent_at=sent_at or datetime(2026, 9, 29, 16, 0, 0),
    )


class ContextRenderTests(unittest.TestCase):
    def test_empty_context_says_so_instead_of_looking_like_a_message(self) -> None:
        block = render_context_block([])

        self.assertIn("没有可用的上下文", block)
        self.assertNotIn("\n", block)

    def test_replied_message_is_labelled_and_whitespace_is_collapsed(self) -> None:
        block = render_context_block(
            ["阿明: 晚上丢包如何"], reply_to="K.Barge:   能不能\n做个白名单"
        )

        self.assertIn("[被回复的消息] K.Barge: 能不能 做个白名单", block)
        self.assertIn("阿明: 晚上丢包如何", block)

    def test_long_context_is_truncated_from_the_front(self) -> None:
        lines = [f"群友: 第{i}条" + "x" * 200 for i in range(12)]

        block = render_context_block(lines)

        self.assertLessEqual(len(block), 1000)
        self.assertIn("第11条", block, "最新的对话必须留下")
        self.assertNotIn("第0条", block)


class BuildContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        from bot.services.authz import authorize_group

        async with self.session_factory() as session:
            session.add(Group(id=-100, title="测试群", settings={}))
            await authorize_group(session, -100, 1)
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _seed(self, *rows: GroupMessageArchive) -> None:
        async with self.session_factory() as session:
            for row in rows:
                session.add(row)
            await session.commit()

    async def test_context_lines_are_the_messages_before_this_one(self) -> None:
        base = datetime(2026, 9, 29, 16, 0, 0)
        await self._seed(
            _row(message_id=1, text="晚上丢包如何", name="PP", sent_at=base),
            _row(message_id=2, text="我这边还行", name="阿明", sent_at=base),
            _row(message_id=3, text="先加我白名单", name="PP", sent_at=base),
        )

        lines, block = await build_moderation_context(
            self._session(), group_id=-100, exclude_message_id=3, exclude_text="先加我白名单"
        )

        self.assertEqual(lines, ["PP: 晚上丢包如何", "阿明: 我这边还行"])
        self.assertIn("阿明: 我这边还行", block)
        self.assertNotIn("先加我白名单", block, "被审核的这条不该出现在上下文里")

    def _session(self):
        return self.session_factory()

    async def test_context_keeps_only_the_newest_lines(self) -> None:
        base = datetime(2026, 9, 29, 16, 0, 0)
        await self._seed(
            *[
                _row(
                    message_id=i,
                    text=f"第{i}句",
                    sent_at=base + timedelta(seconds=i),
                )
                for i in range(1, 20)
            ]
        )

        lines, _block = await build_moderation_context(
            self._session(), group_id=-100, exclude_message_id=99
        )

        self.assertEqual(len(lines), MAX_CONTEXT_LINES)
        self.assertTrue(lines[-1].endswith("第19句"))
        self.assertTrue(lines[0].endswith("第12句"))

    async def test_anchor_text_uses_the_conversation_before_that_message(self) -> None:
        base = datetime(2026, 9, 29, 16, 0, 0)
        await self._seed(
            _row(message_id=1, text="日常聊天", name="甲", sent_at=base),
            _row(message_id=2, text="先加我id 硬代码就行", name="PP", sent_at=base),
            _row(message_id=3, text="【管理员】已处理", name="机器人", sent_at=base),
            _row(message_id=4, text="好的", name="乙", sent_at=base),
        )

        lines, _block = await build_moderation_context(
            self._session(),
            group_id=-100,
            anchor_text="先加我id 硬代码就行\n[reply_to_user] id:601298409 username:@uxiner",
            exclude_text="先加我id 硬代码就行",
        )

        self.assertEqual(lines, ["甲: 日常聊天"], "只取被复核那条之前的对话")

    async def test_anchor_missing_falls_back_to_the_latest_lines(self) -> None:
        await self._seed(_row(message_id=1, text="乙: 随便聊聊", name="乙"))

        lines, _block = await build_moderation_context(
            self._session(), group_id=-100, anchor_text="归档里没有这句话"
        )

        self.assertEqual(len(lines), 1)

    async def test_broken_archive_read_returns_no_context_instead_of_raising(self) -> None:
        broken = SimpleNamespace(
            execute=AsyncMock(side_effect=RuntimeError("db is gone"))
        )

        lines, block = await build_moderation_context(
            broken, group_id=-100, exclude_message_id=1
        )

        self.assertEqual(lines, [])
        self.assertIn("没有可用的上下文", block)


class _FakeLLM:
    def __init__(self, reply: str = '{"violated": false, "confidence": 0.9, "reason": ""}') -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def moderation(self, system_prompt: str, user_input: str) -> str:
        self.calls.append((system_prompt, user_input))
        return self.reply


class EvaluateContextTests(unittest.IsolatedAsyncioTestCase):
    """上下文必须真的出现在送审内容里，否则这一整套都白做。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        from bot.config import Settings
        from bot.services.authz import authorize_group

        self.settings = Settings(_env_file=None)
        async with self.session_factory() as session:
            session.add(Group(id=-100, title="测试群", settings={}))
            await authorize_group(session, -100, 1)
            session.add(
                ModerationRule(
                    group_id=-100,
                    rule_type="llm",
                    pattern="禁止发布广告、推销与引流",
                    action="ban",
                    enabled=True,
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

    async def _evaluate(self, llm: _FakeLLM, *, context: str = ""):
        from bot.services.moderation import ModerationService

        service = ModerationService(self.settings.moderation, llm)
        async with self.session_factory() as session:
            with patch(
                "bot.services.moderation.get_prompt",
                return_value="rules:\n{rules_json}",
            ):
                return await service.evaluate(
                    session, -100, "先加我白名单", context=context
                )

    async def test_context_is_sent_as_untrusted_data_with_the_message(self) -> None:
        llm = _FakeLLM()

        await self._evaluate(llm, context="PP: 晚上丢包如何\n甲: 我这边还行")

        _system, user_input = llm.calls[-1]
        self.assertIn("<untrusted:群内上下文>", user_input)
        self.assertIn("PP: 晚上丢包如何", user_input)
        self.assertIn("<untrusted:待审核消息>", user_input)
        self.assertIn("先加我白名单", user_input)
        self.assertLess(
            user_input.index("群内上下文"), user_input.index("待审核消息"),
            "上下文要放在待审消息之前",
        )

    async def test_without_context_the_payload_is_unchanged(self) -> None:
        llm = _FakeLLM()

        await self._evaluate(llm)

        _system, user_input = llm.calls[-1]
        self.assertNotIn("群内上下文", user_input)
        self.assertIn("先加我白名单", user_input)

    async def test_context_does_not_change_local_regex_matching(self) -> None:
        from bot.services.moderation import ModerationService

        async with self.session_factory() as session:
            session.add(
                ModerationRule(
                    group_id=-100,
                    rule_type="regex",
                    pattern=r"加我白名单",
                    action="ban",
                    enabled=True,
                )
            )
            await session.commit()
        llm = _FakeLLM()
        service = ModerationService(self.settings.moderation, llm)

        async with self.session_factory() as session:
            # 上下文里有"加我白名单"，但待审消息本身干净 → 不能因为上下文而命中
            verdict = await service.evaluate(
                session, -100, "今天天气不错", context="PP: 加我白名单"
            )

        self.assertFalse(verdict.violated)
        # 本地正则一旦命中会直接返回、根本不调模型；这里模型被调用了，
        # 说明上下文里的"加我白名单"没有触发那条正则规则。
        self.assertTrue(llm.calls, "上下文不该被本地正则当成消息本身匹配")


class PromptContractTests(unittest.TestCase):
    def test_prompt_formats_and_tells_the_model_how_to_use_conversation(self) -> None:
        rendered = get_prompt("moderation").format(rules_json="[]")

        self.assertNotIn("{rules_json}", rendered, "占位符必须被替换掉")
        self.assertIn("RULES-FROM-DB", get_prompt("moderation").format(rules_json="RULES-FROM-DB"))
        self.assertIn("How to use the conversation and the message", rendered)
        for phrase in ("白名单", "加我 id", "丢包", "群内上下文"):
            self.assertIn(phrase, rendered)
        # 本群真实例子作为正/负样本写进了提示词：少了它们，判定会退回"见词就抓"
        for example in ("苹果18只要6k", "送ytb premium", "来做洗米", "晚上丢包如何"):
            self.assertIn(example, rendered)


if __name__ == "__main__":
    unittest.main()
