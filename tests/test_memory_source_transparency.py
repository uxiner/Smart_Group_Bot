"""B2-2 / B2-3：``/memory`` 的来源透明、写入方式与权限口径。

红检口径：在冻结基线 ``a6f5a5e`` 上这些用例必须是**业务语义失败**（回执里没有
最近确认时间/写入方式/来源链接，或其它 scope 的来源被加上了元数据），不能是导入失败。

覆盖 Requirement 4/5：只在当前群、当前已有合法读取权限的 group 事实上追加最近
确认时间、写入方式与来源链接；其它群/私聊事实维持原有已授权列表显示而不新增来源
元数据；``evidence_excerpt`` 永不显示；已删除来源不链接；缺 id/未知类型安全降级；
全局序号跨页 ``forget`` 与 ``off`` 幂等行为不变。
"""

from __future__ import annotations

import asyncio
import html
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Group, GroupMessageArchive, UserFact, Violation
from bot.handlers import commands
from bot.utils import telegram as telegram_utils

SUPERGROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009999999999
MEMBER_ID = 777
OTHER_MEMBER_ID = 778
PRESENT_SOURCE_ID = 22
DELETED_SOURCE_ID = 21
EXCERPT_MARKER = "SYNTHETIC-EXCERPT-do-not-render"

CONFIRMED_AT = datetime(2026, 1, 4, 12, 0, 0)
FIRST_SEEN_AT = datetime(2026, 1, 1, 9, 0, 0)


def _settings() -> Settings:
    return Settings(_env_file=None)


def _chat(group_id: int = SUPERGROUP_ID, *, username: str = ""):
    return SimpleNamespace(
        id=group_id, type="supergroup", title="合成群", username=username
    )


def _message(text: str, *, group_id: int = SUPERGROUP_ID, user_id: int = MEMBER_ID):
    return SimpleNamespace(
        text=text,
        chat=_chat(group_id),
        from_user=SimpleNamespace(
            id=user_id, first_name="成员", last_name="", username=""
        ),
        bot=SimpleNamespace(send_message=AsyncMock(), get_chat=AsyncMock()),
    )


