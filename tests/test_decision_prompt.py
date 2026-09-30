import unittest

from bot.utils.prompts import DECISION_SYSTEM


class DecisionPromptTests(unittest.TestCase):
    def test_decision_prompt_uses_actual_runtime_block_names(self) -> None:
        for block in (
            "[IS_MENTIONED]",
            "[IS_REPLY_TO_BOT]",
            "[IS_REPLY_TO_OTHER]",
            "[MENTIONS_OTHER_USER]",
            "[SENDER_IS_OWNER]",
            "[IS_MERGED_MESSAGE]",
            "[RECENT_HISTORY_FOR_DECISION]",
            "[MERGED_MESSAGE_CONTEXT]",
            "[CURRENT_MESSAGE]",
        ):
            self.assertIn(block, DECISION_SYSTEM)

    def test_decision_prompt_is_active_but_rate_limited(self) -> None:
        self.assertIn(
            "the bot is a group member who speaks when it has something worth saying",
            DECISION_SYSTEM,
        )
        self.assertIn("Every reply must carry substance", DECISION_SYSTEM)
        self.assertIn(
            "shows the bot has already posted within the last few messages and "
            "nobody has addressed it since",
            DECISION_SYSTEM,
        )

    def test_decision_prompt_mentions_bot_frequency_signals(self) -> None:
        self.assertIn("role=assistant", DECISION_SYSTEM)
        self.assertIn("sender_id=BOT", DECISION_SYSTEM)
        self.assertIn(
            "a recent-bot-messages section for reply-frequency judgment",
            DECISION_SYSTEM,
        )
        self.assertIn(
            "has already posted within the last few messages",
            DECISION_SYSTEM,
        )

    def test_decision_prompt_defaults_to_skip_when_not_needed(self) -> None:
        self.assertIn(
            "When unsure about a message that is NOT a question, output `skip`.",
            DECISION_SYSTEM,
        )
        self.assertIn(
            "stays out of banter, jokes, greetings, and back-channel chatter",
            DECISION_SYSTEM,
        )
