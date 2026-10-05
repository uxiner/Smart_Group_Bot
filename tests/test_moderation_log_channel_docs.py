"""审核日志频道与交接对象：**默认中性**、覆盖入口的真实能力、注释与文档自洽。

本轮把 ``log_channel_id`` 的默认值从"预填某个具体频道"改成 ``0``（未配置），并新增
``review_handover_mention``（默认空 = 不 @ 任何人）。理由是公开 fork 开箱即用时不应
向任何频道（含任何私人频道）投递审核证据，也不该 @ 到任何人的 bot。**部署者已有的
私有值由运维就地保留**，本仓库只保证"没配置"这件事安全且自洽。

本文件钉住的口径：

- 默认 ``log_channel_id == 0``、``review_handover_mention == ""``；
- ``0`` = 未配置 → 频道路由整体关闭，按既有 fallback 路径私聊最高管理员；
- 老 payload 缺这个键时同样按 0 处理；
- 显式写成 ``-100…`` 的合法频道 id 才会启用频道路由；
- 覆盖入口的真实能力：运行时 API 有效（热生效）、``config.toml`` 只做一次性导入、
  **环境变量无效**（结构上就接不上）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.config import Settings
from bot.handlers import group
from bot.services.runtime_config import ModerationSettingsConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent
#: 合成的频道 id，只用于"显式配置后路由应当打开"这条断言。
_SYNTHETIC_CHANNEL_ID = -1000000000001


class LogChannelDefaultTests(unittest.TestCase):
    def test_default_is_unconfigured(self) -> None:
        """公开树的默认必须是 0：不会向任何频道投递。"""

        settings = Settings(_env_file=None)
        self.assertEqual(settings.moderation.log_channel_id, 0)
        self.assertTrue(settings.moderation.log_channel_enabled)
        self.assertEqual(ModerationSettingsConfig().log_channel_id, 0)

    def test_zero_means_unconfigured_and_falls_back_to_private(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual(group._admin_log_channel_id(settings), 0)
        self.assertFalse(group._log_channel_route_active(settings))

        # 老 payload 里没有这个键时同样按 0 处理。
        legacy = Settings(_env_file=None)
        legacy.moderation = SimpleNamespace(enabled=True, log_channel_enabled=True)
        self.assertEqual(group._admin_log_channel_id(legacy), 0)
        self.assertFalse(group._log_channel_route_active(legacy))

    def test_configured_channel_keeps_the_route_active(self) -> None:
        settings = Settings(_env_file=None)
        settings.moderation.log_channel_id = _SYNTHETIC_CHANNEL_ID
        self.assertEqual(group._admin_log_channel_id(settings), _SYNTHETIC_CHANNEL_ID)
        self.assertTrue(group._log_channel_route_active(settings))

    def test_schema_rejects_values_that_are_neither_zero_nor_a_channel_id(self) -> None:
        import pydantic

        for bad in (12345, -1, -100000000000, -10000000000000):
            with self.subTest(value=bad):
                with self.assertRaises(pydantic.ValidationError):
                    ModerationSettingsConfig(log_channel_id=bad)
        self.assertEqual(
            ModerationSettingsConfig(
                log_channel_id=_SYNTHETIC_CHANNEL_ID
            ).log_channel_id,
            _SYNTHETIC_CHANNEL_ID,
        )


class HandoverMentionTests(unittest.TestCase):
    def test_default_is_empty_and_produces_no_mention(self) -> None:
        self.assertEqual(ModerationSettingsConfig().review_handover_mention, "")
        self.assertEqual(group._review_handover_mention(), "")
        tail = group._handover_tail("登记")
        self.assertNotIn("@", tail)
        self.assertIn("人工审核侧", tail)

    def test_configured_mention_is_used_in_the_tail(self) -> None:
        from bot.services import policy_runtime

        settings = Settings(_env_file=None)
        settings.moderation.review_handover_mention = "@demo_helper_bot"
        policy_runtime.bind(settings)
        try:
            self.assertEqual(group._review_handover_mention(), "@demo_helper_bot")
            self.assertEqual(
                group._handover_tail("登记"),
                "请 @demo_helper_bot 登记：无需调整规则。",
            )
        finally:
            policy_runtime.unbind()

    def test_schema_rejects_unusable_mentions(self) -> None:
        import pydantic

        for bad in (
            "demo_bot",  # 缺 @
            "@",  # 空
            "@1bot",  # 数字开头
            "@has space",
            "@has\nnewline",
            "<b>@demo</b>",  # HTML 注入
            "@" + "x" * 40,  # 超长
        ):
            with self.subTest(value=bad):
                with self.assertRaises(pydantic.ValidationError):
                    ModerationSettingsConfig(review_handover_mention=bad)
        self.assertEqual(
            ModerationSettingsConfig(
                review_handover_mention="@ok_bot_1"
            ).review_handover_mention,
            "@ok_bot_1",
        )


class LogChannelEnvEntryPointTests(unittest.TestCase):
    def test_environment_variable_cannot_override(self) -> None:
        """env **无效**：``MODERATION__LOG_CHANNEL_ID`` 读不到（不要在文档里吹它能）。"""

        import os

        previous = os.environ.get("MODERATION__LOG_CHANNEL_ID")
        os.environ["MODERATION__LOG_CHANNEL_ID"] = str(_SYNTHETIC_CHANNEL_ID)
        try:
            settings = Settings(_env_file=None)
        finally:
            if previous is None:
                os.environ.pop("MODERATION__LOG_CHANNEL_ID", None)
            else:
                os.environ["MODERATION__LOG_CHANNEL_ID"] = previous

        self.assertEqual(settings.moderation.log_channel_id, 0)
        # 没有 env_nested_delimiter / 扁字段：不是"暂时没接"，是结构上就接不上。
        self.assertIsNone(Settings.model_config.get("env_nested_delimiter"))
        self.assertNotIn("moderation_log_channel_id", Settings.model_fields)


class LogChannelDocTests(unittest.TestCase):
    def test_config_comment_matches_the_implementation(self) -> None:
        source = (_REPO_ROOT / "bot" / "config.py").read_text(encoding="utf-8")

        # 旧的"默认值就是下面那个频道"已经不在了。
        self.assertNotIn("默认值就是下面那个频道", source)
        self.assertIn("默认 0 = 未配置", source)
        # 三种入口的真实能力都写进去了。
        self.assertIn("PUT /api/v1/settings", source)
        self.assertIn("环境变量：**无效**", source)
        self.assertIn("config.toml", source)

    def test_public_docs_do_not_claim_a_prefilled_channel(self) -> None:
        readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("默认值就是下面那个频道", readme)
        # docs/configuration.md 必须写清 0 = 未配置。
        configuration = (_REPO_ROOT / "docs" / "configuration.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("log_channel_id", configuration)
        self.assertIn("0 = 未配置", configuration)


if __name__ == "__main__":
    unittest.main()
