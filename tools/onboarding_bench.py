"""本轮新增的可观测性测量：埋点开销、审核调度附加、开关真实性与触发比例。

这个脚本**不是**回归测试，而是把交付报告里引用的数字真正算出来。三条纪律：

* **开关必须是真的**。两个臂都走真实 ``save_budget(enabled=...)``，然后 fresh
  read 回读并记录 effective enabled / revision —— 早先那版只改了一个输出标签，
  那等于没测开关。
* **走真实入口**。审核臂用真实 ``LLMService.moderation`` + 真实 budget guard；
  供应商是**纯本地**合成替身，只数它被真实调用了几次。**不含** Telegram、网络
  与"实际广告删除时延"——那些这里没有被测到，报告里写"未覆盖"，不拿 0 冒充现网
  结论。
* **比例要看落库**。触发比例臂除了 per-outcome 断言，还要回读持久化的 attempt
  与真实 outbox 告警条数，不能只印一个 admitted。

只在这台机器的这套配置下成立，不会被当成现网数字引用。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bot.config import ModelConfig
from bot.db.engine import init_db
from bot.db.models import AuthorizedGroup, BudgetAlertEvent, GroupBudgetDay
from bot.services import (
    budget_alerts,
    group_budget,
    group_costs,
    llm as llm_module,
    pricing,
    usage_scope,
)

GROUP_ID = -100900000001
ROUNDS = 4000
SAFETY_ROUNDS = 500
TRIGGER_CONCURRENCY = 20

#: 设计目标（不是验收门槛）：内存埋点 p95 < 1ms；审核本地附加 p95 < 5ms。
MEMORY_P95_TARGET_MS = 1.0
DISPATCH_P95_TARGET_MS = 5.0


def _model() -> ModelConfig:
    return ModelConfig(
        model="fixture/model-a",
        provider="openai_compatible",
        api_base="https://gateway.example.invalid/v1",
        api_key="fixture-key",
        retry_attempts=1,
        retry_backoff_sec=0.0,
        total_deadline_sec=20.0,
    )


def _catalog() -> pricing.PriceCatalog:
    return pricing.PriceCatalog(
        version=1,
        entries=(
            pricing.PriceEntry(
                provider_key="fixture",
                model="fixture/model-a",
                currency="USD",
                input_per_million="3",
                output_per_million="15",
            ),
        ),
    )


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def _supplier_response() -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))
        ],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=2, cached_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


async def measure_instrumentation(rounds: int = ROUNDS) -> dict[str, Any]:
    """内存埋点 p95（``record_attempt`` + ``record_result`` 两次调用）。"""

    group_costs.reset()
    latencies: list[float] = []
    for _ in range(rounds):
        start = time.perf_counter()
        with usage_scope.usage_scope(
            kind=usage_scope.SCOPE_GROUP, group_id=GROUP_ID,
            purpose=usage_scope.PURPOSE_OPTIONAL,
        ):
            attempt = group_costs.record_attempt(
                stage="main", provider="fixture", model="fixture/model-a"
            )
            group_costs.record_result(
                attempt_id=attempt,
                usage={
                    "prompt_tokens": 10, "completion_tokens": 2,
                    "cached_tokens": 0, "cache_creation_input_tokens": 0,
                },
            )
        latencies.append((time.perf_counter() - start) * 1000.0)
    group_costs.reset()
    p95 = round(percentile(latencies, 0.95), 6)
    return {
        "mode": "memory_instrumentation",
        "rounds": rounds,
        "p50_ms": round(percentile(latencies, 0.50), 6),
        "p95_ms": p95,
        "target_p95_ms": MEMORY_P95_TARGET_MS,
        "target_met": p95 < MEMORY_P95_TARGET_MS,
    }


class _CountingService:
    """把真实 ``GroupBudgetService`` 包一层，数护栏问了几次、真的落了几条预留。

    两件事必须分开数：``admit`` **总是**会问一次 ``reserve``（包括安全用途），
    但安全用途在 ``reserve`` 内部短路、**不写任何预算行**。把"问了几次"当作
    "预留了几次"会得出"安全也占额度"的错误结论。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.reserve_entry_calls = 0
        self.reservations_created = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def reserve(self, *args: Any, **kwargs: Any) -> Any:
        self.reserve_entry_calls += 1
        outcome = await self._inner.reserve(*args, **kwargs)
        if getattr(outcome, "reservation_id", None) is not None:
            self.reservations_created += 1
        return outcome


