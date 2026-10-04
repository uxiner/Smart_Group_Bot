"""修复批 P1-2 / D3-39 + D3-40 + D3-42：配置面（Mini App 入口 + 关键开关三方接线）。

逐条对应 ``GAP-D3-modules-web.md``：

* **D3-42** ``ROLE_META`` 只有 6 个角色，渲染循环 ``Object.keys(ROLE_META).map(renderRoleCard)``
  把 ``models.skill`` 整块排除在模型页 / 供应商校验 / 回退重命名之外——而后端完整支持
  （``rc:1051`` 用 ``models.skill or main`` 建 ``settings.bot.skill_model``），
  运维只能手改 DB payload（还要知道完整的 ``ChatRoleConfig`` 结构与嵌套 JSON）。
* **D3-39** 第 ④ 期「长期记忆」整块 **11 个开关在 Mini App 完全无入口**。它们可 PUT、
  可落库、有 revision 保护、每个都有真消费者——但真·总开关
  ``memory_facts_enabled`` / ``memory_tool_enabled`` **无法关闭**；而 UI 上那个文案相似的
  ``memory_recall_enabled`` 并不是总开关。
* **D3-40** ``load_settings()`` 是死代码（根因），但**不**去改成被调用（那会一次性激活
  约 20 个 env 种子，行为面过大）。只把 ``enable_rich_messages`` 补齐三方接线：
  schema + ``apply_to_settings`` + Mini App。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from bot.services.runtime_config import BotBehaviorConfig, RuntimeConfig

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "bot" / "web" / "static" / "app.js").read_text(encoding="utf-8")

#: 第 ④ 期长期记忆整块（11 项）。
LONG_TERM_MEMORY_SWITCHES = (
    "memory_facts_enabled",
    "memory_extract_enabled",
    "memory_extract_interval_minutes",
    "memory_extract_min_messages",
    "memory_extract_daily_cap",
    "memory_extract_batch_max",
    "memory_tool_enabled",
    "memory_tool_daily_cap",
    "memory_recall_limit",
    "memory_event_ttl_days",
    "memory_deleted_retention_days",
)


def _data_paths() -> set[str]:
    """UI 上真正能编辑的路径：``field("x"...)`` / ``toggle("x"...)`` 的第一个参数。"""

    return set(
        re.findall(r'\b(?:field|toggle)\(\s*"(bot\.[a-z0-9_.]+)"', APP_JS)
    )


class D3_42SkillRoleTests(unittest.TestCase):
    def test_skill_role_is_in_role_meta(self) -> None:
        block = re.search(r"const ROLE_META = \{(.*?)\n  \};", APP_JS, re.S)
        self.assertIsNotNone(block, "找不到 ROLE_META")
        self.assertIn("skill:", block.group(1), "skill 角色必须在模型页可见")

    def test_skill_deadline_default_matches_the_llm_stage_table(self) -> None:
        self.assertRegex(
            APP_JS,
            r"skill:\s*\{[^}]*deadlineDefault:\s*120\s*\}",
            "deadlineDefault 必须对齐 bot/services/llm.py 的 _LLM_STAGE_DEADLINES['skill']=120",
        )
        llm_source = (ROOT / "bot" / "services" / "llm.py").read_text(encoding="utf-8")
        self.assertRegex(llm_source, r'"skill":\s*120\.0,')

    def test_null_skill_role_is_normalized_instead_of_crashing(self) -> None:
        """「留空 = null」必须被归一成「留空 = 继承主模型」的形状。"""

        self.assertIn("function normalizeModelRoles(", APP_JS)
        self.assertIn(
            "normalizeModelRoles();", APP_JS, "加载配置文档后必须调用一次"
        )
        # 供应商校验那行读的是 `role.provider`；null 角色不归一就会在这里炸。
        self.assertIn("if (role.provider && !known.has(role.provider))", APP_JS)

    def test_the_backend_still_accepts_skill_as_null(self) -> None:
        self.assertIsNone(RuntimeConfig().models.skill, "留空 = null 是合法状态")
        self.assertIn("skill", RuntimeConfig.model_fields["models"].annotation.model_fields)

    def test_null_role_is_synthesized_by_cloning_main_not_an_empty_shell(self) -> None:
        """回归：``null`` 在后端等价于 ``models.skill or main``，即**继承主模型的
        temperature / max_tokens / timeout / 回退链**。若 UI 归一化时造一个
        ``fallbacks: []`` 的空壳，管理员随手点一次保存就会悄悄丢掉主模型的回退链。
        """

        body = re.search(
            r"function normalizeModelRoles\(\) \{[\s\S]*?\n  \}\n", APP_JS
        )
        self.assertIsNotNone(body, "找不到 normalizeModelRoles")
        text = body.group(0)
        synthesis = text[text.index("if (!role || typeof role") :]
        self.assertIn(
            "JSON.parse(JSON.stringify(template))",
            synthesis,
            "null 角色必须从主模型（template）整份克隆后只把 provider/model 留空",
        )
        literal = synthesis[ synthesis.index("models[roleName] = {") : ][: synthesis[ synthesis.index("models[roleName] = {") : ].index("};") ]
        self.assertNotIn(
            "fallbacks",
            literal,
            "归一化出来的对象里不得出现 fallbacks（会丢主模型回退链）",
        )
        self.assertNotIn("temperature", literal, "同理，temperature 必须来自克隆")
        self.assertNotIn("total_deadline_sec", literal.replace("total_deadline_sec = ROLE_META[roleName].deadlineDefault", ""))

    def test_leaving_the_skill_role_blank_keeps_inheriting_main(self) -> None:
        config = RuntimeConfig.model_validate(
            {"models": {"skill": {"provider": "", "model": ""}}}
        )
        self.assertEqual(config.models.skill.provider, "")
        self.assertEqual(config.models.skill.model, "")


class D3_39MemorySwitchUiTests(unittest.TestCase):
    def test_every_long_term_memory_switch_has_a_mini_app_entry(self) -> None:
        paths = _data_paths()
        for name in LONG_TERM_MEMORY_SWITCHES:
            with self.subTest(switch=name):
                self.assertIn(
                    f"bot.{name}",
                    paths,
                    f"第 ④ 期开关 {name} 在 Mini App 里没有任何入口（可 PUT 但关不掉）",
                )

    def test_the_master_switch_is_labelled_as_such(self) -> None:
        self.assertIn(
            'toggle("bot.memory_facts_enabled", "启用长期记忆（总开关）"',
            APP_JS,
        )
        self.assertIn(
            'toggle("bot.memory_tool_enabled", "启用模型主动记忆（remember 工具）"',
            APP_JS,
        )

    def test_memory_recall_enabled_is_not_presented_as_the_master_switch(self) -> None:
        match = re.search(
            r'toggle\("bot\.memory_recall_enabled",\s*"([^"]+)"\s*,\s*"([^"]*)"',
            APP_JS,
        )
        self.assertIsNotNone(match, "找不到 memory_recall_enabled 的开关文案")
        label, hint = match.group(1), match.group(2)
        self.assertNotIn("总开关", label)
        self.assertIn(
            "不是",
            hint,
            "必须在 hint 里点明它**不是**第 ④ 期总开关（关掉它照样写库提炼）",
        )

    def test_the_schema_still_backs_every_rendered_switch(self) -> None:
        fields = RuntimeConfig.model_fields["bot"].annotation.model_fields
        for name in LONG_TERM_MEMORY_SWITCHES:
            with self.subTest(switch=name):
                self.assertIn(
                    name, fields, f"{name} 没有出现在 RuntimeConfig 的 bot schema 里"
                )

    def test_bounds_match_the_schema(self) -> None:
        """UI 的 min/max 必须落在 schema 的 ge/le 内，否则管理员根本存不进去。"""

        fields = RuntimeConfig.model_fields["bot"].annotation.model_fields
        rendered = APP_JS
        for name in LONG_TERM_MEMORY_SWITCHES:
            field = fields[name]
            with self.subTest(switch=name):
                match = re.search(
                    r'field\("bot\.%s".*?min:\s*(\d+),\s*max:\s*(\d+)' % name,
                    rendered,
                    re.S,
                )
                if match is None:
                    self.assertIn(f'toggle("bot.{name}"', rendered)
                    continue
                constraints = {
                    key: value
                    for item in (field.metadata or [])
                    for key, value in (item._asdict() if hasattr(item, "_asdict") else {})
                    if key in {"ge", "le"}
                }
                if not constraints:
                    continue
                self.assertEqual(int(match.group(1)), constraints.get("ge", 0))
                self.assertEqual(int(match.group(2)), constraints.get("le", match.group(2)))


class D3_40RichMessagesWiringTests(unittest.TestCase):
    def test_schema_declares_the_switch(self) -> None:
        self.assertIn("enable_rich_messages", BotBehaviorConfig.model_fields)

    def test_apply_to_settings_writes_it_onto_the_process_settings(self) -> None:
        source = (ROOT / "bot" / "services" / "runtime_config.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "settings.bot.enable_rich_messages = bot.enable_rich_messages",
            source,
            "apply_to_settings 必须把它落到进程内的 BotConfig 上，否则读取侧恒为类默认",
        )

    def test_the_settings_export_mirrors_the_bootstrap_override(self) -> None:
        source = (ROOT / "bot" / "services" / "runtime_config.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("enable_rich_messages=(", source)

    def test_mini_app_offers_the_switch(self) -> None:
        self.assertIn('toggle("bot.enable_rich_messages"', APP_JS)

    def test_load_settings_is_still_not_wired(self) -> None:
        """refs 明确要求：**不要**顺手把 ``load_settings()`` 改成被调用。"""

        callers = []
        for path in sorted((ROOT / "bot").rglob("*.py")):
            for line in path.read_text(encoding="utf-8").splitlines():
                code = line.split("#", 1)[0].strip()
                if not code or code.startswith(("def ", "async def ")):
                    continue
                if re.search(r"(?<![\w.])load_settings\s*\(", code):
                    callers.append(f"{path.name}: {code}")
        self.assertEqual(
            callers,
            [],
            f"load_settings() 不该有生产调用点（会一次性激活约 20 个 env 种子）：{callers}",
        )

    def test_the_read_side_actually_consumes_it(self) -> None:
        source = (ROOT / "bot" / "handlers" / "group.py").read_text(encoding="utf-8")
        self.assertIn('getattr(settings.bot, "enable_rich_messages", False)', source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
