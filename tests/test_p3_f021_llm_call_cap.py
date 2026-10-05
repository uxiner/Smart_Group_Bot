"""P3-3 / F-021：审核模型调用要有**成本硬上限**，超限时安全降级（不是静默放行）。

复现的原缺陷
------------
上一轮加的 ``ModerationAdmissionGate`` 是"整形闸"：按 ``(群, 成员)`` 把连发的
审核**推迟**到时间轴上，但**不限量**。一个成员发 N 条 = N 次模型调用，只是被摊开；
过载时整形等于失效，成本放大面（``成员发 N 条 → N 次调用``）原封不动。

修复
----
新增 ``ModerationCallBudget``（``bot/services/moderation_throttle.py``）：每群固定
窗口（默认 1 小时）最多 N 次审核模型调用，**上限来自配置
``moderation.llm_call_cap_per_hour``，默认 0 = 不限 = 与今天逐字一致**。

超限时的降级口径（选报告里的 **①**）
------------------------------------
只跑本地确定性规则并记为"未送审"，返回的 verdict 与"模型调用失败""模型不可用"
**完全同一个口径**——``conclusive=False``。于是：

* 调用方不会把它当成"已审查通过"写进缓存，也不会给它记"干净"；
* 本地关键词/正则（置信度 1.0）在到达这段代码**之前**就全部跑完了，真命中会直接
  返回并照常处置。

所以"既没过本地规则、又没送审"的静默放行路径**不存在**。没选 ② 的有界等待队列的
理由写在 ``moderation_throttle.py`` 的模块文档里。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.config import ModerationConfig
from bot.db.models import ModerationRule
from bot.services.moderation import ModerationService
from bot.services.moderation_throttle import ModerationAdmissionGate

try:  # 未修复的代码里还没有这道闸（红检路径）
    from bot.services.moderation_throttle import ModerationCallBudget
except ImportError:  # pragma: no cover
    ModerationCallBudget = None  # type: ignore[assignment]

GROUP_ID = -100
SENDER_ID = 7


class _NoAutoflush:
    def __enter__(self) -> "_NoAutoflush":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False


class _RowsResult:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> "_RowsResult":
        return self

    def all(self) -> list[object]:
        return self.rows


def _session(rules: list[ModerationRule]) -> SimpleNamespace:
    return SimpleNamespace(
        no_autoflush=_NoAutoflush(),
        execute=AsyncMock(return_value=_RowsResult(list(rules))),
        commit=AsyncMock(),
    )


class _CountingLLM:
    def __init__(self, reply: str = '{"violated": false, "confidence": 0.9, "reason": ""}') -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def moderation(self, system_prompt: str, user_input: str) -> str:
        self.calls.append((system_prompt, user_input))
        return self.reply


def _semantic_rule() -> ModerationRule:
    """语义规则：本地确定性规则不会拦下它，所以一定走到模型调用。"""

    return ModerationRule(
        id=1,
        group_id=GROUP_ID,
        rule_type="llm",
        pattern="禁止发布广告、推销与引流",
        action="ban",
        enabled=True,
        scan_scope="message",
    )


def _keyword_rule(pattern: str = "秒杀") -> ModerationRule:
    return ModerationRule(
        id=2,
        group_id=GROUP_ID,
        rule_type="keyword",
        pattern=pattern,
        action="delete",
        enabled=True,
        scan_scope="message",
    )


def _service(
    llm: _CountingLLM,
    *,
    cap: int = 0,
    clock=None,
) -> tuple[ModerationService, object | None]:
    # 未修复的代码里没有 ``call_budget`` 这个注入点：那就让服务用进程级共享闸，
    # 上限配置会被静默忽略，断言自然变红（而不是在构造时就炸掉）。
    budget = ModerationCallBudget(clock=clock) if ModerationCallBudget else None
    extra: dict[str, object] = {}
    if budget is not None:
        extra["call_budget"] = budget
    service = ModerationService(
        ModerationConfig(llm_call_cap_per_hour=cap),
        llm,
        # 整形闸会真的 sleep，测试里换成零等待的实例（与被测的硬上限无关）。
        admission_gate=ModerationAdmissionGate(spacing_seconds=0.0),
        **extra,
    )
    return service, budget


class _FakeClock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class UncappedDefaultTests(unittest.IsolatedAsyncioTestCase):
    """① 不配上限时行为完全不变（生产今天的口径）。"""

    def test_default_cap_is_unlimited(self) -> None:
        self.assertEqual(ModerationConfig().llm_call_cap_per_hour, 0)

    async def test_without_a_cap_every_message_still_reaches_the_model(self) -> None:
        llm = _CountingLLM()
        service, _budget = _service(llm, cap=0)

        for _ in range(25):
            verdict = await service.evaluate(
                _session([_semantic_rule()]), GROUP_ID, "在吗", sender_id=SENDER_ID
            )
            self.assertTrue(verdict.conclusive)

        self.assertEqual(len(llm.calls), 25, "不配上限就一次调用都不能省")

    async def test_capped_but_zero_sender_is_never_degraded(self) -> None:
        """人工/低频路径（sender_id=0）不受上限影响。"""

        llm = _CountingLLM()
        service, _budget = _service(llm, cap=1)

        for _ in range(5):
            verdict = await service.evaluate(
                _session([_semantic_rule()]), GROUP_ID, "在吗", sender_id=0
            )
            self.assertTrue(verdict.conclusive)

        self.assertEqual(len(llm.calls), 5)


class HardCapTests(unittest.IsolatedAsyncioTestCase):
    """② 配了上限之后，第 N+1 次不再调用模型，且有明确日志。"""

    async def test_n_plus_one_call_is_not_sent_to_the_model(self) -> None:
        llm = _CountingLLM()
        service, budget = _service(llm, cap=2)

        first = await service.evaluate(
            _session([_semantic_rule()]), GROUP_ID, "第一条", sender_id=SENDER_ID
        )
        second = await service.evaluate(
            _session([_semantic_rule()]), GROUP_ID, "第二条", sender_id=SENDER_ID
        )
        third = await service.evaluate(
            _session([_semantic_rule()]), GROUP_ID, "第三条", sender_id=SENDER_ID
        )

        self.assertEqual(len(llm.calls), 2, "上限外的第三次不得再调用模型")
        self.assertTrue(first.conclusive)
        self.assertTrue(second.conclusive)
        # 降级口径：与"模型调用失败"一致 —— 不可信、不得被当成已审查通过。
        self.assertFalse(third.violated)
        self.assertFalse(
            third.conclusive, "未送审的判定必须标记为不可信，绝不能被当成'已审查通过'"
        )
        self.assertIsNone(third.rule)
        self.assertEqual(budget.snapshot()["skipped_total"], 1.0)
        self.assertEqual(budget.snapshot()["consumed_total"], 2.0)

    async def test_exceeding_the_cap_is_logged_clearly(self) -> None:
        llm = _CountingLLM()
        service, _budget = _service(llm, cap=1)

        await service.evaluate(
            _session([_semantic_rule()]), GROUP_ID, "第一条", sender_id=SENDER_ID
        )
        with self.assertLogs("bot.services.moderation_throttle", level="WARNING") as logs:
            await service.evaluate(
                _session([_semantic_rule()]), GROUP_ID, "第二条", sender_id=SENDER_ID
            )

        self.assertTrue(
            any("审核未送审（成本上限）" in line for line in logs.output), logs.output
        )
        self.assertTrue(any("group=-100 used=1/1" in line for line in logs.output))
        self.assertTrue(
            any("conclusive=False" in line for line in logs.output),
            "日志要说清楚降级口径（不静默放行）",
        )

    async def test_cap_is_per_group(self) -> None:
        llm = _CountingLLM()
        service, _budget = _service(llm, cap=1)

        await service.evaluate(
            _session([_semantic_rule()]), -100, "a", sender_id=SENDER_ID
        )
        await service.evaluate(
            _session([_semantic_rule()]), -200, "a", sender_id=SENDER_ID
        )
        self.assertEqual(len(llm.calls), 2, "一个群用完额度不能影响别的群")

    async def test_allowance_returns_after_the_window_rolls_over(self) -> None:
        clock = _FakeClock()
        llm = _CountingLLM()
        service, _budget = _service(llm, cap=1, clock=clock)

        await service.evaluate(
            _session([_semantic_rule()]), GROUP_ID, "a", sender_id=SENDER_ID
        )
        await service.evaluate(
            _session([_semantic_rule()]), GROUP_ID, "b", sender_id=SENDER_ID
        )
        self.assertEqual(len(llm.calls), 1)

        clock.now += 3600.0  # 窗口滚动
        await service.evaluate(
            _session([_semantic_rule()]), GROUP_ID, "c", sender_id=SENDER_ID
        )
        self.assertEqual(len(llm.calls), 2)

    async def test_degradation_log_is_rate_limited(self) -> None:
        """刷屏式连发不能把告警日志也刷爆。"""

        llm = _CountingLLM()
        service, _budget = _service(llm, cap=1)

        await service.evaluate(
            _session([_semantic_rule()]), GROUP_ID, "a", sender_id=SENDER_ID
        )
        with self.assertLogs("bot.services.moderation_throttle", level="WARNING") as logs:
            for _ in range(10):
                await service.evaluate(
                    _session([_semantic_rule()]), GROUP_ID, "x", sender_id=SENDER_ID
                )
        deprecations = [
            line for line in logs.output if "审核未送审（成本上限）" in line
        ]
        self.assertEqual(len(deprecations), 1, deprecations)


class DegradedPathStillEnforcesLocalRulesTests(unittest.IsolatedAsyncioTestCase):
    """③ 降级路径不会把明显违规内容放过去：本地规则仍然照常执行。"""

    async def test_deterministic_hit_is_still_enforced_when_the_cap_is_exhausted(self) -> None:
        llm = _CountingLLM()
        service, _budget = _service(llm, cap=1)
        rules = [_semantic_rule(), _keyword_rule("秒杀")]

        # 先用掉额度。
        await service.evaluate(_session(rules), GROUP_ID, "在吗", sender_id=SENDER_ID)
        self.assertEqual(len(llm.calls), 1)

        # 额度用完之后：命中本地关键词的消息照常被判违规，模型一次都不再调。
        verdict = await service.evaluate(
            _session(rules), GROUP_ID, "双十一秒杀 5 折", sender_id=SENDER_ID
        )

        self.assertTrue(verdict.violated, "本地确定性规则必须照常拦截")
        self.assertEqual(verdict.confidence, 1.0)
        self.assertEqual(verdict.match_source, "own")
        self.assertTrue(verdict.conclusive)
        self.assertEqual(verdict.rule.id, 2)
        self.assertEqual(len(llm.calls), 1)

    async def test_regex_rule_is_still_enforced_when_the_cap_is_exhausted(self) -> None:
        llm = _CountingLLM()
        service, _budget = _service(llm, cap=1)
        regex_rule = ModerationRule(
            id=3,
            group_id=GROUP_ID,
            rule_type="regex",
            pattern=r"加\s*微信",
            action="ban",
            enabled=True,
            scan_scope="message",
        )
        rules = [_semantic_rule(), regex_rule]

        await service.evaluate(_session(rules), GROUP_ID, "在吗", sender_id=SENDER_ID)

        verdict = await service.evaluate(
            _session(rules), GROUP_ID, "有需要加 微信 找我", sender_id=SENDER_ID
        )

        self.assertTrue(verdict.violated)
        self.assertEqual(verdict.rule.id, 3)
        self.assertEqual(len(llm.calls), 1)

    async def test_deterministic_only_path_is_unaffected_by_the_cap(self) -> None:
        """F-008 的编辑消息检查本来就不过模型，硬上限不该拦它。"""

        llm = _CountingLLM()
        service, _budget = _service(llm, cap=0)
        rules = [_semantic_rule(), _keyword_rule("秒杀")]

        verdict = await service.evaluate(
            _session(rules),
            GROUP_ID,
            "秒杀",
            deterministic_only=True,
            sender_id=SENDER_ID,
        )

        self.assertTrue(verdict.violated)
        self.assertEqual(llm.calls, [])


class CallBudgetUnitTests(unittest.TestCase):
    """``ModerationCallBudget`` 本身的账本语义。"""

    def setUp(self) -> None:
        if ModerationCallBudget is None:  # pragma: no cover - 红检路径
            self.fail("bot.services.moderation_throttle 里还没有 ModerationCallBudget")

    def test_limit_zero_means_unlimited(self) -> None:
        budget = ModerationCallBudget()
        for _ in range(1000):
            self.assertTrue(budget.try_consume(-100, 0).allowed)

    def test_counts_are_exposed_for_observation(self) -> None:
        budget = ModerationCallBudget()
        budget.try_consume(-100, 0)
        self.assertEqual(budget.snapshot()["uncapped_total"], 1.0)

    def test_unknown_group_id_is_not_throttled(self) -> None:
        budget = ModerationCallBudget()
        self.assertTrue(budget.try_consume(0, 1).allowed)

    def test_group_state_table_is_bounded(self) -> None:
        budget = ModerationCallBudget(max_groups=16)
        for index in range(500):
            budget.try_consume(-1000 - index, 5)
        self.assertLessEqual(budget.snapshot()["tracked_groups"], 16.0)

    def test_max_groups_never_silently_waives_the_cap(self) -> None:
        """内存上界不能变成"顺手放过去"：已跟踪的群照样受上限约束。"""

        budget = ModerationCallBudget(max_groups=16)
        for index in range(15):
            budget.try_consume(-1000 - index, 1)
        # 第 16 个群进入后，第 17 个群拿不到状态（放行但不计），第 16 个群仍受约束。
        budget.try_consume(-1015, 1)
        budget.try_consume(-1016, 1)
        self.assertFalse(budget.try_consume(-1015, 1).allowed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
