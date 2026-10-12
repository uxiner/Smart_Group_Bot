"""预算护栏的**进程内共享**与生命周期（第八阶段必修）。

父代理实测的三条失败同根：启动装配与 HTTP 接口各 new 一个
``GroupBudgetService``，于是

* HTTP 保存的预算不会让启动 guard 的缓存失效；
* 启动 guard 把「全局没有预算」永久缓存成 False → **第一次启用预算不生效**；
* 装配失败把 guard 留成 None → 数据库里有 cap=0 也会照发。

这里用**同一个进程**跑完整的「保存 → 立即 → TTL 过期后」矩阵，护栏走真实
LLM 入口（无网络的 fixture provider），不发任何网络请求。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from bot.config import ModelConfig
from bot.db.engine import init_db
from bot.services import group_budget as budget
from bot.services import llm as llm_module
from bot.services import usage_scope as scopes
from bot.services.llm import LLMService

GROUP_ID = -100900000001


def _model() -> ModelConfig:
    return ModelConfig(
        model="fixture/main",
        slot="slot-a",
        provider="openai_compatible",
        api_key="k",
        api_base="https://gateway.example.invalid/v1",
        retry_attempts=1,
        retry_backoff_sec=0.0,
        total_deadline_sec=20.0,
    )


def _response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=2, cached_tokens=0,
            cache_creation_input_tokens=0, thinking_tokens=0,
        ),
    )


class SharedBudgetSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        budget.reset_shared_service()
        self.catalog = budget.pricing.PriceCatalog(version=1, entries=())
        self.service = budget.shared_service(
            self.session_factory, lambda: self.catalog
        )
        self.service = self._fresh()  # noqa: F841 - 让上面那次构造走共享路径
        self.service = budget.shared_service(
            self.session_factory, lambda: self.catalog
        )
        self.service._any_budget_seen = False
        self.service._any_budget_probed_at = time.monotonic()
        self.llm = LLMService(_model(), _model())
        self.guard = budget.llm_budget_guard(self.service)("", "")
        self.sent: list[int] = []

        async def provider(**kwargs):
            self.sent.append(1)
            return _response()

        self._patcher = patch_pair(provider)
        self._patcher.__enter__()

    async def asyncTearDown(self) -> None:
        self._patcher.__exit__(None, None, None)
        budget.reset_shared_service()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    def _fresh(self):
        return budget.shared_service(self.session_factory, lambda: self.catalog)

    async def _chat(self, purpose: str) -> int:
        before = len(self.sent)
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_ID, purpose=purpose
        ):
            await self.llm.chat(
                [{"role": "user", "content": "hi"}], cost_guard=self.guard
            )
        return len(self.sent) - before

    async def _enable(self) -> None:
        row = await self.service.get_budget(GROUP_ID)
        await self.service.save_budget(
            GROUP_ID,
            expected_revision=0 if row is None else int(row.revision),
            enabled=True, currency="USD", daily_attempt_limit=0,
        )

    async def _disable(self) -> None:
        row = await self.service.get_budget(GROUP_ID)
        await self.service.save_budget(
            GROUP_ID,
            expected_revision=int(row.revision),
            enabled=False,
            currency="USD",
        )

    # ------------------------------------------------------------------
    async def test_first_enable_takes_effect_immediately(self) -> None:
        self.assertEqual(await self._chat(scopes.PURPOSE_OPTIONAL), 1)
        await self._enable()
        self.assertEqual(
            await self._chat(scopes.PURPOSE_OPTIONAL), 0, "启用后立即就该拦"
        )

    async def test_disable_takes_effect_immediately(self) -> None:
        await self._enable()
        self.assertEqual(await self._chat(scopes.PURPOSE_OPTIONAL), 0)
        await self._disable()
        self.assertEqual(
            await self._chat(scopes.PURPOSE_OPTIONAL), 1, "关闭后立即就该放行"
        )

    async def test_re_enable_takes_effect_immediately(self) -> None:
        await self._enable()
        self.assertEqual(await self._chat(scopes.PURPOSE_OPTIONAL), 0)
        await self._disable()
        self.assertEqual(await self._chat(scopes.PURPOSE_OPTIONAL), 1)
        await self._enable()
        self.assertEqual(await self._chat(scopes.PURPOSE_OPTIONAL), 0)

    async def test_safety_always_runs(self) -> None:
        await self._enable()
        self.assertEqual(await self._chat(scopes.PURPOSE_SAFETY), 1)

    async def test_disabled_fast_path_survives_a_ttl_expiry(self) -> None:
        """关闭后过期 TTL 再查，仍不得因为"探测到有预算"而误放行/误拦。"""

        await self._enable()
        self.assertEqual(await self._chat(scopes.PURPOSE_OPTIONAL), 0)
        await self._disable()
        self.service._any_budget_seen = None
        self.service._any_budget_probed_at = 0.0
        self.service._enabled_cache.clear()
        self.assertEqual(await self._chat(scopes.PURPOSE_OPTIONAL), 1)
        self.assertEqual(await self._chat(scopes.PURPOSE_OPTIONAL), 1)

    async def test_probe_false_is_not_forever(self) -> None:
        """另一个进程新增预算后，本进程不得因为永久 False 继续放行。"""

        self.assertEqual(await self._chat(scopes.PURPOSE_OPTIONAL), 1)
        # 模拟"别的进程刚建了预算"：直接写库，不经过本进程的 service。
        from bot.db.models import GroupBudget
        from bot.utils.timezone import now_shanghai_naive

        async with self.session_factory() as session:
            async with session.begin():
                session.add(
                    GroupBudget(
                        group_id=GROUP_ID, enabled=True, revision=1,
                        currency="USD", daily_attempt_limit=0,
                        created_at=now_shanghai_naive(),
                    )
                )
        # TTL 到期后必须重新探测。
        self.service._any_budget_probed_at = (
            time.monotonic() - budget._ANY_BUDGET_PROBE_TTL_SECONDS - 1
        )
        self.service._enabled_cache.clear()
        self.assertEqual(
            await self._chat(scopes.PURPOSE_OPTIONAL), 0,
            "永久缓存『没有预算』会让另一个进程启用的护栏完全无效",
        )

    async def test_assembly_failure_denies_optional_and_allows_safety(self) -> None:
        guard = budget.UnavailableCostGuard("boom")
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_ID, purpose=scopes.PURPOSE_OPTIONAL
        ):
            denied = await guard.admit(stage="main")
        self.assertFalse(denied.admitted)
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_ID, purpose=scopes.PURPOSE_SAFETY
        ):
            allowed = await guard.admit(stage="moderation")
        self.assertTrue(allowed.admitted)

    async def test_restart_does_not_reuse_a_stale_guard(self) -> None:
        """重启（丢弃共享实例）后必须按库里的真实状态判定。"""

        await self._enable()
        budget.reset_shared_service()
        rebooted = budget.shared_service(self.session_factory, lambda: self.catalog)
        guard = budget.llm_budget_guard(rebooted)("", "")
        self.llm_guard = guard
        before = len(self.sent)
        with scopes.usage_scope(
            kind=scopes.SCOPE_GROUP, group_id=GROUP_ID, purpose=scopes.PURPOSE_OPTIONAL
        ):
            await self.llm.chat(
                [{"role": "user", "content": "hi"}], cost_guard=guard
            )
        self.assertEqual(len(self.sent) - before, 0)


class RateLimiterCapacityTests(unittest.TestCase):
    """固定上限下，不同用户不得全部放行（第八阶段必修第 8 条）。"""

    def test_tracked_users_are_capped_and_new_users_are_refused(self) -> None:
        """上限满时**新**用户被明确拒绝，而已有配额一个都不许被删。"""

        from bot.services import group_onboarding as onboarding

        onboarding.reset_rate_limit()
        cap = onboarding.RATE_LIMIT_MAX_TRACKED
        # 每个用户只调一次（都在窗口内），把跟踪表填到上限。
        for user in range(cap):
            try:
                onboarding.consume_rate_limit(user)
            except onboarding.OnboardingRateLimited:
                break
        self.assertLessEqual(len(onboarding._RATE_WINDOWS), cap)
        tracked_before = len(onboarding._RATE_WINDOWS)
        overflow_refused = False
        for user in range(cap, cap + 5):
            try:
                onboarding.consume_rate_limit(user)
            except onboarding.OnboardingRateLimited:
                overflow_refused = True
                break
        self.assertTrue(
            overflow_refused, "表满之后新用户仍被放行 = 表可以无限涨"
        )
        self.assertEqual(
            len(onboarding._RATE_WINDOWS), tracked_before,
            "容量压力下删掉了别人仍在有效期内的窗口",
        )
        # 已跟踪用户的配额**仍然有效**：窗口内再调必须仍被拒。
        first_user = next(iter(onboarding._RATE_WINDOWS))
        with self.assertRaises(onboarding.OnboardingRateLimited):
            for _ in range(onboarding.RATE_LIMIT_MAX_CALLS + 1):
                onboarding.consume_rate_limit(first_user)
        onboarding.reset_rate_limit()

    def test_expired_windows_are_reclaimed_not_all_of_them(self) -> None:
        """最老的一条过期**不代表**整个窗口过期 —— 不得据此清空别人的配额。"""

        from bot.services import group_onboarding as onboarding

        onboarding.reset_rate_limit()
        base = 1_000_000.0
        window = float(onboarding.RATE_LIMIT_WINDOW_SECONDS)
        later = base + window + 1          # 用户 1 的时间戳此刻恰好过期
        onboarding.consume_rate_limit(1, now=base)
        onboarding.consume_rate_limit(2, now=later - 1)   # 仍在窗口内
        onboarding.consume_rate_limit(3, now=later - 1)
        # 用户 2/3 的配额**仍然有效**：连打满仍会被拒。
        with self.assertRaises(onboarding.OnboardingRateLimited):
            for _ in range(onboarding.RATE_LIMIT_MAX_CALLS + 1):
                onboarding.consume_rate_limit(2, now=later)
        # 用户 1 的窗口过期 → 重新获得额度。
        self.assertGreater(
            onboarding.consume_rate_limit(1, now=later), 0
        )
        onboarding.reset_rate_limit()


def patch_pair(provider):
    from unittest.mock import patch

    return patch.multiple(
        llm_module.litellm, acompletion=provider, aembedding=provider
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class RateLimiterCapacityReclaimTests(unittest.TestCase):
    """满表 + 全部过期之后必须还能进新用户（父探针第 3 臂）。"""

    def setUp(self) -> None:
        from bot.services import group_onboarding as onboarding

        self.onboarding = onboarding
        onboarding.reset_rate_limit()

    def tearDown(self) -> None:
        self.onboarding.reset_rate_limit()

    def test_new_identity_admitted_after_a_fully_expired_table(self) -> None:
        cap = self.onboarding.RATE_LIMIT_MAX_TRACKED
        base = 1_000_000.0
        window = float(self.onboarding.RATE_LIMIT_WINDOW_SECONDS)
        for user in range(cap):
            self.onboarding.consume_rate_limit(user, now=base)
        self.assertEqual(len(self.onboarding._RATE_WINDOWS), cap)
        # 全部过期之后：新身份必须被放行，且表不得无限增长。
        later = base + window + 1
        self.assertGreater(
            self.onboarding.consume_rate_limit(cap, now=later), 0,
            "表满且全部过期后新身份仍被永久拒绝 = 死路",
        )
        self.assertLessEqual(len(self.onboarding._RATE_WINDOWS), cap)

    def test_partially_expired_window_keeps_its_valid_quota(self) -> None:
        base = 2_000_000.0
        window = float(self.onboarding.RATE_LIMIT_WINDOW_SECONDS)
        for user in range(self.onboarding.RATE_LIMIT_MAX_TRACKED):
            self.onboarding.consume_rate_limit(user, now=base)
        # 只让最后一个用户的窗口老到过期。
        last = self.onboarding.RATE_LIMIT_MAX_TRACKED - 1
        stale_at = base + window + 1
        self.onboarding.consume_rate_limit(last, now=stale_at)
        self.assertLessEqual(
            len(self.onboarding._RATE_WINDOWS),
            self.onboarding.RATE_LIMIT_MAX_TRACKED,
        )
        # 仍有效的那些配额一个都没被清掉。
        for user in range(last):
            with self.assertRaises(self.onboarding.OnboardingRateLimited):
                for _ in range(self.onboarding.RATE_LIMIT_MAX_CALLS + 1):
                    self.onboarding.consume_rate_limit(user, now=stale_at)

    def test_full_table_with_live_quotas_still_refuses_new_identities(self) -> None:
        cap = self.onboarding.RATE_LIMIT_MAX_TRACKED
        base = 3_000_000.0
        for user in range(cap):
            self.onboarding.consume_rate_limit(user, now=base)
        with self.assertRaises(self.onboarding.OnboardingRateLimited):
            self.onboarding.consume_rate_limit(cap + 99, now=base + 1)
        self.assertEqual(len(self.onboarding._RATE_WINDOWS), cap)
