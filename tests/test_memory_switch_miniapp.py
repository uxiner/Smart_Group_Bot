"""D3-39（收口）：长期记忆的两个开关在 Mini App 可配，且改完**真的热生效**。

用户 2026-10 拍板：给 ``memory_facts_enabled``（真·总开关，关掉 = 不提炼/不注入/不写）
与 ``memory_tool_enabled`` 补一个配置入口（Mini App 里能配）。本批**只**补这两个，
不顺手把 ``load_settings()`` 改成被调用（D3-40：那会一次性激活约 20 个 env 种子）。

原状：`app.js` 的「上下文与长期记忆」分区里，这两个开关**一个控件都没有**，而
UI 上文案相似的 ``memory_recall_enabled``（"启用长期记忆召回"）**不是**总开关——
关掉它第 ④ 期照样提炼、照样写库。

接线现状（2026-10 复核）：

- ``bot/config.py`` 的 ``BotConfig``：两个字段早已存在 ✅
- ``bot/services/runtime_config.py`` 的 ``BotBehaviorConfig`` + ``apply_to_settings``：
  两个字段早已存在并已映射 ✅
- ``bot/web/static/app.js``：**缺控件** ← 本批补的就是这一腿

所以本文件的重点不是"把三腿补齐"，而是**证明改配置后行为跟着变**：
走真实的 ``RuntimeConfigManager.save`` → ``apply_to_settings`` → 消费者
（``RememberSkill`` 的闸门 + ``long_term_memory`` 的读取函数），断言门真的开了/关了。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.config import BotConfig, Settings
from bot.db.engine import init_db
from bot.services.long_term_memory import (
    memory_extract_enabled,
    memory_facts_enabled,
    memory_tool_enabled,
)
from bot.services.runtime_config import BotBehaviorConfig, RuntimeConfigManager
from bot.services.skills.base import SkillContext
from bot.services.skills.remember import RememberSkill

_REPO_ROOT = Path(__file__).resolve().parent.parent
_APP_JS = _REPO_ROOT / "bot" / "web" / "static" / "app.js"
_SWITCHES = ("memory_facts_enabled", "memory_tool_enabled")


def _make_settings() -> Settings:
    settings = Settings(
        _env_file=None,
        bot_token="42:TEST_TOKEN",
        super_admin_id=42,
        config_master_key="unit-test-master-key",
    )
    settings.bot.token = settings.bot_token
    return settings


class MemorySwitchWiringTests(unittest.TestCase):
    """三方接线本身（静态契约，防止以后再被删掉）。"""

    def test_schema_and_bootstrap_both_declare_the_switches(self) -> None:
        self.assertTrue(hasattr(BotConfig(), "memory_facts_enabled"))
        self.assertTrue(hasattr(BotBehaviorConfig(), "memory_tool_enabled"))
        self.assertTrue(hasattr(BotBehaviorConfig(), "memory_facts_enabled"))

    def test_apply_to_settings_maps_them_onto_settings_bot(self) -> None:
        """第二腿：运行时配置 → ``settings.bot`` 的映射早已存在（本批不动它）。"""

        from bot.services.runtime_config import RuntimeConfig

        settings = _make_settings()
        document = RuntimeConfig()
        document.bot.memory_facts_enabled = False
        document.bot.memory_tool_enabled = False
        document.apply_to_settings(settings)
        self.assertFalse(settings.bot.memory_facts_enabled)
        self.assertFalse(settings.bot.memory_tool_enabled)
        self.assertFalse(memory_facts_enabled(settings))
        self.assertFalse(memory_tool_enabled(settings))

    def test_mini_app_exposes_both_switches(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")

        for name in _SWITCHES:
            with self.subTest(path=f"bot.{name}"):
                self.assertIn(f'toggle("bot.{name}"', source)

    def test_recall_toggle_hint_says_it_is_not_the_master_switch(self) -> None:
        """UI 语义修正：``memory_recall_enabled`` 不是总开关，文案要看得出来。"""

        source = _APP_JS.read_text(encoding="utf-8")
        line = next(
            row for row in source.splitlines() if 'toggle("bot.memory_recall_enabled"' in row
        )

        self.assertIn("不是长期记忆的总开关", line)
        self.assertIn("memory_facts_enabled", source)


class MemorySwitchHotApplyTests(unittest.IsolatedAsyncioTestCase):
    """改配置后**行为**跟着变——走真实保存路径，不用 mock 掉整条链路。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = _make_settings()
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-smart-group-bot.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _save(self, **switches: bool) -> None:
        payload = self.manager.api_document()
        config = dict(payload["config"])
        bot = dict(config["bot"])
        bot.update(switches)
        config["bot"] = bot
        await self.manager.save(
            config,
            expected_revision=int(payload["revision"]),
            updated_by=42,
        )

    async def _remember_call(self) -> tuple[bool, bool]:
        """返回 (是否被闸门拦下, 是否真的走到了写库)。"""

        skill = RememberSkill(self.settings)
        context = SkillContext(
            session_factory=self.session_factory,
            chat_id=-10001,
            sender_user_id=7,
        )
        with patch(
            "bot.services.skills.remember.record_fact", new=AsyncMock(return_value=1)
        ) as record:
            result = await skill.run({"fact": "他养了一只叫豆豆的猫"}, context)
        return result.payload.get("reason") == "disabled", record.await_count > 0

    async def test_bootstrap_defaults_and_api_document_carry_the_switches(self) -> None:
        document = self.manager.api_document()

        for name in _SWITCHES:
            with self.subTest(name=name):
                # 界面拿得到当前值（toggle 的初值就是从这里读的）
                self.assertIn(name, document["config"]["bot"])
                self.assertTrue(document["config"]["bot"][name])

    async def test_turning_the_master_switch_off_changes_behaviour(self) -> None:
        """默认开 → 关：消费者闸门立刻关上，模型不能再写长期记忆。"""

        self.assertTrue(memory_facts_enabled(self.settings))
        self.assertTrue(memory_tool_enabled(self.settings))
        blocked, reached_writer = await self._remember_call()
        self.assertFalse(blocked)
        self.assertTrue(reached_writer)

        await self._save(memory_facts_enabled=False)

        self.assertFalse(memory_facts_enabled(self.settings))
        blocked, reached_writer = await self._remember_call()
        self.assertTrue(blocked)
        self.assertFalse(reached_writer)

        # 总开关关着时，写入子开关开着也没用。
        await self._save(memory_tool_enabled=True)
        self.assertTrue(memory_tool_enabled(self.settings))
        self.assertFalse(memory_facts_enabled(self.settings))
        blocked, _ = await self._remember_call()
        self.assertTrue(blocked)

    async def test_turning_only_the_tool_switch_off_keeps_extraction_readable(self) -> None:
        """只关 ``memory_tool_enabled``：模型不能写，但提炼/注入这条线不受影响。"""

        await self._save(memory_tool_enabled=False)

        self.assertTrue(memory_facts_enabled(self.settings))
        self.assertTrue(memory_extract_enabled(self.settings))
        self.assertFalse(memory_tool_enabled(self.settings))
        blocked, reached_writer = await self._remember_call()
        self.assertTrue(blocked)
        self.assertFalse(reached_writer)

    async def test_switching_back_on_is_also_hot(self) -> None:
        await self._save(memory_facts_enabled=False, memory_tool_enabled=False)
        self.assertFalse(memory_facts_enabled(self.settings))

        await self._save(memory_facts_enabled=True, memory_tool_enabled=True)

        self.assertTrue(memory_facts_enabled(self.settings))
        self.assertTrue(memory_tool_enabled(self.settings))
        blocked, reached_writer = await self._remember_call()
        self.assertFalse(blocked)
        self.assertTrue(reached_writer)


if __name__ == "__main__":
    unittest.main()