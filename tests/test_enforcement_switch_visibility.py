"""F-024 / B-02 / C1-F-024 / C3-11：三个执法开关默认**关闭**（opt-in），且可见。

用户 2026-10 拍板：三个执法开关的**代码默认值 = 关闭**；生产环境会显式配置成打开。

历史（供查）：`8c5b3aa` 曾把三处默认改成 opt-in，3 小时后被 `69a1e2b` 以"NSFW 是
底线 / 引用连坐是防 F-001 必需"为由改回 `True`，并留下"注释写 opt-in、值是 True"的
自相矛盾注释（C3-11）。本次是**用户已确认的方向**，因此代码、注释、启动日志、
README、Mini App 文案五处必须说同一件事：默认关闭、需要显式打开。

原始审查结论（保留备查）：新增的处置开关默认全开 → 升级后线上行为静默改变。

修好之后的口径（本文件锁定）：

- 三个执法开关全部 opt-in（默认 False），配置模型 / 运行时模型 / 真实 Settings
  三处一致；读取辅助函数也不再有"缺字段就当开启"的隐藏默认；
- **显式打开之后行为才真的跟着变**（不是只改了个默认值）；
- 启动日志必须给出可观测信号：只要有开关是开的就 WARNING 逐条列出，全关时
  INFO 说明当前是旧版行为；
- 升级路径（老库 payload 里没有这三个键）读出来仍然是关闭，不会静默打开。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
_READERS = (
    "nsfw_image_guard_enabled",
    "punish_quoted_author_enabled",
    "admin_moderation_enabled",
)


def _bootstrap_settings() -> Settings:
    return Settings(_env_file=None)


class EnforcementDefaultsTests(unittest.TestCase):
    def test_bootstrap_settings_default_every_switch_to_off(self) -> None:
        """三个执法开关默认**关闭**（opt-in）：用户已确认，需运维显式打开。"""

        settings = _bootstrap_settings()

        for name in _SWITCH_NAMES:
            with self.subTest(name=name):
                self.assertFalse(getattr(settings.moderation, name))

    def test_runtime_models_default_every_switch_to_off(self) -> None:
        """DB 侧运行时模型同样默认关闭——否则"恢复默认"会把执法静默打开。"""

        for config in (ModerationSettingsConfig(), RuntimeConfig()):
            with self.subTest(config=type(config).__name__):
                moderation = (
                    config if isinstance(config, ModerationSettingsConfig) else config.moderation
                )
                for name in _SWITCH_NAMES:
                    self.assertFalse(getattr(moderation, name))

    def test_readers_follow_settings_exactly(self) -> None:
        """读取函数只反映配置：默认关闭；显式打开必须真的开。"""

        settings = _bootstrap_settings()
        for name in _READERS:
            with self.subTest(reader=name):
                self.assertFalse(getattr(group, f"_{name}")(settings))

        # 字段缺失（老配置/未传字段）必须按"关闭"处理：不再有隐藏的"缺字段=开启"。
        legacy = _bootstrap_settings()
        legacy.moderation = SimpleNamespace(enabled=True)
        for name in _READERS:
            with self.subTest(reader=name, payload="legacy-without-the-key"):
                self.assertFalse(getattr(group, f"_{name}")(legacy))

        enabled = _bootstrap_settings()
        for name in _SWITCH_NAMES:
            setattr(enabled.moderation, name, True)
        for name in _READERS:
            with self.subTest(reader=name, payload="explicitly-enabled"):
                self.assertTrue(getattr(group, f"_{name}")(enabled))

        disabled = _bootstrap_settings()
        for name in _SWITCH_NAMES:
            setattr(disabled.moderation, name, False)
        for name in _READERS:
            with self.subTest(reader=name, payload="explicitly-disabled"):
                self.assertFalse(getattr(group, f"_{name}")(disabled))

    def test_no_source_file_still_claims_the_switches_default_to_on(self) -> None:
        """C3-11：代码 / 注释 / 启动日志 / README / Mini App 文案必须同一口径。"""

        stale_claims = (
            ("默认开启：NSFW", "bot/config.py"),
            ("默认开启：引用/转发广告连坐", "bot/config.py"),
            ("默认开启：管理员命中犯规", "bot/config.py"),
            ("nsfw_image_guard_enabled`（默认开启）", "README.md"),
            ("admin_moderation_enabled` 默认开启", "README.md"),
            ("punish_quoted_author_enabled` 默认开启", "README.md"),
        )
        for needle, relative in stale_claims:
            with self.subTest(claim=needle):
                source = (_REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn(needle, source)


class EnforcementVisibilityTests(unittest.TestCase):
    def test_all_switches_off_is_logged_as_info(self) -> None:
        """默认口径（全关）走 INFO，并写明"默认 opt-in / 去哪里开"。"""

        settings = _bootstrap_settings()

        with self.assertLogs("bot.config", level="INFO") as logs:
            log_enforcement_switch_state(settings)

        joined = "\n".join(logs.output)
        self.assertIn("全部关闭", joined)
        self.assertIn("admin_moderation", joined)
        self.assertIn("默认 opt-in", joined)
        self.assertFalse(any(record.levelname == "WARNING" for record in logs.records))

    def test_enabled_switch_is_logged_as_warning_with_its_name(self) -> None:
        """生产显式打开之后：逐条 WARNING 点名，让"行为变化"在启动日志里可见。"""

        settings = _bootstrap_settings()
        settings.moderation.punish_quoted_author_enabled = False
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
        self.settings = self._make_settings()
        self.manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=self.settings,
            legacy_config_path="/tmp/nonexistent-smart-group-bot.toml",
            legacy_raw_env={},
        )
        await self.manager.initialize()

    def _make_settings(self) -> Settings:
        settings = Settings(
            _env_file=None,
            bot_token="42:TEST_TOKEN",
            super_admin_id=42,
            config_master_key="unit-test-master-key",
        )
        settings.bot.token = settings.bot_token
        return settings

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

        settings = self._make_settings()
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
        for name in _READERS:
            with self.subTest(reader=name):
                self.assertFalse(getattr(group, f"_{name}")(settings))

    async def test_explicitly_opening_through_the_runtime_config_hot_applies(self) -> None:
        """回归：默认关闭；走真实保存路径显式打开后，settings 与读取函数立刻跟上。

        这条覆盖生产侧"显式打开"的标准动作（``RuntimeConfigManager.save`` →
        ``apply_to_settings``），证明不是只改了个默认值。
        """

        # 1) 刚初始化（全新部署走一次性导入）时，三个开关都必须是关的。
        for name in _SWITCH_NAMES:
            with self.subTest(phase="bootstrap", name=name):
                self.assertFalse(getattr(self.settings.moderation, name))
        self.assertFalse(group._nsfw_image_guard_enabled(self.settings))
        self.assertFalse(group._punish_quoted_author_enabled(self.settings))
        self.assertFalse(group._admin_moderation_enabled(self.settings))

        # 2) 生产侧的标准动作：在设置界面保存一份显式打开的 payload。
        payload = self.manager.api_document()
        config = dict(payload["config"])
        moderation = dict(config["moderation"])
        for name in _SWITCH_NAMES:
            moderation[name] = True
        config["moderation"] = moderation
        await self.manager.save(
            config,
            expected_revision=int(payload["revision"]),
            updated_by=42,
        )

        # 3) 同一份 Settings 实例上，读到的就是"开"——热生效。
        for name in _SWITCH_NAMES:
            with self.subTest(phase="after-explicit-opt-in", name=name):
                self.assertTrue(getattr(self.settings.moderation, name))
        self.assertTrue(group._nsfw_image_guard_enabled(self.settings))
        self.assertTrue(group._punish_quoted_author_enabled(self.settings))
        self.assertTrue(group._admin_moderation_enabled(self.settings))

        # 4) 关回去也立刻生效。
        config["moderation"] = {name: False for name in _SWITCH_NAMES}
        await self.manager.save(
            config,
            expected_revision=int(self.manager.api_document()["revision"]),
            updated_by=42,
        )
        self.assertFalse(group._nsfw_image_guard_enabled(self.settings))
        self.assertFalse(group._punish_quoted_author_enabled(self.settings))
        self.assertFalse(group._admin_moderation_enabled(self.settings))

    async def test_reset_to_defaults_keeps_enforcement_off(self) -> None:
        """界面"恢复默认"必须恢复成关闭：默认口径就是 opt-in。"""

        from bot.services.runtime_config import RuntimeConfig

        defaults = RuntimeConfig.defaults() if hasattr(RuntimeConfig, "defaults") else RuntimeConfig()
        for name in _SWITCH_NAMES:
            with self.subTest(name=name):
                self.assertFalse(getattr(defaults.moderation, name))


if __name__ == "__main__":
    unittest.main()