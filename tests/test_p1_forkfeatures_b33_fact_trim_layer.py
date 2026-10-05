"""修复批 P1-2 / B-33：群聊侧的长期记忆注入必须落在**记忆召回层**。

复现的原缺陷（``AUDIT-B`` B-33）：

``handlers/group.py:_inject_group_long_term_memory`` 把 ``render_facts_block`` 的结果
塞回 ``history``，但**不打来源标记**。最终请求闸门（``services/payload_fit.py``）靠
``context_layer_for_history_row`` 给每条历史分层，缺标记的事实行只能按 ``role`` 落到
``LAYER_HISTORY``——被排在**最优先**裁掉的一层，与文档承诺的
「历史 → 检索留档 → 记忆召回」顺序正好相反；极端情况下整轮资料都为了保住事实而被丢光。

断言分三层，缺一不可：

1. ``payload_fit`` 的来源映射：``long_term_fact`` → ``LAYER_MEMORY_RECALL``；
2. 群聊注入函数产出的每条事实行都带 ``memory_source='long_term_fact'``；
3. 端到端过 ``skills/service.py`` 的 ``tag_rendered_history`` 之后，事实行的层标记
   仍然是 ``memory_recall``（而不是 ``history`` / ``core``）。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace

from bot.config import BotConfig
from bot.db.engine import init_db
from bot.db.models import SearchResultRecord
from bot.handlers import group as group_handler
from bot.services import long_term_memory as ltm
from bot.services import search_memory as sm
from bot.services.memory import MemoryService
from bot.services.payload_fit import (
    LAYER_CORE,
    LAYER_HISTORY,
    LAYER_MEMORY_RECALL,
    LAYER_SEARCH_RECORDS,
    context_layer_for_history_row,
    message_context_layer,
    tag_rendered_history,
)
from bot.services.skills.service import SkillService
from bot.utils.security import sanitize_history_for_llm
from bot.utils.timezone import now_shanghai_naive

GROUP_ID = -100777
USER_ID = 777
FACT_TEXT = "群里都爱聊显卡行情"
QUERY = "咖啡 显卡"
#: 检索留档的哨兵：事实行与它抢预算时，事实必须活到最后。
SEARCH_SENTINEL = "SEARCH_RECORD_SENTINEL_bb22"


def _llm_stub() -> SimpleNamespace:
    return SimpleNamespace(
        main=SimpleNamespace(model="main-model", fallbacks=[]),
        decision_config=SimpleNamespace(model="decision-model", fallbacks=[]),
        vision_config=SimpleNamespace(model="vision-model", fallbacks=[]),
        moderation_config=SimpleNamespace(model="moderation-model", fallbacks=[]),
        compress_config=SimpleNamespace(model="compress-model", fallbacks=[]),
        embed_config=SimpleNamespace(model="embed-model", fallbacks=[]),
    )


def _group_settings(**extra) -> SimpleNamespace:
    bot = SimpleNamespace(
        max_context_tokens=278528,
        group_history_reserve_tokens=32768,
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


class SourceMappingTests(unittest.TestCase):
    """纯函数层：来源标记 → 裁剪层。"""

    def test_long_term_fact_maps_to_the_memory_recall_layer(self) -> None:
        self.assertEqual(
            context_layer_for_history_row(
                {"role": "user", "memory_source": "long_term_fact"}
            ),
            LAYER_MEMORY_RECALL,
        )

    def test_untouched_mapping_stays_unchanged(self) -> None:
        self.assertEqual(
            context_layer_for_history_row({"role": "user"}), LAYER_HISTORY
        )
        self.assertEqual(
            context_layer_for_history_row(
                {"role": "user", "memory_source": "search_record"}
            ),
            LAYER_SEARCH_RECORDS,
        )
        self.assertEqual(
            context_layer_for_history_row(
                {"role": "user", "memory_source": "recalled_archive_index"}
            ),
            LAYER_MEMORY_RECALL,
        )
        self.assertEqual(context_layer_for_history_row({"role": "system"}), LAYER_CORE)


class GroupInjectionLayerTests(unittest.IsolatedAsyncioTestCase):
    """集成层：群聊注入产出的事实行必须带来源标记并被正确分层。"""

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
        async with self.session_factory() as session:
            session.add(
                SearchResultRecord(
                    scope=sm.SCOPE_GROUP,
                    scope_id=GROUP_ID,
                    query=QUERY,
                    digest=SEARCH_SENTINEL,
                    sources=[],
                    kind="fact",
                    outcome="ok",
                    created_at=now_shanghai_naive() - timedelta(minutes=1),
                )
            )
            await ltm.record_fact(
                session,
                scope=ltm.SCOPE_GROUP,
                scope_id=GROUP_ID,
                subject_user_id=0,
                fact_text=FACT_TEXT,
                category=ltm.CATEGORY_PREFERENCE,
                confidence=70,
                source_kind=ltm.SOURCE_PASSIVE,
                evidence_excerpt="群里都爱聊显卡",
            )
            await session.commit()
        await self.memory.archive_message(
            GROUP_ID,
            "user",
            SEARCH_SENTINEL,
            message_id="m1",
            telegram_message_id=1,
            created_at=now_shanghai_naive() - timedelta(minutes=1),
            sender_id=USER_ID,
            sender_display_name="成员",
            message_type="text",
            raw_text=SEARCH_SENTINEL,
        )

    async def asyncTearDown(self) -> None:
        await self.memory.shutdown(timeout_seconds=1.0)
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _injected_history(self) -> list[dict]:
        settings = _group_settings()
        rows = await self.memory.load_group_history_by_budget(GROUP_ID)
        history = await self.memory.get_history_for_llm(
            GROUP_ID, history_rows=rows, recall_query=QUERY
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

    async def test_fact_rows_carry_the_long_term_fact_source(self) -> None:
        history = await self._injected_history()
        sources = [
            str(item.get("memory_source") or "")
            for item in history
            if FACT_TEXT in str(item.get("content") or "")
        ]
        self.assertTrue(sources, "长期记忆事实必须真的注入群聊历史（否则本用例是空话）")
        self.assertTrue(
            all(source == "long_term_fact" for source in sources),
            f"事实行必须打 long_term_fact 标记，实际={sources}",
        )

    async def test_facts_survive_the_skill_prompt_layer_tagging(self) -> None:
        """端到端：过 ``skills/service.py`` 的打层之后仍在记忆召回层。"""

        history = await self._injected_history()
        service = SkillService(_llm_stub())  # type: ignore[arg-type]
        messages = service.build_answer_prompt_payload(
            "咖啡和显卡聊聊", history=history
        )["messages"]
        # ``tag_rendered_history`` 是**按位置**把来源映射到渲染结果的（渲染层严格
        # 一对一），所以这里也按位置取事实行，避免把 ``format_recent_group_context``
        # 那条 system 摘要一起算进来。
        fact_indexes = [
            index
            for index, item in enumerate(history)
            if str(item.get("memory_source") or "") == "long_term_fact"
        ]
        self.assertTrue(fact_indexes)
        # ``build_answer_prompt_payload`` 的 messages[0] 是 defended system 提示词，
        # 历史紧随其后（``skills/service.py:433-442``），所以下标整体 +1。
        self.assertEqual(messages[0]["role"], "system")
        layers = [
            message_context_layer(messages[index + 1]) for index in fact_indexes
        ]
        self.assertTrue(
            all(layer == LAYER_MEMORY_RECALL for layer in layers),
            f"事实行应落在 memory_recall 层，实际={layers}",
        )

    async def test_facts_are_not_trimmed_before_search_records(self) -> None:
        """缺口正是「顺序反了」：事实行不能比检索留档更早被丢。"""

        history = await self._injected_history()
        rendered = sanitize_history_for_llm(history, max_items=len(history))
        tag_rendered_history(history, rendered)
        layers_by_source: dict[str, set[str]] = {}
        for row, message in zip(history, rendered):
            source = str(row.get("memory_source") or "")
            if not source:
                continue
            layers_by_source.setdefault(source, set()).add(
                message_context_layer(message)
            )
        self.assertIn("search_record", layers_by_source, "检索留档应作为正对照出现")
        self.assertEqual(layers_by_source["search_record"], {LAYER_SEARCH_RECORDS})
        self.assertEqual(
            layers_by_source["long_term_fact"],
            {LAYER_MEMORY_RECALL},
            "事实行必须比检索留档**更晚**被裁（memory_recall 在 TRIM_ORDER 里排最后）",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
