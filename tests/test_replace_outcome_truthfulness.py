"""B8-1：自然语言「修改永久记忆」的真实结果语义。

父代理独立探针（真项目 SQLite）证明过的缺陷：``MemoryService.replace_permanent_memory``
先 ``session.delete()`` + ``flush()``，之后才判断新内容是否有效。于是「目标命中 +
新内容为空/纯空白」会**真删掉旧记忆**，返回 ``created_row=None``，技能层再把它渲染成
「永久记忆修改失败，新内容为空或无效。」——一次被报成失败的真实删除。

本模块分两段：

* ``ReplaceTruthfulnessTests``（业务段）：只用基线 ``605234b`` 上**已存在**的入口
  （legacy 三元组方法 + 真 SQLite + 真 ``MemoryService``）构造，在冻结基线上必须红，
  且红必须是业务断言失败（旧行真的没了），不是缺符号。
* ``ReplaceOutcomeProtocolTests``（协议段）：涉及本批新增的 ``PM_REPLACE_*`` 常量、
  ``PermanentMemoryReplaceOutcome`` 与 ``replace_permanent_memory_outcome``。在基线上
  这些符号不存在，守卫式导入会让每个用例在 ``setUp`` 里明确失败——属「缺符号兼容红」，
  不计入业务红检。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bot.config import BotConfig
from bot.db.engine import init_db
from bot.db.models import Group, GroupPermanentMemory
from bot.services.memory import MemoryService

try:  # pragma: no cover - 基线上不存在，守卫式导入只为产出可读的兼容红
    from bot.services.memory import (
        PM_REPLACE_INVALID_CONTENT,
        PM_REPLACE_NOT_FOUND,
        PM_REPLACE_UNCONFIRMED,
        PM_REPLACE_UPDATED,
        PermanentMemoryReplaceOutcome,
    )

    _REPLACE_IMPORT_ERROR: BaseException | None = None
except Exception as exc:  # noqa: BLE001 - 故意捕获任意导入失败
    PM_REPLACE_INVALID_CONTENT = "invalid_new_content"
    PM_REPLACE_NOT_FOUND = "not_found"
    PM_REPLACE_UNCONFIRMED = "unconfirmed"
    PM_REPLACE_UPDATED = "updated"
    PermanentMemoryReplaceOutcome = None  # type: ignore[assignment]
    _REPLACE_IMPORT_ERROR = exc

_REPLACE_OUTCOME_BINDING = getattr(MemoryService, "replace_permanent_memory_outcome", None)

GROUP_A = -1001234567890
GROUP_B = -1009999999999
ADMIN_ID = 779
EARLY = datetime(2020, 1, 1, 0, 0, 0)


class _StubLLM:
    class main:
        model = "synthetic/model"

    async def compress(self, system: str, user_text: str) -> str:
        _ = system, user_text
        return ""


class _ReplaceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(f"sqlite+aiosqlite:///{self.db_path}")
        async with self.session_factory() as session:
            session.add_all(
                [
                    Group(id=GROUP_A, title="合成群 A", settings={}),
                    Group(id=GROUP_B, title="合成群 B", settings={}),
                ]
            )
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            path = f"{self.db_path}{suffix}"
            if os.path.exists(path):
                os.unlink(path)

    def _service(self, *, factory=None, llm=None) -> MemoryService:
        return MemoryService(
            BotConfig(max_context_tokens=4096, max_output_tokens=512),
            llm if llm is not None else _StubLLM(),
            session_factory=factory if factory is not None else self.session_factory,
        )

    async def _seed(self, content: str, *, group_id: int = GROUP_A, created_by: int = ADMIN_ID) -> int:
        async with self.session_factory() as session:
            row = GroupPermanentMemory(
                group_id=group_id,
                content=content,
                created_by=created_by,
                created_at=EARLY,
                updated_at=EARLY,
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def _fresh(self, memory_id: int) -> GroupPermanentMemory | None:
        async with self.session_factory() as session:
            return await session.get(GroupPermanentMemory, memory_id)

    async def _fresh_contents(self, *, group_id: int = GROUP_A) -> list[str]:
        async with self.session_factory() as session:
            rows = (
                await session.scalars(
                    select(GroupPermanentMemory)
                    .where(GroupPermanentMemory.group_id == group_id)
                    .order_by(GroupPermanentMemory.id)
                )
            ).all()
            return [str(row.content) for row in rows]

    async def _assert_write_lock_available(self, *, timeout: float = 1.0) -> None:
        """用一个真实 session 做一次写事务，限时完成 ⇒ 写锁没被前一个事务扣住。"""

        async def _probe() -> None:
            async with self.session_factory() as session:
                await session.execute(
                    GroupPermanentMemory.__table__.delete().where(
                        GroupPermanentMemory.id == -1
                    )
                )
                await session.commit()

        await asyncio.wait_for(_probe(), timeout)


class _FaultFactory:
    """只给服务自己创建的 session 注入故障，不污染夹具自己的会话。"""

    def __init__(self, factory):
        self._factory = factory
        self.armed = False
        self.commit_mode = "ok"
        self.rollback_plan: tuple[str, ...] = ("ok",)
        self.close_plan: tuple[str, ...] = ("ok",)
        self.calls = {"commit": 0, "rollback": 0, "close": 0}
        self.raised: list[tuple[str, BaseException]] = []
        self.close_entered = asyncio.Event()
        self.close_gate = asyncio.Event()

    def __call__(self):
        session = self._factory()
        if self.armed:
            self._install(session)
        return session

    def _consume(self, name: str) -> str:
        plan = self.rollback_plan if name == "rollback" else self.close_plan
        mode = plan[0]
        if len(plan) > 1:
            if name == "rollback":
                self.rollback_plan = plan[1:]
            else:
                self.close_plan = plan[1:]
        return mode

    def _record(self, label: str, exc: BaseException) -> BaseException:
        self.raised.append((label, exc))
        return exc

    def _install(self, session) -> None:
        real_commit = session.commit
        real_rollback = session.rollback
        real_close = session.close

        async def commit():
            self.calls["commit"] += 1
            if self.commit_mode == "fail_before":
                raise self._record("commit_fail", RuntimeError("synthetic commit failure"))
            if self.commit_mode == "fail_after":
                await real_commit()
                raise self._record("commit_fail", RuntimeError("synthetic commit failure"))
            if self.commit_mode == "cancel_before":
                raise self._record("commit_cancel", asyncio.CancelledError("synthetic commit cancelled"))
            if self.commit_mode == "cancel_after":
                await real_commit()
                raise self._record("commit_cancel", asyncio.CancelledError("synthetic commit cancelled"))
            return await real_commit()

        async def rollback():
            self.calls["rollback"] += 1
            mode = self._consume("rollback")
            if mode == "ok":
                return await real_rollback()
            if mode == "fail_before":
                raise self._record("rollback_fail", RuntimeError("synthetic rollback failure"))
            if mode == "fail_after":
                await real_rollback()
                raise self._record("rollback_fail", RuntimeError("synthetic rollback failure"))
            if mode == "cancel_before":
                raise self._record("rollback_cancel", asyncio.CancelledError("synthetic rollback cancelled"))
            if mode == "cancel_after":
                await real_rollback()
                raise self._record("rollback_cancel", asyncio.CancelledError("synthetic rollback cancelled"))
            if mode == "fail":
                raise self._record("rollback_fail", RuntimeError("synthetic rollback failure"))
            raise AssertionError(f"未知 rollback 模式：{mode}")

        async def close():
            self.calls["close"] += 1
            mode = self._consume("close")
            if mode == "block":
                self.close_entered.set()
                await self.close_gate.wait()
                return await real_close()
            if mode == "ok":
                return await real_close()
            if mode == "fail_before":
                raise self._record("close_fail", RuntimeError("synthetic close failure"))
            if mode == "fail_after":
                await real_close()
                raise self._record("close_fail", RuntimeError("synthetic close failure"))
            if mode == "cancel_before":
                raise self._record("close_cancel", asyncio.CancelledError("synthetic close cancelled"))
            if mode == "cancel_after":
                await real_close()
                raise self._record("close_cancel", asyncio.CancelledError("synthetic close cancelled"))
            if mode == "fail":
                raise self._record("close_fail", RuntimeError("synthetic close failure"))
            raise AssertionError(f"未知 close 模式：{mode}")

        session.commit = commit
        session.rollback = rollback
        session.close = close

    def raised_for(self, label: str) -> BaseException:
        for name, exc in self.raised:
            if name == label:
                return exc
        raise AssertionError(f"没有记录到 {label}：{[name for name, _ in self.raised]}")


class ReplaceTruthfulnessTests(_ReplaceTestCase):
    """业务段：只用基线既有入口，冻结基线上必须出现 intended AssertionError。"""

    async def test_blank_new_content_never_deletes_the_existing_memory(self) -> None:
        """核心断言：新内容无效时旧记忆必须原封不动。"""

        memory_id = await self._seed("原始内容必须保留")

        _deleted, _created, _created_new = await self._service().replace_permanent_memory(
            GROUP_A, target=f"#{memory_id}", new_content="   "
        )

        row = await self._fresh(memory_id)
        self.assertIsNotNone(row, "新内容无效时不得删除既有永久记忆")
        self.assertEqual(row.content, "原始内容必须保留")
        self.assertEqual(await self._fresh_contents(), ["原始内容必须保留"])

    async def test_blank_new_content_does_not_create_a_replacement_row(self) -> None:
        memory_id = await self._seed("旧内容")

        _deleted, created, created_new = await self._service().replace_permanent_memory(
            GROUP_A, target=f"#{memory_id}", new_content=""
        )

        self.assertIsNone(created)
        self.assertFalse(created_new)
        self.assertEqual(await self._fresh_contents(), ["旧内容"])

    async def test_missing_target_leaves_every_row_untouched(self) -> None:
        first = await self._seed("甲")
        second = await self._seed("乙")

        deleted, created, created_new = await self._service().replace_permanent_memory(
            GROUP_A, target="#999999", new_content="新内容"
        )

        self.assertEqual(list(deleted), [])
        self.assertIsNone(created)
        self.assertFalse(created_new)
        self.assertEqual(await self._fresh_contents(), ["甲", "乙"])
        self.assertIsNotNone(await self._fresh(first))
        self.assertIsNotNone(await self._fresh(second))

    async def test_keyword_target_matching_nothing_leaves_every_row_untouched(self) -> None:
        await self._seed("完全无关的内容")

        deleted, created, _created_new = await self._service().replace_permanent_memory(
            GROUP_A, target="不存在的关键词", new_content="新内容"
        )

        self.assertEqual(list(deleted), [])
        self.assertIsNone(created)
        self.assertEqual(await self._fresh_contents(), ["完全无关的内容"])

    async def test_cross_group_numeric_target_does_not_touch_the_other_group(self) -> None:
        other_id = await self._seed("别的群的记忆", group_id=GROUP_B)

        deleted, created, _created_new = await self._service().replace_permanent_memory(
            GROUP_A, target=f"#{other_id}", new_content="新内容"
        )

        self.assertEqual(list(deleted), [])
        self.assertIsNone(created)
        self.assertEqual(await self._fresh_contents(group_id=GROUP_B), ["别的群的记忆"])
        self.assertEqual(await self._fresh_contents(group_id=GROUP_A), [])

    async def test_valid_new_content_creates_the_replacement_row(self) -> None:
        memory_id = await self._seed("旧内容")

        deleted, created, created_new = await self._service().replace_permanent_memory(
            GROUP_A, target=f"#{memory_id}", new_content="新内容"
        )

        self.assertEqual([int(row.id) for row in deleted], [memory_id])
        self.assertTrue(created_new)
        self.assertEqual(str(created.content), "新内容")
        self.assertEqual(await self._fresh_contents(), ["新内容"])

    async def test_valid_new_content_can_reuse_an_existing_identical_row(self) -> None:
        target_id = await self._seed("要被替换的")
        keeper_id = await self._seed("已经存在的同内容")

        deleted, created, created_new = await self._service().replace_permanent_memory(
            GROUP_A, target=f"#{target_id}", new_content="已经存在的同内容"
        )

        self.assertEqual([int(row.id) for row in deleted], [target_id])
        self.assertFalse(created_new)
        self.assertEqual(int(created.id), keeper_id)
        fresh_keeper = await self._fresh(keeper_id)
        # 复用既有行不得顺手改掉它的创建者或创建时间。
        self.assertEqual(int(fresh_keeper.created_by), ADMIN_ID)
        self.assertEqual(fresh_keeper.created_at, EARLY)
        self.assertEqual(await self._fresh_contents(), ["已经存在的同内容"])

    async def test_keyword_target_can_replace_several_rows_and_reports_them_all(self) -> None:
        first = await self._seed("共同前缀 一")
        second = await self._seed("共同前缀 二")
        third = await self._seed("无关")

        deleted, created, created_new = await self._service().replace_permanent_memory(
            GROUP_A, target="共同前缀", new_content="替换后的内容"
        )

        self.assertEqual(sorted(int(row.id) for row in deleted), sorted([first, second]))
        self.assertTrue(created_new)
        self.assertEqual(str(created.content), "替换后的内容")
        self.assertIsNotNone(await self._fresh(third))
        self.assertEqual(await self._fresh_contents(), ["无关", "替换后的内容"])

    async def test_empty_target_is_not_found_and_changes_nothing(self) -> None:
        await self._seed("保留我")

        deleted, created, _created_new = await self._service().replace_permanent_memory(
            GROUP_A, target="   ", new_content="新内容"
        )

        self.assertEqual(list(deleted), [])
        self.assertIsNone(created)
        self.assertEqual(await self._fresh_contents(), ["保留我"])

    async def test_legacy_failure_still_rolls_back_and_propagates(self) -> None:
        """既有契约（``tests/test_memory_service.py`` 已覆盖）在此保持：异常必须传播。"""

        memory_id = await self._seed("必须存活")

        class _BoomFactory:
            def __init__(self, inner):
                self._inner = inner

            def __call__(self):
                session = self._inner()

                async def flush():
                    raise RuntimeError("synthetic write failure")

                session.flush = flush
                return session

        service = self._service(factory=_BoomFactory(self.session_factory))
        with self.assertRaises(RuntimeError):
            await service.replace_permanent_memory(
                GROUP_A, target=f"#{memory_id}", new_content="新内容"
            )

        row = await self._fresh(memory_id)
        self.assertIsNotNone(row)
        self.assertEqual(row.content, "必须存活")


class ReplaceOutcomeProtocolTests(_ReplaceTestCase):
    """协议段：本批新增的终态契约、故障注入与技能文案。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        if PermanentMemoryReplaceOutcome is None or _REPLACE_OUTCOME_BINDING is None:
            self.fail(
                "本批新增的 replace 终态契约不可用："
                f"PermanentMemoryReplaceOutcome={PermanentMemoryReplaceOutcome!r} "
                f"binding={_REPLACE_OUTCOME_BINDING!r} error={_REPLACE_IMPORT_ERROR!r}"
            )

    def _outcome_factory(self, factory) -> _FaultFactory:
        proxy = _FaultFactory(factory)
        proxy.armed = True
        return proxy

    async def _replace_outcome(self, service, **kwargs):
        return await service.replace_permanent_memory_outcome(GROUP_A, **kwargs)

    async def test_outcome_reports_not_found_without_touching_rows(self) -> None:
        memory_id = await self._seed("保留")

        outcome = await self._replace_outcome(
            self._service(), target="#999999", new_content="新内容"
        )

        self.assertEqual(outcome.status, PM_REPLACE_NOT_FOUND)
        self.assertTrue(outcome.not_found)
        self.assertEqual(outcome.deleted_count, 0)
        self.assertIsNotNone(await self._fresh(memory_id))

    async def test_outcome_reports_invalid_new_content_without_touching_rows(self) -> None:
        memory_id = await self._seed("必须保留")

        outcome = await self._replace_outcome(
            self._service(), target=f"#{memory_id}", new_content="  \n  "
        )

        self.assertEqual(outcome.status, PM_REPLACE_INVALID_CONTENT)
        self.assertTrue(outcome.invalid_new_content)
        row = await self._fresh(memory_id)
        self.assertIsNotNone(row)
        self.assertEqual(row.content, "必须保留")

    async def test_outcome_reports_updated_with_real_materialised_fields(self) -> None:
        memory_id = await self._seed("旧")

        outcome = await self._replace_outcome(
            self._service(), target=f"#{memory_id}", new_content="新"
        )

        self.assertEqual(outcome.status, PM_REPLACE_UPDATED)
        self.assertTrue(outcome.updated)
        self.assertEqual(outcome.deleted_count, 1)
        self.assertIsInstance(outcome.memory_id, int)
        self.assertEqual(outcome.content, "新")
        self.assertTrue(outcome.created_new)
        self.assertTrue(outcome.resource_released)
        self.assertEqual(await self._fresh_contents(), ["新"])

    async def test_commit_failure_before_and_after_persisting_are_the_same_unconfirmed(self) -> None:
        """commit 没正常返回就是未确认，**不得**从结果反推成功或失败。"""

        before_id = await self._seed("提交前失败")
        before_proxy = self._outcome_factory(self.session_factory)
        before_proxy.commit_mode = "fail_before"
        before = await self._replace_outcome(
            self._service(factory=before_proxy), target=f"#{before_id}", new_content="新内容"
        )
        self.assertEqual(before.status, PM_REPLACE_UNCONFIRMED)
        self.assertTrue(before.unconfirmed)
        # 事务没提交 ⇒ 旧行仍在（真实值，但回执不据此承诺任何事）。
        self.assertIsNotNone(await self._fresh(before_id))
        await self._assert_write_lock_available()

        after_id = await self._seed("提交后失败")
        # SQLite 会复用「被删掉的最大 rowid」：先垫一条尾部哨兵，否则新行可能拿到与
        # 被替换行相同的 id，「旧行是否真的没了」就看不出来。
        await self._seed("尾部哨兵")
        after_proxy = self._outcome_factory(self.session_factory)
        after_proxy.commit_mode = "fail_after"
        after = await self._replace_outcome(
            self._service(factory=after_proxy), target=f"#{after_id}", new_content="新内容"
        )
        self.assertEqual(after.status, PM_REPLACE_UNCONFIRMED)
        self.assertEqual(after.status, before.status)
        # 这次其实已经落库了 —— 回执仍然是未确认，两者形状完全一致。
        self.assertIsNone(await self._fresh(after_id))
        # 提交前失败那条整条回滚仍在；提交后失败那条真的被替换了。
        self.assertEqual(
            await self._fresh_contents(), ["提交前失败", "尾部哨兵", "新内容"]
        )
        await self._assert_write_lock_available()

    async def test_original_cancellation_identity_survives_cleanup(self) -> None:
        memory_id = await self._seed("取消时保留")
        proxy = self._outcome_factory(self.session_factory)
        proxy.commit_mode = "cancel_before"
        proxy.rollback_plan = ("fail",)
        proxy.close_plan = ("fail",)
        original = None

        with self.assertRaises(BaseException) as caught:
            await self._replace_outcome(
                self._service(factory=proxy), target=f"#{memory_id}", new_content="新内容"
            )
        original = caught.exception
        self.assertIsInstance(original, asyncio.CancelledError)
        self.assertIs(original, proxy.raised_for("commit_cancel"))
        self.assertIsNotNone(await self._fresh(memory_id))

    async def test_recoverable_cleanup_exception_keeps_the_confirmed_result(self) -> None:
        """commit 正常返回后 close 首次失败、第二次成功：真值仍确认，资源已归还。"""

        memory_id = await self._seed("可恢复清理异常")
        await self._seed("尾部哨兵")
        proxy = self._outcome_factory(self.session_factory)
        proxy.close_plan = ("fail_before", "ok")

        outcome = await self._replace_outcome(
            self._service(factory=proxy), target=f"#{memory_id}", new_content="新内容"
        )

        self.assertEqual(outcome.status, PM_REPLACE_UPDATED)
        self.assertTrue(outcome.updated)
        self.assertTrue(outcome.resource_released)
        # 第一次 close 失败不得放弃后面仍可成功的 close。
        self.assertGreaterEqual(proxy.calls["close"], 2)
        self.assertIsNone(await self._fresh(memory_id))
        self.assertEqual(await self._fresh_contents(), ["尾部哨兵", "新内容"])
        await self._assert_write_lock_available()

    async def test_recoverable_cleanup_cancellation_is_propagated_after_releasing(self) -> None:
        """没有原始取消时，cleanup 新发生的取消必须原样传播，不得被吞成普通结果。"""

        memory_id = await self._seed("可恢复清理取消")
        await self._seed("尾部哨兵")
        proxy = self._outcome_factory(self.session_factory)
        proxy.close_plan = ("cancel_before", "ok")

        with self.assertRaises(BaseException) as caught:
            await self._replace_outcome(
                self._service(factory=proxy), target=f"#{memory_id}", new_content="新内容"
            )

        self.assertIsInstance(caught.exception, asyncio.CancelledError)
        self.assertIs(caught.exception, proxy.raised_for("close_cancel"))
        self.assertGreaterEqual(proxy.calls["close"], 2)
        # 取消不改写已经确认的持久化真值，也不留持锁 session。
        self.assertIsNone(await self._fresh(memory_id))
        await self._assert_write_lock_available()

    async def test_permanent_recovery_failure_is_disclosed_not_pretended_safe(self) -> None:
        memory_id = await self._seed("永久失败")
        proxy = self._outcome_factory(self.session_factory)
        proxy.commit_mode = "fail_before"
        proxy.rollback_plan = ("fail",)
        proxy.close_plan = ("fail",)

        with self.assertLogs("bot.services.memory", level="WARNING") as captured:
            outcome = await self._replace_outcome(
                self._service(factory=proxy), target=f"#{memory_id}", new_content="新内容"
            )

        self.assertEqual(outcome.status, PM_REPLACE_UNCONFIRMED)
        self.assertFalse(outcome.resource_released)
        self.assertTrue(
            any("permanent memory replace cleanup unreleased" in line for line in captured.output),
            captured.output,
        )
        self.assertIsNotNone(await self._fresh(memory_id))

    async def test_real_task_cancellation_at_the_cleanup_boundary(self) -> None:
        memory_id = await self._seed("真实取消")
        await self._seed("尾部哨兵")
        proxy = self._outcome_factory(self.session_factory)
        proxy.commit_mode = "ok"
        proxy.close_plan = ("block", "ok")

        task = asyncio.ensure_future(
            self._service(factory=proxy).replace_permanent_memory_outcome(
                GROUP_A, target=f"#{memory_id}", new_content="新内容"
            )
        )
        await asyncio.wait_for(proxy.close_entered.wait(), 1.5)
        task.cancel()
        with self.assertRaises(BaseException) as caught:
            await task
        self.assertIsInstance(caught.exception, asyncio.CancelledError)
        # teardown 之前必须已经归还资源并读完真实行。
        proxy.close_gate.set()
        await self._assert_write_lock_available()
        self.assertIsNone(await self._fresh(memory_id))

    async def test_skill_copy_matches_each_terminal_state(self) -> None:
        cases = [
            (PM_REPLACE_NOT_FOUND, "没找到要修改的永久记忆", "未做任何改动"),
            (PM_REPLACE_INVALID_CONTENT, "新内容为空或无效", "未做任何改动"),
            (PM_REPLACE_UNCONFIRMED, "未能确认这次永久记忆修改是否已生效", "重新查看列表"),
        ]

        for status, first, second in cases:
            with self.subTest(status=status):
                stub = SimpleNamespace(
                    replace_permanent_memory_outcome=AsyncMock(
                        return_value=PermanentMemoryReplaceOutcome(status=status)
                    )
                )
                result = await self._run_skill_replace(stub)
                self.assertTrue(result.ok)
                self.assertIn(first, result.summary)
                self.assertIn(second, result.summary)
                self.assertNotIn("修改失败", result.summary)

        updated = SimpleNamespace(
            replace_permanent_memory_outcome=AsyncMock(
                return_value=PermanentMemoryReplaceOutcome(
                    status=PM_REPLACE_UPDATED,
                    deleted_count=2,
                    memory_id=17,
                    content="新内容",
                    created_new=True,
                )
            )
        )
        result = await self._run_skill_replace(updated)
        self.assertIn("已更新", result.summary)
        self.assertIn("删除数量</b>: 2", result.summary)
        self.assertIn("#17", result.summary)
        # 死分支文案不得复活。
        self.assertNotIn("未命中旧记忆，已新增", result.summary)

    async def _run_skill_replace(self, stub_service):
        from bot.services.manage_intent import GroupIntent
        from bot.services.skills import memory_manage as skill_module
        from bot.services.skills.base import SkillContext

        skill = skill_module.MemoryManageSkill()
        context = SkillContext(
            llm=object(),
            chat_id=GROUP_A,
            sender_is_tg_admin=True,
            sender_user_id=ADMIN_ID,
            current_user_text="把 #1 改成 新内容",
        )
        intent = GroupIntent(
            intent="memory_manage",
            memory_action="replace",
            memory_target="#1",
            memory_content="新内容",
        )
        with (
            patch.object(
                skill_module.GroupIntentService, "detect", new=AsyncMock(return_value=intent)
            ),
            patch.object(
                skill_module,
                "memory_holder",
                new=SimpleNamespace(get=lambda: stub_service),
            ),
        ):
            return await skill.run({}, context)


if __name__ == "__main__":
    unittest.main()