async def _fresh_enabled(session_factory: Any, group_id: int) -> bool:
    from sqlalchemy import select

    from bot.db.models import GroupBudget

    async with session_factory() as session:
        row = (
            await session.scalars(
                select(GroupBudget).where(GroupBudget.group_id == group_id)
            )
        ).first()
    return bool(row and row.enabled)


def _semaphore_patch() -> Any:
    return patch.object(
        llm_module, "_LLM_REQUEST_SEMAPHORE",
        asyncio.Semaphore(llm_module._LLM_REQUEST_CAPACITY),
    )


async def measure_moderation_dispatch(
    session_factory: Any,
    *,
    with_budget: bool,
    rounds: int = SAFETY_ROUNDS,
) -> dict[str, Any]:
    """审核主路径在**真实**开关下的本地调度开销。

    安全用途两臂都必须全部可达供应商（安全不被可选预算挡住），且预算预留为 0。
    """

    group_id = GROUP_ID - (2 if with_budget else 1)
    inner = group_budget.GroupBudgetService(
        session_factory=session_factory, catalog_provider=_catalog
    )
    await inner.save_budget(
        group_id, expected_revision=0, enabled=with_budget, currency="USD",
        daily_attempt_limit=0, daily_token_limit=0,
    )
    # fresh read：证明这个臂的开关**真的**落库成了我们要的那个值。
    effective = await inner.get_budget(group_id)
    fresh = await _fresh_enabled(session_factory, group_id)
    if fresh is not with_budget:
        raise AssertionError(
            f"开关没有真的落库：wanted={with_budget} fresh={fresh}"
        )

    counted = _CountingService(inner)
    guard = group_budget.llm_budget_guard(counted)
    service = llm_module.LLMService(_model(), _model())
    provider_calls = {"n": 0}

    async def supplier(**kwargs: Any) -> Any:
        provider_calls["n"] += 1
        return _supplier_response()

    latencies: list[float] = []
    with _semaphore_patch(), patch.object(
        llm_module.litellm, "acompletion", supplier
    ):
        for _ in range(rounds):
            start = time.perf_counter()
            with usage_scope.usage_scope(
                kind=usage_scope.SCOPE_GROUP, group_id=group_id,
                purpose=usage_scope.PURPOSE_SAFETY, stage="moderation",
            ):
                verdict = await service.moderation(
                    "system", "user",
                    cost_guard=guard("fixture", "fixture/model-a"),
                )
            latencies.append((time.perf_counter() - start) * 1000.0)
            if verdict != "ok":
                raise AssertionError(f"安全审核没有正常返回：{verdict!r}")

    if provider_calls["n"] != rounds:
        raise AssertionError(
            f"安全审核没有全部到达供应商：{provider_calls['n']} != {rounds}"
        )
    if counted.reservations_created != 0:
        raise AssertionError(
            f"安全用途不该占用可选额度，却落了 {counted.reservations_created} 条预留"
        )
    p95 = round(percentile(latencies, 0.95), 6)
    return {
        "mode": "budget_on" if with_budget else "budget_off",
        "rounds": rounds,
        "configured_enabled": with_budget,
        "effective_enabled": bool(getattr(effective, "enabled", False)),
        "effective_revision": int(getattr(effective, "revision", 0) or 0),
        "fresh_enabled": fresh,
        "provider_calls": provider_calls["n"],
        "guard_admit_entries": counted.reserve_entry_calls,
        "budget_reservations_created": counted.reservations_created,
        "telegram_calls": "not exercised (no Telegram client in this bench)",
        "p50_ms": round(percentile(latencies, 0.50), 6),
        "p95_ms": p95,
        "target_p95_ms": DISPATCH_P95_TARGET_MS,
        "target_met": p95 < DISPATCH_P95_TARGET_MS,
    }


