import unittest

from bot.utils.prompts import (
    CASUAL_SYSTEM,
    PERSONA_SYSTEM,
    REPLY_MODE_SYSTEM,
    SKILL_TOOL_SYSTEM,
    STICKER_DECISION_SYSTEM,
    with_persona,
)


class RuntimePromptBlockTests(unittest.TestCase):
    def test_reply_mode_prompt_lists_runtime_blocks(self) -> None:
        for block in (
            "[CURRENT_TIME]",
            "[IS_MERGED_MESSAGE]",
            "[MERGED_MESSAGE_COUNT]",
            "[IS_MENTIONED]",
            "[IS_REPLY_TO_BOT]",
            "[IS_REPLY_TO_OTHER]",
            "[MESSAGE_TYPE]",
            "[MERGED_MESSAGE_CONTEXT]",
            "[CURRENT_MESSAGE]",
            "[ASSISTANT_DRAFT_REPLY]",
        ):
            self.assertIn(block, REPLY_MODE_SYSTEM)

    def test_sticker_decision_prompt_lists_runtime_blocks(self) -> None:
        for block in (
            "[REPLY_ACTION]",
            "[MESSAGE_TYPE]",
            "[IS_MENTIONED]",
            "[IS_REPLY_TO_BOT]",
            "[REPLY_SOURCE]",
            "[CURRENT_MESSAGE]",
            "[ASSISTANT_DRAFT_REPLY]",
            "[STICKER_CANDIDATES]",
        ):
            self.assertIn(block, STICKER_DECISION_SYSTEM)

    def test_core_prompts_discourage_blank_line_bubbles(self) -> None:
        for prompt in (CASUAL_SYSTEM, PERSONA_SYSTEM):
            self.assertIn("do not leave blank lines", prompt)
            self.assertIn(
                'a line containing only `[[SPLIT]]` is the signal for "send as separate messages"',
                prompt,
            )
            self.assertIn("a blank line never splits anything", prompt)

    def test_core_prompts_forbid_parenthetical_stage_directions(self) -> None:
        for prompt in (CASUAL_SYSTEM, PERSONA_SYSTEM):
            self.assertIn("Do not write bracketed action descriptions or stage directions", prompt)
            self.assertIn("(swings feet)", prompt)

    def test_skill_prompt_declares_split_marker_rule(self) -> None:
        self.assertIn(
            'a line containing only `[[SPLIT]]` is treated as "send as separate messages"',
            SKILL_TOOL_SYSTEM,
        )
        self.assertIn("a blank line never splits anything", SKILL_TOOL_SYSTEM)

    def test_persona_declares_content_boundaries_for_open_adult_text(self) -> None:
        self.assertIn("[Content Boundaries]", PERSONA_SYSTEM)
        for needle in (
            "Adult-work text is not restricted",
            "codes/serial numbers (番号)",
            "never tell the user to go search for it themselves",
            "Your two roles do not conflict",
            "Adult media is the only hard line",
            "Never send, forward, or display adult images or videos in any group",
            "the guard flow handles it (delete + warning + challenge)",
            "Never fabricate",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, PERSONA_SYSTEM)
        # 位置：紧跟 [Safety Boundaries] 之后
        self.assertGreater(
            PERSONA_SYSTEM.index("[Content Boundaries]"),
            PERSONA_SYSTEM.index("[Safety Boundaries]"),
        )

    def test_default_persona_does_not_carry_the_open_text_block(self) -> None:
        # 该节只在群内开了 /av 时按群单独注入，默认人设里必须先摘掉。
        rendered = with_persona("task")
        self.assertNotIn("[Content Boundaries]", rendered)
        self.assertNotIn("Adult-work text is not restricted", rendered)
        self.assertIn("[Safety Boundaries]", rendered)
