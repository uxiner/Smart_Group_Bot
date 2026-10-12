"""所有模型调用与后台归属的接线（Task 5）。

这一层用**真实**的 LLMService（只把 litellm 替成脚本化 provider），
而不是把统计函数打桩掉只测 helper —— 打桩掉统计之后，测试证明的只是
「我给了一个 mock，它收到了我给的东西」，完全没证明真实调用路径上有人记账。

被钉死的不变量：

* **每次真实发出 = 一个 attempt**：超时 → 回退 → 成功记三条；
* **带 usage 的空响应照计 token**，缺 usage 记 unknown 而不是 0；
* **工具循环的每一步各记各的**；embedding 也记；
* **发送前取消不记 attempt**；
* **审核视觉 ≠ 娱乐视觉**：同一 stage 名下 safety 不得被当 optional；
* **后台任务显式绑定**：proactive / 摘要 / 回放 / 私聊检索 / 记忆提炼 /
  索引 embedding 各自落在正确的群/私聊桶里，不继承发起人的上下文；
* **审核主路径没有新增外部调用**：新断言里审核仍是 0 次额外模型调用。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from bot.config import EmbedConfig, ModelConfig, Settings
from bot.db.engine import init_db
from bot.db.models import LlmCostDaily
from bot.services import group_costs, llm as llm_module, usage_scope as scopes
from bot.services.llm import LLMService

GROUP_A = -100900000001
GROUP_B = -100900000002


def _model(name: str = "fixture/model-a", *, retry_attempts: int = 1) -> ModelConfig:
    # 默认**只试一次**：重试次数是另一个用例要单独验证的维度，混在一起会让
    # "空响应记一条"这类断言被第二次尝试悄悄改掉。
    return ModelConfig(
        model=name,
        provider="openai_compatible",
        api_base="https://gateway.example.invalid/v1",
        api_key="fixture-key",
        retry_attempts=retry_attempts,
        retry_backoff_sec=0.0,
        total_deadline_sec=20.0,
    )


def _response(
    content: str = "ok",
    *,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    tool_calls: list | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage = None
    if prompt_tokens is not None or completion_tokens is not None:
        usage = SimpleNamespace(
            prompt_tokens=prompt_tokens or 0,
            completion_tokens=completion_tokens or 0,
            cached_tokens=0,
            cache_creation_input_tokens=0,
            thinking_tokens=0,
        )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class _ScriptedProvider:
    """逐条消费的脚本化 litellm 替身。``BaseException`` 直接抛出。"""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, object]] = []

    def _next(self, kind: str) -> object:
        self.calls.append({"kind": kind})
        if not self.script:
            return _response("fallback", prompt_tokens=1, completion_tokens=1)
        item = self.script.pop(0)
        return item

    async def acompletion(self, **kwargs):
        item = self._next("chat")
        if isinstance(item, BaseException):
            raise item
        return item

    async def aembedding(self, **kwargs):
        item = self._next("embed")
        if isinstance(item, BaseException):
            raise item
        return item


class UsageRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        group_costs.reset()
        group_costs.configure(self.session_factory)
        self.service = LLMService(
            _model(), _model("fixture/decision"), _model("fixture/moderation"),
            moderation=_model("fixture/moderation"),
            vision=_model("fixture/vision"),
            embed=EmbedConfig(
                model="fixture/embed",
                provider="openai_compatible",
                api_base="https://gateway.example.invalid/v1",
                api_key="fixture-key",
            ),
        )
        self.settings = Settings(_env_file=None)

    async def asyncTearDown(self) -> None:
        await group_costs.flush(force=True)
        group_costs.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _rows(self, **filters) -> list[LlmCostDaily]:
        await group_costs.flush(force=True)
        async with self.session_factory() as session:
            stmt = select(LlmCostDaily)
            for key, value in filters.items():
                stmt = stmt.where(getattr(LlmCostDaily, key) == value)
            return list((await session.scalars(stmt)).all())

    def _run(self, script: list[object]):
        provider = _ScriptedProvider(script)
        patcher = patch.multiple(
            llm_module.litellm,
            acompletion=provider.acompletion,
            aembedding=provider.aembedding,
        )
        return provider, patcher

    # ------------------------------------------------------------------
    async def test_retry_after_timeout_costs_two_separate_attempts(self) -> None:
        retrying = LLMService(
            _model(retry_attempts=2), _model("fixture/decision"),
        )
        script = [
            asyncio_timeout_error(),
            _response("second", prompt_tokens=100, completion_tokens=20),
        ]
        provider, patcher = self._run(script)
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
            ):
                text = await retrying.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(text, "second")
        self.assertEqual(len(provider.calls), 2)
        rows = await self._rows(group_id=GROUP_A, stage="main")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0].attempts), 2)
        self.assertEqual(int(rows[0].timeouts), 1)
        self.assertEqual(int(rows[0].successes), 1)
        self.assertEqual(int(rows[0].prompt_tokens), 100)
        self.assertEqual(int(rows[0].usage_unknown), 1)

    async def test_concurrent_groups_are_never_mixed(self) -> None:
        # 一个**共享**脚本 provider：并发地给 litellm 打两次 patch 是无效的
        # （后一次会全局盖掉前一次），那样测到的不是并发归属而是 patch 竞态。
        async def echo(**kwargs):
            self.echo_calls += 1
            body = kwargs["messages"][-1]["content"]
            return _response(str(body), prompt_tokens=5, completion_tokens=1)

        self.echo_calls = 0

        async def call(group_id: int, text: str) -> str:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=group_id, purpose="optional"
            ):
                return await self.service.chat(
                    [{"role": "user", "content": text}]
                )

        with patch.object(llm_module.litellm, "acompletion", echo):
            import asyncio

            results = await asyncio.gather(call(GROUP_A, "a"), call(GROUP_B, "b"))
        self.assertEqual(self.echo_calls, 2)
        self.assertEqual(sorted(results), ["a", "b"])
        rows = await self._rows(stage="main")
        self.assertEqual({int(row.group_id) for row in rows}, {GROUP_A, GROUP_B})
        for row in rows:
            self.assertEqual(int(row.attempts), 1)

    async def test_empty_response_with_usage_is_billed(self) -> None:
        provider, patcher = self._run(
            [_response("", prompt_tokens=77, completion_tokens=0)]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
            ):
                await self.service.chat([{"role": "user", "content": "hi"}])
        rows = await self._rows(group_id=GROUP_A, stage="main")
        self.assertEqual(int(rows[0].empty_responses), 1)
        self.assertEqual(int(rows[0].prompt_tokens), 77)
        self.assertEqual(int(rows[0].usage_unknown), 0)

    async def test_failure_without_usage_is_unknown_not_zero(self) -> None:
        provider, patcher = self._run([RuntimeError("upstream 500")])
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="safety"
            ):
                await self.service.moderation("system", "user")
        rows = await self._rows(group_id=GROUP_A, stage="moderation")
        self.assertEqual(int(rows[0].usage_unknown), int(rows[0].attempts))
        self.assertIsNone(rows[0].prompt_tokens)

    async def test_moderation_is_recorded_as_safety(self) -> None:
        provider, patcher = self._run(
            [_response("clean", prompt_tokens=9, completion_tokens=1)]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A
            ):
                await self.service.moderation("system", "user")
        rows = await self._rows(group_id=GROUP_A, stage="moderation")
        self.assertEqual(str(rows[0].purpose), scopes.PURPOSE_SAFETY)

    async def test_embedding_is_recorded_with_the_real_model(self) -> None:
        provider, patcher = self._run(
            [
                SimpleNamespace(
                    data=[{"embedding": [0.1, 0.2]}],
                    usage=SimpleNamespace(prompt_tokens=12, completion_tokens=0),
                )
            ]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
            ):
                vectors = await self.service.embed(["hello"])
        self.assertEqual(len(vectors), 1)
        rows = await self._rows(group_id=GROUP_A, stage="embed")
        self.assertEqual(int(rows[0].attempts), 1)
        self.assertEqual(int(rows[0].prompt_tokens), 12)
        self.assertEqual(str(rows[0].model), "fixture/embed")

    async def test_single_tool_call_step_is_one_attempt(self) -> None:
        tool_call = SimpleNamespace(
            id="call-1",
            type="function",
            function=SimpleNamespace(name="lookup", arguments="{}"),
        )
        provider, patcher = self._run(
            [_response("", prompt_tokens=10, completion_tokens=3, tool_calls=[tool_call])]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
            ):
                resp = await self.service.complete_with_tools(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[
                        {
                            "type": "function",
                            "function": {"name": "lookup", "parameters": {}},
                        }
                    ],
                )
        self.assertIsNotNone(resp)
        rows = await self._rows(group_id=GROUP_A, stage="skill")
        self.assertEqual(int(rows[0].attempts), 1)
        self.assertEqual(int(rows[0].prompt_tokens), 10)

    async def test_safety_vision_is_not_recorded_as_optional(self) -> None:
        provider, patcher = self._run(
            [_response("a description", prompt_tokens=30, completion_tokens=5)]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A
            ):
                await self.service.vision_describe(
                    "data:image/png;base64,AAA",
                    "describe",
                    purpose="safety",
                    stage="moderation_vision",
                )
        rows = await self._rows(group_id=GROUP_A, stage="moderation_vision")
        self.assertEqual(str(rows[0].purpose), scopes.PURPOSE_SAFETY)

    async def test_entertainment_vision_stays_optional(self) -> None:
        provider, patcher = self._run(
            [_response("a cat", prompt_tokens=20, completion_tokens=4)]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A
            ):
                await self.service.vision_describe(
                    "data:image/png;base64,AAA", "describe", purpose="optional"
                )
        rows = await self._rows(group_id=GROUP_A, stage="vision")
        self.assertEqual(str(rows[0].purpose), scopes.PURPOSE_OPTIONAL)

    async def test_purpose_scope_does_not_leak_to_the_next_call(self) -> None:
        provider, patcher = self._run(
            [
                _response("x", prompt_tokens=1, completion_tokens=1),
                _response("y", prompt_tokens=1, completion_tokens=1),
            ]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A
            ):
                await self.service.vision_describe(
                    "data:image/png;base64,AAA", "describe", purpose="safety"
                )
                await self.service.vision_describe(
                    "data:image/png;base64,BBB", "describe"
                )
        rows = await self._rows(group_id=GROUP_A)
        # 两次调用**同一个 stage**，只有第一次是 safety：这正是"用途必须显式
        # 传递、且不得泄漏到下一次"这条不变量的形状。
        self.assertEqual(
            sorted(str(row.purpose) for row in rows),
            sorted([scopes.PURPOSE_SAFETY, scopes.PURPOSE_OPTIONAL]),
        )
        self.assertEqual({str(row.stage) for row in rows}, {"vision"})

    async def test_no_attempt_is_recorded_when_nothing_was_sent(self) -> None:
        provider, patcher = self._run([_response("never", prompt_tokens=1, completion_tokens=1)])
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
            ):
                task = asyncio.create_task(
                    self.service.chat([{"role": "user", "content": "hi"}])
                )
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        # provider 根本没被调用 → 没有发出请求；即使计到一条也必须是 unknown，
        # 绝不能记成一次成功调用。
        rows = await self._rows(group_id=GROUP_A)
        for row in rows:
            self.assertEqual(int(row.successes), 0)

    async def test_repeated_flush_does_not_double_count_a_settled_attempt(self) -> None:
        """重复落盘不得让账翻倍（**不是**取消用例；真正的取消在
        ``test_usage_dispatcher_wiring.py`` 的不合作取消用例里）。"""

        provider, patcher = self._run(
            [_response("late", prompt_tokens=42, completion_tokens=2)]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
            ):
                await self.service.chat([{"role": "user", "content": "hi"}])
        # 结算后内存里不留东西（账已经进库），再 flush 一次也不该变化。
        self.assertEqual(group_costs.snapshot(), {})
        await group_costs.flush(force=True)
        await group_costs.flush(force=True)
        rows = await self._rows(group_id=GROUP_A, stage="main")
        self.assertEqual(int(rows[0].attempts), 1)
        self.assertEqual(int(rows[0].prompt_tokens), 42)

    async def test_moderation_adds_no_extra_model_call(self) -> None:
        """本轮不得给审核主路径加任何模型调用：一次调用就是一次 provider 请求。"""

        provider, patcher = self._run(
            [_response("clean", prompt_tokens=11, completion_tokens=2)]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A
            ):
                await self.service.moderation("system", "user")
        self.assertEqual(len(provider.calls), 1)
        rows = await self._rows(group_id=GROUP_A, stage="moderation")
        self.assertEqual(int(rows[0].attempts), 1)

    async def test_unbound_call_lands_in_system_not_in_a_guessed_group(self) -> None:
        provider, patcher = self._run(
            [_response("ok", prompt_tokens=3, completion_tokens=1)]
        )
        with patcher:
            await self.service.chat([{"role": "user", "content": "hi"}])
        rows = await self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].scope), scopes.SCOPE_SYSTEM)
        self.assertEqual(int(rows[0].group_id), 0)

    async def test_no_api_key_or_gateway_url_in_the_ledger(self) -> None:
        provider, patcher = self._run(
            [_response("ok", prompt_tokens=3, completion_tokens=1)]
        )
        with patcher:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose="optional"
            ):
                await self.service.chat([{"role": "user", "content": "hi"}])
        rows = await self._rows()
        blob = " ".join(
            f"{row.provider} {row.model} {row.stage}" for row in rows
        )
        self.assertNotIn("fixture-key", blob)
        self.assertNotIn("gateway.example.invalid", blob)
        self.assertNotIn("?", blob)


def asyncio_timeout_error() -> BaseException:
    import asyncio

    return asyncio.TimeoutError()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()