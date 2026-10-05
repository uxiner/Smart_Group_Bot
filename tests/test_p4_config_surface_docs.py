"""P4-11（D3-41 / D3-43 / D3-44）：配置面注释/说明文字与真实默认值、真实生效路径一致。

本文件**不改任何逻辑**，它把「注释说的是不是真的」变成可执行的检查，并记录本批
实际修掉的几处脱节（详见 ``FIX-p4.md``）：

* D3-43：``bot/services/skills/webfetch.py`` 的注释指向 ``config.py:545-548``，
  那套 ``firecrawl_*`` 字段早已挪到 562-565（照着旧行号跳过去会找不到）。
* D3-41：``config.py`` 里 ``firecrawl_*`` 整块被注释成 "websearch skill backend"，
  其实 ``firecrawl_timeout_sec`` 属于 webfetch、``firecrawl_search_timeout_sec``
  属于 websearch，是两个旋钮。
* B-41：``drop_pending_updates`` 在生产不可达（校验归一 + update_delivery 强制
  False），注释里必须写明现状，而不是让人以为写了 true 就会丢消息。
* D3-44：``group_summary`` 的夹取范围要在 config.py / runtime_config / app.js /
  group_summary.py 四处对齐——本文件逐字段核对 default/low/high。

这些断言在旧代码上同样成立（它们核对的是**已经对齐**的那部分）；本文件的作用是
防止下一次改动再次把它们拉散。
"""

from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.config import Settings
from bot.services import group_summary
from bot.services.skills.webfetch import WebFetchSkill
from bot.services.skills.websearch import WebSearchSkill

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (ROOT / "bot" / "config.py").read_text(encoding="utf-8")
RUNTIME_CONFIG_PY = (ROOT / "bot" / "services" / "runtime_config.py").read_text(
    encoding="utf-8"
)
GROUP_SUMMARY_PY = (ROOT / "bot" / "services" / "group_summary.py").read_text(
    encoding="utf-8"
)
WEBFETCH_PY = (ROOT / "bot" / "services" / "skills" / "webfetch.py").read_text(
    encoding="utf-8"
)
APP_JS = (ROOT / "bot" / "web" / "static" / "app.js").read_text(encoding="utf-8")
CONFIG_TOML = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))


