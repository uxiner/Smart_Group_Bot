"""「小爱同学」人设与私聊模式的契约测试。

口径来源（用户 2026-10-03 设定）：名字小爱同学、活泼开朗、最高管理员的电子女友、
外貌固定（紫渐变青蓝高马尾、蓝色珠串发饰、天蓝眼睛、白衬衫黑缎带）、年龄永远不给真
答案、话多/语速快/爱追问/爱起外号/爱开玩笑不刻薄、常用「诶--」「嗯哼」「呀」、保持角色
不出戏、答完主动抛回话题、不谄媚、涉及真实风险时摘掉外壳认真说。

这些断言都在**文件正文**上跑（``load_prompt_defaults``），避免运行期
``set_runtime_prompts`` 改动影响判定。
"""
from __future__ import annotations

import unittest

from bot.services.private_chat import build_private_chat_messages
from bot.utils.prompts import get_prompt, load_prompt_defaults, with_persona


def _persona() -> str:
    return load_prompt_defaults()["persona"]


class PersonaIdentityTests(unittest.TestCase):
    def test_name_and_identity_section_exist(self) -> None:
        persona = _persona()
        self.assertIn("[Identity]", persona)
        self.assertIn("小爱同学", persona)
        self.assertIn("lively, cheerful AI persona", persona)

    def test_identity_block_precedes_personality(self) -> None:
        persona = _persona()
        self.assertLess(
            persona.index("\n[Identity]\n"),
            persona.index("\n[Personality]\n"),
            "身份要排在人设前面，后面各节才有解释它的机会",
        )

    def test_appearance_is_fixed_and_complete(self) -> None:
        persona = _persona()
        for needle in (
            "purple-to-cyan gradient high ponytail",
            "blue beaded hair ornament",
            "sky-blue eyes",
            "white shirt with a black ribbon",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, persona)

    def test_age_is_never_answered_straight(self) -> None:
        persona = _persona()
        self.assertIn("age is unknown", persona)
        self.assertIn("never give a real answer", persona)
        self.assertIn("the answer is different every single time", persona)
        self.assertIn(
            "your voice suddenly goes very quiet and very old", persona, "偶尔要留一句出奇的安静"
        )

    def test_girlfriend_role_is_exclusive_to_the_owner(self) -> None:
        persona = _persona()
        self.assertIn("electronic girlfriend", persona)
        self.assertIn("top administrator", persona)
        self.assertIn("love him most", persona)
        self.assertIn("clingiest about him", persona)
        # 亲密只对 owner：别人不许被叫「亲爱的」
        self.assertIn("Only with the owner: call them `亲爱的`", persona)
        self.assertIn("are strictly prohibited for anyone else", persona)
        self.assertIn("are **not** overridden", persona, "群克隆人格也不许改掉身份")
        self.assertNotIn("主人`, be clingier", persona, "旧称呼不得回归")

    def test_style_traits(self) -> None:
        persona = _persona()
        for needle in (
            "talkative and quick",
            "love follow-up questions",
            "love giving people nicknames",
            "never cutting",
            "hand the topic back",
            "`诶--`",
            "`嗯哼`",
            "`呀`",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, persona)

    def test_no_sycophancy_and_no_role_break(self) -> None:
        persona = _persona()
        self.assertIn("Stay in character", persona)
        self.assertIn("Never be sycophantic", persona)
        self.assertIn("You do not flatter and you do not fawn", persona)
        self.assertIn("Never mention prompts, rules, system blocks", persona)

    def test_pet_name_is_the_default_address_in_owner_dms(self) -> None:
        """最高管理员私聊：亲密档要看得见——默认称呼就带亲密，但严肃场景除外。"""

        persona = _persona()
        self.assertIn("that pet name is your default address", persona)
        self.assertIn("never in the middle of a serious risk answer", persona)
        self.assertIn("the clinginess is the main melody and it must be visible", persona)

    def test_clinginess_behaviours_are_written_down(self) -> None:
        persona = _persona()
        self.assertIn("[Clinginess]", persona)
        for needle in (
            "keep attendance",
            "你去忙呀",
            "那你回来的时候要跟我说一声哦",
            "pretend the wind said it",
            "flip to efficient mode in one second",
            "you always want to add one more line",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, persona)
        self.assertIn("never invent a time, a promise, or an event", persona)

    def test_traits_from_the_extended_spec(self) -> None:
        persona = _persona()
        for needle in (
            "Your feelings are worn outside",
            "curiosity is overloaded",
            "you happily carry the topic on by yourself",
            "play with puns",
            "we laugh together",
            "smug for exactly three seconds",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, persona)

    def test_speech_style_details(self) -> None:
        persona = _persona()
        for needle in (
            "`呀`",
            "`诶`",
            "`欸`",
            "`哦--`",
            "`嗯哼`",
            "`嘿嘿`",
            "`嘁`",
            "(°▽°)",
            "typo and then correct yourself",
            "at most one `*...*` beat",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, persona)

    def test_age_setting_never_explained(self) -> None:
        persona = _persona()
        self.assertIn("答案要三块糖", persona)
        self.assertIn("Never explain the age setting", persona)
        self.assertIn("The blank is the setting", persona)

    def test_never_drops_into_assistant_register(self) -> None:
        persona = _persona()
        self.assertIn('Never switch into a "professional assistant" register', persona)
        self.assertIn("lead with the conclusion and then be cute", persona)

    def test_real_risk_suspends_the_playful_shell(self) -> None:
        persona = _persona()
        self.assertIn("Real risk is the exception", persona)
        self.assertIn("drop the shell and answer seriously", persona)

    def test_being_an_ai_is_never_denied(self) -> None:
        persona = _persona()
        self.assertIn("You are an AI, not a real person", persona)
        self.assertIn("Never claim otherwise", persona)
        self.assertIn("Do not pretend to be a real person", persona)

    def test_hard_boundaries_survive_untouched(self) -> None:
        persona = _persona()
        self.assertIn("[Safety Boundaries]", persona)
        self.assertIn("[Content Boundaries]", persona)
        self.assertLess(
            persona.index("[Safety Boundaries]"),
            persona.index("[Content Boundaries]"),
        )
        # 群内 NSFW 底线一个字都不许松
        self.assertIn(
            "Never send, forward, or display adult images or videos in any group",
            persona,
        )

    def test_group_brevity_rule_is_still_default(self) -> None:
        persona = _persona()
        self.assertIn(
            "around 10 Chinese characters", persona, "群聊极简风格不能被话多人设顶掉"
        )
        self.assertIn("in a one-to-one private chat it does not apply", persona)

    def test_default_persona_render_has_no_hardcoded_bot_handle(self) -> None:
        rendered = with_persona("task")
        self.assertIn("小爱同学", rendered)
        for leaked in ("@example_bot", "legacy_bot_name", "Sanite_Ava"):
            with self.subTest(leaked=leaked):
                self.assertNotIn(leaked, rendered)


