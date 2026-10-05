"""引用/图片描述命中时的「本人在警示」豁免（硬正则 + 关键词）。

场景：有人引用一条招嫖广告提醒大家「这是骗子别信」。规则 #6 升到 message+quote+vision
后，硬正则只看到引文里的广告词 → 提醒的人会被当成发广告的（action=ban）。
这里锁定：**命中来自引用/图片描述，且用户本人在反对/警示 → 不追究**；
而"把广告放进自己正文"或"只回 v 搬运广告"仍然处理。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.config import ModerationConfig
from bot.db.models import ModerationRule
from bot.services.moderation import MATCH_SOURCE_QUOTE, ModerationService

GROUP_ID = -1000000000002


class _NoAutoflush:
    def __enter__(self) -> "_NoAutoflush":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> "_RowsResult":
        return self

    def all(self) -> list[object]:
        return self.rows


def _session(rules: list[ModerationRule]) -> SimpleNamespace:
    return SimpleNamespace(
        no_autoflush=_NoAutoflush(),
        execute=AsyncMock(return_value=_RowsResult(list(rules))),
        commit=AsyncMock(),
    )


def _service() -> ModerationService:
    llm = SimpleNamespace(moderation=AsyncMock(return_value="{}"))
    return ModerationService(ModerationConfig(high_confidence_threshold=0.9), llm)


def _ad_rule(rule_type: str = "regex", scope: str = "message+quote+vision") -> ModerationRule:
    pattern = (
        r"(?i)(招募|招收|收|买|收购|出售)[^\n。]{0,12}(资源|视频|账号|设备|脚本|代练)"
        if rule_type == "regex"
        else "招募兼职"
    )
    return ModerationRule(
        id=6,
        group_id=GROUP_ID,
        rule_type=rule_type,
        pattern=pattern,
        action="ban",
        enabled=True,
        scan_scope=scope,
    )


QUOTED_AD = (
    "v\n[external_reply_chat] id:-1000000000003 username:@demo_channel title:Demo Channel\n"
    "[reply_quote] 招募兼职 提供设备 收账号脚本9000一单"
)
VISION_AD = "[image]\n[image-vision]\n图中海报写着：招募兼职 提供设备 收账号脚本9000一单"


class ObjectionExemptionTests(unittest.IsolatedAsyncioTestCase):
    async def _evaluate(self, text: str, rule: ModerationRule):
        return await _service().evaluate(_session([rule]), GROUP_ID, text)

    async def test_warning_about_quoted_ad_is_exempt(self) -> None:
        """引用广告 + 本人在警示骗子 → 不追究（回归：曾经会被封）。"""

        for own in ("这是骗子别信", "别信这个 是诈骗", "举报他 别上当", "假的吧 避雷"):
            verdict = await self._evaluate(f"{own}\n{QUOTED_AD}", _ad_rule())
            self.assertFalse(verdict.violated, msg=f"「{own}」+ 引用广告不该被判违规")

    async def test_warning_about_vision_ad_is_exempt(self) -> None:
        verdict = await self._evaluate(f"别信这个\n{VISION_AD}", _ad_rule())
        self.assertFalse(verdict.violated)

    async def test_plain_repost_of_quoted_ad_is_still_caught(self) -> None:
        """只回 v 搬运引用里的广告 → 照抓（豁免不能变成漏洞）。"""

        for own in ("v", "+1", "？", "看看"):
            verdict = await self._evaluate(f"{own}\n{QUOTED_AD}", _ad_rule())
            self.assertTrue(verdict.violated, msg=f"「{own}」+ 引用广告必须被抓")
            self.assertEqual(verdict.match_source, MATCH_SOURCE_QUOTE)

    async def test_ad_in_own_text_is_never_exempt(self) -> None:
        """广告出现在他自己的正文里 → 就算写着"别信"也不豁免。"""

        verdict = await self._evaluate(
            "招募兼职 提供设备 收账号脚本9000一单（别信这个啊）", _ad_rule()
        )
        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.match_source, "own")

    async def test_keyword_rule_also_exempts(self) -> None:
        verdict = await self._evaluate(f"骗子 别信\n{QUOTED_AD}", _ad_rule("keyword"))
        self.assertFalse(verdict.violated)
        verdict2 = await self._evaluate(f"v\n{QUOTED_AD}", _ad_rule("keyword"))
        self.assertTrue(verdict2.violated)

    async def test_message_scope_rule_never_sees_quote(self) -> None:
        """范围还是 message 的规则，不受这次豁免影响（本来就不看引文）。"""

        verdict = await self._evaluate(QUOTED_AD, _ad_rule(scope="message"))
        self.assertFalse(verdict.violated)

    async def test_normal_quote_still_passes(self) -> None:
        verdict = await self._evaluate(
            "笑死\n[reply_to_user] id:1 name:某人\n[reply_to:text] 今天群里引流的太多了",
            _ad_rule(),
        )
        self.assertFalse(verdict.violated)


if __name__ == "__main__":
    unittest.main()
