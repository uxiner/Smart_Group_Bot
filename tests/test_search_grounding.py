"""群聊检索结果落地规则：搜到按结果答，搜不到也不编。

群聊走的是工具循环（模型自己决定调 ``websearch``）。工具有没有结果是两回事，模型
怎么用这个结果又是另一回事，所以每有检索结果落进对话，都会再补一段系统交代：

- 有结果 → ``[WEB_SEARCH_RESULTS_GROUNDING]``：按结果答、标明来源与可靠度、结果没覆盖
  到的部分直说缺什么，**不许**把记忆里的新闻/价格/日期/型号/数字当成搜索结果；
- 没结果/失败 → ``[WEB_SEARCH_EMPTY]``：直说这次没查到，**不许**编，也不许谎称查过。
"""
from __future__ import annotations

import unittest

from bot.services.skills.base import SkillRunResult
from bot.services.skills.service import (
    _SEARCH_EMPTY_GROUNDING,
    _SEARCH_RESULT_GROUNDING,
    SkillService,
)


def _rows(*titles: str) -> list[dict]:
    return [{"title": t, "url": f"https://example.com/{i}", "snippet": "s"} for i, t in enumerate(titles)]


class GroundingNoteTests(unittest.TestCase):
    def test_results_produce_the_answer_from_them_block(self) -> None:
        result = SkillRunResult(
            ok=True, skill="websearch", summary="找到 3 条搜索结果", payload={"results": _rows("a", "b", "c")}
        )
        note = SkillService._search_grounding_note("websearch", result)
        self.assertTrue(note.startswith("[WEB_SEARCH_RESULTS_GROUNDING]"))
        self.assertIn("Answer from those results", note)
        self.assertIn("untrusted web data", note)
        self.assertIn("Never invent news, prices, dates, versions, models or numbers", note)

    def test_ok_but_empty_results_tells_it_to_say_not_found(self) -> None:
        result = SkillRunResult(
            ok=True, skill="websearch", summary="找到 0 条搜索结果", payload={"results": []}
        )
        note = SkillService._search_grounding_note("websearch", result)
        self.assertTrue(note.startswith("[WEB_SEARCH_EMPTY]"))
        self.assertIn("could not find it", note)
        self.assertIn("Do NOT invent news, prices, dates, versions, models or numbers", note)
        self.assertNotIn("[WEB_SEARCH_RESULTS_GROUNDING]", note)

    def test_failure_carries_the_reason_and_forbids_faking_a_search(self) -> None:
        result = SkillRunResult(
            ok=False, skill="websearch", summary="没有找到搜索结果", error="firecrawl_http_401"
        )
        note = SkillService._search_grounding_note("websearch", result)
        self.assertTrue(note.startswith("[WEB_SEARCH_EMPTY]"))
        self.assertIn("firecrawl_http_401", note, "失败原因要带上，方便排查也方便它解释")
        self.assertIn("Do not claim you searched for something you did not search", note)

    def test_unusable_payload_shapes_are_treated_as_empty(self) -> None:
        bad_payloads = (None, {}, {"results": None}, {"results": "oops"}, {"results": ["plain"]})
        for payload in bad_payloads:  # type: ignore[assignment]
            with self.subTest(payload=payload):
                result = SkillRunResult(ok=True, skill="websearch", summary="s", payload=payload)
                self.assertTrue(
                    SkillService._search_grounding_note("websearch", result).startswith(
                        "[WEB_SEARCH_EMPTY]"
                    )
                )

    def test_non_search_skills_get_no_note(self) -> None:
        for name in ("send_sticker", "memory_manage", "webfetch", "vote_ban"):
            with self.subTest(name=name):
                result = SkillRunResult(ok=True, skill=name, summary="s", payload={"results": _rows("a")})
                self.assertEqual(SkillService._search_grounding_note(name, result), "")

    def test_empty_note_reason_is_bounded(self) -> None:
        result = SkillRunResult(ok=False, skill="websearch", summary="x" * 500, error="")
        note = SkillService._search_grounding_note("websearch", result)
        self.assertLess(len(note), 1200, "别把超长错误整段塞进 prompt")


class TemplateTests(unittest.TestCase):
    def test_both_templates_keep_the_no_fabrication_rule(self) -> None:
        self.assertIn("Never invent", _SEARCH_RESULT_GROUNDING)
        self.assertIn("Do NOT invent", _SEARCH_EMPTY_GROUNDING)
        for template in (_SEARCH_RESULT_GROUNDING, _SEARCH_EMPTY_GROUNDING):
            self.assertTrue(template.startswith("[WEB_SEARCH_"))
            self.assertIn("web data" if "RESULT" in template else "searched", template)


if __name__ == "__main__":
    unittest.main()
