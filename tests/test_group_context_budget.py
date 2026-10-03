"""群聊上下文按 token 预算装配（第 2 期，深度对齐 272K）。

覆盖：

* **纯装配语义**：预算内装配、超预算停止、最新一条必留、单条超长截断留痕、
  空内容跳过、条数安全上限、角色归一化、返回时间正序（与私聊
  ``assemble_private_history`` 同口径）；
* **归档取数**：按预算而不是固定条数（300 条短消息全部保留 → 没有丢早期历史）、
  单次读取上限（累计到「预算 × 1.5」就停）、保留期过滤、被审核删除的消息剔除、
  归档为空 / 装配为空时退回改造前的热窗口；
* **硬闸门**：装配出来的历史 + 固定余量 ≤ ``max_context_tokens``；
* **配置**：默认 278528 / 夹取范围 / runtime_config 写穿 / MemoryService 生效预算；
* **接线**：群聊回复链路两处（参与判定、回复生成）都走按预算装配，且不再用
  「最近 N 条」的热窗口当主驱动。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import func, select

from bot.config import BotConfig, Settings
from bot.db.engine import init_db
from bot.db.models import GroupMessageArchive, MessageVector, Violation
from bot.handlers import group
from bot.services import group_context
from bot.services.group_context import (
    GROUP_HISTORY_MAX_MESSAGES,
    GROUP_HISTORY_RESERVE_TOKENS,
    GROUP_HISTORY_TOKEN_BUDGET,
    GROUP_HISTORY_TRUNCATION_NOTE,
    assemble_group_history,
    bounded_group_history_reserve_tokens,
    bounded_group_history_token_budget,
    effective_group_history_budget,
)
from bot.services.memory import MemoryService
from bot.utils.timezone import now_shanghai_naive
from bot.utils.tokens import estimate_text_tokens

#: 一条历史消息在预算里的固定开销（与 group_context 同口径，用例里要独立复算）
_OVERHEAD = 12


class _StubLLM:
    class main:
        model = "test/model"


def _row(content: str, *, role: str = "user", index: int = 0) -> dict:
    """一条历史条目（形状与 ``MemoryService._history_item`` 一致）。"""

    return {
        "role": role,
        "content": content,
        "created_at": f"2026-01-01T00:00:{index % 60:02d}",
        "sender_id": None if role == "assistant" else 7,
        "sender_name": "bot" if role == "assistant" else "Alice",
        "message_type": "text",
        "message_id": f"-10001:{index}",
    }


# ---------------------------------------------------------------------------
# 1. 纯装配语义
# ---------------------------------------------------------------------------


class GroupHistoryAssemblyTests(unittest.TestCase):
    def test_keeps_newest_messages_within_budget_in_chronological_order(self) -> None:
        # 每条约 500 token（500 个 CJK 字 + 12 固定开销），预算 1024 → 只装最新两条。
        rows = [_row("甲" * 500, index=index) for index in range(5)]

        selected = assemble_group_history(rows, budget_tokens=1024)

        self.assertEqual(
            [item["message_id"] for item in selected],
            ["-10001:3", "-10001:4"],
        )

    def test_newest_message_is_always_kept_when_budget_runs_out(self) -> None:
        # 每条约 2012 token：预算 4096 只装得下两条，最新那条必须在。
        rows = [_row("甲" * 2000, index=index) for index in range(3)]

        selected = assemble_group_history(rows, budget_tokens=4096)

        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[-1]["message_id"], "-10001:2")

    def test_single_oversized_newest_message_is_truncated_with_note(self) -> None:
        # 单条自身就超过整个预算：截断保留，而不是整条丢掉。
        rows = [_row("乙" * 9000, index=0)]

        selected = assemble_group_history(rows, budget_tokens=1024)

        self.assertEqual(len(selected), 1)
        self.assertIn(GROUP_HISTORY_TRUNCATION_NOTE, selected[0]["content"])
        self.assertLessEqual(
            estimate_text_tokens(selected[0]["content"]) + _OVERHEAD,
            1024,
        )

    def test_oversized_newest_survives_with_older_messages_ahead_of_it(self) -> None:
        rows = [_row("早", index=0), _row("丙" * 9000, index=1)]

        selected = assemble_group_history(rows, budget_tokens=1024)

        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0]["content"].startswith("丙"))
        self.assertIn(GROUP_HISTORY_TRUNCATION_NOTE, selected[0]["content"])

    def test_empty_and_blank_contents_are_skipped(self) -> None:
        rows = [
            _row("", index=0),
            _row("   ", index=1),
            _row("有用的一句", role="assistant", index=2),
        ]

        selected = assemble_group_history(rows, budget_tokens=4096)

        self.assertEqual([item["content"] for item in selected], ["有用的一句"])
        self.assertEqual(selected[0]["role"], "assistant")

    def test_message_count_safety_cap_keeps_the_newest(self) -> None:
        rows = [_row(f"m{index}", index=index) for index in range(300)]

        selected = assemble_group_history(
            rows,
            budget_tokens=10_000_000,
            max_messages=50,
        )

        self.assertEqual(len(selected), 50)
        self.assertEqual(selected[0]["content"], "m250")
        self.assertEqual(selected[-1]["content"], "m299")

    def test_metadata_is_preserved_and_dirty_role_normalizes_to_user(self) -> None:
        rows = [
            {
                "role": "system",
                "content": "x",
                "created_at": "2026-01-01T00:00:00",
                "sender_name": "Alice",
                "message_type": "text",
                "message_id": "-10001:1",
            }
        ]

        selected = assemble_group_history(rows, budget_tokens=4096)

        self.assertEqual(selected[0]["role"], "user")
        self.assertEqual(selected[0]["sender_name"], "Alice")
        self.assertEqual(selected[0]["message_id"], "-10001:1")

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual(assemble_group_history([]), [])
        self.assertEqual(assemble_group_history(None), [])


# ---------------------------------------------------------------------------
# 2. 预算夹取与硬闸门
# ---------------------------------------------------------------------------


class GroupHistoryBudgetGateTests(unittest.TestCase):
    def test_default_target_and_clamping_ranges(self) -> None:
        self.assertEqual(GROUP_HISTORY_TOKEN_BUDGET, 278_528)
        self.assertEqual(GROUP_HISTORY_RESERVE_TOKENS, 32_768)
        # 条数只是安全上限（token 预算才是主驱动）：默认 2000 条 = 旧窗口 50 条的 40 倍。
        self.assertEqual(GROUP_HISTORY_MAX_MESSAGES, 2000)

        self.assertEqual(bounded_group_history_token_budget(0), 1024)
        self.assertEqual(bounded_group_history_token_budget(9_000_000), 2_000_000)
        self.assertEqual(bounded_group_history_token_budget("bad"), 278_528)
        self.assertEqual(bounded_group_history_token_budget(None), 278_528)

        self.assertEqual(bounded_group_history_reserve_tokens(0), 1024)
        self.assertEqual(bounded_group_history_reserve_tokens(9_000_000), 1_000_000)
        self.assertEqual(bounded_group_history_reserve_tokens(None), 32_768)

    def test_effective_budget_leaves_room_for_the_reserve(self) -> None:
        budget = effective_group_history_budget(
            configured_budget=GROUP_HISTORY_TOKEN_BUDGET,
            reserve_tokens=GROUP_HISTORY_RESERVE_TOKENS,
            model_window_tokens=278_528,
        )

        self.assertEqual(budget, 278_528 - 32_768)
        self.assertEqual(budget + GROUP_HISTORY_RESERVE_TOKENS, 278_528)

    def test_effective_budget_never_exceeds_window_for_any_combination(self) -> None:
        for window in (1024, 2048, 5000, 64_000, 256_000, 278_528, 1_000_000):
            for reserve in (0, 1024, 8192, 32_768, 1_000_000):
                budget = effective_group_history_budget(
                    configured_budget=GROUP_HISTORY_TOKEN_BUDGET,
                    reserve_tokens=reserve,
                    model_window_tokens=window,
                )
                effective_reserve = max(
                    0,
                    min(reserve, max(0, max(1024, window) - 1024)),
                )

                self.assertGreaterEqual(budget, 1024)
                self.assertLessEqual(budget + effective_reserve, max(1024, window))

    def test_assembled_history_plus_reserve_stays_within_the_window(self) -> None:
        """硬闸门：真实装配出来的历史 + 固定余量 ≤ 278528。"""

        window = 278_528
        reserve = GROUP_HISTORY_RESERVE_TOKENS
        budget = effective_group_history_budget(
            configured_budget=GROUP_HISTORY_TOKEN_BUDGET,
            reserve_tokens=reserve,
            model_window_tokens=window,
        )
        rows = [_row("群聊正文" * 200, index=index) for index in range(400)]

        selected = assemble_group_history(rows, budget_tokens=budget)
        used = sum(
            estimate_text_tokens(item["content"]) + _OVERHEAD for item in selected
        )

        self.assertTrue(selected)
        self.assertLessEqual(used, budget)
        self.assertLessEqual(used + reserve, window)


# ---------------------------------------------------------------------------
# 3. 归档取数（真实 sqlite）
# ---------------------------------------------------------------------------


class GroupArchiveHistoryTests(unittest.IsolatedAsyncioTestCase):
    GROUP_ID = -100555

    async def asyncSetUp(self) -> None:
        self._db_paths: list[str] = []
        self._engines: list = []

    async def asyncTearDown(self) -> None:
        for engine in self._engines:
            await engine.dispose()
        for path in self._db_paths:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(f"{path}{suffix}")
                except OSError:
                    pass

    async def _memory(self, **overrides: object) -> MemoryService:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._db_paths.append(path)
        engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
        self._engines.append(engine)
        return MemoryService(
            BotConfig(**overrides),  # type: ignore[arg-type]
            _StubLLM(),  # type: ignore[arg-type]
            session_factory=session_factory,
        )

    async def _seed_sequence(
        self,
        memory: MemoryService,
        contents: list[str],
        *,
        roles: list[str] | None = None,
    ) -> None:
        """按给定顺序写入归档：最后一条是最新的，``telegram_message_id`` = 1000+i。"""

        base = now_shanghai_naive() - timedelta(minutes=len(contents) + 1)
        async with memory.session_factory() as session:
            await MemoryService._ensure_group_row(session, self.GROUP_ID)
            session.add_all(
                [
                    GroupMessageArchive(
                        group_id=self.GROUP_ID,
                        message_key=f"{self.GROUP_ID}:{1000 + index}",
                        telegram_message_id=1000 + index,
                        role=(roles[index] if roles else "user"),
                        direction=(
                            "outbound"
                            if roles and roles[index] == "assistant"
                            else "inbound"
                        ),
                        sender_display_name=(
                            "bot" if roles and roles[index] == "assistant" else "Alice"
                        ),
                        sender_id=(
                            None if roles and roles[index] == "assistant" else 7
                        ),
                        message_type="text",
                        content=content,
                        raw_text=content,
                        sent_at=base + timedelta(seconds=index),
                    )
                    for index, content in enumerate(contents)
                ]
            )
            await session.commit()

    async def _count_archive_rows(self, memory: MemoryService) -> int:
        async with memory.session_factory() as session:
            return int(
                (
                    await session.execute(
                        select(func.count(GroupMessageArchive.id)).where(
                            GroupMessageArchive.group_id == self.GROUP_ID
                        )
                    )
                ).scalar_one()
            )

    async def test_reads_by_budget_not_by_a_fixed_message_count(self) -> None:
        """条数远多于 50、总 token 仍在预算内 → 早期历史一条都不许丢。"""

        memory = await self._memory()
        contents = [f"msg-{index:03d}" for index in range(300)]
        await self._seed_sequence(memory, contents)

        history = await memory.load_group_history_by_budget(self.GROUP_ID)

        self.assertEqual(memory.group_history_token_budget, 278_528 - 32_768)
        self.assertEqual(len(history), 300)
        self.assertEqual(history[0]["content"], "msg-000")
        self.assertEqual(history[-1]["content"], "msg-299")
        used = sum(
            estimate_text_tokens(item["content"]) + _OVERHEAD for item in history
        )
        self.assertLessEqual(used, memory.group_history_token_budget)

    async def test_read_stops_at_about_150_percent_of_the_budget(self) -> None:
        memory = await self._memory()
        await self._seed_sequence(memory, ["甲" * 1000 for _ in range(100)])

        scanned: list[set[int]] = []
        original = memory._removed_message_ids

        async def _spy(group_id: int, ids: set[int]) -> set[int]:
            scanned.append(set(ids))
            return await original(group_id, ids)

        memory._removed_message_ids = _spy  # type: ignore[method-assign]

        history = await memory.load_group_history_by_budget(
            self.GROUP_ID,
            budget_tokens=4096,
        )

        # 每条 1012 token：1.5 × 4096 = 6144 → 最多读到越界那一条（≤8 行）。
        self.assertEqual(len(scanned), 1)
        self.assertLessEqual(len(scanned[0]), 8)
        self.assertGreaterEqual(len(scanned[0]), 4)
        self.assertLess(len(history), 8)
        self.assertTrue(history[-1]["content"].startswith("甲"))

    async def test_tiny_budget_keeps_the_newest_message_truncated(self) -> None:
        memory = await self._memory()
        await self._seed_sequence(memory, ["甲" * 5000, "乙" * 5000, "丙" * 5000])

        history = await memory.load_group_history_by_budget(
            self.GROUP_ID,
            budget_tokens=2048,
        )

        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]["content"].startswith("丙"))
        self.assertIn(GROUP_HISTORY_TRUNCATION_NOTE, history[0]["content"])

    async def test_assistant_rows_from_the_archive_keep_their_role(self) -> None:
        memory = await self._memory()
        await self._seed_sequence(
            memory,
            ["群友的问题", "机器人的回复"],
            roles=["user", "assistant"],
        )

        history = await memory.load_group_history_by_budget(self.GROUP_ID)

        self.assertEqual(
            [(item["role"], item["content"]) for item in history],
            [("user", "群友的问题"), ("assistant", "机器人的回复")],
        )
        self.assertEqual(history[-1]["sender_name"], "bot")

    async def test_rows_outside_the_retention_window_are_ignored(self) -> None:
        memory = await self._memory(memory_retention_days=7)
        base = now_shanghai_naive() - timedelta(days=8)
        async with memory.session_factory() as session:
            await MemoryService._ensure_group_row(session, self.GROUP_ID)
            session.add_all(
                [
                    GroupMessageArchive(
                        group_id=self.GROUP_ID,
                        message_key=f"{self.GROUP_ID}:1",
                        telegram_message_id=1,
                        role="user",
                        content="八天前的旧消息",
                        sent_at=base,
                    ),
                    GroupMessageArchive(
                        group_id=self.GROUP_ID,
                        message_key=f"{self.GROUP_ID}:2",
                        telegram_message_id=2,
                        role="user",
                        content="今天的消息",
                        sent_at=now_shanghai_naive(),
                    ),
                ]
            )
            await session.commit()

        history = await memory.load_group_history_by_budget(self.GROUP_ID)

        self.assertEqual([item["content"] for item in history], ["今天的消息"])

    async def test_moderation_deleted_rows_are_not_served_as_history(self) -> None:
        """F-016：被审核删除的消息不得重新作为历史喂回模型。"""

        memory = await self._memory()
        await self._seed_sequence(memory, ["正常一", "广告正文", "正常二"])
        async with memory.session_factory() as session:
            session.add(
                Violation(
                    group_id=self.GROUP_ID,
                    user_id=7,
                    action_taken="delete",
                    source_message_id=1001,
                )
            )
            await session.commit()

        history = await memory.load_group_history_by_budget(self.GROUP_ID)

        contents = [item["content"] for item in history]
        self.assertNotIn("广告正文", contents)
        self.assertEqual(contents, ["正常一", "正常二"])

    async def test_empty_archive_falls_back_to_the_legacy_working_window(self) -> None:
        """归档为空（例如老库）→ 行为与改造前一致：退回热窗口。"""

        memory = await self._memory()
        async with memory.session_factory() as session:
            await MemoryService._ensure_group_row(session, self.GROUP_ID)
            session.add(
                MessageVector(
                    group_id=self.GROUP_ID,
                    message_id=f"{self.GROUP_ID}:1",
                    role="user",
                    sender_name="Alice",
                    message_type="text",
                    content="改造前热窗口里的一句",
                    created_at=now_shanghai_naive(),
                )
            )
            await session.commit()

        self.assertEqual(await self._count_archive_rows(memory), 0)
        history = await memory.load_group_history_by_budget(self.GROUP_ID)

        self.assertEqual(
            [item["content"] for item in history],
            ["改造前热窗口里的一句"],
        )

    async def test_empty_archive_and_empty_window_return_no_history(self) -> None:
        """归档为空、热窗口也为空：返回空列表，绝不抛异常。"""

        memory = await self._memory()

        self.assertEqual(await memory.load_group_history_by_budget(self.GROUP_ID), [])

    async def test_archive_read_failure_degrades_to_the_working_window(self) -> None:
        memory = await self._memory()
        memory._replace_working_history(
            self.GROUP_ID,
            [_row("热窗口兜底", index=1)],
        )
        memory._history_loaded.add(self.GROUP_ID)
        memory._read_group_archive_history = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("db down")
        )

        history = await memory.load_group_history_by_budget(self.GROUP_ID)

        self.assertEqual([item["content"] for item in history], ["热窗口兜底"])

    async def test_get_history_for_llm_uses_injected_history_rows(self) -> None:
        """回复生成处注入的按预算历史必须取代热窗口。"""

        memory = await self._memory()
        memory._replace_working_history(
            self.GROUP_ID,
            [_row("热窗口里的旧消息", index=1)],
        )
        memory._history_loaded.add(self.GROUP_ID)
        injected = [_row("归档里深度装配的一句", index=9)]

        history = await memory.get_history_for_llm(
            self.GROUP_ID,
            history_rows=injected,
            reserve_tokens=0,
        )

        contents = [item["content"] for item in history]
        self.assertIn("归档里深度装配的一句", contents)
        self.assertNotIn("热窗口里的旧消息", contents)

    async def test_get_history_for_llm_defaults_to_the_working_window(self) -> None:
        """不传注入历史时行为与改造前一致（主动发言链路走这条）。"""

        memory = await self._memory()
        memory._replace_working_history(
            self.GROUP_ID,
            [_row("热窗口里的旧消息", index=1)],
        )
        memory._history_loaded.add(self.GROUP_ID)

        history = await memory.get_history_for_llm(self.GROUP_ID, reserve_tokens=0)

        self.assertIn(
            "热窗口里的旧消息",
            [item["content"] for item in history],
        )


# ---------------------------------------------------------------------------
# 4. 配置默认值 / 运行时覆盖
# ---------------------------------------------------------------------------


class GroupHistoryConfigTests(unittest.TestCase):
    def test_defaults_target_272k_everywhere(self) -> None:
        from bot.services.runtime_config import BotBehaviorConfig

        self.assertEqual(BotConfig().max_context_tokens, 278_528)
        self.assertEqual(BotConfig().group_history_token_budget, 278_528)
        self.assertEqual(BotConfig().group_history_reserve_tokens, 32_768)

        settings = Settings(_env_file=None)
        self.assertEqual(settings.max_context_tokens, 278_528)
        self.assertEqual(settings.bot.max_context_tokens, 278_528)
        self.assertEqual(settings.bot.group_history_token_budget, 278_528)
        self.assertEqual(settings.bot.group_history_reserve_tokens, 32_768)
        self.assertEqual(settings.bot_group_history_token_budget, 278_528)
        self.assertEqual(settings.bot_group_history_reserve_tokens, 32_768)

        behavior = BotBehaviorConfig()
        self.assertEqual(behavior.max_context_tokens, 278_528)
        self.assertEqual(behavior.group_history_token_budget, 278_528)
        self.assertEqual(behavior.group_history_reserve_tokens, 32_768)

    def test_runtime_config_bounds_are_enforced(self) -> None:
        from pydantic import ValidationError

        from bot.services.runtime_config import BotBehaviorConfig

        with self.assertRaises(ValidationError):
            BotBehaviorConfig(group_history_token_budget=1023)
        with self.assertRaises(ValidationError):
            BotBehaviorConfig(group_history_token_budget=2_000_001)
        with self.assertRaises(ValidationError):
            BotBehaviorConfig(group_history_reserve_tokens=1023)
        with self.assertRaises(ValidationError):
            BotBehaviorConfig(group_history_reserve_tokens=1_000_001)

    def test_runtime_config_writes_through_to_settings(self) -> None:
        from bot.services.runtime_config import RuntimeConfig

        settings = Settings(_env_file=None)
        config = RuntimeConfig.model_validate(
            {
                "bot": {
                    "group_history_token_budget": 300_000,
                    "group_history_reserve_tokens": 20_000,
                }
            }
        )

        config.apply_to_settings(settings, apply_prompts=False)

        self.assertEqual(settings.bot.group_history_token_budget, 300_000)
        self.assertEqual(settings.bot.group_history_reserve_tokens, 20_000)

    def test_legacy_import_carries_the_new_fields(self) -> None:
        from bot.services.runtime_config import build_legacy_runtime_config

        imported = build_legacy_runtime_config(
            "/tmp/nonexistent-group-context.toml",
            settings=Settings(_env_file=None),
            raw_env={},
        )

        self.assertEqual(imported.bot.max_context_tokens, 278_528)
        self.assertEqual(imported.bot.group_history_token_budget, 278_528)
        self.assertEqual(imported.bot.group_history_reserve_tokens, 32_768)

    def test_memory_service_applies_effective_budget(self) -> None:
        memory = MemoryService(
            BotConfig(
                max_context_tokens=278_528,
                group_history_token_budget=278_528,
                group_history_reserve_tokens=32_768,
            ),
            _StubLLM(),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
        )

        self.assertEqual(memory.group_history_token_budget, 245_760)
        self.assertEqual(memory.group_history_reserve_tokens, 32_768)
        self.assertLessEqual(
            memory.group_history_token_budget + memory.group_history_reserve_tokens,
            memory.max_context,
        )

    def test_reconfigure_applies_a_runtime_budget_change(self) -> None:
        memory = MemoryService(
            BotConfig(max_context_tokens=278_528),
            _StubLLM(),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
        )

        memory.reconfigure(
            BotConfig(
                max_context_tokens=278_528,
                group_history_token_budget=100_000,
                group_history_reserve_tokens=8192,
            )
        )

        self.assertEqual(memory.group_history_token_budget, 100_000)
        self.assertEqual(memory.group_history_reserve_tokens, 8192)

    def test_runtime_budget_cannot_push_history_past_the_window(self) -> None:
        memory = MemoryService(
            BotConfig(
                max_context_tokens=278_528,
                group_history_token_budget=2_000_000,
                group_history_reserve_tokens=278_528,
            ),
            _StubLLM(),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
        )

        self.assertLessEqual(
            memory.group_history_token_budget + min(
                memory.group_history_reserve_tokens,
                max(0, memory.max_context - 1024),
            ),
            memory.max_context,
        )


# ---------------------------------------------------------------------------
# 5. 本轮批次消息的剔除（归档正文与热窗口正文形状不同）
# ---------------------------------------------------------------------------


class BatchMessageExclusionTests(unittest.TestCase):
    def test_message_key_exclusion_drops_archive_content_variants(self) -> None:
        history = [
            {
                "role": "user",
                "content": "本轮消息（归档里补充过图片描述的版本）",
                "message_id": "-10001:99",
            },
            {"role": "user", "content": "别人上一条", "message_id": "-10001:98"},
        ]

        kept = group._exclude_batch_messages(
            history,
            [],
            message_keys=["-10001:99"],
        )

        self.assertEqual([item["message_id"] for item in kept], ["-10001:98"])

    def test_without_message_keys_the_content_match_behaviour_is_unchanged(self) -> None:
        history = [
            {"role": "user", "content": "[id:1] 你好", "message_id": "-10001:99"},
            {"role": "user", "content": "别人上一条", "message_id": "-10001:98"},
        ]

        kept = group._exclude_batch_messages(history, ["[id:1] 你好"])

        self.assertEqual([item["message_id"] for item in kept], ["-10001:98"])

    def test_batch_scoped_message_keys_use_the_group_message_shape(self) -> None:
        items = [
            SimpleNamespace(message=SimpleNamespace(message_id=99)),
            SimpleNamespace(message=SimpleNamespace(message_id=0)),
        ]

        self.assertEqual(
            group._batch_scoped_message_keys(-10001, items),
            ["-10001:99"],
        )


# ---------------------------------------------------------------------------
# 6. 群聊回复链路接线（参与判定 + 回复生成）
# ---------------------------------------------------------------------------


class _PendingSession:
    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> "_PendingSession":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get(self, model: object, key: int) -> SimpleNamespace:
        return SimpleNamespace(settings={})

    async def execute(self, statement: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def _processing_settings() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(
            inbound_debounce_seconds=1.0,
            main_model="",
            decision_model="",
            compress_model="",
            moderation_model="",
            vision_model="",
            embed_model="",
            max_context_tokens=0,
            decision_context_items=0,
            enable_typing=False,
            enable_streaming=False,
            stream_chunk_size=100,
            stream_edit_interval_sec=0.0,
        ),
        skill_sticker_file_ids="",
    )


class GroupReplyPathWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_group_reply_steps_assemble_history_by_budget(self) -> None:
        message = SimpleNamespace(
            message_id=99,
            text="在吗",
            caption=None,
            from_user=SimpleNamespace(
                id=123,
                is_bot=False,
                username="tester",
                full_name="Tester",
            ),
            sender_chat=None,
            reply_to_message=None,
            chat=SimpleNamespace(id=-10001, type="supergroup"),
        )
        item = group._PendingReplyItem(
            message=message,
            group_id=-10001,
            user_id=123,
            input_text="在吗",
            msg_type="text",
            sender_username="tester",
            sender_is_owner=False,
            sender_is_tg_admin=False,
            user_tag="id:123",
            explicit_mention=True,
            mentioned=True,
            is_reply=False,
            reply_to_bot=False,
            reply_to_other=False,
            mention_other=False,
        )
        session = _PendingSession()
        decision_tail = [_row("判定只需要尾部", index=1)]
        deep_history = [
            _row("很久以前的一句", index=5),
            _row("机器人当时的回复", role="assistant", index=6),
        ]
        loader = AsyncMock(side_effect=[decision_tail, deep_history])
        history_for_llm = AsyncMock(return_value=[])
        memory = SimpleNamespace(
            session_factory=lambda: session,
            get_history=Mock(return_value=[]),
            load_group_history_by_budget=loader,
            get_history_for_llm=history_for_llm,
            add_message=AsyncMock(),
        )
        fake_skill = SimpleNamespace(
            tts_service=SimpleNamespace(available=False),
            build_answer_prompt_payload=Mock(
                return_value={"messages": [], "tools": []}
            ),
            answer_with_skill=AsyncMock(
                return_value=SimpleNamespace(
                    text="",
                    handled=True,
                    sticker_sent=False,
                    tts_sent=False,
                    sticker_file_id="",
                    tts_text="",
                )
            ),
        )
        fake_progress = SimpleNamespace(
            visible=False,
            start=AsyncMock(),
            report=AsyncMock(),
            composing=AsyncMock(),
            handoff=AsyncMock(return_value=None),
            finish=AsyncMock(),
            fail=AsyncMock(),
            dismiss=AsyncMock(),
            close=AsyncMock(),
        )

        with (
            patch("bot.handlers.group.memory_holder.get", return_value=memory),
            patch("bot.handlers.group.LLMService", return_value=object()),
            patch("bot.handlers.group.SkillService", return_value=fake_skill),
            patch(
                "bot.handlers.group.ReplyProgressTracker",
                return_value=fake_progress,
            ),
            patch(
                "bot.handlers.group._is_user_admin_cached",
                new=AsyncMock(return_value=False),
            ),
            patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
        ):
            await group._process_pending_reply_batch([item], _processing_settings())

        # 参与判定：小预算 + 尾部条数上限。
        self.assertEqual(loader.await_count, 2)
        decision_call = loader.await_args_list[0]
        self.assertEqual(decision_call.args, (-10001,))
        self.assertEqual(
            decision_call.kwargs["budget_tokens"],
            group_context.DECISION_HISTORY_TOKEN_BUDGET,
        )
        self.assertEqual(
            decision_call.kwargs["max_messages"],
            group_context.DECISION_HISTORY_MAX_MESSAGES,
        )
        # 回复生成：默认（生效）预算装配出来的历史被显式注入提示词历史。
        generation_call = loader.await_args_list[1]
        self.assertEqual(generation_call.args, (-10001,))
        self.assertEqual(generation_call.kwargs, {})
        self.assertEqual(
            history_for_llm.await_args.kwargs["history_rows"],
            deep_history,
        )
        # 「最近 N 条」的热窗口不再是主驱动。
        self.assertEqual(memory.get_history.call_count, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
