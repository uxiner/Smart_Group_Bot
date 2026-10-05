"""B-01 / C4-02 / D3-38：审核日志频道的**默认值、注释与文档披露**必须自洽。

用户 2026-10 裁定：`log_channel_id` **保留现有默认值**（`-1000000000001` 就是有意
为之的默认值，不是占位符，也不改成 0），只把注释与文档改对。

原状（审计记的三处缺陷，本文件逐条钉住"已修好"）：

- ``bot/config.py`` 的注释写「0 表示未配置（此时频道投递不可用）」，紧跟着的默认值
  却是那个具体的频道 id → **注释与实现直接矛盾**（C3-12 / D3-38）。
- README 只说「开关 ``log_channel_enabled``（默认开）/ ``log_channel_id``」，
  **没有披露这个值已经预填**（C4-02）。
- 覆盖入口的实际能力没有一处写对：env 其实**无效**，Mini App 界面其实**没有控件**。

本文件锁定的口径：

- 默认值仍然是 ``-1000000000001``（不许被顺手改成 0）；``0`` 才表示"未配置"；
- ``_admin_log_channel_id`` 把 0 视为不可用 → 频道路由整体关闭、回退私聊老路径；
- 注释/README 必须写出默认频道 id 与三种覆盖入口的真实能力。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.config import Settings
from bot.handlers import group
from bot.services.runtime_config import ModerationSettingsConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXPECTED_DEFAULT = -1000000000001


class LogChannelDefaultTests(unittest.TestCase):
    def test_default_value_is_kept(self) -> None:
        """裁定：默认值保留 ``-1000000000001``，**不改**。"""

        settings = Settings(_env_file=None)
        self.assertEqual(settings.moderation.log_channel_id, _EXPECTED_DEFAULT)
        self.assertTrue(settings.moderation.log_channel_enabled)
        self.assertEqual(
            ModerationSettingsConfig().log_channel_id, _EXPECTED_DEFAULT
        )

    def test_zero_means_unconfigured_and_falls_back_to_private(self) -> None:
        """``0`` 才是"未配置"：频道路由整体关闭。"""

        settings = Settings(_env_file=None)
        settings.moderation.log_channel_id = 0
        self.assertEqual(group._admin_log_channel_id(settings), 0)
        self.assertFalse(group._log_channel_route_active(settings))

        # 老 payload 里没有这个键时同样按 0 处理。
        legacy = Settings(_env_file=None)
        legacy.moderation = SimpleNamespace(enabled=True, log_channel_enabled=True)
        self.assertEqual(group._admin_log_channel_id(legacy), 0)
        self.assertFalse(group._log_channel_route_active(legacy))

    def test_configured_channel_keeps_the_route_active(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual(group._admin_log_channel_id(settings), _EXPECTED_DEFAULT)
        self.assertTrue(group._log_channel_route_active(settings))


class LogChannelEnvEntryPointTests(unittest.TestCase):
    def test_environment_variable_cannot_override(self) -> None:
        """env **无效**：``MODERATION__LOG_CHANNEL_ID`` 读不到（不要在文档里吹它能）。"""

        import os

        previous = os.environ.get("MODERATION__LOG_CHANNEL_ID")
        os.environ["MODERATION__LOG_CHANNEL_ID"] = "-1009999999999"
        try:
            settings = Settings(_env_file=None)
        finally:
            if previous is None:
                os.environ.pop("MODERATION__LOG_CHANNEL_ID", None)
            else:
                os.environ["MODERATION__LOG_CHANNEL_ID"] = previous

        self.assertEqual(settings.moderation.log_channel_id, _EXPECTED_DEFAULT)
        # 没有 env_nested_delimiter / 扁字段：不是"暂时没接"，是结构上就接不上。
        self.assertIsNone(Settings.model_config.get("env_nested_delimiter"))
        self.assertNotIn("moderation_log_channel_id", Settings.model_fields)


class LogChannelDocTests(unittest.TestCase):
    def test_config_comment_matches_the_implementation(self) -> None:
        """注释不再自称"默认值是 0"，并写明覆盖入口的真实能力。"""

        source = (_REPO_ROOT / "bot" / "config.py").read_text(encoding="utf-8")

        # 旧的矛盾注释（"0 表示未配置"紧挨着非 0 默认值的那句）已经不在。
        self.assertNotIn("证据频道 id；0 表示未配置", source)
        self.assertIn("证据频道 id。**默认值就是下面那个频道**", source)
        # 三种入口的真实能力都写进去了。
        self.assertIn("PUT /api/v1/settings", source)
        self.assertIn("环境变量：**无效**", source)
        self.assertIn("config.toml", source)

    def test_readme_discloses_the_default_channel_id(self) -> None:
        """C4-02：README 必须披露默认频道 id 与"怎么改"。"""

        readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(str(_EXPECTED_DEFAULT), readme)
        self.assertIn("审核日志频道", readme)
        self.assertIn("配成 `0` 才表示", readme)
        self.assertIn("PUT /api/v1/settings", readme)


if __name__ == "__main__":
    unittest.main()