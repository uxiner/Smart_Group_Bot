"""修复批 P1-2 / D3-35：``group_can_read_private_history`` 是「可配置、无消费者」的假旋钮。

复现的原缺陷（``GAP-D3`` D3-35，对 ``AUDIT-B`` B-41 第 4 项的独立复核确认）：

getter ``search_memory.group_can_read_private_history`` 写好了，
``RuntimeConfig`` 有字段、``app.js`` 做成了**可点的 toggle**、DB 里可 PUT、有 revision
保护——而 ``bot/`` 里**零调用方**：拨 true 不改变任何行为。``rc:269-272`` 的注释自己
都写着「指向不支持 tools 的端点会白白烧预算」式的免责声明，docstring 也写明
「本期**不实现**打开后的读取逻辑」。

假开关比没有更糟：运维会以为自己放开了某个方向，而它在 C 项隐私红线的名义下显示。
修法（refs）：从 ``RuntimeConfig`` 移除，``app.js`` 删掉 toggle，
``_normalize_deprecated_runtime_payload`` 做**一次性**剥离（否则老库 ``extra="forbid"``
会让 ``initialize()`` 抛错），getter 恒返回 False 并保留为这条红线的可执行断言。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.services import runtime_config as rc
from bot.services import search_memory as sm

ROOT = Path(__file__).resolve().parents[1]


class GetterIsAConstantTests(unittest.TestCase):
    def test_no_settings_object_can_open_the_group_to_private_direction(self) -> None:
        for settings in (
            SimpleNamespace(),
            SimpleNamespace(bot=SimpleNamespace()),
            SimpleNamespace(bot=SimpleNamespace(group_can_read_private_history=False)),
            # 残留属性（老库 payload / 外部脚本塞进来的）也不得被当成放行开关
            SimpleNamespace(bot=SimpleNamespace(group_can_read_private_history=True)),
        ):
            with self.subTest(settings=settings):
                self.assertFalse(
                    sm.group_can_read_private_history(settings),
                    "群→私聊是单向红线：不存在任何能打开它的配置",
                )

    def test_the_stale_getter_is_not_removed(self) -> None:
        """保留函数：它是这条红线的**可执行断言**。"""

        self.assertTrue(callable(sm.group_can_read_private_history))


class SchemaSurfaceTests(unittest.TestCase):
    def test_runtime_config_no_longer_declares_the_knob(self) -> None:
        self.assertNotIn(
            "group_can_read_private_history",
            rc.RuntimeConfig.model_fields,
            "严格 schema 里不得再留这个无消费者的旋钮",
        )

    def test_mini_app_no_longer_offers_the_toggle(self) -> None:
        app_js = (ROOT / "bot" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn(
            "group_can_read_private_history",
            app_js,
            "UI 上不得再提供一个点了也没用的 toggle",
        )

    def test_legacy_payload_is_stripped_before_strict_validation(self) -> None:
        """老库里那一行必须被一次性剥离，否则 extra='forbid' 会让 initialize 抛错。"""

        payload = {
            "bot": {"group_can_read_private_history": True, "drop_pending_updates": False},
            "models": {},
        }
        normalized, changed = rc._normalize_deprecated_runtime_payload(payload)
        self.assertTrue(changed)
        self.assertNotIn(
            "group_can_read_private_history",
            (normalized.get("bot") or {}),
        )
        # 剥离后必须仍能通过严格校验
        rc.RuntimeConfig.model_validate(normalized)

    def test_stripping_is_idempotent(self) -> None:
        payload = {"bot": {"group_can_read_private_history": True}}
        first, changed_first = rc._normalize_deprecated_runtime_payload(payload)
        second, changed_second = rc._normalize_deprecated_runtime_payload(first)
        self.assertTrue(changed_first)
        self.assertFalse(
            changed_second and "group_can_read_private_history" in (second.get("bot") or {}),
            "第二次不再重复剥离",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
