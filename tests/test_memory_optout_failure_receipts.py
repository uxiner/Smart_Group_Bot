"""B4：``/memory off`` 与 ``/memory on`` 的写失败回执与可判别结果。

背景（父代理合成注入证据，基线 ``2880ecd`` / 规划 ``265214c``）：``set_optout`` 与
``clear_optout`` 吞掉事务异常后返回 ``0`` / ``False``，与「成功但零变化」同形；命令层
据此发成功回执。真实 ``DbSessionMiddleware`` 还会在 handler 返回后补一次 commit。

本文件按**红检资格**分两段：

* ``MemoryOptOutReceiptTests`` —— 只用**既有**产品符号（真实 ``cmd_memory`` + 真实
  service + 真实临时 SQLite + 真实 ``DbSessionMiddleware``）。在冻结基线上这些用例
  必须是**业务语义 AssertionError**（回执宣称成功、或残留写入被补偿 commit 落下），
  不能是导入失败或夹具错误。红检只跑这一段。
* ``MemoryWriteOutcomeProtocolTests`` —— 直接验证新增判别协议（新符号）。基线不存在
  这些符号，故**不参与旧红检**；这里用守卫式导入标注，运行时若缺符号会明确失败。

覆盖要求：计数与开关变更分离、异常只表示「未确认成功」、取消必须传播且清理自己的
未提交事务、``no_change`` 不得声称全局开关已开、旧 int/bool 包装保持精确。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import Group, MemoryOptout, UserFact
from bot.handlers import commands
from bot.middlewares.db import DbSessionMiddleware
from bot.services import long_term_memory as ltm
from bot.utils import telegram as telegram_utils

try:  # B4 新增协议；基线不存在 → 该段不参与旧红检。
    from bot.services.long_term_memory import (
        MemorySessionCleanupUnsafeError,
        MemoryWriteOutcome,
        clear_optout_outcome,
        set_optout_outcome,
    )

    _HAS_OUTCOME_PROTOCOL = True
except ImportError:  # pragma: no cover - 基线
    MemoryWriteOutcome = None  # type: ignore[assignment]
    MemorySessionCleanupUnsafeError = None  # type: ignore[assignment,misc]
    clear_optout_outcome = None  # type: ignore[assignment]
    set_optout_outcome = None  # type: ignore[assignment]
    _HAS_OUTCOME_PROTOCOL = False


SUPERGROUP_ID = -1001234567890
OTHER_GROUP_ID = -1009999999999
MEMBER_ID = 777
OTHER_MEMBER_ID = 778

CONFIRMED_AT = datetime(2026, 1, 4, 12, 0, 0)
FIRST_SEEN_AT = datetime(2026, 1, 1, 9, 0, 0)

# 审定的失败文案（父代理任务书逐字给定）。
OFF_UNCONFIRMED_HEAD = "<b>未能确认关闭长期记忆</b>"
OFF_UNCONFIRMED_BODY = (
    "这次操作未能确认成功，请稍后重试 /memory off；如果持续失败，请联系机器人维护者。"
)
ON_UNCONFIRMED_HEAD = "<b>未能确认重新打开长期记忆</b>"
ON_UNCONFIRMED_BODY = (
    "这次操作未能确认成功，请稍后重试 /memory on；如果持续失败，请联系机器人维护者。"
)

# 不得出现在失败回执里的东西：异常文本、SQL、个人/群标识、更强的承诺。
FORBIDDEN_IN_FAILURE = (
    "synthetic",
    "RuntimeError",
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "-1001234567890",
    "-1009999999999",
    "彻底删除",
    "无残留",
    "永不再记",
    "已持久生效",
)


def _settings() -> Settings:
    return Settings(_env_file=None)


def _message(text: str, *, group_id: int = SUPERGROUP_ID, user_id: int = MEMBER_ID):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(
            id=group_id, type="supergroup", title="合成群", username=""
        ),
        from_user=SimpleNamespace(
            id=user_id, first_name="成员", last_name="", username=""
        ),
        bot=SimpleNamespace(send_message=AsyncMock(), get_chat=AsyncMock()),
    )


class _OptOutTestCase(unittest.IsolatedAsyncioTestCase):
    """真实 handler + 真实 service + 真实 SQLite；只替换发送边界与外部授权查询。"""

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

    # ---- 播种与读取 --------------------------------------------------------

    async def _seed_fact(self, *, fingerprint: str, **overrides: Any) -> int:
        values: dict[str, Any] = {
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

    async def _fresh_fact(self, fact_id: int) -> tuple[str, Any]:
        async with self.session_factory() as session:
            row = await session.get(UserFact, fact_id)
            return str(row.status), row.last_confirmed_at

    async def _fresh_status(self, fact_id: int) -> str:
        status, _ = await self._fresh_fact(fact_id)
        return status

    async def _optout_exists(self, user_id: int = MEMBER_ID) -> bool:
        async with self.session_factory() as session:
            return (await session.get(MemoryOptout, user_id)) is not None

    async def _active_own_facts(self) -> list[int]:
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(UserFact.id).where(
                        UserFact.subject_user_id == MEMBER_ID,
                        UserFact.status == "active",
                    )
                )
            ).all()
        return [int(value) for value in rows]

    # ---- 命令入口 ----------------------------------------------------------

    async def _invoke(
        self, session: AsyncSession, text: str, answers: list[str], *, admin: bool = False
    ) -> None:
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
            await commands.cmd_memory(_message(text), session, _settings())

    async def _run(self, text: str, *, admin: bool = False) -> list[str]:
        answers: list[str] = []
        async with self.session_factory() as session:
            await self._invoke(session, text, answers, admin=admin)
        return answers

    async def _run_with_session(
        self, text: str, *, side_effects: dict[str, Any], admin: bool = False
    ) -> list[str]:
        """在**真实** service/DB 路径上只替换给定 session 方法的副作用。"""

        answers: list[str] = []
        async with self.session_factory() as session:
            with contextlib.ExitStack() as stack:
                for name, effect in side_effects.items():
                    stack.enter_context(patch.object(session, name, side_effect=effect))
                await self._invoke(session, text, answers, admin=admin)
        return answers

    # ---- 真链发送（只替换 transport） --------------------------------------

    async def _invoke_real_send(
        self,
        session: AsyncSession,
        text: str,
        sent: list[dict[str, Any]],
        *,
        admin: bool = False,
    ) -> None:
        """**不**替换 ``commands._answer``：走真实渲染 + 真实发送助手。

        只把 ``Message.answer``（transport 边界）换成记录器，其余全是产品代码。
        """

        message = _message(text)
        message.message_id = 1

        async def _transport(body: object, **kwargs: object) -> Any:
            sent.append(
                {"body": str(body), "options": {k: str(v) for k, v in kwargs.items()}}
            )
            return SimpleNamespace(message_id=99, chat=message.chat)

        message.answer = _transport
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
            await commands.cmd_memory(message, session, _settings())

    async def _run_real_send(
        self, text: str, *, side_effects: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """真链发送，可选地在**真实 session** 上替换给定方法的副作用。"""

        sent: list[dict[str, Any]] = []
        async with self.session_factory() as session:
            with contextlib.ExitStack() as stack:
                for name, effect in (side_effects or {}).items():
                    stack.enter_context(patch.object(session, name, side_effect=effect))
                await self._invoke_real_send(session, text, sent)
        return sent

    async def _run_through_middleware(
        self,
        text: str,
        *,
        class_side_effects: dict[str, Any],
        sent: list[dict[str, Any]],
    ) -> list[BaseException]:
        """真实 ``DbSessionMiddleware`` + 真实 handler + 真链发送。

        ``class_side_effects`` 打在上游基类 ``AsyncSession`` 上——这正是父代理探针
        用的注入面，生产 session 是它的子类，``super()`` 会命中被替换的实现。
        返回从 ``middleware(...)`` 逃出来的异常列表（正常情况下应为空）。
        """

        errors: list[BaseException] = []

        async def handler(event: object, data: dict[str, Any]) -> None:
            await self._invoke_real_send(data["session"], text, sent)

        middleware = DbSessionMiddleware(self.session_factory)
        with contextlib.ExitStack() as stack:
            for name, factory in class_side_effects.items():
                stack.enter_context(patch.object(AsyncSession, name, factory))
            try:
                await middleware(handler, object(), {})
            except BaseException as exc:  # noqa: BLE001 - 探针要记录逃出来的异常
                errors.append(exc)
        return errors

    # ---- 共用断言 ----------------------------------------------------------

    def _assert_failure_receipt(
        self, answers: list[str], *, head: str, body: str
    ) -> None:
        self.assertEqual(len(answers), 1, f"应当只发一条回执，实际 {answers!r}")
        receipt = answers[0]
        self.assertIn(head, receipt)
        self.assertIn(body, receipt)
        self.assertNotIn("已关闭长期记忆</b>", receipt)
        self.assertNotIn("已重新打开长期记忆</b>", receipt)
        for phrase in FORBIDDEN_IN_FAILURE:
            self.assertNotIn(phrase, receipt, f"失败回执不得包含 {phrase!r}")


# ---------------------------------------------------------------------------
# 一、红检资格段：只用既有产品符号
# ---------------------------------------------------------------------------


class MemoryOptOutReceiptTests(_OptOutTestCase):
    # ---- 旧包装的精确语义（计数只算事实） --------------------------------

    async def test_zero_fact_off_persists_the_switch_but_legacy_return_stays_zero(
        self,
    ) -> None:
        """0 事实首次 off 会持久创建开关行，但旧 ``set_optout`` 必须仍返回 0。"""

        async with self.session_factory() as session:
            removed = await ltm.set_optout(session, MEMBER_ID, reason="/memory off")

        self.assertEqual(removed, 0, "0 条事实时不得把开关行算进事实计数")
        self.assertTrue(await self._optout_exists(), "开关行必须已经持久化")

    async def test_off_with_one_fact_returns_one_not_two(self) -> None:
        """有 1 条事实时返回 1：开关行不得被加进事实计数（不能返回 2）。"""

        await self._seed_fact(fingerprint="fp-count-one")

        async with self.session_factory() as session:
            removed = await ltm.set_optout(session, MEMBER_ID, reason="/memory off")

        self.assertEqual(removed, 1)
        self.assertTrue(await self._optout_exists())

    async def test_legacy_clear_optout_bool_tracks_the_switch_row_only(self) -> None:
        """``clear_optout`` 的真假只反映开关行是否存在，与事实状态无关。"""

        await self._seed_fact(fingerprint="fp-legacy-bool")
        async with self.session_factory() as session:
            await ltm.set_optout(session, MEMBER_ID, reason="/memory off")

        async with self.session_factory() as session:
            self.assertTrue(await ltm.clear_optout(session, MEMBER_ID))
        async with self.session_factory() as session:
            self.assertFalse(
                await ltm.clear_optout(session, MEMBER_ID),
                "本来就没有开关行时必须返回 False",
            )

    # ---- off 正常路径 ------------------------------------------------------

    async def test_off_with_facts_soft_deletes_across_scopes_and_keeps_others(
        self,
    ) -> None:
        own_group = await self._seed_fact(fingerprint="fp-own-group")
        own_other = await self._seed_fact(
            fingerprint="fp-own-other", scope_id=OTHER_GROUP_ID
        )
        own_private = await self._seed_fact(
            fingerprint="fp-own-private", scope="private", scope_id=MEMBER_ID
        )
        other_member = await self._seed_fact(
            fingerprint="fp-other-member", subject_user_id=OTHER_MEMBER_ID
        )
        group_public = await self._seed_fact(
            fingerprint="fp-group-public", subject_user_id=0
        )

        answers = await self._run("/memory off")

        self.assertEqual(len(answers), 1)
        self.assertIn("<b>已关闭长期记忆</b>", answers[0])
        self.assertIn(
            "关于你在所有群与私聊里的 3 条长期事实已标记删除。", answers[0]
        )
        for fact_id in (own_group, own_other, own_private):
            self.assertEqual(await self._fresh_status(fact_id), "deleted")
        for fact_id in (other_member, group_public):
            self.assertEqual(await self._fresh_status(fact_id), "active")
        self.assertTrue(await self._optout_exists())

    async def test_off_without_facts_closes_the_switch_and_says_so(self) -> None:
        """0 事实首次关闭：开关真的关上了，就应当报成功，而不是报 0 条已删除。"""

        answers = await self._run("/memory off")

        self.assertEqual(len(answers), 1)
        receipt = answers[0]
        self.assertIn("<b>已关闭长期记忆</b>", receipt)
        self.assertTrue(await self._optout_exists(), "0 事实也必须真的持久化开关")
        self.assertNotIn(
            "彻底删除",
            receipt,
        )

    async def test_off_retry_does_not_claim_new_deletions(self) -> None:
        """已关闭状态下重试 off：不得声称「这次又删了 N 条」。"""

        await self._seed_fact(fingerprint="fp-retry")
        first = (await self._run("/memory off"))[0]
        second = (await self._run("/memory off"))[0]

        self.assertIn("<b>已关闭长期记忆</b>", first)
        self.assertIn(
            "关于你在所有群与私聊里的 1 条长期事实已标记删除。", first
        )
        self.assertIn("<b>已关闭长期记忆</b>", second)
        self.assertNotIn("长期事实已标记删除", second)
        self.assertIn("没有新的记录被删除", second)
        self.assertNotIn("0 条长期事实已标记删除", second)

    async def test_off_does_not_refresh_last_confirmed_at(self) -> None:
        """``/memory off`` 只改状态，绝不刷新 ``last_confirmed_at``（清理时钟不变）。"""

        fact_id = await self._seed_fact(fingerprint="fp-clock")
        before = (await self._fresh_fact(fact_id))[1]

        await self._run("/memory off")

        status, after = await self._fresh_fact(fact_id)
        self.assertEqual(status, "deleted")
        self.assertEqual(after, before, "off 不得重置墓碑的清理时钟")

    # ---- on 正常路径 -------------------------------------------------------

    async def test_on_with_switch_deleted_reports_reopened(self) -> None:
        await self._seed_fact(fingerprint="fp-on-normal")
        await self._run("/memory off")

        answers = await self._run("/memory on")

        self.assertEqual(len(answers), 1)
        self.assertIn("<b>已重新打开长期记忆</b>", answers[0])
        self.assertFalse(await self._optout_exists())

    async def test_on_without_switch_does_not_claim_the_global_switch_is_on(
        self,
    ) -> None:
        """没有开关行时 on 成功，只说明「个人关闭标记已清除」，不能说总开关是开的。"""

        answers = await self._run("/memory on")

        self.assertEqual(len(answers), 1)
        receipt = answers[0]
        self.assertIn("是否提炼仍取决于机器人设置", receipt)
        for overclaim in ("长期记忆本来就是打开的", "总开关已开启", "已开启长期记忆"):
            self.assertNotIn(overclaim, receipt)

    async def test_on_never_revives_deleted_facts(self) -> None:
        fact_id = await self._seed_fact(fingerprint="fp-revive")
        await self._run("/memory off")
        await self._run("/memory on")

        self.assertEqual(await self._fresh_status(fact_id), "deleted")
        self.assertEqual(await self._active_own_facts(), [])

    # ---- 失败路径：只表示「未确认成功」 ------------------------------------

    async def test_off_commit_failure_reports_unconfirmed_and_keeps_no_switch(
        self,
    ) -> None:
        await self._seed_fact(fingerprint="fp-off-commit-fail")

        answers = await self._run_with_session(
            "/memory off",
            side_effects={"commit": RuntimeError("synthetic commit failure")},
        )

        self._assert_failure_receipt(
            answers, head=OFF_UNCONFIRMED_HEAD, body=OFF_UNCONFIRMED_BODY
        )
        self.assertFalse(await self._optout_exists())
        self.assertEqual(len(await self._active_own_facts()), 1)

    async def test_off_execute_failure_reports_unconfirmed(self) -> None:
        await self._seed_fact(fingerprint="fp-off-exec-fail")

        answers = await self._run_with_session(
            "/memory off",
            side_effects={"execute": RuntimeError("synthetic execute failure")},
        )

        self._assert_failure_receipt(
            answers, head=OFF_UNCONFIRMED_HEAD, body=OFF_UNCONFIRMED_BODY
        )
        self.assertFalse(await self._optout_exists())

    async def test_off_read_failure_reports_unconfirmed(self) -> None:
        await self._seed_fact(fingerprint="fp-off-read-fail")

        answers = await self._run_with_session(
            "/memory off",
            side_effects={"get": RuntimeError("synthetic read failure")},
        )

        self._assert_failure_receipt(
            answers, head=OFF_UNCONFIRMED_HEAD, body=OFF_UNCONFIRMED_BODY
        )
        self.assertFalse(await self._optout_exists())

    async def test_on_commit_failure_reports_unconfirmed(self) -> None:
        await self._seed_fact(fingerprint="fp-on-commit-fail")
        await self._run("/memory off")

        answers = await self._run_with_session(
            "/memory on",
            side_effects={"commit": RuntimeError("synthetic commit failure")},
        )

        self._assert_failure_receipt(
            answers, head=ON_UNCONFIRMED_HEAD, body=ON_UNCONFIRMED_BODY
        )
        self.assertTrue(await self._optout_exists(), "失败后开关行必须原样保留")

    async def test_on_execute_failure_reports_unconfirmed(self) -> None:
        await self._seed_fact(fingerprint="fp-on-exec-fail")
        await self._run("/memory off")

        answers = await self._run_with_session(
            "/memory on",
            side_effects={"execute": RuntimeError("synthetic execute failure")},
        )

        self._assert_failure_receipt(
            answers, head=ON_UNCONFIRMED_HEAD, body=ON_UNCONFIRMED_BODY
        )
        self.assertTrue(await self._optout_exists())

    async def test_off_after_a_real_commit_does_not_claim_no_effect(self) -> None:
        """commit 已经成功、随后才抛错：必须保持「未确认」，不得宣称没有生效。"""

        await self._seed_fact(fingerprint="fp-off-late-error")
        answers: list[str] = []
        async with self.session_factory() as session:
            real_commit = session.commit

            async def late_error():
                await real_commit()
                raise RuntimeError("synthetic error after actual commit")

            with patch.object(session, "commit", side_effect=late_error):
                await self._invoke(session, "/memory off", answers)

        self._assert_failure_receipt(
            answers, head=OFF_UNCONFIRMED_HEAD, body=OFF_UNCONFIRMED_BODY
        )
        # 持久化变化**确实已经发生** —— 所以不能承诺「没有生效」。
        self.assertTrue(await self._optout_exists())
        self.assertEqual(await self._active_own_facts(), [])

    async def test_on_after_a_real_commit_does_not_claim_no_effect(self) -> None:
        await self._seed_fact(fingerprint="fp-on-late-error")
        await self._run("/memory off")

        answers: list[str] = []
        async with self.session_factory() as session:
            real_commit = session.commit

            async def late_error():
                await real_commit()
                raise RuntimeError("synthetic error after actual commit")

            with patch.object(session, "commit", side_effect=late_error):
                await self._invoke(session, "/memory on", answers)

        self._assert_failure_receipt(
            answers, head=ON_UNCONFIRMED_HEAD, body=ON_UNCONFIRMED_BODY
        )
        self.assertFalse(await self._optout_exists())

    # ---- 取消：必须传播，且不得把未提交事务留给中间件 ----------------------

    async def test_off_cancellation_propagates_without_receipt_and_cleans_up(
        self,
    ) -> None:
        await self._seed_fact(fingerprint="fp-off-cancel")
        answers: list[str] = []

        async with self.session_factory() as session:
            with patch.object(
                session, "commit", side_effect=asyncio.CancelledError()
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await self._invoke(session, "/memory off", answers)

            # 在上下文清理**之前**观测：本服务必须自己收拾干净。
            self.assertFalse(
                session.in_transaction(),
                "取消后不得把未提交事务留给 DbSessionMiddleware 的补偿 commit",
            )
            self.assertEqual(list(session.new), [])
            self.assertEqual(list(session.dirty), [])

        self.assertEqual(answers, [], "取消不得产生任何回执")
        self.assertFalse(await self._optout_exists())

    async def test_on_cancellation_propagates_without_receipt_and_cleans_up(
        self,
    ) -> None:
        await self._seed_fact(fingerprint="fp-on-cancel")
        await self._run("/memory off")
        answers: list[str] = []

        async with self.session_factory() as session:
            with patch.object(
                session, "commit", side_effect=asyncio.CancelledError()
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await self._invoke(session, "/memory on", answers)

            self.assertFalse(session.in_transaction())
            self.assertEqual(list(session.deleted), [])

        self.assertEqual(answers, [])
        self.assertTrue(await self._optout_exists(), "取消后开关行必须仍然存在")

    async def test_off_execute_cancellation_also_cleans_up(self) -> None:
        await self._seed_fact(fingerprint="fp-off-exec-cancel")
        answers: list[str] = []

        async with self.session_factory() as session:
            with patch.object(
                session, "execute", side_effect=asyncio.CancelledError()
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await self._invoke(session, "/memory off", answers)

            self.assertFalse(session.in_transaction())
            self.assertEqual(list(session.new), [])

        self.assertEqual(answers, [])
        self.assertFalse(await self._optout_exists())

    # ---- 回滚也失败时不得留下可被补偿 commit 写下的残留 --------------------

    async def test_broken_rollback_cannot_leave_residual_writes(self) -> None:
        """commit 与 rollback 同时失败：残留写入不得被中间件的补偿 commit 落下。"""

        await self._seed_fact(fingerprint="fp-residual")
        middleware = DbSessionMiddleware(self.session_factory)
        real_commit = AsyncSession.commit
        calls = {"commit": 0}

        async def flaky_commit(self):  # noqa: ANN001 - 与 SQLAlchemy 签名对齐
            calls["commit"] += 1
            if calls["commit"] == 1:
                raise RuntimeError("synthetic first commit failure")
            return await real_commit(self)

        async def broken_rollback(self):  # noqa: ANN001
            raise RuntimeError("synthetic rollback failure")

        captured: list[list[str]] = []

        async def handler(_event, data):
            answers: list[str] = []
            await self._invoke(data["session"], "/memory off", answers)
            captured.append(answers)
            return answers

        with patch.object(AsyncSession, "commit", flaky_commit), patch.object(
            AsyncSession, "rollback", broken_rollback
        ):
            await middleware(handler, object(), {})

        self.assertEqual(calls["commit"], 2, "中间件应当仍然尝试了补偿 commit")
        self._assert_failure_receipt(
            captured[0], head=OFF_UNCONFIRMED_HEAD, body=OFF_UNCONFIRMED_BODY
        )
        self.assertFalse(
            await self._optout_exists(),
            "rollback 失败时本服务必须阻断残留写入被补偿 commit 提交",
        )
        self.assertEqual(len(await self._active_own_facts()), 1)

    async def test_middleware_compensating_commit_does_write_pending_work(self) -> None:
        """同运行内的正常对照：中间件的补偿 commit 确实会落下待提交写入。"""

        middleware = DbSessionMiddleware(self.session_factory)

        async def handler(_event, data):
            data["session"].add(
                MemoryOptout(
                    user_id=MEMBER_ID,
                    created_at=ltm.now_shanghai_naive(),
                    reason="/memory off",
                )
            )
            return "ok"

        await middleware(handler, object(), {})

        self.assertTrue(
            await self._optout_exists(),
            "对照必须成立，否则上面的残留断言就没有意义",
        )


# ---------------------------------------------------------------------------
# 二、协议段：直接验证新增判别结果（基线无这些符号，不参与旧红检）
# ---------------------------------------------------------------------------


class MemoryWriteOutcomeProtocolTests(_OptOutTestCase):
    def setUp(self) -> None:
        if not _HAS_OUTCOME_PROTOCOL:
            self.fail(
                "B4 判别协议尚未实现：缺少 set_optout_outcome / clear_optout_outcome "
                "/ MemoryWriteOutcome（本段按设计不参与旧基线红检）"
            )

    async def test_off_outcome_separates_switch_change_from_fact_count(self) -> None:
        async with self.session_factory() as session:
            outcome = await set_optout_outcome(session, MEMBER_ID, reason="/memory off")

        self.assertTrue(outcome.committed)
        self.assertTrue(outcome.switch_changed, "首次关闭新建了开关行")
        self.assertEqual(outcome.facts_soft_deleted, 0)
        self.assertTrue(outcome.changed)

    async def test_off_outcome_retry_reports_no_change(self) -> None:
        async with self.session_factory() as session:
            await set_optout_outcome(session, MEMBER_ID, reason="/memory off")
        async with self.session_factory() as session:
            outcome = await set_optout_outcome(session, MEMBER_ID, reason="/memory off")

        self.assertTrue(outcome.committed)
        self.assertFalse(outcome.switch_changed)
        self.assertEqual(outcome.facts_soft_deleted, 0)
        self.assertFalse(outcome.changed)

    async def test_outcome_never_carries_exception_text(self) -> None:
        async with self.session_factory() as session:
            with patch.object(
                session, "commit", side_effect=RuntimeError("synthetic-leak-marker")
            ):
                outcome = await set_optout_outcome(session, MEMBER_ID)

        self.assertFalse(outcome.committed)
        self.assertNotIn("synthetic-leak-marker", repr(outcome))
        self.assertNotIn("RuntimeError", repr(outcome))
        self.assertTrue(outcome.detail, "必须带一个固定原因码用于日志")

    async def test_on_outcome_reports_switch_change(self) -> None:
        async with self.session_factory() as session:
            await set_optout_outcome(session, MEMBER_ID, reason="/memory off")
        async with self.session_factory() as session:
            outcome = await clear_optout_outcome(session, MEMBER_ID)

        self.assertTrue(outcome.committed)
        self.assertTrue(outcome.switch_changed)

        async with self.session_factory() as session:
            again = await clear_optout_outcome(session, MEMBER_ID)

        self.assertTrue(again.committed)
        self.assertFalse(again.switch_changed, "没有开关行时是「成功但零变化」")


# ---------------------------------------------------------------------------
# 三、真链发送：**不**替换 commands._answer，只替换 Message.answer（transport）
# ---------------------------------------------------------------------------


class MemoryRealSendChainTests(_OptOutTestCase):
    """B4 返工补的发送边界证据。

    原 ``_invoke`` 把 ``commands._answer`` 换成了记录器——那只证明「handler 传了什么
    字符串」，**不是**真实发送边界的证据。本段走 ``cmd_memory`` → 真实 ``_answer`` →
    真实 ``_render_action_response`` / ``answer_with_auto_delete``，只把
    ``Message.answer`` 换成记录器。
    """

    OFF_SUCCESS_HEAD = "已关闭长期记忆"
    ON_SUCCESS_HEAD = "已重新打开长期记忆"

    async def test_off_success_receipt_through_real_send_chain(self) -> None:
        fact = await self._seed_fact(fingerprint="real-send-off-success")

        sent = await self._run_real_send("/memory off")

        self.assertEqual(len(sent), 1, f"应当只发一条消息，实际 {sent!r}")
        body = sent[0]["body"]
        self.assertIn(self.OFF_SUCCESS_HEAD, body)
        self.assertIn("关于你在所有群与私聊里的 1 条长期事实已标记删除。", body)
        self.assertIn("这个开关是全局的", body)
        self.assertNotIn("未能确认", body)
        self.assertEqual(await self._fresh_status(fact), "deleted")
        self.assertTrue(await self._optout_exists())

    async def test_on_success_receipt_through_real_send_chain(self) -> None:
        await self._run_real_send("/memory off")
        sent = await self._run_real_send("/memory on")

        self.assertEqual(len(sent), 1, f"应当只发一条消息，实际 {sent!r}")
        body = sent[0]["body"]
        self.assertIn(self.ON_SUCCESS_HEAD, body)
        self.assertNotIn("未能确认", body)
        self.assertFalse(await self._optout_exists())

    async def test_off_unconfirmed_receipt_through_real_send_chain(self) -> None:
        await self._seed_fact(fingerprint="real-send-off-failure")

        sent = await self._run_real_send(
            "/memory off",
            side_effects={"commit": RuntimeError("synthetic-real-send")},
        )

        self.assertEqual(len(sent), 1, f"应当只发一条消息，实际 {sent!r}")
        body = sent[0]["body"]
        self.assertIn(OFF_UNCONFIRMED_HEAD, body)
        self.assertIn(OFF_UNCONFIRMED_BODY, body)
        self.assertNotIn(self.OFF_SUCCESS_HEAD, body)
        for phrase in FORBIDDEN_IN_FAILURE:
            self.assertNotIn(phrase, body, f"失败回执不得包含 {phrase!r}")

    async def test_on_unconfirmed_receipt_through_real_send_chain(self) -> None:
        await self._run_real_send("/memory off")

        sent = await self._run_real_send(
            "/memory on",
            side_effects={"commit": RuntimeError("synthetic-real-send")},
        )

        self.assertEqual(len(sent), 1, f"应当只发一条消息，实际 {sent!r}")
        body = sent[0]["body"]
        self.assertIn(ON_UNCONFIRMED_HEAD, body)
        self.assertIn(ON_UNCONFIRMED_BODY, body)
        self.assertNotIn(self.ON_SUCCESS_HEAD, body)
        for phrase in FORBIDDEN_IN_FAILURE:
            self.assertNotIn(phrase, body, f"失败回执不得包含 {phrase!r}")

    async def test_off_post_commit_error_reports_unconfirmed_and_may_persist(
        self,
    ) -> None:
        """commit 已经**真实返回**、随后才抛错：只能发未确认回执，且状态可能已落库。"""

        fact = await self._seed_fact(fingerprint="real-send-post-commit-off")
        sent: list[dict[str, Any]] = []
        async with self.session_factory() as session:
            real_commit = session.commit

            async def _commit_then_fail() -> None:
                await real_commit()
                raise RuntimeError("synthetic-post-commit")

            with patch.object(session, "commit", new=_commit_then_fail):
                await self._invoke_real_send(session, "/memory off", sent)

        self.assertEqual(len(sent), 1, f"应当只发一条消息，实际 {sent!r}")
        body = sent[0]["body"]
        self.assertIn(OFF_UNCONFIRMED_HEAD, body)
        self.assertNotIn(self.OFF_SUCCESS_HEAD, body, "不得把结果伪造成成功")
        # commit 真的返回过 ⇒ 变化**可能**已经持久化；这里如实读实际状态。
        self.assertTrue(await self._optout_exists())
        self.assertEqual(await self._fresh_status(fact), "deleted")

    async def test_off_post_commit_cancel_sends_nothing_and_keeps_changes(
        self,
    ) -> None:
        """commit 已经**真实返回**、随后才取消：不得发任何回执，且状态已落库。"""

        fact = await self._seed_fact(fingerprint="real-send-post-cancel-off")
        sent: list[dict[str, Any]] = []
        async with self.session_factory() as session:
            real_commit = session.commit

            async def _commit_then_cancel() -> None:
                await real_commit()
                raise asyncio.CancelledError()

            with patch.object(session, "commit", new=_commit_then_cancel):
                with self.assertRaises(asyncio.CancelledError):
                    await self._invoke_real_send(session, "/memory off", sent)

        self.assertEqual(sent, [], "取消路径不得发出任何回执")
        self.assertTrue(await self._optout_exists())
        self.assertEqual(await self._fresh_status(fact), "deleted")

    async def test_on_post_commit_cancel_sends_nothing_and_keeps_changes(
        self,
    ) -> None:
        await self._run_real_send("/memory off")
        sent: list[dict[str, Any]] = []
        async with self.session_factory() as session:
            real_commit = session.commit

            async def _commit_then_cancel() -> None:
                await real_commit()
                raise asyncio.CancelledError()

            with patch.object(session, "commit", new=_commit_then_cancel):
                with self.assertRaises(asyncio.CancelledError):
                    await self._invoke_real_send(session, "/memory on", sent)

        self.assertEqual(sent, [], "取消路径不得发出任何回执")
        self.assertFalse(await self._optout_exists(), "真实 commit 已经删掉开关行")


# ---------------------------------------------------------------------------
# 四、真实中间件下的清理失败：fail-closed 必须阻断补偿 commit
# ---------------------------------------------------------------------------


class MemoryMiddlewareCleanupFaultTests(_OptOutTestCase):
    """父代理阻断项的回归：commit 未执行 + rollback 失败 + close 失败。

    旧行为：``_discard_pending`` 吞掉 close 失败 → handler 正常 return →
    ``DbSessionMiddleware`` 的补偿 commit 把残留真的写进库（``commit=2``）。
    修后必须 fail-closed：异常逃到中间件之外，补偿 commit 不发生。
    """

    def setUp(self) -> None:
        if not _HAS_OUTCOME_PROTOCOL:
            self.fail("B4 判别协议尚未实现（本段按设计不参与旧基线红检）")

    @staticmethod
    def _fault_wrappers(*, fail_commit: bool, fail_rollback: bool, fail_close: bool):
        real_commit = AsyncSession.commit
        real_rollback = AsyncSession.rollback
        real_close = AsyncSession.close
        calls = {"commit": 0, "close": 0, "rollback": 0}

        async def commit(session: Any) -> None:
            calls["commit"] += 1
            if fail_commit and calls["commit"] == 1:
                raise RuntimeError("synthetic first commit")
            await real_commit(session)

        async def rollback(session: Any) -> None:
            calls["rollback"] += 1
            if fail_rollback:
                raise RuntimeError("synthetic rollback")
            await real_rollback(session)

        async def close(session: Any) -> None:
            calls["close"] += 1
            if fail_close and calls["close"] == 1:
                raise RuntimeError("synthetic close")
            await real_close(session)

        return calls, {"commit": commit, "rollback": rollback, "close": close}

    async def test_off_double_cleanup_fault_blocks_compensation_commit(self) -> None:
        fact = await self._seed_fact(fingerprint="middleware-fault-off")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(
            fail_commit=True, fail_rollback=True, fail_close=True
        )

        errors = await self._run_through_middleware(
            "/memory off", class_side_effects=wrappers, sent=sent
        )

        self.assertEqual(len(errors), 1, f"应当上抛一次清理阻断异常，实际 {errors!r}")
        self.assertIsInstance(errors[0], MemorySessionCleanupUnsafeError)
        self.assertEqual(
            calls["commit"], 1, "第一次 commit 之后不得再发生补偿 commit"
        )
        self.assertEqual(len(sent), 1, f"仍应发出固定失败回执，实际 {sent!r}")
        self.assertIn(OFF_UNCONFIRMED_HEAD, sent[0]["body"])
        self.assertNotIn("已关闭长期记忆</b>", sent[0]["body"])
        for phrase in FORBIDDEN_IN_FAILURE:
            self.assertNotIn(phrase, sent[0]["body"], f"失败回执不得包含 {phrase!r}")
        # 残留**没有**被补偿提交写下去。
        self.assertFalse(await self._optout_exists(), "不得新建开关行")
        self.assertEqual(await self._fresh_status(fact), "active", "事实不得被软删")

    async def test_on_double_cleanup_fault_blocks_compensation_commit(self) -> None:
        await self._run_real_send("/memory off")
        fact = await self._seed_fact(fingerprint="middleware-fault-on", status="deleted")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(
            fail_commit=True, fail_rollback=True, fail_close=True
        )

        errors = await self._run_through_middleware(
            "/memory on", class_side_effects=wrappers, sent=sent
        )

        self.assertEqual(len(errors), 1, f"应当上抛一次清理阻断异常，实际 {errors!r}")
        self.assertIsInstance(errors[0], MemorySessionCleanupUnsafeError)
        self.assertEqual(calls["commit"], 1, "不得发生补偿 commit")
        self.assertEqual(len(sent), 1, f"仍应发出固定失败回执，实际 {sent!r}")
        self.assertIn(ON_UNCONFIRMED_HEAD, sent[0]["body"])
        for phrase in FORBIDDEN_IN_FAILURE:
            self.assertNotIn(phrase, sent[0]["body"], f"失败回执不得包含 {phrase!r}")
        self.assertTrue(await self._optout_exists(), "开关行不得被残留写入删掉")
        self.assertEqual(await self._fresh_status(fact), "deleted", "on 不复活事实")

    async def test_rollback_fault_with_working_close_is_contained(self) -> None:
        """只有 rollback 失败、close 正常：仍可安全清理，照常发未确认回执、不上抛。"""

        fact = await self._seed_fact(fingerprint="middleware-rollback-only")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(
            fail_commit=True, fail_rollback=True, fail_close=False
        )

        errors = await self._run_through_middleware(
            "/memory off", class_side_effects=wrappers, sent=sent
        )

        self.assertEqual(errors, [], "close 已证明丢弃事务时不应上抛")
        self.assertEqual(len(sent), 1, f"应当只发一条回执，实际 {sent!r}")
        self.assertIn(OFF_UNCONFIRMED_HEAD, sent[0]["body"])
        self.assertFalse(await self._optout_exists())
        self.assertEqual(await self._fresh_status(fact), "active")

    async def test_middleware_success_control_persists_normally(self) -> None:
        """同运行内的正常对照：无故障注入时一切照旧。"""

        fact = await self._seed_fact(fingerprint="middleware-success-control")
        sent: list[dict[str, Any]] = []
        calls, wrappers = self._fault_wrappers(
            fail_commit=False, fail_rollback=False, fail_close=False
        )

        errors = await self._run_through_middleware(
            "/memory off", class_side_effects=wrappers, sent=sent
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(sent), 1, f"应当只发一条回执，实际 {sent!r}")
        self.assertIn("已关闭长期记忆", sent[0]["body"])
        self.assertNotIn("未能确认", sent[0]["body"])
        self.assertTrue(await self._optout_exists())
        self.assertEqual(await self._fresh_status(fact), "deleted")
