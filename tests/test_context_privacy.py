"""第 3 期 C 项 / **第 1 验收项**：群聊侧绝不许读到私聊正文。

用户口径（2026-10-03）：方向规则是单向的。

* **群 → 私聊：允许**（群里公开说的话可以进私聊，见 ``tests/test_group_public_context.py``）；
* **私聊 → 群：默认禁止，本期不实现读取。**

这个文件用一个**独特哨兵**把「禁止」钉死：先往私聊表写一行独有字符串
``SENTINEL_PRIVATE_ONLY_9f3a``，再**构造群聊上下文的各种分支**（有历史、有检索留档、
有记忆召回、超预算裁剪、开关打开），断言群聊 prompt 里**不出现**这个哨兵。

为了让「不出现」不是空话，同一份哨兵在私聊侧的装配里**必须出现**（正对照）：如果哪天
私聊装配坏了，这条用例会先失败，而不是让「群聊没有泄漏」变成一个永远为真的空断言。

另外还有一条结构断言：群聊侧的模块**根本不 import** 私聊表 / 私聊历史读取器——
隐私红线靠的是「读不到」，不是「读过之后过滤掉」。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import BotConfig
from bot.db.engine import init_db
from bot.handlers import group as group_handler
from bot.services import group_public_context as gpc
from bot.services import search_memory as sm
from bot.services import private_chat as dm
from bot.services.casual import CasualService
from bot.services.memory import MemoryService
from bot.services.skills.service import SkillService
from bot.utils.timezone import now_shanghai_naive

from sqlalchemy import select

from bot.db.models import PrivateChatMessage

GROUP_ID = -100777
OTHER_GROUP_ID = -100888
USER_ID = 777
#: 只存在于私聊表的独特哨兵（出现在群聊 prompt 里 = 泄漏）
SENTINEL = "SENTINEL_PRIVATE_ONLY_9f3a"
#: 正对照：群里检索留档的摘要（必须能出现在群聊 prompt 里）
GROUP_SEARCH_DIGEST = "GROUP_SCOPE_SEARCH_DIGEST_aa11"


def _llm_stub() -> SimpleNamespace:
    """与 ``tests/test_project_info.py`` 同一套模型配置替身（装配提示词要读它）。"""

    return SimpleNamespace(
        main=SimpleNamespace(model="main-model", fallbacks=[]),
        decision_config=SimpleNamespace(model="decision-model", fallbacks=[]),
        vision_config=SimpleNamespace(model="vision-model", fallbacks=[]),
        moderation_config=SimpleNamespace(model="moderation-model", fallbacks=[]),
        compress_config=SimpleNamespace(model="compress-model", fallbacks=[]),
        embed_config=SimpleNamespace(model="embed-model", fallbacks=[]),
    )


def _group_settings(*, max_context_tokens: int = 278528, reserve: int = 32768, **extra):
    bot = SimpleNamespace(
        max_context_tokens=max_context_tokens,
        group_history_reserve_tokens=reserve,
        group_history_token_budget=278528,
    )
    for key, value in extra.items():
        setattr(bot, key, value)
    return SimpleNamespace(bot=bot)


def _joined(messages: list[dict]) -> str:
    return "\n".join(str(item.get("content") or "") for item in messages)


class PrivateToGroupPrivacyTests(unittest.IsolatedAsyncioTestCase):
    """第 1 验收项。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.memory = MemoryService(
            BotConfig(),
            _llm_stub(),  # type: ignore[arg-type]
            session_factory=self.session_factory,
        )
        # 群里公开说过的一句话（群归档，允许被群聊与「群→私聊」读到）
        self.memory._archive_last_pruned_at[GROUP_ID] = time.monotonic()
        await self.memory.archive_message(
            GROUP_ID,
            "user",
            "群里公开聊到 5090 的行情",
            message_id="g1",
            telegram_message_id=1,
            created_at=now_shanghai_naive() - timedelta(minutes=5),
            sender_id=924,
            sender_display_name="群友",
            message_type="text",
            raw_text="群里公开聊到 5090 的行情",
        )
        # 1) 私聊正文：哨兵落在 private_chat_messages（群聊侧绝不许读）
        async with self.session_factory() as session:
            await dm.record_private_turn(
                session,
                user_id=USER_ID,
                user_content=SENTINEL,
                assistant_content=f"{SENTINEL}-这只有私聊看得到",
                message_id=1,
            )
        # 2) 私聊检索留档：摘要里也带哨兵（作用域是 private）
        async with self.session_factory() as session:
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_PRIVATE,
                scope_id=USER_ID,
                query="5090 价格",
                digest=SENTINEL,
                kind=sm.KIND_PRICE,
                outcome=sm.OUTCOME_OK,
            )
            # 3) 群检索留档（正对照：必须能进群聊 prompt）
            await sm.record_search_result(
                session,
                scope=sm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                query="5090 价格",
                digest=GROUP_SEARCH_DIGEST,
                kind=sm.KIND_PRICE,
                outcome=sm.OUTCOME_OK,
            )

    async def asyncTearDown(self) -> None:
        await self.memory.shutdown(timeout_seconds=1.0)
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    # -- 构造群聊上下文的两个真实装配点 -------------------------------------

    async def _group_history(self, settings) -> list[dict]:
        """群聊这一轮的历史：归档装配 + 记忆召回 + 检索留档（第 3 期）。"""

        rows = await self.memory.load_group_history_by_budget(GROUP_ID)
        history = await self.memory.get_history_for_llm(
            GROUP_ID,
            history_rows=rows,
            recall_query="5090 行情",
        )
        return await group_handler._inject_group_search_records(
            history=history,
            group_id=GROUP_ID,
            memory=self.memory,
            settings=settings,
        )

    def _skill_prompt(self, history: list[dict]) -> list[dict]:
        service = SkillService(_llm_stub())  # type: ignore[arg-type]
        return service.build_answer_prompt_payload(
            "5090 行情怎么样", history=history
        )["messages"]

    def _casual_prompt(self, history: list[dict]) -> list[dict]:
        service = CasualService(_llm_stub(), settings=None, skill_names=[])
        return service._build_messages_from_normalized_input(
            "5090 行情怎么样",
            history=history,
            sender_user_id=USER_ID,
            sender_username="member",
            sender_is_owner=False,
            sender_is_tg_admin=False,
            intent_type="casual",
            merged_count=1,
            merged_context="",
            reply_targets_context="",
            input_limit=1000,
        )

    # -- 验收断言 -----------------------------------------------------------

    async def test_group_prompt_never_contains_the_private_sentinel(self) -> None:
        settings = _group_settings()
        history = await self._group_history(settings)
        prompts = {
            "skills": self._skill_prompt(history),
            "casual": self._casual_prompt(history),
            "history_blocks": history,
        }
        for name, messages in prompts.items():
            with self.subTest(branch=name):
                gpc.assert_no_private_content(
                    _joined(messages), private_markers=[SENTINEL]
                )

    async def test_group_search_records_do_reach_the_group_prompt(self) -> None:
        """正对照：群聊留档必须真的进 prompt，否则上面的「没有哨兵」是空的。"""

        settings = _group_settings()
        history = await self._group_history(settings)
        self.assertIn(GROUP_SEARCH_DIGEST, _joined(history))
        self.assertIn(GROUP_SEARCH_DIGEST, _joined(self._skill_prompt(history)))
        self.assertIn(GROUP_SEARCH_DIGEST, _joined(self._casual_prompt(history)))

    async def test_sentinel_is_absent_even_when_the_budget_forces_trimming(self) -> None:
        """超预算裁剪这一分支：裁到只剩固定层，也不能把私聊内容裁出来。"""

        # 预算必须真的触发裁剪：业务总预算 1025、预留 1024（合法且几乎不留输入空间）。
        settings = _group_settings(max_context_tokens=1025, reserve=1024)
        observed: list[object] = []
        real_assemble = group_handler.assemble_context_within_budget

        def _spy(*args, **kwargs):
            assembly = real_assemble(*args, **kwargs)
            observed.append(assembly)
            return assembly

        with patch.object(
            group_handler, "assemble_context_within_budget", side_effect=_spy
        ):
            history = await self._group_history(settings)
        self.assertTrue(observed, "这条分支必须真的走到统一闸门")
        self.assertTrue(
            any(assembly.trims for assembly in observed),
            "预算必须真的触发了裁剪（否则这条分支没被覆盖）",
        )
        gpc.assert_no_private_content(
            _joined(self._skill_prompt(history)), private_markers=[SENTINEL]
        )
        gpc.assert_no_private_content(
            _joined(self._casual_prompt(history)), private_markers=[SENTINEL]
        )

    async def test_recall_layer_branch_keeps_sentinel_out(self) -> None:
        """带记忆召回索引这一分支：拆分「历史 / 召回」两层后拼接，也不能带出私聊内容。

        ``get_history_for_llm`` 把召回索引固定加在尾部（第 2 期口径），这里用一条带
        ``memory_source="recalled_archive_index"`` 的合成消息显式覆盖该分支。
        """

        settings = _group_settings()
        recall = {
            "role": "user",
            "content": "[RECALLED_MEMORY_INDEX]\n- 群里以前聊过显卡",
            "memory_source": "recalled_archive_index",
        }
        history = [
            {"role": "user", "content": "群里的历史消息"},
            recall,
        ]
        merged = await group_handler._inject_group_search_records(
            history=history,
            group_id=GROUP_ID,
            memory=self.memory,
            settings=settings,
        )
        joined = _joined(merged)
        gpc.assert_no_private_content(joined, private_markers=[SENTINEL])
        self.assertIn("群里的历史消息", joined)
        self.assertIn("[RECALLED_MEMORY_INDEX]", joined)
        self.assertEqual(
            str(merged[-1].get("memory_source") or ""),
            "recalled_archive_index",
            "召回索引仍然固定在尾部（第 2 期口径不变）",
        )

    async def test_turning_the_switch_on_still_reads_nothing(self) -> None:
        """本期不实现打开后的读取：开关为 True 也不会有任何私聊正文进群聊。"""

        settings = _group_settings(group_can_read_private_history=True)
        self.assertTrue(sm.group_can_read_private_history(settings))
        history = await self._group_history(settings)
        for messages in (history, self._skill_prompt(history), self._casual_prompt(history)):
            gpc.assert_no_private_content(
                _joined(messages), private_markers=[SENTINEL]
            )

    async def test_the_same_sentinel_is_visible_on_the_private_side(self) -> None:
        """正对照：哨兵在私聊装配里**必须出现**，证明上面的断言不是恒真。"""

        async with self.session_factory() as session:
            history = await dm.load_private_history(session, USER_ID)
            records = await sm.load_search_records(
                session, scope=sm.SCOPE_PRIVATE, scope_id=USER_ID
            )
        messages = dm.build_private_chat_messages(
            "5090 行情怎么样",
            history=history,
            sender_user_id=USER_ID,
            search_records=records,
        )
        self.assertIn(SENTINEL, _joined(messages))

    async def test_private_rows_are_invisible_from_the_group_scope(self) -> None:
        async with self.session_factory() as session:
            group_rows = await sm.load_search_records(
                session, scope=sm.SCOPE_GROUP, scope_id=GROUP_ID
            )
            private_rows = await session.execute(
                select(PrivateChatMessage.content).where(
                    PrivateChatMessage.user_id == USER_ID
                )
            )
        self.assertEqual(
            [row["digest"] for row in group_rows], [GROUP_SEARCH_DIGEST]
        )
        # 哨兵确实只躺在私聊表里（数据侧隔离）
        self.assertTrue(
            any(SENTINEL in str(value) for (value,) in private_rows.all())
        )

    async def test_group_public_context_only_reads_the_users_groups(self) -> None:
        """B 项允许的那一半：只读调用方给的群，且标注来源。"""

        records = await gpc.load_user_public_group_context(
            query="5090 行情",
            group_ids=[GROUP_ID],
            titles={GROUP_ID: "显卡群"},
            memory=self.memory,
        )
        self.assertTrue(records)
        block = gpc.render_group_public_block(records, titles={GROUP_ID: "显卡群"})
        self.assertIn("[群聊公开记录 · 显卡群/-100777]", block)
        gpc.assert_no_private_content(block, private_markers=[SENTINEL])

    async def test_group_public_context_ignores_groups_the_user_cannot_access(self) -> None:
        self.memory._archive_last_pruned_at[OTHER_GROUP_ID] = time.monotonic()
        await self.memory.archive_message(
            OTHER_GROUP_ID,
            "user",
            "别的群的公开内容",
            message_id="o1",
            telegram_message_id=1,
            created_at=now_shanghai_naive() - timedelta(minutes=1),
            sender_id=925,
            sender_display_name="别人",
            message_type="text",
            raw_text="别的群的公开内容",
        )
        records = await gpc.load_user_public_group_context(
            query="5090 行情", group_ids=[GROUP_ID], memory=self.memory
        )
        self.assertTrue(
            all(record["group_id"] == GROUP_ID for record in records),
            "不在 group_ids 里的群，一个字都不能读",
        )