class StaleLineReferenceTests(unittest.TestCase):
    def test_the_webfetch_comment_points_at_real_field_names(self) -> None:
        """注释按**字段名**指路，不写行号（行号必然随编辑漂移，D3-43）。"""

        head = WEBFETCH_PY.split("class WebFetchSkill")[1][:2000]
        self.assertIn("bot/config.py", head)
        for name in ("firecrawl_api_key", "firecrawl_api_base", "firecrawl_timeout_sec"):
            with self.subTest(field=name):
                self.assertIn(name, head)

    def test_no_comment_references_a_config_py_line_range(self) -> None:
        """全仓不再有 ``config.py:123-456`` 这种必然过期的引用。"""

        offenders = []
        for path in sorted((ROOT / "bot").rglob("*.py")):
            for match in re.finditer(r"config\.py:\d+", path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
        self.assertEqual(offenders, [])


class FirecrawlSurfaceDocsTests(unittest.TestCase):
    def test_the_block_comment_no_longer_claims_websearch_owns_the_fetch_timeout(self) -> None:
        block = CONFIG_PY.split("# Firecrawl 接入")[1].split("av_enabled")[0]
        self.assertIn("firecrawl_timeout_sec", block)
        self.assertIn("webfetch", block)
        self.assertIn("firecrawl_search_timeout_sec", block)
        self.assertIn("websearch", block)

    def test_the_two_timeouts_are_distinct_knobs_with_distinct_defaults(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual(settings.firecrawl_timeout_sec, 20.0)
        self.assertEqual(settings.firecrawl_search_timeout_sec, 18.0)
        self.assertNotEqual(
            settings.firecrawl_timeout_sec, settings.firecrawl_search_timeout_sec
        )

    def test_both_skills_read_those_top_level_fields(self) -> None:
        """技能拿到的是根 Settings，两个技能各自读各自那个超时。"""

        settings = SimpleNamespace(
            firecrawl_api_key="fc-key",
            firecrawl_api_base="https://example.invalid",
            firecrawl_timeout_sec=7.0,
            firecrawl_search_timeout_sec=9.0,
        )
        self.assertEqual(WebFetchSkill(settings)._timeout, 7.0)
        self.assertEqual(WebSearchSkill(settings)._search_timeout, 9.0)


class DropPendingUpdatesReachabilityTests(unittest.TestCase):
    """B-41：这个开关在生产不可达，注释必须写明，而不是假装它有效。"""

    def test_the_config_comment_says_it_is_inert(self) -> None:
        match = re.search(
            r"((?:[ \t]*#.*\n)+?)[ \t]*drop_pending_updates: bool = False",
            CONFIG_PY,
            re.M,
        )
        self.assertIsNotNone(match, "应当能定位到 drop_pending_updates 的注释块")
        comment = match.group(1)
        self.assertIn("不可达", comment)
        self.assertIn("update_delivery", comment)
        self.assertIn("_preserve_pending_updates", comment)

    def test_the_schema_really_does_normalize_every_write_to_false(self) -> None:
        from bot.services.runtime_config import BotBehaviorConfig

        self.assertFalse(
            BotBehaviorConfig.model_validate({"drop_pending_updates": True})
            .drop_pending_updates
        )

    def test_the_delivery_path_really_ignores_it(self) -> None:
        source = (ROOT / "bot" / "services" / "update_delivery.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("polling_drop_pending_updates = False", source)
        self.assertIn("ignored deprecated drop_pending_updates=true", source)
        # 真删 webhook / 收尾的两处也写死 False，没有任何地方透传这个开关。
        self.assertEqual(source.count("drop_pending_updates=False"), 2)
        self.assertNotIn("drop_pending_updates=settings.bot", source)

    def test_the_shipped_toml_says_so_too(self) -> None:
        self.assertIs(CONFIG_TOML["bot"]["drop_pending_updates"], False)
        text = (ROOT / "config.toml").read_text(encoding="utf-8")
        self.assertIn("生产不可达", text)


class GroupSummarySurfaceConsistencyTests(unittest.TestCase):
    """D3-44：夹取范围要在四处对齐（本文件逐字段核对 default/low/high）。

    ``(settings.bot 里的属性名后缀, GroupSummaryConfig 的字段名)``。
    注意两个命名空间**不完全同名**：``group_summary_max_tokens`` 落到 dataclass
    上叫 ``max_summary_tokens``。
    """

    FIELDS = (
        ("recent_raw_messages", "recent_raw_messages"),
        ("max_tokens", "max_summary_tokens"),
        ("batch_max_messages", "batch_max_messages"),
        ("batch_max_input_tokens", "batch_max_input_tokens"),
        ("global_concurrency", "global_concurrency"),
        ("deadline_seconds", "deadline_seconds"),
        ("queue_wait_seconds", "queue_wait_seconds"),
        ("min_refresh_seconds", "min_refresh_seconds"),
        ("failure_backoff_seconds", "failure_backoff_seconds"),
        ("failure_backoff_max_seconds", "failure_backoff_max_seconds"),
        ("pending_capacity", "pending_capacity"),
        ("trigger_messages", "trigger_messages"),
        ("trigger_budget_ratio", "trigger_budget_ratio"),
    )

    @staticmethod
    def _num(value: str) -> float:
        return float(value.replace("_", ""))

    def _clamp_bounds(self) -> dict[str, tuple[float, float, float]]:
        block = GROUP_SUMMARY_PY.split("def group_summary_config(")[1]
        found: dict[str, tuple[float, float, float]] = {}
        for _target, attr, default, low, high in re.findall(
            r"(\w+)=_bounded_(?:int|float)\(\s*\n"
            r'\s*getattr\(view, "([a-z_]+)", None\),\s*\n'
            r"\s*default=([^,]+),\s*\n"
            r"\s*low=([^,]+),\s*\n"
            r"\s*high=([^,]+),",
            block,
        ):
            found[attr] = (self._num(default), self._num(low), self._num(high))
        return found

    @staticmethod
    def _declared_default(source: str, field: str) -> float:
        """从 ``group_summary_x: int = Field(default=200, ge=20, le=10_000)``
        或 ``group_summary_x: float = 0.85`` 里取默认值（允许多行 Field）。"""

        match = re.search(
            rf"    {field}: (?:int|float) = (?:Field\(\s*)?default=([0-9_.]+)",
            source,
        ) or re.search(
            rf"    {field}: (?:int|float) = ([0-9_.]+)\s*$",
            source,
            re.M,
        )
        assert match is not None, f"{field} 的声明里找不到默认值"
        return float(match.group(1).replace("_", ""))

    def test_every_group_summary_field_is_clamped_in_the_reader(self) -> None:
        found = self._clamp_bounds()
        for attr, _dataclass_field in self.FIELDS:
            with self.subTest(field=attr):
                self.assertIn(f"group_summary_{attr}", found)

    def test_reader_clamp_matches_the_dataclass_defaults(self) -> None:
        found = self._clamp_bounds()
        for attr, dataclass_field in self.FIELDS:
            with self.subTest(field=attr):
                self.assertEqual(
                    found[f"group_summary_{attr}"][0],
                    float(getattr(group_summary.GroupSummaryConfig, dataclass_field)),
                )

    def test_reader_clamp_matches_the_botconfig_declaration(self) -> None:
        found = self._clamp_bounds()
        for attr, _dataclass_field in self.FIELDS:
            with self.subTest(field=attr):
                self.assertEqual(
                    found[f"group_summary_{attr}"][0],
                    self._declared_default(CONFIG_PY, f"group_summary_{attr}"),
                    f"group_summary_{attr} 的默认值在两处不一致",
                )

    def test_reader_clamp_matches_the_runtime_config_schema(self) -> None:
        found = self._clamp_bounds()
        for attr, _dataclass_field in self.FIELDS:
            with self.subTest(field=attr):
                self.assertEqual(
                    found[f"group_summary_{attr}"][0],
                    self._declared_default(RUNTIME_CONFIG_PY, f"group_summary_{attr}"),
                    f"group_summary_{attr} 的默认值在两处不一致",
                )

    def test_mini_app_min_max_cover_the_effective_range(self) -> None:
        found = self._clamp_bounds()
        for attr, _dataclass_field in self.FIELDS:
            with self.subTest(field=attr):
                control = re.search(
                    rf'field\("bot\.group_summary_{attr}"[^\n]*?min:\s*([0-9.]+),'
                    r"\s*max:\s*([0-9.]+)",
                    APP_JS,
                )
                self.assertIsNotNone(control, f"Mini App 里没有 group_summary_{attr}")
                _default, low, high = found[f"group_summary_{attr}"]
                self.assertLessEqual(float(control.group(1)), low)
                self.assertGreaterEqual(float(control.group(2)), high)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
