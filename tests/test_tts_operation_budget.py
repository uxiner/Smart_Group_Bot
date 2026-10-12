"""直接 TTS 发送入口的功能操作额度（正式回归）。

覆盖 ``bot/handlers/group.py`` 与 ``bot/services/proactive.py`` 直接调用的两个
sender：真实 ``Settings``（edge 语音）+ 真实 ``DoubaoTTSService`` + 真实 DB /
共享预算 service，**只**把最外层供应商合成与 TG 发送换成替身。

要点：

* 预检（available / empty / too_many_segments）之后、首个供应商合成之前扣一次；
  一次**逻辑**发送只扣一次，不对每个 segment 扣；
* 私聊正 chat_id 在继承了群 scope 的情况下也不扣（不能拿私聊去扣那个群的钱）；
* 显式负 chat_id 就是群，即使任务没有 scope 也照扣；
* 失败/取消在供应商入口之后仍算一次，不退款；
* 中央技能链（真实 ``DoubaoTTSSkill`` → 真实 sender）只扣一次；
* 替身按**用例生命周期**装一次（``asyncSetUp`` 装、``asyncTearDown`` 摘），并发
  调用共享同一组绑定 —— 曾经每个协程各自 patch 同一个实例，先结束的协程会把真实
  供应商入口写回去（v76）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from bot.config import Settings
from bot.db.engine import init_db
from bot.db.models import GroupBudgetDay
from bot.services import group_budget, pricing
from bot.services import usage_scope as scopes
from bot.services.doubao_tts import DoubaoTTSService

GROUP_A = -1001234567890
GROUP_B = -1005550001111
PRIVATE = 556677


def _tts_settings(database_url: str) -> Settings:
    """真实 Settings，edge 语音 + mp3（合成与 TG 发送在测试里被替换）。"""

    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=database_url,
        doubao_tts_enabled=True,
        doubao_tts_provider="edge",
        doubao_tts_speaker="zh-CN-XiaoxiaoNeural",
        doubao_tts_audio_format="mp3",
    )


class TtsOperationBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        self.catalog = pricing.PriceCatalog(
            version=1,
            entries=(
                pricing.PriceEntry(
                    provider_key="slot-a", model="m", currency="USD",
                    input_per_million="3", output_per_million="15",
                    cache_read_per_million="1", cache_write_per_million="4",
                ),
            ),
        )
        self.budget = group_budget.shared_service(
            self.session_factory, lambda: self.catalog
        )
        self.budget.invalidate()
        self.tts = DoubaoTTSService(_tts_settings(
            f"sqlite+aiosqlite:///{self._db_path}"
        ))
        self.synth_calls = 0
        self.tg_calls = 0
        self.fail_synth = False
        # v76：替身在**用例生命周期**内只装一次。曾经每个协程各自
        # ``with self._stack()`` patch 同一个 ``self.tts``：并发时先进入的协程先
        # 结束，就把自己进入时看到的**真实**方法写回实例，仍在运行的协程随后一次
        # ``self.synthesize`` 查找就落到真实 edge 供应商（父代理在 network=none 的
        # Linux 容器里实测到 ``edge_tts_error`` + ``1 != 2``）。patch 嵌套并不是
        # LIFO 结束的，所以"每组协程自己 patch"这个形态本身就是竞态。
        self.patched_names: list[str] = []
        self.synth_gates: tuple[asyncio.Event, ...] = ()
        self.synth_parked: set[int] = set()
        self.synth_parked_all = asyncio.Event()
        # 恢复证明：cleanup 在 asyncTearDown 之后跑（LIFO），那时替身已经摘掉。
        self.addCleanup(self._assert_original_binding_restored)
        self.doubles = self._stack()
        self.doubles.__enter__()
        self.shared_synth = self.tts.synthesize

    def _close_doubles(self) -> None:
        stack = getattr(self, "doubles", None)
        if stack is not None:
            self.doubles = None
            stack.close()

    def _assert_original_binding_restored(self) -> None:
        """用例结束后实例上不得留任何替身：原方法 binding 必须已经恢复。"""

        self.assertIn(
            "synthesize", self.patched_names,
            "本用例根本没装供应商替身，这条恢复断言会是空绿",
        )
        leftovers = sorted(
            name for name in getattr(self, "patched_names", []) if name in vars(self.tts)
        )
        self.assertEqual(
            leftovers, [], f"用例结束后实例上仍留着替身：{leftovers}"
        )
        self.assertIs(
            self.tts.synthesize.__func__, DoubaoTTSService.synthesize,
            "原方法 binding 没有恢复",
        )

    async def asyncTearDown(self) -> None:
        self._close_doubles()
        group_budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._db_path + suffix)
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _stack(self) -> ExitStack:
        stack = ExitStack()
        service = self.tts

        async def fake_synthesize(*args, **kwargs):
            self.synth_calls += 1
            index = self.synth_calls
            # 可选的确定性闸门：按第几次合成调用挂住，供并发/乱序用例用事件同步。
            if index <= len(self.synth_gates):
                self.synth_parked.add(index)
                if len(self.synth_parked) >= len(self.synth_gates):
                    self.synth_parked_all.set()
                await self.synth_gates[index - 1].wait()
            if self.fail_synth:
                return SimpleNamespace(
                    ok=False, audio_bytes=b"", payload=b"", error="synth_failed"
                )
            return SimpleNamespace(
                ok=True, audio_bytes=b"audio", payload=b"audio", error=""
            )

        async def fake_send(*args, **kwargs):
            self.tg_calls += 1
            return SimpleNamespace(message_id=self.tg_calls)

        for name in (
            "synthesize",
            "synthesize_voice_payload",
            "synthesize_audio_payload",
        ):
            if hasattr(service, name):
                stack.enter_context(patch.object(service, name, fake_synthesize))
                self.patched_names.append(name)
        for name in ("_send_voice_message", "_send_audio_message", "send_voice",
                     "_send_audio", "_deliver_audio"):
            if hasattr(service, name):
                stack.enter_context(patch.object(service, name, fake_send))
                self.patched_names.append(name)
        return stack

    async def _save(self, *, enabled=True, operation_limits=None, **kw):
        await self.budget.save_budget(
            GROUP_A, expected_revision=kw.pop("revision", 0), enabled=enabled,
            currency="USD", operation_limits=operation_limits, **kw
        )
        self.budget.invalidate()

    async def _tts_count(self, group_id=GROUP_A) -> int:
        async with self.session_factory() as session:
            row = (
                await session.scalars(
                    select(GroupBudgetDay).where(GroupBudgetDay.group_id == group_id)
                )
            ).first()
        return int(((row.used_operations or {}) if row else {}).get("tts", 0) or 0)

    def _message(self, chat_id: int):
        async def _send(*args, **kwargs):
            self.tg_calls += 1
            return SimpleNamespace(message_id=self.tg_calls)

        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_id=1,
            answer_audio=_send, answer_voice=_send, answer=_send,
            reply_audio=_send, reply_voice=_send, reply=_send,
        )

    def _bot(self):
        async def _send(*args, **kwargs):
            self.tg_calls += 1
            return SimpleNamespace(message_id=self.tg_calls)

        return SimpleNamespace(
            send_audio=_send, send_voice=_send, send_message=_send,
        )

    async def _send_via_message(self, chat_id: int, text: str = "你好"):
        # 替身由用例生命周期持有（asyncSetUp 装、asyncTearDown 摘）：并发调用共享
        # 同一组绑定，任何协程结束都不会把真实供应商入口写回来。
        return await self.tts.send_message_tts_result(
            self._message(chat_id), text
        )

    async def _send_via_chat(self, chat_id: int, text: str = "你好"):
        return await self.tts.send_chat_tts_result(
            self._bot(), chat_id, text
        )

    # ------------------------------------------------------------------
    async def test_zero_limit_denies_both_senders_without_calling_out(self) -> None:
        await self._save(operation_limits={"tts": 0})
        first = await self._send_via_message(GROUP_A)
        self.assertEqual(first.error, "operation_budget_denied", first)
        second = await self._send_via_chat(GROUP_A)
        self.assertEqual(second.error, "operation_budget_denied", second)
        self.assertEqual(self.synth_calls, 0, "被拒的发送仍调用了供应商")
        self.assertEqual(self.tg_calls, 0, "被拒的发送仍发了 TG 消息")
        self.assertEqual(await self._tts_count(), 0)

    async def test_cap_one_first_succeeds_second_denied(self) -> None:
        await self._save(operation_limits={"tts": 1})
        first = await self._send_via_message(GROUP_A)
        self.assertFalse(first.error, first)
        self.assertGreaterEqual(self.synth_calls, 1)
        second = await self._send_via_chat(GROUP_A)
        self.assertEqual(second.error, "operation_budget_denied", second)
        self.assertEqual(await self._tts_count(), 1, "day.tts 不是严格 1")

    async def test_disabled_or_unconfigured_keeps_tts_usable(self) -> None:
        await self._save(enabled=False, operation_limits={"tts": 0})
        result = await self._send_via_chat(GROUP_A)
        self.assertFalse(result.error, result)
        self.assertEqual(await self._tts_count(), 0)

        # 真正未配置的**群**（GROUP_B 没有任何预算行）而不是私聊。
        unconfigured = await self._send_via_chat(GROUP_B)
        self.assertFalse(unconfigured.error, unconfigured)
        self.assertEqual(await self._tts_count(GROUP_B), 0, "未配置的群被计费了")

    async def test_private_chat_never_charges_an_inherited_group_scope(self) -> None:
        await self._save(operation_limits={"tts": 0})
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
        ):
            result = await self._send_via_message(PRIVATE)
        self.assertFalse(
            result.error, f"私聊正 chat_id 被套上了继承来的群额度：{result.error}"
        )
        self.assertEqual(await self._tts_count(GROUP_A), 0)

    async def test_explicit_group_without_scope_still_charges_that_group(self) -> None:
        await self._save(operation_limits={"tts": 0})
        # 没有 usage_scope：显式负 chat_id 仍必须按那个群扣。
        result = await self._send_via_chat(GROUP_A)
        self.assertEqual(result.error, "operation_budget_denied", result)

    async def test_different_explicit_groups_do_not_cross_charge(self) -> None:
        await self._save(operation_limits={"tts": 0})
        await self.budget.save_budget(
            GROUP_B, expected_revision=0, enabled=True, currency="USD",
            operation_limits={"tts": 1},
        )
        self.budget.invalidate()
        denied = await self._send_via_chat(GROUP_A)
        self.assertEqual(denied.error, "operation_budget_denied")
        allowed = await self._send_via_chat(GROUP_B)
        self.assertFalse(allowed.error, allowed)
        self.assertEqual(await self._tts_count(GROUP_A), 0)
        self.assertEqual(await self._tts_count(GROUP_B), 1)

    async def test_two_segments_cost_one_operation(self) -> None:
        await self._save(operation_limits={"tts": 2})
        with patch.object(
            self.tts, "split_text", lambda text: ("第一段", "第二段")
        ):
            result = await self.tts.send_chat_tts_result(
                self._bot(), GROUP_A, "第一段第二段"
            )
        self.assertFalse(result.error, result)
        self.assertEqual(
            await self._tts_count(), 1, "按 segment 扣费了（应当按逻辑发送扣一次）"
        )

    async def test_admitted_send_that_fails_still_counts_once(self) -> None:
        await self._save(operation_limits={"tts": 1})
        self.fail_synth = True
        result = await self._send_via_chat(GROUP_A)
        # 精确错误码：空字符串也会让 assertIsNotNone 通过，那是假绿。
        self.assertEqual(result.error, "synth_failed", result)
        self.assertEqual(self.synth_calls, 1, "供应商调用次数不对")
        self.assertEqual(self.tg_calls, 0, "合成都失败了还发了 TG")
        self.assertEqual(await self._tts_count(), 1, "已准入的失败调用被退款了")

    async def test_concurrent_cap_two_admits_exactly_two(self) -> None:
        """六个真实 sender 并发抢 cap2：两个成功、四个**明确**额度拒绝。

        v76：整段 gather 共享同一组替身（用例生命周期持有），所以"两个成功"
        与"供应商两次"必须同时成立 —— 不再有哪个协程结束时把真实供应商入口
        写回来。
        """

        await self._save(operation_limits={"tts": 2})
        results = await asyncio.gather(
            *(self._send_via_chat(GROUP_A) for _ in range(6)),
            return_exceptions=True,
        )
        self.assertEqual(
            [repr(x) for x in results if isinstance(x, BaseException)], [],
            f"并发发送抛了异常：{results!r}",
        )
        ok = [r for r in results if not r.error]
        denied = [r for r in results if r.error]
        self.assertEqual(len(ok), 2, [r.error for r in results])
        self.assertEqual(len(denied), 4)
        for item in denied:
            self.assertEqual(item.error, "operation_budget_denied", item.error)
        # 成功的两条必须**真的**走完合成 + TG 各一次（不能有一条靠真实供应商兜着）
        self.assertEqual(self.synth_calls, 2, self.synth_calls)
        self.assertEqual(self.tg_calls, 2, self.tg_calls)
        self.assertEqual(await self._tts_count(), 2, "落库额度不是严格 2")
        # gather 结束之后，这一组共享替身仍然原样在实例上（没有被谁提前摘掉）；
        # 用例结束之后"原方法 binding 恢复"由 _assert_original_binding_restored
        # 这个 cleanup 证明。
        self.assertIs(self.tts.synthesize, self.shared_synth, "共享替身被提前摘掉了")

    async def test_overlapping_senders_never_restore_the_real_supplier_entry(
        self,
    ) -> None:
        """事件同步的确定性重叠 + 乱序结束：绑定绝不回到真实供应商入口。

        v76 夹具回归。旧形态在每个协程内部各自 ``with self._stack()`` patch 同一个
        ``self.tts``，而这些 patch 并不是按 LIFO 结束的：先进入的协程先结束时，会
        把它进入时看到的**真实**方法写回实例，仍在运行的协程随后一次
        ``self.synthesize`` 属性查找就落到真实 edge 供应商 —— 父代理在
        ``network=none`` 的 Linux 容器里实测到的 ``edge_tts_error`` 与 ``1 != 2``
        就是这条路径。

        这里用两个事件把两次合成**同时**卡在替身内部，再让后进入的那次先放行、
        先结束（乱序），逐点断言实例绑定始终是**同一个**共享替身对象，而不是
        真实入口、也不是另一个替身。
        """

        real_entry = DoubaoTTSService.synthesize
        shared = self.shared_synth
        self.assertIsNot(shared, real_entry, "本用例的共享替身根本不在实例上")
        self.assertIs(
            vars(DoubaoTTSService)["synthesize"], real_entry,
            "类上的真实入口本身被改过（替身必须只挂在实例上）",
        )

        await self._save(operation_limits={"tts": 2})
        gates = (asyncio.Event(), asyncio.Event())
        self.synth_gates = gates
        self.synth_parked = set()
        self.synth_parked_all = asyncio.Event()
        bindings: list = []

        async def send():
            result = await self._send_via_chat(GROUP_A)
            # 每次发送结束后再看一眼此刻实例上的绑定。
            bindings.append((result.error, self.tts.synthesize))
            return result

        first = asyncio.create_task(send(), name="first")
        second = asyncio.create_task(send(), name="second")
        try:
            await asyncio.wait_for(self.synth_parked_all.wait(), 10)
            # 两次合成重叠：两边都还没返回。
            self.assertEqual(self.synth_parked, {1, 2}, self.synth_parked)
            self.assertIs(self.tts.synthesize, shared, "重叠期间绑定被改掉了")
            # 乱序结束：第 2 次调用（后进入的那次）先放行、先结束。
            gates[1].set()
            await asyncio.wait_for(asyncio.shield(second), 10)
            self.assertIs(
                self.tts.synthesize, shared,
                "先进入的协程还在跑，实例绑定却已经不是那组共享替身了",
            )
            self.assertIsNot(self.tts.synthesize, real_entry)
            gates[0].set()
            await asyncio.wait_for(asyncio.gather(first, second), 10)
        finally:
            for gate in gates:
                gate.set()
            await asyncio.gather(first, second, return_exceptions=True)

        self.assertEqual(len(bindings), 2, bindings)
        for error, binding in bindings:
            self.assertEqual(error, "", (error, binding))
            self.assertIs(
                binding, shared, "这个协程看到的绑定不是那组共享替身"
            )
        self.assertIs(
            self.tts.synthesize, shared,
            "gather 结束后共享替身不在了（每个协程各自 patch 才会这样）",
        )
        self.assertIsNot(self.tts.synthesize, real_entry)
        self.assertEqual(self.synth_calls, 2, self.synth_calls)
        self.assertEqual(self.tg_calls, 2, self.tg_calls)
        self.assertEqual(await self._tts_count(), 2)

    async def test_storage_failure_fails_closed_for_groups_but_not_private(self) -> None:
        await self._save(operation_limits={"tts": 5})

        def _boom(*args, **kwargs):
            raise RuntimeError("budget storage exploded")

        with patch.object(self.budget, "consume_operation", _boom):
            broken = await self._send_via_chat(GROUP_A)
            self.assertEqual(
                broken.error, "operation_budget_unavailable", broken
            )
            self.assertEqual(self.synth_calls, 0)
            private = await self._send_via_message(PRIVATE)
        self.assertFalse(private.error, f"私聊不该被群预算故障拖下水：{private.error}")


    async def test_real_skill_chain_charges_exactly_once(self) -> None:
        """真实 DoubaoTTSSkill -> 真实 SkillService -> 真实 sender：只扣一次。

        不用 OuterSkill 证明底层去重 —— 那只能证明替身被调用，证不了
        "中央准入 + 底层直发"两层只扣一次。
        """

        from bot.services.skills.base import SkillContext
        from bot.services.skills.doubao_tts import DoubaoTTSSkill
        from bot.services.skills.service import SkillService

        await self._save(operation_limits={"tts": 1})
        # 真实技能实现：把它注册进真实 SkillService 的 skills 表。
        skill = DoubaoTTSSkill(self.tts)
        skills = {"doubao_tts": skill}
        skill_service = SkillService(llm=None)

        context = SkillContext(
            chat_id=GROUP_A, session_factory=self.session_factory,
            bot=self._bot(),
        )
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
        ):
            first = await skill_service._run_tool(
                name="doubao_tts",
                arguments={"text": "你好"},
                context=context, skills=skills,
            )
        self.assertTrue(first.ok, (first.error, first.summary))
        self.assertEqual(
            await self._tts_count(), 1,
            "中央技能链把同一次发送扣了不止一次",
        )
        self.assertGreaterEqual(self.synth_calls, 1)

        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
        ):
            second = await skill_service._run_tool(
                name="doubao_tts",
                arguments={"text": "你好"},
                context=context, skills=skills,
            )
        self.assertFalse(second.ok, second)
        self.assertEqual(second.error, "operation_budget_denied", second.error)
        self.assertEqual(await self._tts_count(), 1, "第二次也被扣了")


    async def test_unassembled_budget_service_denies_group_tts(self) -> None:
        """共享预算服务**没装配** = 预算装配没走完 → 群语音必须 fail-closed。

        不能因为"看不到服务"就 fail-open：那正是绕过。
        """

        group_budget.reset_shared_service()
        denied = await self._send_via_chat(GROUP_A)
        self.assertEqual(denied.error, "operation_budget_unavailable", denied)
        also = await self._send_via_message(GROUP_A)
        self.assertEqual(also.error, "operation_budget_unavailable", also)
        self.assertEqual(self.synth_calls, 0, "未装配却调用了供应商")
        self.assertEqual(self.tg_calls, 0)
        # 私聊不套群额度，不该被群预算缺失拖下水：**确实**走了外部一遍。
        private = await self._send_via_message(PRIVATE)
        self.assertFalse(private.error, private)
        self.assertEqual(self.synth_calls, 1, "私聊正控没有真的调用供应商")
        self.assertEqual(self.tg_calls, 1, "私聊正控没有真的发 TG")

    async def test_central_safety_control_runs_without_budget_service(self) -> None:
        """未装配预算护栏时，未映射的安全技能仍能在中央真实跑通。"""

        from bot.services.skills.base import SkillContext, SkillRunResult
        from bot.services.skills.service import SkillService

        class _SafetySkill:
            name = "vote_ban"

            async def run(self, arguments, context):  # noqa: ANN001
                self.calls = getattr(self, "calls", 0) + 1
                return SkillRunResult(ok=True, skill=self.name, summary="ok")

        group_budget.reset_shared_service()
        service = SkillService(llm=None)
        skill = _SafetySkill()
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
        ):
            result = await service._run_tool(
                name="vote_ban", arguments={},
                context=SkillContext(
                    chat_id=GROUP_A, session_factory=self.session_factory
                ),
                skills={"vote_ban": skill},
            )
        self.assertTrue(result.ok, (result.error, result.summary))
        self.assertEqual(getattr(skill, "calls", 0), 1)

    async def test_same_task_second_sender_still_charges(self) -> None:
        """**同一次** ``_run_tool`` 里同任务连发两次：第一次完整送达，第二次精确拒绝。

        之前这条调用两次 ``_run_tool``，第二次在**中央**就被拒了 —— 根本没测到
        "中央预扣一次之后，同任务里第二次走底层 sender"这条路径。
        """

        from bot.services.skills.base import SkillContext, SkillRunResult
        from bot.services.skills.service import SkillService

        await self._save(operation_limits={"tts": 1})
        tts = self.tts
        seen: list = []

        class SameTaskTwiceSkill:
            name = "doubao_tts"

            async def run(self, arguments, context):  # noqa: ANN001
                # 同一个任务（没有 create_task），连续两次真实 sender。
                first = await tts.send_chat_tts_result(
                    context.bot, int(context.chat_id), "你好"
                )
                second = await tts.send_chat_tts_result(
                    context.bot, int(context.chat_id), "你好"
                )
                seen.extend([first, second])
                return SkillRunResult(
                    ok=not first.error, skill=self.name,
                    summary=f"first={first.error!r} second={second.error!r}",
                )

        skill_service = SkillService(llm=None)
        context = SkillContext(
            chat_id=GROUP_A, session_factory=self.session_factory, bot=self._bot()
        )
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
        ):
            result = await skill_service._run_tool(
                name="doubao_tts", arguments={"text": "你好"},
                context=context, skills={"doubao_tts": SameTaskTwiceSkill()},
            )
        self.assertEqual(len(seen), 2, seen)
        self.assertFalse(seen[0].error, seen[0])
        self.assertTrue(seen[0].any_sent, seen[0])
        self.assertEqual(
            seen[1].error, "operation_budget_denied",
            f"同任务第二次发送白嫖了凭证：{seen[1]}",
        )
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(self.synth_calls, 1, self.synth_calls)
        self.assertEqual(self.tg_calls, 1, self.tg_calls)
        self.assertEqual(await self._tts_count(), 1)

    async def test_child_tasks_cannot_copy_the_admission_credit(self) -> None:
        """子任务上下文会复制凭证 —— 但 owner 不匹配，领不走。

        本臂**没有**中央预扣（只建立凭证），所以两个 child 都领不到父任务的
        凭证、各自去扣 cap=1：恰好一个成功、一个拒。真正被证明的是"子任务拿不到
        父任务那份凭证"——gather 结束后父任务自己仍能领走它。
        """

        from bot.services import operation_budget

        await self._save(operation_limits={"tts": 1})
        results: list = []

        async def child():
            results.append(await self._send_via_chat(GROUP_A))

        with operation_budget.admitted(GROUP_A):
            await asyncio.gather(*[asyncio.create_task(child()) for _ in range(2)])
            # 父任务（owner）此刻仍持有那份没被领走的凭证。
            self.assertTrue(
                operation_budget.claim_credit(GROUP_A),
                "子任务把父任务的凭证领走了",
            )
        errors = sorted(str(r.error) for r in results)
        self.assertEqual(
            errors, ["", "operation_budget_denied"],
            f"子任务白嫖了凭证：{errors}",
        )
        self.assertEqual(self.synth_calls, 1, self.synth_calls)
        self.assertEqual(self.tg_calls, 1, self.tg_calls)
        self.assertEqual(await self._tts_count(), 1)

    async def test_cap_two_with_child_tasks_admits_only_one_more(self) -> None:
        await self._save(operation_limits={"tts": 2})
        from bot.services import operation_budget

        results: list = []

        async def child():
            results.append(await self._send_via_chat(GROUP_A))

        # 中央先真的扣掉 1 次（真实 _run_tool 就是这么做的），再建立凭证。
        self.assertIsNone(await operation_budget.consume_tts(GROUP_A))
        with operation_budget.admitted(GROUP_A):
            await asyncio.gather(
                *[asyncio.create_task(child()) for _ in range(3)]
            )
        ok = [r for r in results if not r.error]
        denied = [r for r in results if r.error]
        self.assertEqual(len(ok), 1, [r.error for r in results])
        for item in denied:
            self.assertEqual(item.error, "operation_budget_denied", item.error)
        self.assertEqual(self.synth_calls, 1, self.synth_calls)
        self.assertEqual(await self._tts_count(), 2)

    async def test_credit_is_scoped_to_one_group_and_expires(self) -> None:
        from bot.services import operation_budget

        await self._save(operation_limits={"tts": 0})
        await self.budget.save_budget(
            GROUP_B, expected_revision=0, enabled=True, currency="USD",
            operation_limits={"tts": 1},
        )
        self.budget.invalidate()
        with operation_budget.admitted(GROUP_A):
            # 凭证是 GROUP_A 的，不能拿去免 GROUP_B 的钱。
            crossed = await self._send_via_chat(GROUP_B)
            self.assertFalse(crossed.error, crossed)
            self.assertEqual(await self._tts_count(GROUP_B), 1)
        # 退出之后 **GROUP_A 自己那份凭证也过期了**：cap=0 必须拒，且不能新增
        # 供应商调用。（只验 GROUP_B 的话根本没碰到原凭证的过期。）
        supplier_before = self.synth_calls
        expired = await self._send_via_chat(GROUP_A)
        self.assertEqual(expired.error, "operation_budget_denied", expired)
        self.assertEqual(
            self.synth_calls, supplier_before, "过期凭证又被用来发了语音"
        )
        self.assertEqual(await self._tts_count(GROUP_A), 0)
        # 控制组：GROUP_B 的额度确实被那次跨群调用用完，不是整体坏掉。
        again = await self._send_via_chat(GROUP_B)
        self.assertEqual(again.error, "operation_budget_denied", again)

    async def _run_fork_skill(self, *, children: int) -> tuple:
        """真实 worker 里 fork ``children`` 个真实 sender；**不吞异常**。"""

        from bot.services.skills.base import SkillContext, SkillRunResult
        from bot.services.skills.service import SkillService

        tts = self.tts
        out: list = []

        class ForkSkill:
            name = "doubao_tts"

            async def run(self, arguments, context):  # noqa: ANN001
                async def one():
                    return await tts.send_chat_tts_result(
                        context.bot, int(context.chat_id), "你好"
                    )

                gathered = await asyncio.gather(
                    *[asyncio.create_task(one()) for _ in range(children)]
                )
                out.extend(gathered)
                return SkillRunResult(
                    ok=any(not item.error for item in gathered),
                    skill=self.name,
                    summary=str([item.error for item in gathered]),
                )

        skill_service = SkillService(llm=None)
        context = SkillContext(
            chat_id=GROUP_A, session_factory=self.session_factory, bot=self._bot()
        )
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
        ):
            result = await skill_service._run_tool(
                name="doubao_tts", arguments={"text": "你好"},
                context=context, skills={"doubao_tts": ForkSkill()},
            )
        return result, out

    async def test_fork_skill_children_are_charged_conservatively(self) -> None:
        """真实 worker fork 两个真实 sender，cap1：都拒，一条外部调用都没有。"""

        await self._save(operation_limits={"tts": 1})
        result, out = await self._run_fork_skill(children=2)
        self.assertEqual(len(out), 2, out)
        for item in out:
            # 逐个断言：不过滤异常 —— 子任务真抛错就是测试失败。
            self.assertIsNotNone(item, item)
            self.assertEqual(
                item.error, "operation_budget_denied",
                f"子任务白嫖了凭证：{item}",
            )
        self.assertFalse(result.ok, result.summary)
        self.assertEqual(self.synth_calls, 0, self.synth_calls)
        self.assertEqual(self.tg_calls, 0, self.tg_calls)
        self.assertEqual(await self._tts_count(), 1, "一次准入被复制成多次发送")

    async def test_fork_skill_children_cap_two_allow_at_most_one_more(self) -> None:
        """cap2 + 真实 worker 子任务：最多再放行一条，day 严格 2。"""

        await self._save(operation_limits={"tts": 2})
        result, out = await self._run_fork_skill(children=3)
        self.assertEqual(len(out), 3, out)
        ok = [item for item in out if not item.error]
        denied = [item for item in out if item.error]
        self.assertEqual(len(ok), 1, [item.error for item in out])
        for item in denied:
            self.assertEqual(item.error, "operation_budget_denied", item)
        self.assertEqual(self.synth_calls, 1, self.synth_calls)
        self.assertEqual(self.tg_calls, 1, self.tg_calls)
        self.assertEqual(await self._tts_count(), 2)
        self.assertTrue(result.ok, result.summary)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
