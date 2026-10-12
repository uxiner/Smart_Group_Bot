"""B2-1：``/find`` 的来源回链 —— 当前群隔离、删除保护与降级口径。

红检口径：在冻结基线 ``a6f5a5e`` 上这些用例必须是**业务语义失败**（回执里没有
链接、或已删除来源仍有链接），不能是导入失败/函数不存在。

覆盖 Requirement 2/3：只给当前可信 ``message.chat.id`` 的已授权命中加链接；
公开 username / 私有超级群 ``t.me/c`` / 基础群与缺 id 三种形状分别验证；
群成员限定标记；已删除来源即便 ``--all`` 显示正文也不给链接（依据真实处置记录，
不靠事先构造的布尔字段）；审核回链输出逐字不变。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Group, GroupMessageArchive, Violation
from bot.handlers import commands, group

#: 合成超级群（无公开用户名 → 只能给 t.me/c 链接）
SUPERGROUP_ID = -1001234567890
#: 合成公开群（有 username → 公开链接）
PUBLIC_GROUP_ID = -1001234567999
#: 合成基础群（既没有 username，也不是 -100 形状 → 没有链接）
BASIC_GROUP_ID = -55
#: 另一个合成群（跨群隔离用）
OTHER_GROUP_ID = -1009999999999

PUBLIC_USERNAME = "synthetic_public_group"
MEMBER_ID = 777
DELETED_MESSAGE_ID = 21
PRESENT_MESSAGE_ID = 22
OTHER_GROUP_MESSAGE_ID = 23


def _settings() -> Settings:
    return Settings(_env_file=None)


def _chat(group_id: int, *, username: str = "", chat_type: str = "supergroup"):
    return SimpleNamespace(
        id=group_id, type=chat_type, title="合成群", username=username
    )


def _message(text: str, chat, *, user_id: int = MEMBER_ID):
    return SimpleNamespace(
        text=text,
        chat=chat,
        from_user=SimpleNamespace(
            id=user_id, first_name="成员", last_name="", username=""
        ),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


def _hit(message_id: int, body: str = "普通消息") -> dict:
    return {
        "telegram_message_id": message_id,
        "message_type": "text",
        "content": body,
        "sent_at": "2026-01-05 09:00:00",
        "sender_name": "成员",
    }


def _baseline_link(chat: object, message_id: object) -> str:
    """冻结基线 ``a6f5a5e`` 里 ``_message_evidence_link`` 的逐字实现（回归参照）。"""

    try:
        mid = int(message_id or 0)
        chat_id = int(getattr(chat, "id", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if mid <= 0 or chat_id == 0:
        return ""
    username = str(getattr(chat, "username", "") or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}/{mid}"
    raw = str(chat_id)
    if raw.startswith("-100") and len(raw) > 4:
        return f"https://t.me/c/{raw[4:]}/{mid}"
    return ""


class FindSourceLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._db_path = handle.name
        handle.close()
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        async with self.session_factory() as session:
            session.add(Group(id=SUPERGROUP_ID, title="合成超级群", settings={}))
            session.add(Group(id=PUBLIC_GROUP_ID, title="合成公开群", settings={}))
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        if os.path.exists(self._db_path):
            os.unlink(self._db_path)

    async def _record_violation(
        self, group_id: int, message_id: int, action: str = "delete"
    ) -> None:
        async with self.session_factory() as session:
            if await session.get(Group, group_id) is None:
                session.add(Group(id=group_id, title="合成群", settings={}))
            session.add(
                Violation(
                    group_id=group_id,
                    user_id=message_id,
                    action_taken=action,
                    source_message_id=message_id,
                    message_text="合成违规文本",
                )
            )
            await session.commit()

    async def _run(
        self,
        text: str,
        *,
        chat,
        hits: list[dict] | None = None,
        admin: bool = False,
    ) -> tuple[list[str], AsyncMock, AsyncMock]:
        answers: list[str] = []

        async def _answer(_message, _settings, body, **_kwargs):
            answers.append(str(body))

        # C3 起 /find 的回链要「显式本群身份 + 本群现存归档行」双重现读：真实
        # MemoryService._archive_row_document 一定带 group_id，所以这里按同样口径补齐
        # 替身，并为命中种下真实归档行（旧用例只给召回快照，缺的是归档前置条件；
        # 断言一个字都没改）。
        prepared = [dict(hit) for hit in hits or []]
        chat_id = int(getattr(chat, "id", 0) or 0)
        for hit in prepared:
            hit.setdefault("group_id", chat_id)
        archive_ids = sorted(
            {
                int(hit.get("telegram_message_id") or 0)
                for hit in prepared
                if int(hit.get("telegram_message_id") or 0) > 0
            }
        )
        if archive_ids:
            async with self.session_factory() as seed_session:
                existing = set(
                    (
                        await seed_session.execute(
                            select(GroupMessageArchive.telegram_message_id).where(
                                GroupMessageArchive.group_id == chat_id,
                                GroupMessageArchive.telegram_message_id.in_(
                                    archive_ids
                                ),
                            )
                        )
                    ).scalars()
                )
                for message_id in archive_ids:
                    if message_id in existing:
                        continue
                    seed_session.add(
                        GroupMessageArchive(
                            group_id=chat_id,
                            message_key=f"u:{message_id}",
                            telegram_message_id=message_id,
                            content="合成归档正文",
                            raw_text="合成归档正文",
                            message_thread_id=None,
                        )
                    )
                await seed_session.commit()

        admin_check = AsyncMock(return_value=admin)
        recall = AsyncMock(return_value=prepared)
        stub = SimpleNamespace(recall_archive=recall)
        with patch.object(commands, "_answer", new=_answer), patch.object(
            commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
        ), patch.object(
            commands, "ensure_group_admin_permission", new=admin_check
        ), patch.object(
            commands.memory_holder, "get_optional", return_value=stub
        ):
            async with self.session_factory() as session:
                await commands.cmd_find(_message(text, chat), session, _settings())
        return answers, recall, admin_check

    # ---- 形状：公开 username / 私有超级群 / 基础群 / 缺 id -----------------

    async def test_public_group_hit_links_to_the_public_username(self) -> None:
        chat = _chat(PUBLIC_GROUP_ID, username=PUBLIC_USERNAME)
        answers, _recall, _admin = await self._run(
            "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
        )

        receipt = answers[-1]
        self.assertIn(
            f"https://t.me/{PUBLIC_USERNAME}/{PRESENT_MESSAGE_ID}", receipt
        )
        self.assertIn("查看原消息", receipt)
        # 公开链接不该被标成"仅群成员可用"
        self.assertNotIn("仅群成员可用", receipt)

    async def test_supergroup_hit_links_to_member_only_c_link(self) -> None:
        chat = _chat(SUPERGROUP_ID)
        answers, _recall, _admin = await self._run(
            "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
        )

        receipt = answers[-1]
        self.assertIn("https://t.me/c/1234567890/22", receipt)
        # 群成员限定标记（内联 + 页脚）
        self.assertIn("仅群成员可用", receipt)

    async def test_basic_group_hit_degrades_without_claiming_private_groups_are_unlinkable(
        self,
    ) -> None:
        chat = _chat(BASIC_GROUP_ID, chat_type="group")
        answers, _recall, _admin = await self._run(
            "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
        )

        receipt = answers[-1]
        self.assertNotIn("https://t.me", receipt)
        self.assertIn("没有链接", receipt)
        # 普通群没有链接，不能笼统说"所有私有群都无法链接"
        self.assertNotIn("所有", receipt)
        self.assertNotIn("私有群都无法链接", receipt)

    async def test_missing_message_id_degrades_without_link(self) -> None:
        chat = _chat(SUPERGROUP_ID)
        answers, _recall, _admin = await self._run(
            "/find 普通消息", chat=chat, hits=[_hit(0)]
        )

        receipt = answers[-1]
        self.assertNotIn("https://t.me", receipt)
        self.assertIn("缺少消息 id", receipt)

    # ---- 删除保护：真实处置记录，不是事先构造的布尔 -----------------------

    async def test_deleted_source_is_never_linked_even_for_admin_all(self) -> None:
        await self._record_violation(SUPERGROUP_ID, DELETED_MESSAGE_ID, "delete")
        chat = _chat(SUPERGROUP_ID)
        with self.assertLogs("bot.handlers.commands", level="WARNING") as captured:
            answers, recall, _admin = await self._run(
                "/find --all 普通消息",
                chat=chat,
                hits=[_hit(DELETED_MESSAGE_ID, "已被删除的旧正文")],
                admin=True,
            )

        receipt = answers[-1]
        # --all 仍然是管理员审计口径：正文照旧显示
        self.assertIn("已被删除的旧正文", receipt)
        self.assertIn("已包含被审核删除的消息（管理员审计口径）。", receipt)
        # 但绝不给链接
        self.assertNotIn("https://t.me", receipt)
        self.assertIn("已被审核删除的来源不提供链接。", receipt)
        self.assertTrue(recall.await_args.kwargs["include_disposed"])
        self.assertTrue(
            any("/find --all 含已删除内容" in line for line in captured.output)
        )

    async def test_present_source_keeps_its_link_next_to_a_deleted_one(self) -> None:
        await self._record_violation(SUPERGROUP_ID, DELETED_MESSAGE_ID, "delete")
        chat = _chat(SUPERGROUP_ID)
        answers, _recall, _admin = await self._run(
            "/find 普通消息",
            chat=chat,
            hits=[
                _hit(DELETED_MESSAGE_ID, "已被删除的旧正文"),
                _hit(PRESENT_MESSAGE_ID, "还在群里的正文"),
            ],
        )

        receipt = answers[-1]
        # 正控制：没被处置的来源照常给链接；负控制：已删除的那条不给
        self.assertEqual(receipt.count("https://t.me"), 1)
        self.assertIn(f"https://t.me/c/1234567890/{PRESENT_MESSAGE_ID}", receipt)
        self.assertIn("还在群里的正文", receipt)

    async def test_warned_but_present_source_still_gets_a_link(self) -> None:
        await self._record_violation(SUPERGROUP_ID, PRESENT_MESSAGE_ID, "warn")
        chat = _chat(SUPERGROUP_ID)
        answers, _recall, _admin = await self._run(
            "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
        )

        receipt = answers[-1]
        self.assertIn(f"https://t.me/c/1234567890/{PRESENT_MESSAGE_ID}", receipt)

    async def test_deletion_evidence_is_scoped_to_the_current_group(self) -> None:
        # 另一群的处置记录不该影响当前群的同号消息
        await self._record_violation(OTHER_GROUP_ID, PRESENT_MESSAGE_ID, "delete")
        chat = _chat(SUPERGROUP_ID)
        answers, _recall, _admin = await self._run(
            "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
        )

        receipt = answers[-1]
        self.assertIn(f"https://t.me/c/1234567890/{PRESENT_MESSAGE_ID}", receipt)
        self.assertNotIn(str(OTHER_GROUP_ID), receipt)

    async def test_disposal_check_failure_fails_closed(self) -> None:
        chat = _chat(SUPERGROUP_ID)
        # create=True：基线 a6f5a5e 上这个符号还不存在，patch 仍要能生效，
        # 这样红检失败会落在"链接照样渲染出来"的业务语义上，而不是 AttributeError。
        with patch.object(
            commands,
            "disposed_message_ids",
            new=AsyncMock(side_effect=RuntimeError("db")),
            create=True,
        ), self.assertLogs("bot.handlers.commands", level="WARNING"):
            answers, _recall, _admin = await self._run(
                "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
            )

        receipt = answers[-1]
        # 核验不了就不给链接（宁可少给，也不给一条可能指向已删除内容的链接）
        self.assertNotIn("https://t.me", receipt)
        # 但"核验失败/未知"不能被说成"已被审核删除"：没有证据时不得谎报已确认删除
        self.assertIn("来源状态暂无法核验，本次不提供链接。", receipt)
        self.assertNotIn("已被审核删除", receipt)

    async def test_disposal_check_recovers_after_a_failure(self) -> None:
        chat = _chat(SUPERGROUP_ID)
        with patch.object(
            commands,
            "disposed_message_ids",
            new=AsyncMock(side_effect=RuntimeError("db")),
            create=True,
        ):
            failed, _recall, _admin = await self._run(
                "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
            )

        self.assertNotIn("https://t.me", failed[-1])

        recovered, _recall2, _admin2 = await self._run(
            "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
        )

        self.assertIn(
            f"https://t.me/c/1234567890/{PRESENT_MESSAGE_ID}", recovered[-1]
        )
        self.assertNotIn("暂无法核验", recovered[-1])

    async def test_disposal_check_cancellation_is_not_swallowed(self) -> None:
        chat = _chat(SUPERGROUP_ID)
        with patch.object(
            commands,
            "disposed_message_ids",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
            create=True,
        ), self.assertRaises(asyncio.CancelledError):
            await self._run(
                "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
            )

    async def test_html_special_characters_in_hits_are_escaped(self) -> None:
        chat = _chat(PUBLIC_GROUP_ID, username=PUBLIC_USERNAME)
        answers, _recall, _admin = await self._run(
            '/find 普通消息',
            chat=chat,
            hits=[_hit(PRESENT_MESSAGE_ID, '<b>加V</b> & "优惠"')],
        )

        receipt = answers[-1]
        self.assertIn("&lt;b&gt;加V&lt;/b&gt;", receipt)
        self.assertNotIn("<b>加V</b>", receipt)

    # ---- 权限口径不变 ------------------------------------------------------

    async def test_non_admin_all_is_still_rejected_without_recall(self) -> None:
        chat = _chat(SUPERGROUP_ID)
        answers, recall, admin_check = await self._run(
            "/find --all 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)], admin=False
        )

        admin_check.assert_awaited()
        recall.assert_not_awaited()
        self.assertEqual(answers, [])

    async def test_default_member_query_keeps_excluding_disposed(self) -> None:
        chat = _chat(SUPERGROUP_ID)
        _answers, recall, admin_check = await self._run(
            "/find 普通消息", chat=chat, hits=[_hit(PRESENT_MESSAGE_ID)]
        )

        self.assertFalse(recall.await_args.kwargs["include_disposed"])
        admin_check.assert_not_awaited()


class AuditLinkRegressionTests(unittest.TestCase):
    """B2-A1：审核证据线用的 ``_message_evidence_link`` 输出必须逐字不变。"""

    def test_audit_link_output_matches_the_frozen_baseline(self) -> None:
        cases = [
            (_chat(PUBLIC_GROUP_ID, username=PUBLIC_USERNAME), 22),
            (_chat(PUBLIC_GROUP_ID, username="@synthetic_public_group"), 22),
            (_chat(SUPERGROUP_ID), 22),
            (_chat(BASIC_GROUP_ID, chat_type="group"), 22),
            (_chat(SUPERGROUP_ID), 0),
            (_chat(SUPERGROUP_ID), None),
            (_chat(SUPERGROUP_ID), "not-a-number"),
            (_chat(0), 22),
            (SimpleNamespace(), 22),
        ]
        for chat, message_id in cases:
            with self.subTest(chat=getattr(chat, "id", None), message_id=message_id):
                self.assertEqual(
                    group._message_evidence_link(chat, message_id),
                    _baseline_link(chat, message_id),
                )

    def test_public_and_private_shapes_are_intact(self) -> None:
        self.assertEqual(
            group._message_evidence_link(
                _chat(PUBLIC_GROUP_ID, username=PUBLIC_USERNAME), 22
            ),
            f"https://t.me/{PUBLIC_USERNAME}/22",
        )
        self.assertEqual(
            group._message_evidence_link(_chat(SUPERGROUP_ID), 22),
            "https://t.me/c/1234567890/22",
        )
        self.assertEqual(
            group._message_evidence_link(_chat(BASIC_GROUP_ID, chat_type="group"), 22),
            "",
        )
