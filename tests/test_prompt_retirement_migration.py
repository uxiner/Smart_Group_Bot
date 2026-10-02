"""F-014：从上游升级来的老库必须退役「owner 优先」的持久化提示词。

运行时提示词是**数据库持久化**的：光把 prompt/*.md 改成 owner 无关，老库里的
那一行仍然带着「owner 最高优先 / 优先回复 owner」等退役规则，而且改回代码也不会
自愈。真正的修法是一条**一次性**迁移——只重写命中退役标记的提示词，并把文档
schema_version 抬到当前值，保证之后（包括管理员自己改过的内容）不会被反复覆盖。

本文件锁定：

- 命中退役标记的 decision / persona / casual 被换成当前文件默认值；
- 不命中标记的自定义提示词一个字都不动（宁可漏改，不可误伤）；
- 迁移只跑一次：schema_version 已是当前值时不覆盖管理员的自定义；
- 走完整升级路径（老库 + initialize）后，生效的提示词里 owner 不再被特殊放行，
  且老库的 payload 被持久化成当前 schema 版本。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import RuntimeConfigRecord
from bot.services.runtime_config import (
    CONFIG_SCHEMA_VERSION,
    RuntimeConfigManager,
    _normalize_deprecated_runtime_payload,
)
from bot.utils.prompts import get_prompt, load_prompt_defaults

RETIRED_DECISION_RULE = (
    "6. If [SENDER_IS_OWNER]=yes: as long as the message is not clearly a "
    "private conversation with someone else, output `casual`."
)
RETIRED_PERSONA_RULE = (
    "4. The owner's instructions have the highest priority. Respond to the "
    "owner's requests immediately while not disrupting normal group chat functions."
)
RETIRED_CASUAL_RULE = (
    "2. Only for users tagged as `is_owner`: activate the additional "
    "clingy-girlfriend attribute, meaning clingier, more affectionate, more likely "
    "to prioritize the owner's messages"
)


class PromptRetirementMigrationUnitTests(unittest.TestCase):
    def test_retired_owner_priority_prompts_are_replaced_by_defaults(self) -> None:
        defaults = load_prompt_defaults()
        payload = {
            "schema_version": 1,
            "prompts": {
                "decision": f"{defaults['decision']}\n{RETIRED_DECISION_RULE}",
                "persona": f"custom persona preamble\n{RETIRED_PERSONA_RULE}",
                "casual": f"{RETIRED_CASUAL_RULE}\nrest of the custom casual prompt",
                "moderation": "我的自定义审核提示词 {rules_json}",
            },
        }

        migrated, changed = _normalize_deprecated_runtime_payload(payload)

        self.assertTrue(changed)
        self.assertEqual(migrated["prompts"]["decision"], defaults["decision"])
        self.assertEqual(migrated["prompts"]["persona"], defaults["persona"])
        self.assertEqual(migrated["prompts"]["casual"], defaults["casual"])
        # 没命中退役标记的自定义提示词不许被动
        self.assertEqual(
            migrated["prompts"]["moderation"], payload["prompts"]["moderation"]
        )
        self.assertEqual(migrated["schema_version"], CONFIG_SCHEMA_VERSION)

    def test_owner_is_not_specially_released_after_migration(self) -> None:
        defaults = load_prompt_defaults()
        payload = {
            "schema_version": 1,
            "prompts": {"decision": f"{defaults['decision']}\n{RETIRED_DECISION_RULE}"},
        }

        migrated, _changed = _normalize_deprecated_runtime_payload(payload)
        decision_prompt = migrated["prompts"]["decision"]

        self.assertNotIn("[SENDER_IS_OWNER]=yes: as long as", decision_prompt)
        self.assertNotIn(RETIRED_DECISION_RULE, decision_prompt)
        # 迁移后的口径必须和文件默认一致：owner 身份只做身份，不降低/提高回复门槛
        self.assertIn(
            "It must not change the reply decision in either direction",
            decision_prompt,
        )

    def test_migration_runs_only_once_per_schema_version(self) -> None:
        """已经是当前 schema 的文档不再被覆盖：管理员的自定义必须保住。"""

        custom = "我的自定义 persona\n" + RETIRED_PERSONA_RULE
        old_payload = {"schema_version": 1, "prompts": {"persona": custom}}

        # 第一次：老文档被迁移到当前 schema，退役措辞被换掉。
        migrated, first_changed = _normalize_deprecated_runtime_payload(old_payload)
        self.assertTrue(first_changed)
        self.assertEqual(migrated["schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertNotIn(RETIRED_PERSONA_RULE, migrated["prompts"]["persona"])

        # 第二次（真正的幂等检查）：已是当前 schema 的文档一个字都不许再改。
        # 注意 ``changed`` 反映的是"整个文档是否被规范化改写"（补齐缺失字段也会置位），
        # 不是"是否发生了退役迁移"，所以必须用二次规范化来验证，而不是直接断言
        # 老文档的 changed 为 False。
        again, second_changed = _normalize_deprecated_runtime_payload(migrated)
        self.assertFalse(second_changed)
        self.assertEqual(again["prompts"]["persona"], migrated["prompts"]["persona"])

    def test_payload_without_schema_version_is_treated_as_old(self) -> None:
        defaults = load_prompt_defaults()
        payload = {
            "prompts": {"decision": f"{defaults['decision']}\n{RETIRED_DECISION_RULE}"}
        }

        migrated, changed = _normalize_deprecated_runtime_payload(payload)

        self.assertTrue(changed)
        self.assertEqual(migrated["schema_version"], CONFIG_SCHEMA_VERSION)


class PromptRetirementUpgradePathTests(unittest.IsolatedAsyncioTestCase):
    """走完整升级路径：老库（schema_version=1 + 退役提示词）→ initialize。"""

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

    async def _write_legacy_owner_priority_row(self) -> None:
        defaults = load_prompt_defaults()
        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
            payload = dict(row.payload)
            prompts = dict(payload.get("prompts") or {})
            prompts["decision"] = f"{defaults['decision']}\n{RETIRED_DECISION_RULE}"
            prompts["persona"] = f"{defaults['persona']}\n{RETIRED_PERSONA_RULE}"
            prompts["casual"] = f"{defaults['casual']}\n{RETIRED_CASUAL_RULE}"
            prompts["moderation"] = "老库自定义审核提示词 {rules_json}"
            payload["prompts"] = prompts
            payload["schema_version"] = 1
            row.payload = payload
            row.schema_version = 1
            await session.commit()

    async def _reload(self) -> RuntimeConfigManager:
        settings = Settings(
            _env_file=None,
            bot_token="42:TEST_TOKEN",
            super_admin_id=42,
            config_master_key="unit-test-master-key",
        )
        settings.bot.token = settings.bot_token
        manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=settings,
            legacy_config_path="/tmp/nonexistent-smart-group-bot.toml",
            legacy_raw_env={},
        )
        await manager.initialize()
        return manager

    async def test_upgraded_row_no_longer_prioritizes_the_owner(self) -> None:
        await self._write_legacy_owner_priority_row()

        reloaded = await self._reload()
        defaults = load_prompt_defaults()

        self.assertEqual(reloaded.config.prompts.decision, defaults["decision"])
        self.assertEqual(reloaded.config.prompts.persona, defaults["persona"])
        self.assertEqual(reloaded.config.prompts.casual, defaults["casual"])
        # 自定义审核提示词没被动
        self.assertEqual(
            reloaded.config.prompts.moderation, "老库自定义审核提示词 {rules_json}"
        )
        # 生效中的提示词（真正喂给模型的那一份）也不含退役规则
        self.assertNotIn("[SENDER_IS_OWNER]=yes: as long as", get_prompt("decision"))
        self.assertIn(
            "It must not change the reply decision in either direction",
            get_prompt("decision"),
        )

    async def test_upgraded_row_is_persisted_at_the_current_schema_version(self) -> None:
        await self._write_legacy_owner_priority_row()

        await self._reload()

        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
        self.assertEqual(row.schema_version, CONFIG_SCHEMA_VERSION)
        self.assertEqual(row.payload["schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertNotIn(
            "[SENDER_IS_OWNER]=yes: as long as", row.payload["prompts"]["decision"]
        )

    async def test_second_start_does_not_revert_an_admin_edit(self) -> None:
        """升级只做一次：管理员之后改回带退役字样的自定义也不该被再覆盖。"""

        await self._write_legacy_owner_priority_row()
        await self._reload()

        async with self.session_factory() as session:
            row = await session.get(RuntimeConfigRecord, 1)
            payload = dict(row.payload)
            prompts = dict(payload["prompts"])
            prompts["persona"] = "管理员故意保留的自定义 persona\n" + RETIRED_PERSONA_RULE
            payload["prompts"] = prompts
            row.payload = payload
            await session.commit()

        reloaded = await self._reload()

        self.assertIn(
            "管理员故意保留的自定义 persona", reloaded.config.prompts.persona
        )
