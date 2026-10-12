"""B12 返工 v1（F4/F5）：真实消费端的来源链接必须来自「现存、同群」的归档行。

红检口径：在交付基线 ``3e51860`` 上这些用例必须是**业务语义失败**（回执里出现链接、
或链接的 thread 来自存储字段而不是归档行现读值），不能是导入失败/函数不存在。
覆盖两个真实消费端：``/find``（``commands.cmd_find``）与
``ConversationRecallSkill.run``（它的 ``summary`` 就是最终回复面）。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.db.models import GroupMessageArchive, Violation
from bot.handlers import commands
from bot.services.memory import MemoryService
from bot.services.skills.base import SkillContext
from bot.services.skills.conversation_recall import ConversationRecallSkill
from tests.test_permanent_memory_create_dedup import GROUP_A, GROUP_B, _RaceInjectedTestCase

MESSAGE_ID = 22
ARCHIVE_THREAD_ID = 7
#: 形如真实回链的「陈旧链接」：旧一轮渲染留在行里的 source_link，必须被清掉后重建。
STALE_LINK = "https://t.me/c/9999999999/999"


def _chat(group_id: int):
    return SimpleNamespace(
        id=group_id, type="supergroup", title="合成群", username=""
    )


def _message(text: str, chat):
    return SimpleNamespace(
        text=text,
        chat=chat,
        from_user=SimpleNamespace(id=777, first_name="成员", last_name="", username=""),
        bot=SimpleNamespace(send_message=AsyncMock()),
    )


class SourceLinkConsumerTests(_RaceInjectedTestCase):
    async def _archive_document(
        self, *, group_id: int, message_id: int = MESSAGE_ID, thread: int | None
    ) -> dict:
        """建一条真归档行，再用生产同款文档构造器产出召回文档。"""

        async with self.session_factory() as session:
            session.add(
                GroupMessageArchive(
                    group_id=group_id,
                    message_key=f"u:{message_id}",
                    telegram_message_id=message_id,
                    message_thread_id=thread,
                    content="归档正文（普通消息）",
                )
            )
            await session.commit()
            row = await session.scalar(
                select(GroupMessageArchive).where(
                    GroupMessageArchive.group_id == group_id,
                    GroupMessageArchive.telegram_message_id == message_id,
                )
            )
            await session.refresh(row)
            return MemoryService._archive_row_document(row)

    async def _run_find(self, hits: list[dict]) -> str:
        answers: list[str] = []

        async def _answer(_message, _settings, body, **_kwargs):
            answers.append(str(body))

        stub = SimpleNamespace(recall_archive=AsyncMock(return_value=list(hits)))
        with patch.object(commands, "_answer", new=_answer), patch.object(
            commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
        ), patch.object(
            commands, "ensure_group_admin_permission", new=AsyncMock(return_value=False)
        ), patch.object(commands.memory_holder, "get_optional", return_value=stub):
            async with self.session_factory() as session:
                await commands.cmd_find(
                    _message("/find 归档正文", _chat(GROUP_A)), session, self.settings
                )
        return answers[-1]

    async def test_find_link_uses_the_thread_from_the_live_archive_row(self) -> None:
        document = await self._archive_document(group_id=GROUP_A, thread=ARCHIVE_THREAD_ID)
        receipt = await self._run_find([document])
        self.assertIn(
            f"https://t.me/c/{str(GROUP_A)[4:]}/{ARCHIVE_THREAD_ID}/{MESSAGE_ID}", receipt
        )
        self.assertNotIn(f"https://t.me/c/{str(GROUP_A)[4:]}/{MESSAGE_ID}", receipt)

    async def test_find_link_withheld_for_a_hit_from_another_group(self) -> None:
        document = await self._archive_document(group_id=GROUP_B, thread=ARCHIVE_THREAD_ID)
        receipt = await self._run_find([document])
        self.assertNotIn("https://t.me", receipt)
        self.assertIn("来源归档不属于当前群", receipt)

    async def test_find_never_fabricates_a_thread(self) -> None:
        document = await self._archive_document(group_id=GROUP_A, thread=None)
        receipt = await self._run_find([document])
        self.assertIn(f"https://t.me/c/{str(GROUP_A)[4:]}/{MESSAGE_ID}", receipt)
        self.assertNotIn(f"/1/{MESSAGE_ID}", receipt, "General 线程号不能被凭空补上")

    async def _set_archive_thread(self, thread: int | None) -> None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(GroupMessageArchive).where(
                    GroupMessageArchive.group_id == GROUP_A,
                    GroupMessageArchive.telegram_message_id == MESSAGE_ID,
                )
            )
            row.message_thread_id = thread
            await session.commit()

    async def _delete_archive_row(self) -> None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(GroupMessageArchive).where(
                    GroupMessageArchive.group_id == GROUP_A,
                    GroupMessageArchive.telegram_message_id == MESSAGE_ID,
                )
            )
            await session.delete(row)
            await session.commit()

    async def test_find_rereads_the_live_thread_and_ignores_the_recall_snapshot(self) -> None:
        """/find 必须现读归档行取 thread：召回快照里的 thread 不作数。"""

        document = await self._archive_document(group_id=GROUP_A, thread=ARCHIVE_THREAD_ID)
        await self._set_archive_thread(9)

        receipt = await self._run_find([dict(document)])

        self.assertIn(f"https://t.me/c/{str(GROUP_A)[4:]}/9/{MESSAGE_ID}", receipt)
        self.assertNotIn(
            f"https://t.me/c/{str(GROUP_A)[4:]}/{ARCHIVE_THREAD_ID}/{MESSAGE_ID}",
            receipt,
            "陈旧快照 thread 不能出现在链接里",
        )

    async def test_find_withholds_the_link_after_the_archive_row_is_gone(self) -> None:
        document = await self._archive_document(group_id=GROUP_A, thread=ARCHIVE_THREAD_ID)
        await self._delete_archive_row()

        receipt = await self._run_find([dict(document)])

        self.assertNotIn("https://t.me", receipt)
        self.assertIn("来源消息已不在本群归档中，不提供链接。", receipt)
        # 「归档里没有」不等于「已被审核删除」，不能谎报处置结论
        self.assertNotIn("已被审核删除", receipt)

    async def test_find_withholds_the_link_without_group_provenance(self) -> None:
        document = await self._archive_document(group_id=GROUP_A, thread=ARCHIVE_THREAD_ID)
        stripped = dict(document)
        stripped.pop("group_id", None)

        receipt = await self._run_find([stripped])

        self.assertNotIn("https://t.me", receipt)
        self.assertIn("来源状态暂无法核验，本次不提供链接。", receipt)

    async def test_find_archive_lookup_fault_fails_closed_then_recovers(self) -> None:
        from bot.db.sqlite_session import SQLiteSafeAsyncSession

        document = await self._archive_document(group_id=GROUP_A, thread=ARCHIVE_THREAD_ID)
        original = SQLiteSafeAsyncSession.execute

        async def fault(session, statement, *args, **kwargs):
            if "group_message_archive" in str(statement):
                raise RuntimeError("archive lookup fault")
            return await original(session, statement, *args, **kwargs)

        with patch.object(SQLiteSafeAsyncSession, "execute", fault):
            failed = await self._run_find([dict(document)])

        self.assertNotIn("https://t.me", failed)
        self.assertIn("来源状态暂无法核验，本次不提供链接。", failed)
        # 核验失败只是未知：不能说成已删除
        self.assertNotIn("已被审核删除", failed)

        recovered = await self._run_find([dict(document)])
        self.assertIn(
            f"https://t.me/c/{str(GROUP_A)[4:]}/{ARCHIVE_THREAD_ID}/{MESSAGE_ID}",
            recovered,
        )
        self.assertNotIn("来源状态暂无法核验", recovered)

    async def _run_recall(self, rows: list[dict]) -> str:
        stub = SimpleNamespace(recall_archive=AsyncMock(return_value=list(rows)))
        async with self.session_factory() as session:
            with patch(
                "bot.services.skills.conversation_recall.memory_holder.get",
                return_value=stub,
            ):
                result = await ConversationRecallSkill().run(
                    {"query": "归档正文", "limit": 5},
                    SkillContext(chat_id=GROUP_A, session=session),
                )
        self.assertTrue(result.ok, result.error)
        return result.summary

    async def test_recall_summary_uses_the_live_archive_thread(self) -> None:
        document = await self._archive_document(group_id=GROUP_A, thread=ARCHIVE_THREAD_ID)
        summary = await self._run_recall([document])
        self.assertIn(
            f"source_link: https://t.me/c/{str(GROUP_A)[4:]}/{ARCHIVE_THREAD_ID}/{MESSAGE_ID}",
            summary,
        )
        self.assertIn("归档正文", summary)

    async def test_recall_drops_a_stale_or_unverified_link(self) -> None:
        # 行里带着「看起来已核验」的旧链接，但归档行并不存在：
        # 必须被清掉，不能被当成已核验来源直接透出。
        async with self.session_factory() as session:
            document = {
                "group_id": GROUP_A,
                "telegram_message_id": MESSAGE_ID,
                "message_thread_id": ARCHIVE_THREAD_ID,
                "message_type": "text",
                "content": "归档正文（普通消息）",
                "sent_at": "2026-01-05 09:00:00",
                "sender_name": "成员",
                "source_link": STALE_LINK,
                "source_verified": True,
            }
            self.assertIsNone(
                await session.scalar(
                    select(GroupMessageArchive).where(
                        GroupMessageArchive.group_id == GROUP_A,
                        GroupMessageArchive.telegram_message_id == MESSAGE_ID,
                    )
                )
            )
        summary = await self._run_recall([document])
        self.assertNotIn(STALE_LINK, summary)
        self.assertNotIn("source_link:", summary)
        self.assertNotIn("https://t.me", summary)

    async def test_recall_rebuilds_the_link_from_the_archive_not_the_stale_value(self) -> None:
        # 行里的旧链接与归档行现读的 thread 不一致：必须以归档行为准。
        document = await self._archive_document(group_id=GROUP_A, thread=ARCHIVE_THREAD_ID)
        document["source_link"] = STALE_LINK
        document["source_verified"] = True
        summary = await self._run_recall([document])
        self.assertNotIn(STALE_LINK, summary)
        self.assertIn(
            f"source_link: https://t.me/c/{str(GROUP_A)[4:]}/{ARCHIVE_THREAD_ID}/{MESSAGE_ID}",
            summary,
        )

    async def test_recall_withholds_link_when_the_row_group_is_missing_or_foreign(self) -> None:
        # 缺 group_id / 跨群：都不能凭行内的 origin 承诺拿链接。
        for group_value in (None, GROUP_B):
            document = {
                "telegram_message_id": MESSAGE_ID,
                "message_thread_id": ARCHIVE_THREAD_ID,
                "message_type": "text",
                "content": "归档正文（普通消息）",
                "sent_at": "2026-01-05 09:00:00",
                "sender_name": "成员",
                "source_link": STALE_LINK,
                "source_verified": True,
            }
            if group_value is not None:
                document["group_id"] = group_value
            summary = await self._run_recall([document])
            self.assertNotIn("https://t.me", summary, document.get("group_id"))
            self.assertNotIn("source_link:", summary)

    async def test_recall_withholds_link_for_a_deleted_source(self) -> None:
        document = await self._archive_document(group_id=GROUP_A, thread=ARCHIVE_THREAD_ID)
        async with self.session_factory() as session:
            session.add(
                Violation(
                    group_id=GROUP_A,
                    user_id=1,
                    source_message_id=MESSAGE_ID,
                    action_taken="delete",
                    message_text="合成违规文本",
                )
            )
            await session.commit()
        summary = await self._run_recall([document])
        self.assertNotIn("https://t.me", summary)
        self.assertNotIn("source_link:", summary)
        self.assertIn("归档正文", summary)


class ModerationEvidenceLinkTests(_RaceInjectedTestCase):
    """审核证据链（``group._violation_evidence`` → ``_render_admin_violation_report``）。

    这两个函数是真实消费端：证据卡上的「消息回链」由它们从实时消息对象构造并渲染。
    红检口径（``3e51860``）：thread 无法进入链接 → 渲染出的仍是旧的普通结构。
    """

    def _live_message(self, *, thread):
        chat = SimpleNamespace(id=GROUP_A, type="supergroup", title="合成群", username="")
        return SimpleNamespace(
            message_id=MESSAGE_ID,
            message_thread_id=thread,
            chat=chat,
            date=datetime(2026, 1, 5, 9, 0),
        )

    def _evidence(self, *, thread):
        from bot.handlers import group as group_module

        return group_module._violation_evidence(
            message=self._live_message(thread=thread),
            group_id=GROUP_A,
            user_id=777,
            display_name="成员",
            username="member",
            identity_label="成员",
            rule=None,
            action="delete",
            confidence=0.9,
            reason="合成理由",
            submitted_text="合成送审原文",
            executed=("已删除",),
        )

    def test_moderation_evidence_uses_the_live_message_thread(self) -> None:
        from bot.handlers import group as group_module

        evidence = self._evidence(thread=ARCHIVE_THREAD_ID)
        expected = f"https://t.me/c/{str(GROUP_A)[4:]}/{ARCHIVE_THREAD_ID}/{MESSAGE_ID}"
        self.assertEqual(evidence.message_link, expected)
        report = group_module._render_admin_violation_report(evidence)
        self.assertIn(expected, report)

    def test_moderation_evidence_keeps_the_plain_shape_without_a_thread(self) -> None:
        from bot.handlers import group as group_module

        for thread in (None, 0, True, "7"):
            evidence = self._evidence(thread=thread)
            self.assertEqual(
                evidence.message_link, f"https://t.me/c/{str(GROUP_A)[4:]}/{MESSAGE_ID}"
            )
        report = group_module._render_admin_violation_report(self._evidence(thread=None))
        self.assertNotIn(f"/{ARCHIVE_THREAD_ID}/{MESSAGE_ID}", report)