async def measure_optional_positive_control(session_factory: Any) -> dict[str, Any]:
    """开关**真的驱动** guard：同一实例，cap=0 时可选拒绝，disabled 时放行。

    控制组，不混进 500 次样本。
    """

    group_id = GROUP_ID - 5
    inner = group_budget.GroupBudgetService(
        session_factory=session_factory, catalog_provider=_catalog
    )
    service = llm_module.LLMService(_model(), _model())
    provider_calls = {"n": 0}

    async def supplier(**kwargs: Any) -> Any:
        provider_calls["n"] += 1
        return _supplier_response()

    async def run(enabled: bool, attempt_limit: int) -> tuple[int, str]:
        # 第二次保存要带**当前** revision（CAS 冲突就是合同在起作用）。
        current = await inner.get_budget(group_id)
        await inner.save_budget(
            group_id,
            expected_revision=int(getattr(current, "revision", 0) or 0),
            enabled=enabled, currency="USD",
            daily_attempt_limit=attempt_limit,
        )
        inner.invalidate()
        before = provider_calls["n"]
        with _semaphore_patch(), patch.object(
            llm_module.litellm, "acompletion", supplier
        ):
            # 被额度拒绝时 `chat` 是**正常返回**的（只记日志、不抛异常），所以
            # 这里不需要任何 except：真的抛出来的就是内部崩溃，必须让测量炸掉，
            # 不能因为"provider 调用 0 次"就把崩溃认证成"护栏起作用"。
            with usage_scope.usage_scope(
                kind=usage_scope.SCOPE_GROUP, group_id=group_id,
                purpose="optional",
            ):
                verdict = await service.chat(
                    [{"role": "user", "content": "hi"}],
                    cost_guard=group_budget.llm_budget_guard(inner)(
                        "fixture", "fixture/model-a"
                    ),
                )
            if not isinstance(verdict, str):
                raise AssertionError(
                    f"可选调用返回了非预期类型：{type(verdict).__name__}"
                )
        return provider_calls["n"] - before, verdict

    capped_calls, capped_verdict = await run(True, 0)
    disabled_calls, disabled_verdict = await run(False, 0)
    # 只断言"返回的是字符串"区分不出"护栏拒绝了"和"内部静默失败了"：
    #   * 被拒时 chat 返回**空串**且供应商 0 次；
    #   * 放行时必须拿回供应商合成的正文，且供应商至少被调一次。
    if capped_calls != 0 or capped_verdict != "":
        raise AssertionError(
            f"cap=0 时本该被拒却拿到 calls={capped_calls} verdict={capped_verdict!r}"
        )
    if disabled_calls < 1 or disabled_verdict != "ok":
        raise AssertionError(
            "disabled 时本该放行却拿到 "
            f"calls={disabled_calls} verdict={disabled_verdict!r}"
        )
    return {
        "mode": "optional_positive_control",
        "enabled_cap0_provider_calls": capped_calls,
        "enabled_cap0_verdict": capped_verdict,
        "disabled_provider_calls": disabled_calls,
        "disabled_verdict": disabled_verdict,
        "switch_drives_guard": True,
    }


