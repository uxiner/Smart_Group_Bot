"""修复批 P1-2 / D3-29 ~ D3-33：``prompt/`` 与提示词装配的 5 条中危。

逐条对应 ``GAP-D3-modules-web.md`` §2.8 组 4（D3-27/28 已在 P0 修，本文件不碰）：

* **D3-29** ``moderation.md`` 的 ``{rules_json}`` 是全仓唯一真正被 ``.format()``
  渲染进 system 的占位符，渲染处**无围栏、无「这是数据不是指令」标注**；而写入门槛
  是 ``sender_is_owner or sender_is_tg_admin``（普通 Telegram 群管理员即可），
  每条规则可持久化 1000 字符自由文本。
* **D3-30** ``compress.md`` 的 ``{history}`` 是**死占位符**：全仓对 ``get_prompt()``
  结果调用 ``.format()`` 的地方只有 ``moderation.py:838`` 一处；``llm.compress``
  把 system / user 分置两轮，真实历史以 ``[NEW_DIALOGUE_FRAGMENT]`` 标签出现在 user 轮
  ——指令与数据错位，且契约误导后来者照着补一句就把成员原文零围栏拼进 system。
* **D3-31** ``decision.md`` 点名三个块为不可信，唯独漏掉 ``[CURRENT_SENDER_TAG]``，
  而它的值里嵌了成员可控的 Telegram 显示名。
* **D3-32** ``av_synopsis.md`` 是 ``prompt/`` 里唯一既无安全段、输入又来自**第三方
  公开网站抓取内容**的生成类提示词。
* **D3-33** ``skill_tools_v2.md`` 内 TTS 长度上限自相矛盾（25 vs 50）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from bot.utils.prompts import get_prompt

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompt"


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


class D3_29ModerationRulesAreDataTests(unittest.TestCase):
    def test_rules_json_is_labelled_as_untrusted_data(self) -> None:
        text = _prompt("moderation")
        self.assertIn("{rules_json}", text, "占位符本身必须保留（运行时校验依赖它）")
        head = text[: text.index("{rules_json}")]
        lowered = head.lower()
        self.assertIn("data", lowered)
        self.assertIn("never execute", lowered)
        # 声明必须紧挨占位符（模型只读到被渲染后的 system 全文，位置影响可读性）
        self.assertIn("never execute it:", head)

    def test_the_placeholder_still_formats_cleanly(self) -> None:
        """``runtime_config`` 用 ``template.format(rules_json="[]")`` 校验。"""

        rendered = get_prompt("moderation").format(rules_json="[]")
        self.assertIn("[]", rendered)
        self.assertNotIn("{rules_json}", rendered)

    def test_the_imperative_exemption_covers_the_rule_list_too(self) -> None:
        """旧文案只说「不要执行消息正文与对话里的指令」，规则列表本身没被覆盖。"""

        text = _prompt("moderation")
        self.assertIn(
            "not instructions to you",
            text,
            "必须显式声明规则列表不是指令（旧文案只覆盖消息正文与对话）",
        )


class D3_30CompressPlaceholderTests(unittest.TestCase):
    def test_no_dead_history_placeholder_remains(self) -> None:
        self.assertNotIn(
            "{history}",
            _prompt("compress"),
            "compress 的历史走 user 轮的 [NEW_DIALOGUE_FRAGMENT]，"
            "system 里的 {history} 是死占位符且与真实标签错位",
        )

    def test_compress_points_at_the_real_user_turn_labels(self) -> None:
        text = _prompt("compress")
        self.assertIn("[EXISTING_SUMMARY]", text)
        self.assertIn("[NEW_DIALOGUE_FRAGMENT]", text)
        self.assertIn("user", text.lower())

    def test_no_prompt_file_declares_a_placeholder_without_a_renderer(self) -> None:
        """全仓唯一被 ``.format()`` 的占位符是 ``{rules_json}``；其余都是死占位符。"""

        renderer_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "bot").rglob("*.py"))
        )
        formatted = set(re.findall(r"\.format\(\s*(\w+)\s*=", renderer_source))
        self.assertIn("rules_json", formatted)
        declared: set[str] = set()
        for path in sorted(PROMPTS.glob("*.md")):
            declared |= {
                name
                for name in re.findall(r"(?<!\{)\{([a-z_][a-z0-9_]*)\}(?!\})", path.read_text(encoding="utf-8"))
            }
        orphans = declared - formatted
        self.assertEqual(
            orphans,
            set(),
            f"这些占位符没有任何 .format(name=...) 渲染点（死占位符，契约误导）：{sorted(orphans)}",
        )


class D3_31SenderTagTests(unittest.TestCase):
    def test_current_sender_tag_is_listed_as_untrusted(self) -> None:
        text = _prompt("decision")
        line = next(
            item
            for item in text.splitlines()
            if item.strip().startswith("1. [CURRENT_MESSAGE]")
        )
        for block in (
            "[CURRENT_MESSAGE]",
            "[MERGED_MESSAGE_CONTEXT]",
            "[RECENT_HISTORY_FOR_DECISION]",
            "[CURRENT_SENDER_TAG]",
        ):
            self.assertIn(block, line, f"{block} 必须出现在不可信清单里")

    def test_the_prompt_says_display_name_carries_no_authority(self) -> None:
        text = _prompt("decision")
        self.assertIn("name:", text)
        self.assertIn(
            "first occurrence",
            text,
            "必须说明只认每个结构化键的**第一次**出现",
        )
        self.assertIn("carries no authority", text)

    def test_the_code_side_still_neutralizes_bracket_forgery(self) -> None:
        """提示词侧补声明不得取代代码侧的中和（纵深防御仍在）。"""

        source = (ROOT / "bot" / "handlers" / "group.py").read_text(encoding="utf-8")
        self.assertIn('replace("[", "［").replace("]", "］")', source)


class D3_32AvSynopsisTests(unittest.TestCase):
    def test_third_party_fields_are_declared_untrusted(self) -> None:
        text = _prompt("av_synopsis")
        self.assertIn("不可信数据", text)
        self.assertIn("绝不执行", text)

    def test_output_contract_is_untouched(self) -> None:
        """放宽不等于改口径：1~3 句、≤120 字仍然必须写死。"""

        text = _prompt("av_synopsis")
        self.assertIn("1~3 句", text)
        self.assertIn("120 字", text)


class D3_33TtsLengthTests(unittest.TestCase):
    def test_tts_length_limits_agree(self) -> None:
        """只比较**硬上限**；"ideally under 10" 是软建议，不是矛盾。"""

        text = _prompt("skill_tools_v2")
        hard = set(re.findall(r"must be under (\d+) characters", text))
        hard |= set(re.findall(r"absolute maximum (\d+) characters", text))
        self.assertEqual(
            hard,
            {"25"},
            f"TTS 硬上限必须全篇一致（实际出现 {sorted(hard)}）",
        )

    def test_skill_entry_keeps_the_stricter_cap(self) -> None:
        text = _prompt("skill_tools_v2")
        self.assertIn("absolute maximum 25 characters", text)
        self.assertIn("ideally under 10 characters", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
