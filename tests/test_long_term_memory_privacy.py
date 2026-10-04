"""第 4 期 / **第 1 验收项**：长期记忆也不许把私聊事实带进群聊。

第 3 期已经钉死「群聊侧不读私聊正文」（``tests/test_context_privacy.py``）。第 4 期
新增的 ``user_facts`` 是**同一类风险的第二条通道**：从私聊里提炼出来的稳定事实
（口味、身份、关系…）比原始消息更「精炼」，一旦泄漏进群聊 prompt，危害只会更大。

这个文件用与第 3 期**完全相同的手法**再钉一次：

* 往私聊作用域写一条带独特哨兵 ``SENTINEL_PRIVATE_ONLY_9f3a`` 的事实；
* 构造群聊上下文的各个分支（有检索留档、有长期记忆、超预算裁剪、方向开关打开），
  断言群聊 prompt 里**绝无**这个哨兵；
* 正对照两条：同一哨兵在**私聊装配**里必须出现；同一查询下**群作用域**的事实必须
  真的进了群聊 prompt（否则「没有哨兵」就是一句恒真的空话）。
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
from bot.services import long_term_memory as ltm
from bot.services import private_chat as dm
from bot.services.casual import CasualService
from bot.services.memory import MemoryService
from bot.services.skills.service import SkillService
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -100777
USER_ID = 777
#: 只存在于私聊作用域的独特哨兵（出现在群聊 prompt 里 = 泄漏）
SENTINEL = "SENTINEL_PRIVATE_ONLY_9f3a"
#: 正对照：群作用域的事实（必须能出现在群聊 prompt 里）
GROUP_FACT = "GROUP_SCOPE_FACT_aa11"
#: 一句话查询，同时能命中私聊哨兵事实与群事实（靠「咖啡」「显卡」两个关键词）
QUERY = "咖啡 显卡"
PRIVATE_FACT_TEXT = f"{SENTINEL} 喜欢喝咖啡"
GROUP_FACT_TEXT = f"{GROUP_FACT} 群里都爱聊显卡"


def _llm_stub() -> SimpleNamespace:
    """与 ``tests/test_context_privacy.py`` 同一套模型配置替身。"""

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
        memory_facts_enabled=True,
        memory_extract_enabled=True,
        memory_extract_interval_minutes=30,
        memory_extract_min_messages=20,
        memory_extract_daily_cap=48,
        memory_extract_batch_max=200,
        memory_tool_enabled=True,
        memory_tool_daily_cap=30,
        memory_recall_limit=8,
        memory_event_ttl_days=30,
        memory_deleted_retention_days=30,
    )
    for key, value in extra.items():
        setattr(bot, key, value)
    return SimpleNamespace(bot=bot)


def _joined(messages: list[dict]) -> str:
    return "\n".join(str(item.get("content") or "") for item in messages)


class LongTermMemoryPrivacyTests(unittest.IsolatedAsyncioTestCase):
    """第 1 验收项：私聊 → 群，绝对禁止。"""

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
        self.memory._archive_last_pruned_at[GROUP_ID] = time.monotonic()
        await self.memory.archive_message(
            GROUP_ID,
            "user",
            "群里公开聊到 5090 的显卡行情",
            message_id="g1",
            telegram_message_id=1,
            created_at=now_shanghai_naive() - timedelta(minutes=5),
            sender_id=924,
            sender_display_name="群友",
            message_type="text",
            raw_text="群里公开聊到 5090 的显卡行情",
        )
        # 1) 私聊作用域的长期记忆（哨兵在这里；群聊侧绝不许读到）
        async with self.session_factory() as session:
            await ltm.record_fact(
                session,
                scope=ltm.SCOPE_PRIVATE,
                scope_id=USER_ID,
                subject_user_id=USER_ID,
                fact_text=PRIVATE_FACT_TEXT,
                category=ltm.CATEGORY_PREFERENCE,
                confidence=70,
                source_kind=ltm.SOURCE_TOOL,
                evidence_excerpt=SENTINEL,
            )
            # 2) 群作用域的长期记忆（正对照：必须能进群聊 prompt）
            await ltm.record_fact(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=0,
                fact_text=GROUP_FACT_TEXT,
                category=ltm.CATEGORY_PREFERENCE,
                confidence=70,
                source_kind=ltm.SOURCE_PASSIVE,
                evidence_excerpt=GROUP_FACT,
            )

    async def asyncTearDown(self) -> None:
        await self.memory.shutdown(timeout_seconds=1.0)
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    # -- 构造群聊上下文 -----------------------------------------------------

    async def _group_history(self, settings) -> list[dict]:
        rows = await self.memory.load_group_history_by_budget(GROUP_ID)
        history = await self.memory.get_history_for_llm(
            GROUP_ID,
            history_rows=rows,
            recall_query="显卡行情",
        )
        history = await group_handler._inject_group_search_records(
            history=history,
            group_id=GROUP_ID,
            memory=self.memory,
            settings=settings,
        )
        return await group_handler._inject_group_long_term_memory(
            history=history,
            group_id=GROUP_ID,
            speaker_user_id=USER_ID,
            query=QUERY,
            memory=self.memory,
            settings=settings,
        )

    def _skill_prompt(self, history: list[dict]) -> list[dict]:
        service = SkillService(_llm_stub())  # type: ignore[arg-type]
        return service.build_answer_prompt_payload(
            "咖啡和显卡聊聊", history=history
        )["messages"]

    def _casual_prompt(self, history: list[dict]) -> list[dict]:
        service = CasualService(_llm_stub(), settings=None, skill_names=[])
        return service._build_messages_from_normalized_input(
            "咖啡和显卡聊聊",
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

    async def test_group_prompt_never_contains_the_private_fact(self) -> None:
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

    async def test_group_scope_facts_do_reach_the_group_prompt(self) -> None:
        """正对照：群作用域的事实必须真的进 prompt，否则上面的断言是空的。"""

        settings = _group_settings()
        history = await self._group_history(settings)
        self.assertIn(GROUP_FACT, _joined(history))
        self.assertIn(GROUP_FACT, _joined(self._skill_prompt(history)))
        self.assertIn(GROUP_FACT, _joined(self._casual_prompt(history)))

    async def test_sentinel_absent_even_when_the_budget_forces_trimming(self) -> None:
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

    async def test_turning_the_direction_switch_on_reads_nothing(self) -> None:
        settings = _group_settings(group_can_read_private_history=True)
        history = await self._group_history(settings)
        for messages in (
            history,
            self._skill_prompt(history),
            self._casual_prompt(history),
        ):
            gpc.assert_no_private_content(
                _joined(messages), private_markers=[SENTINEL]
            )

    async def test_the_same_sentinel_is_visible_on_the_private_side(self) -> None:
        """正对照：哨兵事实在私聊装配里**必须出现**。"""

        async with self.session_factory() as session:
            history = await dm.load_private_history(session, USER_ID)
            facts = await ltm.load_private_chat_facts(
                session,
                user_id=USER_ID,
                group_ids=[],
                query=QUERY,
                limit=8,
            )
        self.assertTrue(facts, "私聊作用域里必须有这条事实")
        messages = dm.build_private_chat_messages(
            QUERY,
            history=history,
            sender_user_id=USER_ID,
            long_term_facts=facts,
        )
        self.assertIn(SENTINEL, _joined(messages))

    async def test_private_scope_rows_are_invisible_from_the_group_scope(self) -> None:
        async with self.session_factory() as session:
            group_rows = await ltm.load_relevant_facts(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=[0, USER_ID],
                query=QUERY,
            )
            private_rows = await ltm.load_relevant_facts(
                session,
                scope=ltm.SCOPE_PRIVATE,
                scope_id=USER_ID,
                subject_user_id=USER_ID,
                query=QUERY,
            )
        self.assertEqual(
            [row["fact_text"] for row in group_rows], [GROUP_FACT_TEXT]
        )
        self.assertEqual(
            [row["fact_text"] for row in private_rows], [PRIVATE_FACT_TEXT]
        )


class StructuralIsolationTests(unittest.TestCase):
    """结构断言：群聊侧根本不 import 长期记忆的私聊读取器。"""

    ROOT = Path(__file__).resolve().parents[1]

    def _source(self, relative: str) -> str:
        return (self.ROOT / relative).read_text(encoding="utf-8")

    def test_group_handler_only_reads_the_group_scope(self) -> None:
        source = self._source("bot/handlers/group.py")
        self.assertIn("_inject_group_long_term_memory", source)
        self.assertIn("SCOPE_GROUP", source)
        for forbidden in (
            "SCOPE_PRIVATE",
            "PrivateChatMessage",
            "load_private_chat_facts",
            "private_chat_messages",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_group_injection_never_asks_for_private_scope(self) -> None:
        source = self._source("bot/handlers/group.py")
        injection = source.split("async def _inject_group_long_term_memory", 1)[1]
        injection = injection.split("\nasync def ", 1)[0]
        self.assertIn("scope=SCOPE_GROUP", injection)
        self.assertNotIn("SCOPE_PRIVATE", injection)

    def test_service_module_keeps_the_scope_boundary_in_the_query(self) -> None:
        source = self._source("bot/services/long_term_memory.py")
        self.assertIn("UserFact.scope == normalized_scope", source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
