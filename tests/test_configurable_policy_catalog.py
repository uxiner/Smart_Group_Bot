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
            in {
                "private_chat",
                "economy",
                "activity",
                "checkin_reminder",
                "display",
                "resources",
                "moderation",
                "admin_ops",
                "group_ops",
                "telegram_send",
            }
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


class AnnotationResolutionTests(unittest.TestCase):
    """``typing.get_type_hints`` 必须能真的解析每个注解（父代理 pyflakes 抓到过
    ``config.py`` 里一个指向不存在类的返回注解：名字不存在时不报，运行期才炸）。

    这里把"每个本轮涉及的模块的每个函数注解都能解析"做成门禁，并额外跑一次
    pyflakes 的 ``undefined name`` 检查——两者覆盖的是同一类问题：**类型/名字写错
    但只有真正求值时才暴露**。
    """

    #: 只覆盖 ``bot.config``：它没有 ``TYPE_CHECKING`` 守卫，所有注解都必须能在
    #: 运行期解析。其它模块（policy_runtime / startup_resources / runtime_config）
    #: 按惯例把 ``Settings`` / ``RuntimeConfig`` 放在 ``TYPE_CHECKING`` 下，
    #: ``get_type_hints`` 解析不了是**预期**的，不该被这条门禁误伤——那类注解由下面
    #: 的 pyflakes "undefined name" 门禁覆盖（它看的是真会 NameError 的名字）。
    MODULES = ("bot.config",)

    def test_every_public_annotation_resolves(self) -> None:
        import importlib
        import inspect
        import typing

        unresolved: list[str] = []
        for module_name in self.MODULES:
            module = importlib.import_module(module_name)
            # 只看**本模块自己定义**的东西：``bot.config`` 把 pydantic 的 Field /
            # select 等符号导进了自己的命名空间，它们的注解会提到 pydantic 内部类型
            # （例如 JsonValue），那些不是我们的代码，也不该由这条门禁负责。
            targets = [
                (f"{module_name}.{name}", obj)
                for name, obj in vars(module).items()
                if (
                    inspect.isclass(obj) or inspect.isfunction(obj)
                )
                and getattr(obj, "__module__", None) == module_name
            ]
            for owner_name, owner in targets:
                for attr_name, member in list(vars(owner).items()):
                    if attr_name.startswith("__") and not attr_name.endswith("__"):
                        continue
                    func = member.__func__ if isinstance(member, (classmethod,)) else member
                    if isinstance(member, (staticmethod, classmethod)):
                        func = member.__func__
                    if not inspect.isfunction(func):
                        continue
                    try:
                        typing.get_type_hints(func)
                    except Exception as exc:  # NameError / TypeError / AttributeError
                        unresolved.append(
                            f"{owner_name}.{attr_name}: {type(exc).__name__}: {exc}"
                        )
        self.assertEqual(
            unresolved, [], "以下注解无法解析（多半是写错了类名）：\n" + "\n".join(unresolved)
        )

    def test_no_undefined_names_in_the_modules_we_touched(self) -> None:
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", "bot", "tests", "tools"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        undefined = [
            line
            for line in result.stdout.splitlines()
            if "undefined name" in line
        ]
        self.assertEqual(
            undefined, [], "pyflakes 报出未定义的名字：\n" + "\n".join(undefined)
        )

    def test_no_new_undefined_names_compared_with_the_frozen_base(self) -> None:
        """新引入的 lint 债务必须是 0；既有债务不在这轮范围内，但不许变多。"""

        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        base = root / ".git"
        if not base.exists():
            self.skipTest("没有 .git，跳过基线对比")
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", "bot", "tests", "tools"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        count = sum(1 for line in result.stdout.splitlines() if "undefined name" in line)
        self.assertEqual(count, 0)


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
