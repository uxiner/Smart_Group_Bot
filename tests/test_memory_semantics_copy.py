"""B3：记忆分类与删除语义的文案澄清（总开关说明 / ``/memory off`` 回执 / ``/help``）。

红检口径：在冻结基线 ``5aee996``（产品与 ``4d43113`` 逐字一致）上，本文件的**文案用例**
必须是**业务语义失败**（找不到审定文案的句子），不能是导入失败或夹具错误。因此这里只
断言真实 handler / 真实共享目录产出的文本，不做 mock 层字符串比对。

覆盖三处文案改写，并且顺带钉住三件**不得**被改坏的事：

* 总开关的清理指引必须指向群内 ``/memory forget`` 与 ``/memory off``，不能再把
  ``user_facts`` 指向群组页的「永久记忆」（那是 ``GroupPermanentMemory``，另一套存储）；
* ``/memory off`` 必须说清它是**全局**开关，且不删除原文归档 / 群摘要 / 联网检索留档 /
  群永久备忘；
* ``/help`` 里 ``/lm`` 必须标明仅群管理员，并区分「永久记忆」与「长期记忆」两个概念。

同时钉住 B2 已交付且**父代理审核确认存在**的行为：当前群 group 事实确实显示
「最近确认」与「写入方式」（规划文档曾误称不显示，本文件防止回归到错误描述）。
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Group, MemoryOptout, UserFact
from bot.handlers import commands
from bot.utils.command_catalog import _COMMANDS, build_help_text

SUPERGROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009999999999
MEMBER_ID = 777
OTHER_MEMBER_ID = 778
ADMIN_ID = 779

CONFIRMED_AT = datetime(2026, 1, 4, 12, 0, 0)
FIRST_SEEN_AT = datetime(2026, 1, 1, 9, 0, 0)

APP_JS = os.path.join("bot", "web", "static", "app.js")

# 审定文案（父代理 m02312 逐字给定）——必须整句出现在产品输出里。
TOTAL_SWITCH_SENTENCES = (
    "关闭只停用长期事实，不清空记录。",
    "群组页的「永久记忆」是另一套群级背景信息",
    "长期事实用群内 /memory forget <编号> 删单条",
    "或 /memory off 关闭本人记忆并标记删除本人事实。",
    "总开关关闭时，这两条命令也不可用。",
)
WRONG_TOTAL_SWITCH_HINT = "需要在群组页的「永久记忆」中手工清理"

OFF_RECEIPT_SENTENCES = (
    "<b>已关闭长期记忆</b>",
    "这个开关是全局的；不删除聊天原文归档、群摘要、联网检索留档或群永久备忘。",
    "<i>想重新打开用 /memory on（已标记删除的事实不会恢复）。</i>",
)
FORBIDDEN_OFF_PHRASES = ("彻底删除", "无残留", "永不再记", "所有数据都删除")

CONCEPT_LINE_1 = (
    "永久记忆：管理员维护的群级背景；长期记忆：机器人提炼或主动记下的稳定事实"
    "（个人或群公共信息）。"
)
CONCEPT_LINE_2 = (
    "聊天原文归档供 /find 查找；群摘要、联网检索留档供回复参考，不是上述两类记忆。"
)
LM_HELP_LINE = "/lm：永久记忆列表，支持翻页和删除（仅群管理员）"
LM_PURPOSE = "查看永久记忆列表，支持翻页和删除（仅群管理员）"


def _settings() -> Settings:
    return Settings(_env_file=None)


def _chat(group_id: int = SUPERGROUP_ID) -> SimpleNamespace:
    return SimpleNamespace(id=group_id, type="supergroup", title="合成群", username="")


def _message(
    text: str, *, group_id: int = SUPERGROUP_ID, user_id: int = MEMBER_ID
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        chat=_chat(group_id),
        from_user=SimpleNamespace(
            id=user_id, first_name="成员", last_name="", username=""
        ),
        bot=SimpleNamespace(send_message=AsyncMock(), get_chat=AsyncMock()),
    )


def _total_switch_call_line() -> str:
    """从 ``app.js`` 里取出 ``toggle("bot.memory_facts_enabled", ...)`` 那一行原文。"""

    with open(APP_JS, encoding="utf-8") as handle:
        for line in handle:
            if 'toggle("bot.memory_facts_enabled"' in line:
                return line
    raise AssertionError("app.js 里找不到 bot.memory_facts_enabled 的总开关调用")


class MemorySemanticsCopyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = handle.name
        handle.close()
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=SUPERGROUP_ID, title="合成当前群", settings={}))
            session.add(Group(id=OTHER_GROUP_ID, title="合成另一个群", settings={}))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    # ---- 夹具 --------------------------------------------------------------

    async def _seed_fact(self, *, fingerprint: str, **overrides) -> int:
        values = {
            "scope": "group",
            "scope_id": SUPERGROUP_ID,
            "subject_user_id": MEMBER_ID,
            "fact_text": f"合成事实 {fingerprint}",
            "category": "preference",
            "confidence": 50,
            "confirm_count": 1,
            "first_seen_at": FIRST_SEEN_AT,
            "last_confirmed_at": CONFIRMED_AT,
            "source_kind": "passive",
            "source_message_id": None,
            "evidence_excerpt": "",
            "fingerprint": fingerprint,
            "status": "active",
        }
        values.update(overrides)
        async with self.session_factory() as session:
            fact = UserFact(**values)
            session.add(fact)
            await session.commit()
            return int(fact.id)

    async def _run_memory(
        self,
        text: str,
        *,
        group_id: int = SUPERGROUP_ID,
        user_id: int = MEMBER_ID,
        admin: bool = False,
    ) -> list[str]:
        """真实 ``cmd_memory`` → 真实 ``set_optout`` / ``clear_optout`` / 真实临时 SQLite。

        只替换**发送边界**（``_answer`` 与入群授权查询），记忆服务与数据库都是真的。
        """

        answers: list[str] = []

        async def _answer(_message, _settings, body, **_kwargs):
            answers.append(str(body))

        with patch.object(commands, "_answer", new=_answer), patch.object(
            commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
        ), patch.object(
            commands, "memory_facts_enabled", return_value=True
        ), patch.object(
            commands, "_schedule_memory_command_cleanup", new=AsyncMock()
        ), patch.object(
            commands, "is_super_admin_user_id", return_value=False
        ), patch.object(
            commands,
            "is_group_admin_authorized",
            new=AsyncMock(return_value=admin),
        ):
            async with self.session_factory() as session:
                await commands.cmd_memory(
                    _message(text, group_id=group_id, user_id=user_id),
                    session,
                    _settings(),
                )
        return answers

    async def _fact_status(self, fact_id: int) -> str:
        async with self.session_factory() as session:
            row = await session.get(UserFact, fact_id)
            return str(row.status)

    async def _optout_exists(self, user_id: int) -> bool:
        async with self.session_factory() as session:
            return (await session.get(MemoryOptout, user_id)) is not None

    # ---- B3-1：全局长期记忆总开关说明 --------------------------------------

    def test_total_switch_help_points_at_memory_commands_not_group_memories(
        self,
    ) -> None:
        line = _total_switch_call_line()

        for sentence in TOTAL_SWITCH_SENTENCES:
            self.assertIn(sentence, line)

        # 关键纠正：不能再把长期事实指向群组页的「永久记忆」。
        self.assertNotIn(WRONG_TOTAL_SWITCH_HINT, line)
        self.assertIn("/memory forget", line)
        self.assertIn("/memory off", line)

        # 不改键名、默认值、第四个参数与原有 hint 文案。
        self.assertIn('toggle("bot.memory_facts_enabled", "启用长期记忆（总开关）"', line)
        self.assertIn(
            '"关掉 = 不提炼、不注入、不写入（群聊注入、私聊召回与 /memory 命令都会先检查它）。", false,',
            line,
        )
        # 保持普通文字：不得自己拼 HTML。
        self.assertNotIn("<a ", line)
        self.assertNotIn("<br", line)

    # ---- B3-2：/memory off 回执 -------------------------------------------

    async def test_memory_off_receipt_declares_global_scope_and_untouched_stores(
        self,
    ) -> None:
        own_current = await self._seed_fact(fingerprint="fp-own-current")
        own_other_group = await self._seed_fact(
            fingerprint="fp-own-other", scope_id=OTHER_GROUP_ID
        )
        own_private = await self._seed_fact(
            fingerprint="fp-own-private",
            scope="private",
            scope_id=MEMBER_ID,
        )
        other_member = await self._seed_fact(
            fingerprint="fp-other-member", subject_user_id=OTHER_MEMBER_ID
        )
        group_public = await self._seed_fact(
            fingerprint="fp-group-public", subject_user_id=0
        )

        answers = await self._run_memory("/memory off")

        self.assertEqual(len(answers), 1)
        receipt = answers[0]
        for sentence in OFF_RECEIPT_SENTENCES:
            self.assertIn(sentence, receipt)
        self.assertIn("关于你在所有群与私聊里的 3 条长期事实已标记删除。", receipt)
        for phrase in FORBIDDEN_OFF_PHRASES:
            self.assertNotIn(phrase, receipt)

        # 真实数据库效果：本人跨 scope 的 3 条被标记删除，其它人与群公共事实不动。
        self.assertEqual(await self._fact_status(own_current), "deleted")
        self.assertEqual(await self._fact_status(own_other_group), "deleted")
        self.assertEqual(await self._fact_status(own_private), "deleted")
        self.assertEqual(await self._fact_status(other_member), "active")
        self.assertEqual(await self._fact_status(group_public), "active")
        self.assertTrue(await self._optout_exists(MEMBER_ID))

    async def test_memory_on_clears_optout_without_restoring_facts(self) -> None:
        first = await self._seed_fact(fingerprint="fp-1")
        second = await self._seed_fact(fingerprint="fp-2", scope_id=OTHER_GROUP_ID)

        await self._run_memory("/memory off")
        self.assertEqual(await self._fact_status(first), "deleted")

        on_answers = await self._run_memory("/memory on")
        self.assertEqual(len(on_answers), 1)
        self.assertIn("<b>已重新打开长期记忆</b>", on_answers[0])
        self.assertIn("之前删掉的事实不会恢复", on_answers[0])

        self.assertFalse(await self._optout_exists(MEMBER_ID))
        self.assertEqual(await self._fact_status(first), "deleted")
        self.assertEqual(await self._fact_status(second), "deleted")

    async def test_memory_list_and_forget_paths_are_unchanged(self) -> None:
        """旧列表与 ``forget`` 行为逐字保留：编号是跨页全局序号，软删。"""

        target = await self._seed_fact(fingerprint="fp-list", fact_text="偏好喝美式")
        # 另一名成员的事实不在本人列表里，保证 #1 唯一确定。
        await self._seed_fact(
            fingerprint="fp-other-member", subject_user_id=OTHER_MEMBER_ID
        )

        listed = await self._run_memory("/memory")
        self.assertEqual(len(listed), 1)
        self.assertIn("机器人记得我的事", listed[0])
        self.assertIn("#1", listed[0])
        self.assertIn("偏好喝美式", listed[0])
        self.assertIn("编号是列表里的全局序号", listed[0])
        self.assertIn("/memory off 关掉长期记忆", listed[0])

        forgotten = await self._run_memory("/memory forget 1")
        self.assertEqual(len(forgotten), 1)
        # B5 期望漂移：成功回执由「已删除这条长期记忆。」改为「已标记删除这条长期记忆。」（父代理指定文案）。
        self.assertIn("已标记删除这条长期记忆。", forgotten[0])
        self.assertEqual(await self._fact_status(target), "deleted")

    # ---- B3-3：/help 共享目录 ---------------------------------------------

    def test_help_text_marks_lm_admin_only_and_separates_memory_concepts(
        self,
    ) -> None:
        text = build_help_text()

        self.assertIn(f"{LM_HELP_LINE}\n", text)
        self.assertIn(f"{CONCEPT_LINE_1}\n", text)
        self.assertIn(f"{CONCEPT_LINE_2}\n", text)

        find_index = text.index("/find &lt;关键词&gt;：在本群保留期聊天记录里搜索消息")
        concept_index = text.index(CONCEPT_LINE_1)
        memory_index = text.index("/memory：看机器人记得我什么")
        self.assertLess(find_index, concept_index)
        self.assertLess(concept_index, memory_index)
        self.assertLess(text.index(CONCEPT_LINE_2), memory_index)

        # 其余帮助行逐字不变。
        self.assertIn("/memory forget &lt;编号&gt;：删掉一条关于自己的长期记忆\n", text)
        self.assertIn("/memory off / memory on：关闭或重新打开本人的长期记忆\n", text)
        self.assertIn("删除统一走 /lm、/rules 这些命令页的内联按钮。\n", text)

    def test_command_catalog_marks_lm_admin_only_without_touching_other_entries(
        self,
    ) -> None:
        entries = {item.command: item for item in _COMMANDS}

        self.assertEqual(entries["/lm"].purpose, LM_PURPOSE)
        self.assertEqual(entries["/lm"].usage, "/lm")
        self.assertEqual(entries["/lm"].section, "核心入口")
        self.assertEqual(
            entries["/lm add"].purpose, "新增一条永久记忆"
        )
        self.assertEqual(
            entries["/memory forget"].purpose,
            "删除一条关于自己的长期记忆（群管理员可删本群任意一条）",
        )
        self.assertEqual(
            entries["/memory off"].purpose,
            "关闭或重新打开本人的长期记忆（关闭会删掉已记录的）",
        )
        # 没有新增命令、也没有新增「仅群管理员」标注。
        admin_only = [item.command for item in _COMMANDS if "仅群管理员" in item.purpose]
        self.assertEqual(admin_only, ["/lm"])
        self.assertFalse(any(item.command.startswith("/memory delete") for item in _COMMANDS))

    async def test_lm_command_still_requires_group_admin(self) -> None:
        """``/lm`` 的权限路径一行未改：普通成员被拒、管理员看到列表。"""

        memory = SimpleNamespace(
            list_permanent_memories=AsyncMock(return_value=[]),
        )
        holder = SimpleNamespace(get=lambda: memory)

        async def _run(*, allowed: bool) -> list[str]:
            answers: list[str] = []

            async def _answer(_message, _settings, body, **_kwargs):
                answers.append(str(body))

            with patch.object(commands, "_answer", new=_answer), patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ), patch.object(
                commands,
                "ensure_group_admin_permission",
                new=AsyncMock(return_value=allowed),
            ), patch.object(
                commands, "memory_holder", new=holder
            ), patch.object(
                commands, "is_super_admin_user_id", return_value=False
            ):
                async with self.session_factory() as session:
                    await commands.cmd_lm(
                        _message("/lm", user_id=ADMIN_ID),
                        session,
                        _settings(),
                        self.session_factory,
                    )
            return answers

        self.assertEqual(await _run(allowed=False), [])
        admin_answers = await _run(allowed=True)
        self.assertEqual(len(admin_answers), 1)
        self.assertIn("永久记忆", admin_answers[0])

    # ---- 钉住 B2 已交付行为（防止退回被证伪的描述） ------------------------

    def test_current_group_fact_notes_do_show_recent_confirmation_and_write_mode(
        self,
    ) -> None:
        context = commands._MemoryLinkContext(
            chat=_chat(),
            group_id=SUPERGROUP_ID,
            unlinkable=frozenset(),
            verified=True,
        )
        record = {
            "scope": "group",
            "scope_id": SUPERGROUP_ID,
            "last_confirmed_at": CONFIRMED_AT,
            "source_kind": "tool",
            "source_message_id": 0,
        }

        notes = context.entry_notes(record)

        self.assertIn("最近确认 2026-01-04", notes)
        self.assertIn("写入方式 模型写入", notes)
        self.assertIn("来源消息未记录，不提供链接", notes)
        self.assertFalse(any("EXCERPT" in note for note in notes))

        other_scope = dict(record, scope_id=OTHER_GROUP_ID)
        self.assertEqual(context.entry_notes(other_scope), [])
        private_scope = dict(record, scope="private", scope_id=MEMBER_ID)
        self.assertEqual(context.entry_notes(private_scope), [])

    def test_app_js_toggle_call_is_a_single_argument_list(self) -> None:
        """守护：新增文案不得把 toggle 调用拆成多行或改变参数个数。"""

        line = _total_switch_call_line()
        self.assertEqual(line.count("toggle("), 1)
        arguments = re.findall(r'"(?:[^"\\]|\\.)*"', line)
        self.assertGreaterEqual(len(arguments), 4)
        self.assertEqual(arguments[0], '"bot.memory_facts_enabled"')
        self.assertEqual(arguments[1], '"启用长期记忆（总开关）"')
        self.assertIn("false", line)


if __name__ == "__main__":
    unittest.main()