class MemorySourceTransparencyTests(unittest.IsolatedAsyncioTestCase):
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
            # C3 起「有没有回链」不只看删除证据，还要看**本群现存归档**里有没有这条消息：
            # PRESENT_SOURCE_ID 的语义就是"来源消息确实存在"，所以这里必须真的播种归档行
            # （旧用例只播种事实、靠删除证据推断，属于当时还没有归档核验的旧前提）。
            session.add(
                GroupMessageArchive(
                    group_id=SUPERGROUP_ID,
                    message_key=f"u:{PRESENT_SOURCE_ID}",
                    telegram_message_id=PRESENT_SOURCE_ID,
                    content="合成来源原文",
                    raw_text="合成来源原文",
                    message_thread_id=None,
                )
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    # ---- 播种 --------------------------------------------------------------

    async def _seed_fact(self, **overrides) -> int:
        values = {
            "scope": "group",
            "scope_id": SUPERGROUP_ID,
            "subject_user_id": MEMBER_ID,
            "fact_text": "事实正文",
            "category": "preference",
            "confidence": 50,
            "confirm_count": 1,
            "first_seen_at": FIRST_SEEN_AT,
            "last_confirmed_at": CONFIRMED_AT,
            "source_kind": "passive",
            "source_message_id": None,
            "evidence_excerpt": "",
            "fingerprint": f"fp-{overrides.get('fact_text', 'x')}",
            "status": "active",
        }
        values.update(overrides)
        async with self.session_factory() as session:
            fact = UserFact(**values)
            session.add(fact)
            await session.commit()
            return int(fact.id)

    async def _record_violation(self, message_id: int, action: str = "delete") -> None:
        async with self.session_factory() as session:
            session.add(
                Violation(
                    group_id=SUPERGROUP_ID,
                    user_id=message_id,
                    action_taken=action,
                    source_message_id=message_id,
                    message_text="合成违规文本",
                )
            )
            await session.commit()

    async def _fact_status(self, fact_id: int) -> str:
        async with self.session_factory() as session:
            row = await session.get(UserFact, fact_id)
            return str(row.status)

    # ---- 运行 --------------------------------------------------------------

    async def _run(
        self,
        text: str,
        *,
        group_id: int = SUPERGROUP_ID,
        user_id: int = MEMBER_ID,
        admin: bool = False,
    ) -> list[str]:
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

    # ---- B2-2：当前群来源透明 ---------------------------------------------

    async def test_current_group_fact_shows_recent_confirmation_write_mode_and_link(
        self,
    ) -> None:
        await self._seed_fact(
            fact_text="偏好喝美式",
            source_kind="tool",
            source_message_id=PRESENT_SOURCE_ID,
        )

        receipt = (await self._run("/memory"))[-1]

        self.assertIn("偏好喝美式", receipt)
        self.assertIn("最近确认 2026-01-04", receipt)
        self.assertIn("写入方式 模型写入", receipt)
        self.assertIn(f"https://t.me/c/1234567890/{PRESENT_SOURCE_ID}", receipt)
        self.assertIn("查看来源消息", receipt)
        self.assertIn("仅群成员可用", receipt)

    async def test_passive_source_is_labelled_as_auto_extracted(self) -> None:
        await self._seed_fact(
            fact_text="自动提炼的事实",
            source_kind="passive",
            source_message_id=PRESENT_SOURCE_ID,
        )

        receipt = (await self._run("/memory"))[-1]

        self.assertIn("写入方式 自动提炼", receipt)

    async def test_unknown_source_kind_degrades_without_claiming_a_writer(self) -> None:
        await self._seed_fact(fact_text="来源类型未知", source_kind="legacy-import")

        receipt = (await self._run("/memory"))[-1]

        self.assertIn("来源类型 legacy-import", receipt)
        self.assertNotIn("写入方式", receipt)

    async def test_deleted_source_is_not_linked(self) -> None:
        await self._record_violation(DELETED_SOURCE_ID, "delete")
        await self._seed_fact(
            fact_text="来源已被删除",
            source_kind="tool",
            source_message_id=DELETED_SOURCE_ID,
        )

        receipt = (await self._run("/memory"))[-1]

        self.assertNotIn("https://t.me", receipt)
        self.assertIn("来源消息已被审核删除，不提供链接", receipt)

    async def test_missing_source_id_degrades_without_link(self) -> None:
        await self._seed_fact(fact_text="没有来源消息", source_kind="tool")

        receipt = (await self._run("/memory"))[-1]

        self.assertNotIn("https://t.me", receipt)
        self.assertIn("来源消息未记录，不提供链接", receipt)

    async def test_other_group_and_private_facts_add_no_source_metadata(self) -> None:
        await self._seed_fact(
            fact_text="当前群的事实",
            source_kind="tool",
            source_message_id=PRESENT_SOURCE_ID,
        )
        await self._seed_fact(
            fact_text="另一个群的事实",
            scope="group",
            scope_id=OTHER_GROUP_ID,
            source_kind="tool",
            source_message_id=PRESENT_SOURCE_ID,
        )
        await self._seed_fact(
            fact_text="私聊的事实",
            scope="private",
            # 私聊事实按 (scope, scope_id=本人私聊 id) 存；这是既有读取口径
            scope_id=MEMBER_ID,
            subject_user_id=MEMBER_ID,
            source_kind="tool",
            source_message_id=PRESENT_SOURCE_ID,
        )

        receipt = (await self._run("/memory"))[-1]

        # 三条事实都还在（原有已授权列表显示不变）……
        self.assertIn("当前群的事实", receipt)
        self.assertIn("另一个群的事实", receipt)
        self.assertIn("私聊的事实", receipt)
        # ……但只有当前群的那条带上来源元数据
        self.assertEqual(receipt.count("最近确认"), 1)
        self.assertEqual(receipt.count("写入方式"), 1)
        self.assertEqual(receipt.count("https://t.me"), 1)

    async def test_source_metadata_follows_the_open_group(self) -> None:
        await self._seed_fact(
            fact_text="当前群的事实",
            source_kind="tool",
            source_message_id=PRESENT_SOURCE_ID,
        )

        # 同一个用户、同一个列表，但在另一个群里打开：不该出现来源元数据
        receipt = (await self._run("/memory", group_id=OTHER_GROUP_ID))[-1]

        self.assertIn("当前群的事实", receipt)
        self.assertNotIn("最近确认", receipt)
        self.assertNotIn("写入方式", receipt)
        self.assertNotIn("https://t.me", receipt)

    async def test_evidence_excerpt_is_never_rendered(self) -> None:
        await self._seed_fact(
            fact_text="只显示事实正文",
            source_kind="tool",
            source_message_id=PRESENT_SOURCE_ID,
            evidence_excerpt=f"{EXCERPT_MARKER} 原始片段",
        )

        receipt = (await self._run("/memory"))[-1]

        self.assertIn("只显示事实正文", receipt)
        self.assertNotIn(EXCERPT_MARKER, receipt)

    async def test_html_special_characters_are_escaped(self) -> None:
        raw = '<b>加V</b> & "优惠"'
        await self._seed_fact(
            fact_text=raw, source_kind="tool", source_message_id=PRESENT_SOURCE_ID
        )

        receipt = (await self._run("/memory"))[-1]

        self.assertIn(html.escape(raw), receipt)
        self.assertNotIn(raw, receipt)

    async def test_receipt_footer_states_the_global_index_permission_scope(self) -> None:
        await self._seed_fact(fact_text="页脚口径", source_kind="passive")

        receipt = (await self._run("/memory"))[-1]

        self.assertIn("全局序号", receipt)
        self.assertIn("跨页连续", receipt)
        self.assertIn("只能删关于你本人的", receipt)
        self.assertIn("群管理员可以删本群任意一条", receipt)
        self.assertIn("编号不存在或无权操作时，回执口径相同", receipt)

    # ---- B2-3：全局序号与权限口径 -----------------------------------------

    async def test_cross_page_global_index_forget_deletes_the_numbered_fact(
        self,
    ) -> None:
        identifiers = []
        for index in range(1, 12):
            identifiers.append(
                await self._seed_fact(
                    fact_text=f"事实 {index}",
                    confirm_count=index,
                    last_confirmed_at=CONFIRMED_AT - timedelta(minutes=index),
                    source_kind="passive",
                )
            )

        page_two = (await self._run("/memory 2"))[-1]
        self.assertIn("（第 2/2 页，共 11 条）", page_two)
        self.assertIn("#11", page_two)

        answer = (await self._run("/memory forget 11"))[-1]
        # B5 期望漂移：成功回执由「已删除这条长期记忆。」改为「已标记删除这条长期记忆。」（父代理指定文案）。
        self.assertIn("已标记删除这条长期记忆。", answer)
        # 全局序号 11 = 第 11 条（不是当前页的第一条）
        self.assertEqual(await self._fact_status(identifiers[10]), "deleted")
        self.assertEqual(await self._fact_status(identifiers[0]), "active")

    async def test_forget_unknown_number_uses_one_bundled_reason(self) -> None:
        await self._seed_fact(fact_text="唯一的事实", source_kind="passive")

        answer = (await self._run("/memory forget 999"))[-1]

        self.assertIn("没有找到这个编号", answer)
        self.assertIn("没有权限", answer)
        self.assertIn("全局序号", answer)
        # 不得引入按页拒绝的口径，也不得区分"存在但无权"与"不存在"
        self.assertNotIn("这一页", answer)
        self.assertNotIn("当前页", answer)

    async def test_group_admin_can_forget_another_users_fact_by_index(self) -> None:
        other_fact = await self._seed_fact(
            fact_text="别人的事实",
            subject_user_id=OTHER_MEMBER_ID,
            source_kind="passive",
        )

        answer = (await self._run("/memory forget 1", admin=True))[-1]

        # B5 期望漂移：成功回执由「已删除这条长期记忆。」改为「已标记删除这条长期记忆。」（父代理指定文案）。
        self.assertIn("已标记删除这条长期记忆。", answer)
        self.assertEqual(await self._fact_status(other_fact), "deleted")

    async def test_non_admin_cannot_forget_another_users_fact(self) -> None:
        other_fact = await self._seed_fact(
            fact_text="别人的事实",
            subject_user_id=OTHER_MEMBER_ID,
            source_kind="passive",
        )

        answer = (await self._run("/memory forget 1", admin=False))[-1]

        self.assertIn("没有找到这个编号", answer)
        self.assertEqual(await self._fact_status(other_fact), "active")

    async def test_off_is_idempotent_and_soft_deletes_own_facts(self) -> None:
        fact_id = await self._seed_fact(fact_text="我的一条事实", source_kind="passive")

        first = (await self._run("/memory off"))[-1]
        second = (await self._run("/memory off"))[-1]

        self.assertIn("已关闭长期记忆", first)
        self.assertIn("已关闭长期记忆", second)
        self.assertEqual(await self._fact_status(fact_id), "deleted")
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(UserFact).where(
                        UserFact.subject_user_id == MEMBER_ID,
                        UserFact.status == "active",
                    )
                )
            ).all()
        self.assertEqual(list(rows), [])

    async def test_current_group_list_still_shows_authorized_other_group_facts(
        self,
    ) -> None:
        await self._seed_fact(
            fact_text="另一个群的私事",
            scope="group",
            scope_id=OTHER_GROUP_ID,
            source_kind="tool",
            source_message_id=PRESENT_SOURCE_ID,
        )

        receipt = (await self._run("/memory"))[-1]

        self.assertIn("另一个群的私事", receipt)
        self.assertIn("群 合成另一个群", receipt)
        self.assertNotIn("https://t.me", receipt)

    # ---- 发送边界：真实 _answer → 真实发送助手，只替换 Message.answer ---------

    async def _run_at_send_boundary(
        self,
        text: str,
        *,
        group_id: int = SUPERGROUP_ID,
        user_id: int = MEMBER_ID,
        admin: bool = False,
    ) -> list[str]:
        """真实 ``cmd_memory`` → 真实 ``commands._answer`` → 真实发送助手。

        只把 ``Message.answer`` 换成本地记录替身：产品渲染与发送助手都不 mock，
        所以 ``_render_action_response`` / ``sanitize_outgoing_text`` / 模板拼装
        任何一环漏了转义，逃逸出来的 HTML 都会留在这份 payload 里。
        """

        sent: list[str] = []

        async def _record_answer(body, **_options):
            sent.append(str(body))
            return SimpleNamespace(message_id=99, chat=_chat(group_id))

        message = _message(text, group_id=group_id, user_id=user_id)
        message.message_id = 1
        message.answer = _record_answer
        with patch.object(
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
        ), patch.object(
            telegram_utils,
            "schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
            create=True,
        ):
            async with self.session_factory() as session:
                await commands.cmd_memory(message, session, _settings())
        return sent

    async def test_unknown_source_kind_is_escaped_at_the_send_boundary(self) -> None:
        forged = '<a href="https://example.invalid/forged">forged-source</a>'
        await self._seed_fact(
            fact_text="偏好喝美式",
            source_kind=forged,
            source_message_id=PRESENT_SOURCE_ID,
        )

        payload = (await self._run_at_send_boundary("/memory"))[-1]

        # 服务自己构造的已核验合法回链仍然可用
        self.assertIn(
            f'<a href="https://t.me/c/1234567890/{PRESENT_SOURCE_ID}">', payload
        )
        # 未知来源类型只能作为**文本**出现，不能变成可点击 HTML
        self.assertIn("&lt;a href=", payload)
        self.assertNotIn('<a href="https://example.invalid', payload)
        self.assertNotIn('<a href=&quot;https://example.invalid', payload)
        self.assertEqual(payload.count('<a href="'), 1)

    async def test_other_unknown_source_kind_shapes_are_escaped_too(self) -> None:
        await self._seed_fact(
            fact_text="事实甲", source_kind="<bad>", source_message_id=None
        )
        await self._seed_fact(
            fact_text="事实乙", source_kind="a & b", source_message_id=None
        )

        payload = (await self._run_at_send_boundary("/memory"))[-1]

        self.assertIn("来源类型 &lt;bad&gt;", payload)
        self.assertNotIn("<bad>", payload)
        self.assertIn("来源类型 a &amp; b", payload)
        self.assertNotIn("a & b", payload)

    async def test_source_state_unknown_is_not_reported_as_deleted_at_send_boundary(
        self,
    ) -> None:
        await self._seed_fact(
            fact_text="偏好喝美式",
            source_kind="passive",
            source_message_id=PRESENT_SOURCE_ID,
        )

        with patch.object(
            commands,
            "disposed_message_ids",
            new=AsyncMock(side_effect=RuntimeError("db")),
            create=True,
        ):
            payload = (await self._run_at_send_boundary("/memory"))[-1]

        self.assertNotIn("https://t.me", payload)
        self.assertIn("来源状态暂无法核验，本次不提供链接", payload)
        self.assertNotIn("已被审核删除", payload)

    async def test_source_link_recovers_after_a_verification_failure(self) -> None:
        await self._seed_fact(
            fact_text="偏好喝美式",
            source_kind="passive",
            source_message_id=PRESENT_SOURCE_ID,
        )

        with patch.object(
            commands,
            "disposed_message_ids",
            new=AsyncMock(side_effect=RuntimeError("db")),
            create=True,
        ):
            failed = (await self._run_at_send_boundary("/memory"))[-1]

        self.assertNotIn("https://t.me", failed)

        recovered = (await self._run_at_send_boundary("/memory"))[-1]

        self.assertIn(
            f'<a href="https://t.me/c/1234567890/{PRESENT_SOURCE_ID}">', recovered
        )
        self.assertNotIn("暂无法核验", recovered)

    async def test_source_verification_cancellation_is_not_swallowed(self) -> None:
        await self._seed_fact(
            fact_text="偏好喝美式",
            source_kind="passive",
            source_message_id=PRESENT_SOURCE_ID,
        )

        with patch.object(
            commands,
            "disposed_message_ids",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
            create=True,
        ), self.assertRaises(asyncio.CancelledError):
            await self._run_at_send_boundary("/memory")
