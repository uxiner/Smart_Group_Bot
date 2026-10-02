"""F-024：新增的处置开关默认全开 → 升级后线上行为静默改变。

审查结论：升级且不改配置时同时发生三件事（引用连坐追溯原作者、管理员/群主不再
豁免、露骨图片处置默认开启），都属于「对用户可见的执法策略变更」，却没有任何
提示。load_settings 只做 bool()/max() 归一，界面上也不会提醒管理员。

修好之后的口径（本文件锁定）：

- 三个执法开关全部 opt-in（默认 False），配置模型 / 运行时模型 / 真实 Settings
  三处一致；读取辅助函数也不再有"缺字段就当开启"的隐藏默认；
- 启动日志必须给出可观测信号：只要有开关是开的就 WARNING 逐条列出，全关时
  INFO 说明当前是旧版行为；
- 升级路径（老库 payload 里没有这三个键）读出来仍然是关闭，不会静默打开。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from bot.config import Settings, log_enforcement_switch_state
from bot.db.engine import init_db
from bot.db.models import RuntimeConfigRecord
from bot.handlers import group
from bot.services.runtime_config import (
    ModerationSettingsConfig,
    RuntimeConfig,
    RuntimeConfigManager,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SWITCH_NAMES = (
    "nsfw_image_guard_enabled",
    "punish_quoted_author_enabled",
    "admin_moderation_enabled",
)


class EnforcementDefaultsTests(unittest.TestCase):
    def test_bootstrap_settings_default_every_switch_to_off(self) -> None:
        settings = Settings(_env_file=None)

        for name in _SWITCH_NAMES:
            with self.subTest(name=name):
                self.assertFalse(getattr(settings.moderation, name))

    def test_runtime_models_default_every_switch_to_off(self) -> None:
        for config in (ModerationSettingsConfig(), RuntimeConfig()):
            with self.subTest(config=type(config).__name__):
                moderation = (
                    config if isinstance(config, ModerationSettingsConfig) else config.moderation
                )
                for name in _SWITCH_NAMES:
                    self.assertFalse(getattr(moderation, name))

    def test_readers_have_no_hidden_default_on_fallback(self) -> None:
        """缺字段也当成"没开启"：不允许任何隐式默认打开执法的路径。"""

        settings = Settings(_env_file=None)
        self.assertFalse(group._nsfw_image_guard_enabled(settings))
        self.assertFalse(group._punish_quoted_author_enabled(settings))
        self.assertFalse(group._admin_moderation_enabled(settings))

        enabled = Settings(_env_file=None)
        enabled.moderation.nsfw_image_guard_enabled = True
        enabled.moderation.punish_quoted_author_enabled = True
        enabled.moderation.admin_moderation_enabled = True
        self.assertTrue(group._nsfw_image_guard_enabled(enabled))
        self.assertTrue(group._punish_quoted_author_enabled(enabled))
        self.assertTrue(group._admin_moderation_enabled(enabled))


class EnforcementVisibilityTests(unittest.TestCase):
    def test_all_switches_off_is_logged_as_info(self) -> None:
        with self.assertLogs("bot.config", level="INFO") as logs:
            log_enforcement_switch_state(Settings(_env_file=None))

        joined = "\n".join(logs.output)
        self.assertIn("全部关闭", joined)
        self.assertIn("admin_moderation", joined)
        self.assertFalse(any(record.levelname == "WARNING" for record in logs.records))

    def test_enabled_switch_is_logged_as_warning_with_its_name(self) -> None:
        settings = Settings(_env_file=None)
        settings.moderation.nsfw_image_guard_enabled = True
        settings.moderation.admin_moderation_enabled = True
        with self.assertLogs("bot.config", level="WARNING") as logs:
            log_enforcement_switch_state(settings)

        joined = "\n".join(logs.output)
        self.assertIn("nsfw_image_guard=on", joined)
        self.assertIn("admin_moderation=on", joined)
        self.assertNotIn("punish_quoted_author=on", joined)
        self.assertTrue(
            any(record.levelname == "WARNING" for record in logs.records), logs.output
        )

    def test_startup_wiring_calls_the_visibility_logger(self) -> None:
        main_source = (_REPO_ROOT / "bot" / "__main__.py").read_text(encoding="utf-8")

        self.assertIn("log_enforcement_switch_state(settings)", main_source)


class EnforcementUpgradePathTests(unittest.IsolatedAsyncioTestCase):
    """老库（payload 里没有这三个键）升级后必须读到"关闭"。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.settings = Settings(
            _env_file=None,
            bot_token="42:TEST_TOKEN",
            super_admin_id=42,
            config_master_key="unit-test-master-key",
        )
        self.settings.bot.token = self.settings.bot_token
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

    async def test_legacy_row_without_the_new_keys_reads_as_off(self) -> None:
        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
            payload = dict(row.payload)
            moderation = dict(payload.get("moderation") or {})
            for name in _SWITCH_NAMES:
                moderation.pop(name, None)
            payload["moderation"] = moderation
            payload["schema_version"] = 1
            row.payload = payload
            await session.commit()

        settings = Settings(
            _env_file=None,
            bot_token="42:TEST_TOKEN",
            super_admin_id=42,
            config_master_key="unit-test-master-key",
        )
        settings.bot.token = settings.bot_token
        reloaded = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=settings,
            legacy_config_path="/tmp/nonexistent-smart-group-bot.toml",
            legacy_raw_env={},
        )
        await reloaded.initialize()

        with self.assertLogs("bot.config", level="INFO") as logs:
            log_enforcement_switch_state(settings)
        self.assertIn("全部关闭", "\n".join(logs.output))
        for name in _SWITCH_NAMES:
            with self.subTest(name=name):
                self.assertFalse(getattr(settings.moderation, name))

    async def test_reset_to_defaults_keeps_enforcement_off(self) -> None:
        """界面"恢复默认"之后不能反而把执法打开。"""

        from bot.services.runtime_config import RuntimeConfig

        defaults = RuntimeConfig.defaults() if hasattr(RuntimeConfig, "defaults") else RuntimeConfig()
        for name in _SWITCH_NAMES:
            with self.subTest(name=name):
                self.assertFalse(getattr(defaults.moderation, name))