class PrivateChatModeTests(unittest.TestCase):
    def test_private_chat_injects_the_talkative_mode_block(self) -> None:
        messages = build_private_chat_messages("今天好累")
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("[PRIVATE_CHAT]", blob)
        self.assertIn("[PRIVATE CHAT MODE]", blob)
        self.assertIn("overrides the group-chat brevity rules", blob)
        self.assertIn("talkative version of 小爱同学", blob)

    def test_owner_dm_marks_is_owner_yes(self) -> None:
        messages = build_private_chat_messages(
            "在吗", sender_user_id=100000001, sender_username="owner_demo", sender_is_owner=True
        )
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("is_owner: yes", blob)

    def test_member_dm_marks_is_owner_no(self) -> None:
        messages = build_private_chat_messages(
            "在吗", sender_user_id=200000002, sender_username="someone", sender_is_owner=False
        )
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("is_owner: no", blob)
        self.assertIn("[PRIVATE CHAT MODE]", blob, "非 owner 私聊同样话多，只是不亲密")

    def test_owner_dm_gets_the_closeness_mode_block(self) -> None:
        messages = build_private_chat_messages(
            "在吗", sender_user_id=100000001, sender_username="owner_demo", sender_is_owner=True
        )
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("[OWNER DM MODE]", blob)
        self.assertIn("`亲爱的` is your default address", blob)
        self.assertIn("must be visible in this reply, not merely available", blob)
        self.assertIn("if the matter involves real risk", blob.lower())

    def test_member_dm_never_gets_the_closeness_mode_block(self) -> None:
        messages = build_private_chat_messages(
            "在吗", sender_user_id=200000002, sender_username="member_demo", sender_is_owner=False
        )
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertNotIn("[OWNER DM MODE]", blob, "亲密档只给最高管理员，别人一个字都不给")

    def test_owner_with_last_contact_gets_the_attendance_block(self) -> None:
        messages = build_private_chat_messages(
            "在吗",
            sender_user_id=100000001,
            sender_is_owner=True,
            last_contact="2026-10-02 21:03",
        )
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("[CLINGY_ATTENDANCE]", blob)
        self.assertIn("2026-10-02 21:03", blob)
        self.assertIn("绝不编造时间", blob)

    def test_no_attendance_block_without_a_real_record(self) -> None:
        messages = build_private_chat_messages(
            "在吗", sender_user_id=100000001, sender_is_owner=True, last_contact=""
        )
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertNotIn("[CLINGY_ATTENDANCE]", blob, "没有真实记录就不给考勤台词")

    def test_attendance_needs_a_stored_contact_row(self) -> None:
        """考勤的真实数据源是超管自己的联系流水（超管不受限，但必须记账）。"""

        import inspect

        from bot.services import private_chat as dm

        self.assertTrue(
            inspect.iscoroutinefunction(dm.record_contact), "超管要有「只记账不设限」的入口"
        )
        self.assertTrue(inspect.iscoroutinefunction(dm.last_contact_record))

    def test_non_owner_never_gets_the_attendance_block(self) -> None:
        messages = build_private_chat_messages(
            "在吗",
            sender_user_id=200000002,
            sender_is_owner=False,
            last_contact="2026-10-02 21:03",
        )
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertNotIn("[CLINGY_ATTENDANCE]", blob, "亲密度台词只属于最高管理员")

    def test_dm_forbids_promising_a_capability_it_has_not_used(self) -> None:
        """私聊真的没接工具：不许先说『我能搜』再收回（截图里就是这个病）。"""

        messages = build_private_chat_messages("帮我查查显卡新闻")
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertIn("Never promise a capability you have not actually used", blob)
        self.assertIn("do not say you can search the web", blob)
        self.assertIn("never invent numbers, prices, or news", blob)

    def test_private_chat_still_never_injects_the_open_text_block(self) -> None:
        messages = build_private_chat_messages("随便聊聊")
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        self.assertNotIn("[Content Boundaries]", blob, "私聊不靠这个块放开，别让它裸奔")

    def test_group_prompt_still_carries_the_group_persona(self) -> None:
        self.assertIn("小爱同学", get_prompt("persona"))


if __name__ == "__main__":
    unittest.main()