async def measure_trigger_ratio(
    session_factory: Any,
    *,
    limit: int,
    concurrent: int = TRIGGER_CONCURRENCY,
) -> dict[str, Any]:
    """不同预算设定下的 denied / admitted 比例，并回读持久 attempt 与真实告警。"""

    from sqlalchemy import func, select

    group_id = GROUP_ID - 70 - limit
    service = group_budget.GroupBudgetService(
        session_factory=session_factory, catalog_provider=_catalog
    )
    await service.save_budget(
        group_id, expected_revision=0, enabled=True, currency="USD",
        daily_attempt_limit=limit,
    )
    group_budget.configure_alert_enqueuer(
        budget_alerts.enqueue_threshold_events
    )

    async def one() -> bool:
        with usage_scope.usage_scope(
            kind=usage_scope.SCOPE_GROUP, group_id=group_id, purpose="optional"
        ):
            reservation = await service.reserve(
                group_id=group_id,
                purpose="optional",
                stage="main",
                provider="fixture",
                model="fixture/model-a",
                prompt_tokens=100,
                # 真实严格合同要求合法上界；不给就是"未知入参"，启用上限时会被拒
                # —— 那是另一条合同，不是这条比例测量要测的东西。
                max_output_tokens=20,
            )
            if reservation.admitted and reservation.reservation_id is not None:
                await service.settle(
                    reservation.reservation_id,
                    usage={
                        "prompt_tokens": 100, "completion_tokens": 20,
                        "cached_tokens": 0, "cache_creation_input_tokens": 0,
                    },
                )
            return reservation.admitted

    outcomes = await asyncio.gather(*(one() for _ in range(concurrent)))
    admitted = sum(1 for item in outcomes if item)
    denied = concurrent - admitted

    async with session_factory() as session:
        day = (
            await session.scalars(
                select(GroupBudgetDay).where(GroupBudgetDay.group_id == group_id)
            )
        ).first()
        alert_rows = (
            await session.execute(
                select(BudgetAlertEvent.threshold_kind, func.count())
                .where(BudgetAlertEvent.group_id == group_id)
                .group_by(BudgetAlertEvent.threshold_kind)
            )
        ).all()
    persisted_attempts = int(getattr(day, "used_attempts", 0) or 0)
    alerts = {str(kind): int(count) for kind, count in alert_rows}

    # 工具自己必须拦住非法结果：打印出来不等于测量成立。
    expected_admitted = min(limit, concurrent)
    expected_denied = concurrent - expected_admitted
    if admitted != expected_admitted:
        raise AssertionError(
            f"limit={limit} 放行数 {admitted} != {expected_admitted}"
        )
    if denied != expected_denied:
        raise AssertionError(
            f"limit={limit} 拒绝数 {denied} != {expected_denied}"
        )
    if persisted_attempts != expected_admitted:
        raise AssertionError(
            f"limit={limit} 落库 attempt {persisted_attempts} != {expected_admitted}"
        )
    # cap 到顶就会同时越过 80% 与 100%：两档各一条；没到 80% 则一条都不该有。
    ratio = expected_admitted / limit if limit else 0.0
    if ratio >= 1.0:
        if alerts != {"warn": 1, "pause": 1}:
            raise AssertionError(
                f"limit={limit} 告警档位不合法：{alerts}"
            )
    elif ratio >= 0.8:
        if alerts != {"warn": 1}:
            raise AssertionError(f"limit={limit} 只该有 warn：{alerts}")
    elif alerts:
        raise AssertionError(f"limit={limit} 不该有任何告警：{alerts}")

    return {
        "mode": "trigger_ratio",
        "configured_limit": limit,
        "requested": concurrent,
        "admitted": admitted,
        "denied": denied,
        "persisted_attempts": persisted_attempts,
        "alert_counts": alerts,
        "alerts_total": sum(alerts.values()),
    }


async def run(
    *, rounds: int = ROUNDS, safety_rounds: int = SAFETY_ROUNDS
) -> dict[str, Any]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine, session_factory = await init_db(f"sqlite+aiosqlite:///{path}")
    try:
        async with session_factory() as session:
            async with session.begin():
                session.add(AuthorizedGroup(group_id=GROUP_ID, authorized_by=1))
        group_costs.reset()
        group_costs.configure(session_factory)

        return {
            "environment": {
                "python": sys.version.split()[0],
                "database": "sqlite (temp)",
                "note": (
                    "local synthetic load; no production data, no Telegram, "
                    "no network; advertisement-deletion latency NOT exercised"
                ),
            },
            "instrumentation": await measure_instrumentation(rounds),
            "moderation_off": await measure_moderation_dispatch(
                session_factory, with_budget=False, rounds=safety_rounds
            ),
            "moderation_on": await measure_moderation_dispatch(
                session_factory, with_budget=True, rounds=safety_rounds
            ),
            "optional_positive_control": await measure_optional_positive_control(
                session_factory
            ),
            "triggers": [
                await measure_trigger_ratio(session_factory, limit=1),
                await measure_trigger_ratio(session_factory, limit=20),
            ],
        }
    finally:
        group_budget.configure_alert_enqueuer(None)
        await group_costs.flush(force=True)
        group_costs.reset()
        await engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


def _print(results: dict[str, Any]) -> None:
    print("=== environment ===")
    print(json.dumps(results["environment"], ensure_ascii=False))
    print("=== instrumentation (memory only) ===")
    print(json.dumps(results["instrumentation"], ensure_ascii=False))
    for key in ("moderation_off", "moderation_on"):
        print(f"=== {key} (safety purpose, real LLMService + real guard) ===")
        print(json.dumps(results[key], ensure_ascii=False))
    print("=== optional positive control (switch really drives the guard) ===")
    print(json.dumps(results["optional_positive_control"], ensure_ascii=False))
    print("=== trigger ratio (synthetic, persisted readback) ===")
    for item in results["triggers"]:
        print(json.dumps(item, ensure_ascii=False))


async def main() -> None:
    results = await run()
    _print(results)
    out = os.environ.get("ONBOARDING_BENCH_OUT", "")
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