class StructuralIsolationTests(unittest.TestCase):
    """结构断言：群聊侧根本不 import 私聊表 / 私聊历史读取器。"""

    #: 群聊装配路径上的模块（含本期新增的统一闸门/留档/公开记录）
    GROUP_SIDE_FILES = (
        "bot/handlers/group.py",
        "bot/services/casual.py",
        "bot/services/memory.py",
        "bot/services/skills/service.py",
        "bot/services/context_gate.py",
        "bot/services/search_memory.py",
        "bot/services/group_public_context.py",
    )

    def _source(self, relative: str) -> str:
        return (Path(__file__).resolve().parents[1] / relative).read_text(
            encoding="utf-8"
        )

    def test_group_side_modules_never_import_the_private_history(self) -> None:
        for relative in self.GROUP_SIDE_FILES:
            with self.subTest(file=relative):
                source = self._source(relative)
                self.assertNotIn(
                    "PrivateChatMessage",
                    source,
                    "群聊侧不许碰私聊表（隐私红线：私聊 → 群默认禁止）",
                )
                for forbidden in (
                    "load_private_history",
                    "record_private_turn",
                    "private_chat_messages",
                ):
                    # group_public_context/search_memory 的 docstring 里会提到表名，
                    # 但**不许**出现可执行的引用（import / 调用）
                    self.assertNotIn(
                        f"import {forbidden}",
                        source,
                        f"{relative} 不该 import {forbidden}",
                    )
                    self.assertNotIn(
                        f"{forbidden}(",
                        source,
                        f"{relative} 不该调用 {forbidden}",
                    )

    def test_search_memory_scope_is_an_hard_boundary(self) -> None:
        """留档按作用域读取：群聊侧的读取器只有 scope=group 一条路。"""

        source = self._source("bot/services/search_memory.py")
        self.assertIn("SearchResultRecord.scope == normalized_scope", source)
        group_source = self._source("bot/handlers/group.py")
        self.assertIn("SCOPE_GROUP", group_source)
        self.assertNotIn("SCOPE_PRIVATE", group_source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
