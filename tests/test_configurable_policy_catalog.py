"""配置目录的自检：每个新字段都必须有**真实**读侧，并且默认值与 schema 一致。

这个文件是"新加了一个参数但没人读"这种半成品的最后一道闸：

* 机器清单里每个字段都必须登记 ``read_consumers``，且登记的 ``file:symbol`` 里的
  文件必须真实存在、符号必须真的出现在那个文件里；
* ``bot.config`` 里的**读侧视图**默认值必须与 ``runtime_config`` 的 strict schema
  逐字段相等（两边不一致会让"没绑定运行时配置的进程"跑出另一套口径）；
* ``docs/configuration-fields.json`` 必须与当前 schema 一致（不许手改漂移）；
* restart 字段清单必须与 ``Field(json_schema_extra={"reload_kind": "restart"})`` 一致。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from bot.config import (
    ActivityPolicyConfig,
    CheckinReminderPolicyConfig,
    DisplayPolicyConfig,
    EconomyPolicyConfig,
    PrivateChatPolicyConfig,
    ResourcesPolicyConfig,
)
from bot.services import policy_runtime
from bot.services.runtime_config import (
    RESTART_REQUIRED_PATHS,
    ActivitySettingsConfig,
    CheckinReminderSettingsConfig,
    DisplaySettingsConfig,
    EconomySettingsConfig,
    PrivateChatSettingsConfig,
    ResourceSettingsConfig,
)
from bot.tools.config_catalog import MODERATION_FIELDS, build_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "docs" / "configuration-fields.json"

#: runtime strict schema ↔ bot.config 读侧视图。
SECTION_PAIRS: tuple[tuple[type, type], ...] = (
    (PrivateChatSettingsConfig, PrivateChatPolicyConfig),
    (EconomySettingsConfig, EconomyPolicyConfig),
    (ActivitySettingsConfig, ActivityPolicyConfig),
    (CheckinReminderSettingsConfig, CheckinReminderPolicyConfig),
    (DisplaySettingsConfig, DisplayPolicyConfig),
    (ResourceSettingsConfig, ResourcesPolicyConfig),
)


class CatalogTests(unittest.TestCase):
    def test_every_new_field_has_a_registered_real_consumer(self) -> None:
        missing = [
            path
            for path in policy_runtime.CONSUMER_REGISTRY
            if not path.split(".")[0]
            in {"private_chat", "economy", "activity", "checkin_reminder", "display", "resources", "moderation"}
        ]
        self.assertEqual(missing, [], "CONSUMER_REGISTRY 里出现了未知段的字段")

        for path, consumers in policy_runtime.CONSUMER_REGISTRY.items():
            with self.subTest(path=path):
                self.assertTrue(consumers, f"{path} 没有登记读侧")
                for consumer in consumers:
                    file_part, _, symbol = consumer.partition(":")
                    target = REPO_ROOT / file_part
                    self.assertTrue(
                        target.is_file(), f"{path} 的读侧文件不存在：{file_part}"
                    )
                    source = target.read_text(encoding="utf-8")
                    self.assertIn(
                        symbol,
                        source,
                        f"{path} 的读侧符号 {symbol} 不在 {file_part} 里",
                    )

    def test_every_schema_field_is_registered(self) -> None:
        catalog = build_catalog()
        registered = set(policy_runtime.CONSUMER_REGISTRY)
        for entry in catalog["fields"]:
            self.assertIn(
                entry["path"],
                registered,
                f"{entry['path']} 进了机器清单但没登记读侧 —— 这不算完成",
            )

    def test_catalog_entries_carry_the_required_metadata(self) -> None:
        for entry in build_catalog()["fields"]:
            with self.subTest(path=entry["path"]):
                for key in (
                    "path",
                    "default",
                    "bounds",
                    "reload_kind",
                    "read_consumers",
                    "api_role",
                    "test_node",
                ):
                    self.assertIn(key, entry)
                self.assertIn(entry["reload_kind"], {"hot", "restart"})
                self.assertEqual(entry["api_role"], "super_admin")
                self.assertTrue(entry["read_consumers"])
                self.assertTrue((REPO_ROOT / entry["test_node"]).is_file())

    def test_checked_in_catalog_matches_the_schema(self) -> None:
        self.assertTrue(
            CATALOG_PATH.is_file(),
            "缺少 docs/configuration-fields.json；跑 python -m bot.tools.config_catalog",
        )
        self.assertEqual(
            json.loads(CATALOG_PATH.read_text(encoding="utf-8")),
            build_catalog(),
            "机器目录与 schema 漂移了；重跑 python -m bot.tools.config_catalog",
        )

    def test_restart_paths_match_the_field_markers(self) -> None:
        marked = {
            f"resources.{name}"
            for name, field in ResourceSettingsConfig.model_fields.items()
            if isinstance(field.json_schema_extra, dict)
            and field.json_schema_extra.get("reload_kind") == "restart"
        }
        self.assertEqual(
            set(RESTART_REQUIRED_PATHS) - {"bot.parse_mode"},
            marked,
            "RESTART_REQUIRED_PATHS 与字段上的 restart 标记不一致",
        )
        self.assertIn("bot.parse_mode", RESTART_REQUIRED_PATHS)

    def test_moderation_binding_defaults_are_neutral(self) -> None:
        from bot.services.runtime_config import ModerationSettingsConfig

        defaults = ModerationSettingsConfig()
        self.assertEqual(
            defaults.log_channel_id,
            0,
            "公开树默认不得绑定任何具体频道",
        )
        self.assertEqual(
            defaults.review_handover_mention,
            "",
            "公开树默认不得 @ 任何具体 bot",
        )
        self.assertIn("review_handover_mention", MODERATION_FIELDS)


class DefaultsParityTests(unittest.TestCase):
    """读侧视图与 strict schema 的默认值必须逐字段相等。"""

    def test_view_defaults_equal_schema_defaults(self) -> None:
        for schema_model, view_model in SECTION_PAIRS:
            schema_defaults = schema_model()
            view_defaults = view_model()
            schema_names = set(schema_model.model_fields)
            view_names = set(view_model.model_fields)
            self.assertEqual(
                schema_names,
                view_names,
                f"{schema_model.__name__} 与 {view_model.__name__} 的字段集不同",
            )
            for name in sorted(schema_names):
                with self.subTest(model=schema_model.__name__, field=name):
                    if schema_model is EconomySettingsConfig and name == "lottery_prizes":
                        # 读侧把奖池摊成 (payout, weight, label) 三元组，比对时
                        # 走 runtime_config 里那条唯一的转换函数，不复制一份逻辑。
                        from bot.services.runtime_config import _economy_policy_payload

                        payload = _economy_policy_payload(schema_defaults)
                        self.assertEqual(
                            payload[name], getattr(view_defaults, name)
                        )
                        continue
                    self.assertEqual(
                        getattr(schema_defaults, name),
                        getattr(view_defaults, name),
                        f"{schema_model.__name__}.{name} 的默认值与读侧视图不一致",
                    )

    def test_unbound_process_runs_the_same_as_a_fresh_install(self) -> None:
        # 没有绑定运行时配置时，快照必须等于"未配置"的 schema 默认值。
        policy_runtime.unbind()
        try:
            from bot.services.runtime_config import RuntimeConfig

            fresh = RuntimeConfig()
            self.assertEqual(
                policy_runtime.private_chat_policy().per_user_daily_limit,
                fresh.private_chat.per_user_daily_limit,
            )
            self.assertEqual(
                policy_runtime.economy_policy().lottery_total_weight,
                fresh.economy.lottery_total_weight,
            )
            self.assertEqual(
                policy_runtime.activity_policy().weekly_total_points,
                fresh.activity.weekly_total_points,
            )
            self.assertEqual(
                policy_runtime.display_policy().bot_display_name,
                fresh.display.bot_display_name,
            )
        finally:
            policy_runtime.unbind()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
