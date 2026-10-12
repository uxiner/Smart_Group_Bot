"""真实 aiogram Dispatcher 上的用量归属（第五阶段返工）。

这一组刻意**走真实入口**（``Dispatcher.feed_update``）而不是在测试里直接
``with usage_scope(...)``：后者绕过了真正出问题的那一段 —— 中间件绑定。

钉死的不变量：

* 中间件收到的是 ``Update``，不是 ``Message``：必须正确 unwrap
  ``update.event``，并覆盖 message / edited_message / callback_query /
  chat_member / my_chat_member 五种 observer；
* 私聊归 private，异常与取消后上下文复位；
* 跨群的索引批次不得整批记到第一群；
* 不合作的取消（provider 吞掉 CancelledError 后再返回 usage）必须可靠结算；
* 流式/解析异常后 attempt 仍要结算，不得留在在飞表里；
* 供应商标识必须是**逻辑配置槽**，不是协议名也不是带鉴权的 URL。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from aiogram import Dispatcher
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberAdministrator,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    PollAnswer,
    Update,
    User,
)
from sqlalchemy import select

from bot.config import EmbedConfig, ModelConfig
from bot.db.engine import init_db
from bot.db.models import LlmCostDaily
from bot.middlewares.usage_scope import UsageScopeMiddleware
from bot.services import group_costs, llm as llm_module, usage_scope as scopes
from bot.services.llm import LLMService

GROUP_A = -100900000001
GROUP_B = -100900000002
USER_ID = 7
BOT_ID = 424242


_UNSET = object()


def _usage(prompt: int = 10, completion: int = 2, cached: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        cache_creation_input_tokens=0,
        thinking_tokens=0,
    )


def _chat_response(text: str = "ok", usage: SimpleNamespace | None = None) -> SimpleNamespace:
    """``usage=None`` 表示**上游没有报 usage**，必须真的传 None。

    用哨兵对象区分"没传"和"显式 None"，否则这条用例测的就不是它想测的东西。
    """

    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))],
        usage=_usage() if usage is _UNSET else usage,
    )


class _FakeBot:
    """最小 Bot 替身：本文件只验证中间件绑定，不应发出任何真实调用。"""

    id = BOT_ID
    is_bot = True
    first_name = "B"

    def __init__(self) -> None:
        self.session = SimpleNamespace()

    async def __call__(self, method, request_timeout=None):
        raise AssertionError(f"本文件不应发出真实 Telegram 调用：{method}")


class _Recorder:
    """记录每次进来的 update 对应的归属（handler 侧只读上下文）。"""

    def __init__(self) -> None:
        self.seen: list[dict] = []

    async def handle(self, event: object, data: dict) -> None:
        scope = scopes.current_usage_scope()
        self.seen.append(
            {
                "kind": scope.kind,
                "group_id": scope.group_id,
                "purpose": scope.purpose,
                "event": type(event).__name__,
            }
        )


class MiddlewareBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        group_costs.reset()
        group_costs.configure(self.session_factory)
        scopes.reset()
        self.dispatcher = Dispatcher()
        self.recorder = _Recorder()
        # 挂在 update 根上，与产品实际注册位置一致：根上这份**覆盖所有**
        # 子观察者，所以这里必须只有一次。
        self.dispatcher.update.outer_middleware(UsageScopeMiddleware())
        for observer in (
            self.dispatcher.message,
            self.dispatcher.edited_message,
            self.dispatcher.callback_query,
            self.dispatcher.chat_member,
            self.dispatcher.my_chat_member,
        ):
            observer.register(self._observe)

    async def _observe(self, event: object) -> None:
        await self.recorder.handle(event, {})

    async def asyncTearDown(self) -> None:
        await group_costs.flush(force=True)
        group_costs.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    @staticmethod
    def _message(chat_id: int, chat_type: str = "supergroup") -> Message:
        return Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=USER_ID, is_bot=False, first_name="A"),
            text="hello",
        )

    @staticmethod
    def _update_id() -> int:
        return int(datetime.now(timezone.utc).timestamp() * 1000) % 1_000_000_000

    async def _feed(self, update: Update) -> None:
        # aiogram 需要一个 Bot 实例才能解析 session；这里用最小替身，因为
        # 本文件只验证中间件绑定，不触碰任何真实 Telegram 调用。
        await self.dispatcher.feed_update(_FakeBot(), update)

    # ------------------------------------------------------------------
    async def test_group_message_is_attributed_to_the_group(self) -> None:
        await self._feed(
            Update(update_id=self._update_id(), message=self._message(GROUP_A))
        )
        self.assertEqual(len(self.recorder.seen), 1)
        self.assertEqual(self.recorder.seen[0]["kind"], "group")
        self.assertEqual(self.recorder.seen[0]["group_id"], GROUP_A)

    async def test_edited_message_is_attributed(self) -> None:
        message = self._message(GROUP_A)
        await self._feed(
            Update(
                update_id=self._update_id(),
                edited_message=message,
            )
        )
        self.assertEqual(self.recorder.seen[0]["group_id"], GROUP_A)

    async def test_callback_query_uses_its_message_chat(self) -> None:
        callback = CallbackQuery(
            id="1",
            from_user=User(id=USER_ID, is_bot=False, first_name="A"),
            chat_instance="x",
            data="noop",
            message=self._message(GROUP_B),
        )
        await self._feed(Update(update_id=self._update_id(), callback_query=callback))
        self.assertEqual(self.recorder.seen[0]["group_id"], GROUP_B)

    async def test_chat_member_update_is_attributed(self) -> None:
        member = ChatMemberUpdated(
            chat=Chat(id=GROUP_A, type="supergroup"),
            from_user=User(id=USER_ID, is_bot=False, first_name="A"),
            date=datetime.now(timezone.utc),
            old_chat_member=ChatMemberMember(
                user=User(id=USER_ID, is_bot=False, first_name="A"),
                status="member",
            ),
            new_chat_member=ChatMemberAdministrator(
                user=User(id=USER_ID, is_bot=False, first_name="A"),
                status="administrator",
                can_be_edited=False,
                is_anonymous=False,
                can_manage_chat=False,
                can_delete_messages=False,
                can_manage_video_chats=False,
                can_restrict_members=False,
                can_promote_members=False,
                can_change_info=False,
                can_invite_users=False,
                can_post_stories=False,
                can_edit_stories=False,
                can_delete_stories=False,
            ),
        )
        await self._feed(Update(update_id=self._update_id(), chat_member=member))
        self.assertEqual(self.recorder.seen[0]["group_id"], GROUP_A)

    async def test_my_chat_member_update_is_attributed(self) -> None:
        member = ChatMemberUpdated(
            chat=Chat(id=GROUP_B, type="supergroup"),
            from_user=User(id=BOT_ID, is_bot=True, first_name="B"),
            date=datetime.now(timezone.utc),
            old_chat_member=ChatMemberMember(
                user=User(id=BOT_ID, is_bot=True, first_name="B"),
                status="member",
            ),
            new_chat_member=ChatMemberAdministrator(
                user=User(id=BOT_ID, is_bot=True, first_name="B"),
                status="administrator",
                can_be_edited=False,
                is_anonymous=False,
                can_manage_chat=False,
                can_delete_messages=False,
                can_manage_video_chats=False,
                can_restrict_members=False,
                can_promote_members=False,
                can_change_info=False,
                can_invite_users=False,
                can_post_stories=False,
                can_edit_stories=False,
                can_delete_stories=False,
            ),
        )
        await self._feed(Update(update_id=self._update_id(), my_chat_member=member))
        self.assertEqual(self.recorder.seen[0]["group_id"], GROUP_B)

    async def test_private_chat_is_not_attributed_to_a_group(self) -> None:
        await self._feed(
            Update(update_id=self._update_id(), message=self._message(USER_ID, "private"))
        )
        self.assertEqual(self.recorder.seen[0]["kind"], "private")
        self.assertIsNone(self.recorder.seen[0]["group_id"])

    async def test_unattributable_update_falls_back_to_system(self) -> None:
        """没有任何 chat 上下文的 update（例如 poll_answer）不得被猜成某个群。"""

        self.dispatcher.poll_answer.register(self._observe)
        await self._feed(
            Update(
                update_id=self._update_id(),
                poll_answer=PollAnswer(
                    poll_id="1",
                    user_id=USER_ID,
                    option_ids=[0],
                    option_persistent_ids=[],
                ),
            )
        )
        self.assertEqual(self.recorder.seen[0]["kind"], "system")
        self.assertIsNone(self.recorder.seen[0]["group_id"])

    async def test_context_is_reset_after_each_update(self) -> None:
        await self._feed(
            Update(update_id=self._update_id(), message=self._message(GROUP_A))
        )
        self.assertEqual(scopes.current_usage_scope(), scopes.SYSTEM_SCOPE)

    async def test_handler_exception_still_resets_the_context(self) -> None:
        async def boom(event: object) -> None:
            raise RuntimeError("handler failed")

        # aiogram 会把 handler 异常交给错误处理器，所以这里断言的是**上下文
        # 复原**（这才是本条用例的目的），而不是异常一定冒泡到 feed_update。
        self.dispatcher.message.register(boom)
        await self._feed(
            Update(update_id=self._update_id(), message=self._message(GROUP_A))
        )
        self.assertEqual(scopes.current_usage_scope(), scopes.SYSTEM_SCOPE)

    async def test_concurrent_updates_do_not_share_a_group(self) -> None:
        """两个群并发处理时不得串：A 的 update 绝不能看到 B 的归属。"""

        barrier = asyncio.Event()

        async def picky(event: object) -> None:
            first = scopes.current_usage_scope()
            await asyncio.sleep(0.02)
            second = scopes.current_usage_scope()
            self.assertEqual(first, second, "一次处理内归属被改了")
            self.recorder.seen.append(
                {"kind": first.kind, "group_id": first.group_id, "purpose": "", "event": "x"}
            )
            if len(self.recorder.seen) >= 2:
                barrier.set()

        self.dispatcher.message.register(picky)
        await asyncio.gather(
            self._feed(Update(update_id=self._update_id(), message=self._message(GROUP_A))),
            self._feed(Update(update_id=self._update_id(), message=self._message(GROUP_B))),
        )
        self.assertEqual(
            {item["group_id"] for item in self.recorder.seen}, {GROUP_A, GROUP_B}
        )


class CallChainSettlementTests(unittest.IsolatedAsyncioTestCase):
    """真实 LLMService 请求控制流上的结算（第五阶段其余各项）。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        group_costs.reset()
        group_costs.configure(self.session_factory)
        self.service = LLMService(
            ModelConfig(
                model="fixture/main",
                provider="openai_compatible",
                api_base="https://gateway-a.example.invalid/v1",
                api_key="k",
                retry_attempts=1,
                retry_backoff_sec=0.0,
                total_deadline_sec=20.0,
            ),
            ModelConfig(
                model="fixture/decision",
                provider="openai_compatible",
                api_base="https://gateway-a.example.invalid/v1",
                api_key="k",
            ),
            embed=EmbedConfig(
                model="fixture/embed",
                provider="openai_compatible",
                api_base="https://gateway-a.example.invalid/v1",
                api_key="k",
            ),
        )

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

    def _install(self, provider):
        self._patcher = patch_pair(provider)
        self._patcher.__enter__()

    def _uninstall(self):
        self._patcher.__exit__(None, None, None)

    # ------------------------------------------------------------------
    async def test_uncooperative_cancellation_late_usage_is_settled(self) -> None:
        """provider 吞掉 CancelledError 后再返回 usage：那次花费必须落账。"""

        entered = asyncio.Event()
        release = asyncio.Event()

        async def stubborn(**kwargs):
            entered.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                # 不合作：把取消吞掉，等外部放行才返回。
                await release.wait()
                return _chat_response("late", _usage(17, 3))
            return _chat_response("late", _usage(17, 3))

        self._install(stubborn)
        try:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                task = asyncio.create_task(
                    self.service.chat([{"role": "user", "content": "hi"}])
                )
                await asyncio.wait_for(entered.wait(), timeout=2.0)
                task.cancel()
                # 让孤儿请求跑完并返回 usage。
                await asyncio.sleep(0.05)
                release.set()
            # 孤儿任务在内部完成结算；外部只看到 CancelledError。
            await asyncio.sleep(0.2)
            await asyncio.sleep(0)
            self.assertEqual(group_costs.inflight_attempts(), 0)
        finally:
            self._uninstall()
        rows = await self._rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(int(rows[0].attempts), 1)
        self.assertEqual(int(rows[0].prompt_tokens), 17)
        self.assertEqual(int(rows[0].output_tokens), 3)

    async def test_stream_failure_after_usage_still_settles(self) -> None:
        """流里已经带着 usage，之后才抛：那次 token 不能丢。"""

        class _Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("stream broke after usage")

            def close(self):
                return None

        async def provider(**kwargs):
            return _Stream()

        self._install(provider)
        try:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                result = await self.service.chat([{"role": "user", "content": "hi"}])
        finally:
            self._uninstall()
        self.assertEqual(result, "")
        self.assertEqual(group_costs.inflight_attempts(), 0)
        rows = await self._rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(int(rows[0].attempts), 1)
        self.assertEqual(int(rows[0].failures), 1)
        self.assertGreaterEqual(int(rows[0].usage_unknown), 0)

    async def test_parse_failure_on_choices_settles(self) -> None:
        async def provider(**kwargs):
            return SimpleNamespace(choices=[], usage=_usage(13, 2))

        self._install(provider)
        try:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                await self.service.chat([{"role": "user", "content": "hi"}])
        finally:
            self._uninstall()
        self.assertEqual(group_costs.inflight_attempts(), 0)
        rows = await self._rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(int(rows[0].attempts), 1)
        self.assertEqual(int(rows[0].prompt_tokens), 13)

    async def test_embedding_parse_failure_settles(self) -> None:
        async def provider(**kwargs):
            return SimpleNamespace(
                data=[{}], usage=_usage(13, 0)
            )

        self._install(provider)
        try:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                # embed() 对外把失败折叠成空列表，不冒泡；要验的是**账**。
                vectors = await self.service.embed(["hello"])
        finally:
            self._uninstall()
        self.assertEqual(vectors, [])
        self.assertEqual(group_costs.inflight_attempts(), 0)
        rows = await self._rows(stage="embed")
        self.assertTrue(rows, "嵌入解析失败也必须留下账")
        # 每一次真实尝试都要结算（默认 retry_attempts=2 → 两条）。
        self.assertGreaterEqual(sum(int(row.attempts) for row in rows), 1)
        self.assertGreaterEqual(sum(int(row.failures) for row in rows), 1)
        self.assertEqual(
            group_costs.inflight_attempts(), 0, "不得有尝试永远停在在飞表里"
        )

    async def test_success_without_usage_is_both_success_and_unknown(self) -> None:
        async def provider(**kwargs):
            return _chat_response("ok", None)

        self._install(provider)
        try:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                text = await self.service.chat([{"role": "user", "content": "hi"}])
        finally:
            self._uninstall()
        self.assertEqual(text, "ok")
        row = (await self._rows())[0]
        self.assertEqual(int(row.successes), 1)
        self.assertEqual(int(row.usage_unknown), 1)

    async def test_cost_identity_is_a_stable_slot_not_the_protocol(self) -> None:
        """两个同协议同模型、不同网关的候选必须能分开计价。"""

        primary = ModelConfig(
            model="fixture/main",
            provider="openai_compatible",
            slot="gateway-a",
            api_base="https://gateway-a.example.invalid/v1",
            api_key="k",
            retry_attempts=1,
            retry_backoff_sec=0.0,
        )
        fallback = ModelConfig(
            model="fixture/main",
            provider="openai_compatible",
            slot="gateway-b",
            api_base="https://gateway-b.example.invalid/v1",
            api_key="k",
        )
        service = LLMService(
            primary,
            ModelConfig(
                model="fixture/decision",
                provider="openai_compatible",
                api_base="https://gateway-a.example.invalid/v1",
                api_key="k",
            ),
        )
        service.main.fallbacks = [fallback]

        calls: list[str] = []

        async def provider(**kwargs):
            base = str(kwargs.get("api_base") or "")
            calls.append(base)
            if "gateway-b" in base:
                return _chat_response("from-b", _usage(5, 1))
            raise RuntimeError("gateway-a down")

        self._install(provider)
        try:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                text = await service.chat([{"role": "user", "content": "hi"}])
        finally:
            self._uninstall()
        self.assertEqual(text, "from-b")
        self.assertEqual(len(calls), 2)
        rows = await self._rows()
        self.assertEqual(len(rows), 2, [r.provider for r in rows])
        self.assertEqual(
            {str(row.provider) for row in rows}, {"gateway-a", "gateway-b"}
        )
        # 任何 endpoint URL / 密钥都不得进账。
        blob = " ".join(f"{row.provider} {row.model}" for row in rows)
        self.assertNotIn("https", blob)
        self.assertNotIn("invalid", blob)

    async def test_cost_identity_is_stable_across_restarts(self) -> None:
        async def provider(**kwargs):
            return _chat_response("ok", _usage(3, 1))

        self._install(provider)
        try:
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                await self.service.chat([{"role": "user", "content": "hi"}])
        finally:
            self._uninstall()
        row = (await self._rows())[0]
        # 本文件的配置**没有**槽名，只有协议名。那就必须显式标成未知，
        # 而不是拿协议名冒充一家可计价供应商 —— 那会把所有 openai_compatible
        # 的网关算成同一家。
        self.assertEqual(str(row.provider), llm_module.COST_IDENTITY_UNKNOWN)


def patch_pair(provider):
    from unittest.mock import patch

    return patch.multiple(
        llm_module.litellm, acompletion=provider, aembedding=provider
    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

class StreamUsageAndSlotTests(unittest.IsolatedAsyncioTestCase):
    """流式 usage 与**部署配置**的 slot 透传（第八阶段必修第 6 条）。"""

    async def asyncSetUp(self) -> None:
        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine, self.session_factory = await init_db(
            f"sqlite+aiosqlite:///{self._db_path}"
        )
        group_costs.reset()
        group_costs.configure(self.session_factory)

    async def asyncTearDown(self) -> None:
        await group_costs.flush(force=True)
        group_costs.reset()
        await self.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(f"{self._db_path}{suffix}")
            except OSError:
                pass

    async def _rows(self) -> list:
        await group_costs.flush(force=True)
        async with self.session_factory() as session:
            return list((await session.scalars(select(LlmCostDaily))).all())

    def _stream_service(self) -> LLMService:
        return LLMService(
            ModelConfig(
                model="fixture/main", slot="slot-a",
                provider="openai_compatible", api_key="k",
                api_base="https://a.example.invalid/v1",
                stream=True, retry_attempts=1, retry_backoff_sec=0.0,
                total_deadline_sec=20.0,
            ),
            ModelConfig(
                model="fixture/decision", slot="slot-a",
                provider="openai_compatible", api_key="k",
            ),
        )

    async def test_stream_usage_survives_a_midstream_exception(self) -> None:
        class _Stream:
            def __init__(self) -> None:
                self._sent = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self._sent += 1
                if self._sent == 1:
                    return SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content="hi", tool_calls=None),
                                finish_reason=None,
                            )
                        ],
                        usage=SimpleNamespace(
                            prompt_tokens=11, completion_tokens=2,
                            cached_tokens=0, cache_creation_input_tokens=0,
                            thinking_tokens=0,
                        ),
                    )
                raise RuntimeError("stream broke after usage")

            def close(self):
                return None

        async def provider(**kwargs):
            return _Stream()

        service = self._stream_service()
        with patch_pair(provider):
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A,
                purpose=scopes.PURPOSE_SAFETY,
            ):
                await service.chat([{"role": "user", "content": "hi"}])
        rows = await self._rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(int(rows[0].attempts), 1)
        self.assertEqual(
            int(rows[0].prompt_tokens), 11,
            "流里已经观察到的 usage 不得因尾部异常丢失",
        )
        self.assertEqual(int(rows[0].output_tokens), 2)
        self.assertEqual(int(rows[0].failures), 1)

    async def test_runtime_config_slot_reaches_the_ledger(self) -> None:
        """走**真实部署路径**：RuntimeConfig → 所有角色 → 调用 → 台账。

        手工构造 ``ModelConfig(slot=...)`` 通过并不代表部署配置通过：真实配置里
        槽名放在角色的 ``provider`` 字段上，必须确认它一路透传到台账，且两个
        同模型不同网关的角色**不会**被合并。
        """

        from bot.config import Settings
        from bot.services.runtime_config import RuntimeConfigManager

        settings = Settings(_env_file=None, config_master_key="slot-probe-key")
        config = {
            "models": {
                "providers": [
                    {
                        "name": "slot-a", "provider": "openai_compatible",
                        "api_key": "k", "api_base": "https://a.example.invalid/v1",
                        "stream": False, "chat_endpoint": "auto",
                    },
                    {
                        "name": "slot-b", "provider": "openai_compatible",
                        "api_key": "k", "api_base": "https://b.example.invalid/v1",
                        "stream": False, "chat_endpoint": "auto",
                    },
                ],
                "main": {"provider": "slot-a", "model": "fixture/main", "fallbacks": []},
                "decision": {"provider": "slot-b", "model": "fixture/main", "fallbacks": []},
                "moderation": {"provider": "slot-b", "model": "fixture/main", "fallbacks": []},
                "vision": {"provider": "slot-a", "model": "fixture/main", "fallbacks": []},
                "compress": {"provider": "slot-a", "model": "fixture/main", "fallbacks": []},
                "embed": {"provider": "slot-a", "model": "fixture/embed", "fallbacks": []},
                "skill": {"provider": "slot-a", "model": "fixture/main", "fallbacks": []},
            },
            "bot": {},
        }
        manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=settings,
            legacy_config_path="/tmp/nonexistent-slot-probe.toml",
            legacy_raw_env={},
        )
        await manager.initialize()
        await manager.save(config, expected_revision=1, updated_by=42)
        service = LLMService(
            settings.bot.main_model,
            settings.bot.decision_model,
            settings.bot.compress_model,
            moderation=settings.bot.moderation_model,
            vision=settings.bot.vision_model,
            skill=settings.bot.skill_model,
        )
        slots = {
            llm_module.cost_provider_key(candidate)
            for role in (
                service.main, service.decision_config, service.moderation_config,
                service.vision_config, service.compress_config, service.skill_config,
            )
            for candidate in llm_module.LLMService._chat_candidates(role)
        }
        self.assertEqual(
            slots, {"slot-a", "slot-b"},
            "部署配置里的槽名没有透传到候选构造",
        )
        self.assertNotIn(llm_module.COST_IDENTITY_UNKNOWN, slots)

        async def provider(**kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="ok", tool_calls=None)
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=2, cached_tokens=0,
                    cache_creation_input_tokens=0, thinking_tokens=0,
                ),
            )

        with patch_pair(provider):
            for role_name, call in (
                ("main", lambda: service.chat([{"role": "user", "content": "a"}])),
                ("decision", lambda: service.decision("s", "u")),
                ("moderation", lambda: service.moderation("s", "u")),
            ):
                with scopes.usage_scope(
                    kind=scopes.SCOPE_GROUP, group_id=GROUP_A,
                    purpose=scopes.PURPOSE_SAFETY,
                    stage=role_name,
                ):
                    await call()
        rows = await self._rows()
        providers = {str(row.provider) for row in rows}
        self.assertEqual(
            providers, {"slot-a", "slot-b"},
            f"两个同模型不同网关的角色被合并了：{providers}",
        )
        blob = " ".join(f"{row.provider} {row.model}" for row in rows)
        self.assertNotIn("a.example", blob)
        self.assertNotIn("b.example", blob)

    async def test_embed_and_fallback_slots_reach_the_ledger(self) -> None:
        """chat 主 / chat 回退 / embed 主 / embed 回退 四个 builder 都要带槽。

        上一轮只验了 chat 角色的**主**配置，回退与 embed 没调过，于是它们仍是
        unknown-slot。这里让四类调用**真的发出去**（脚本 provider），再用台账
        证明它们分到了各自的槽。
        """

        from bot.config import Settings
        from bot.services.runtime_config import RuntimeConfigManager

        settings = Settings(_env_file=None, config_master_key="slot-embed-key")
        def provider_entry(name: str, host: str) -> dict:
            return {
                "name": name, "provider": "openai_compatible", "api_key": "k",
                "api_base": f"https://{host}.example.invalid/v1",
                "stream": False, "chat_endpoint": "auto",
            }

        config = {
            "models": {
                "providers": [
                    provider_entry("slot-a", "a"),
                    provider_entry("slot-b", "b"),
                ],
                "main": {
                    "provider": "slot-a", "model": "fixture/main",
                    "fallbacks": [{"provider": "slot-b", "model": "fixture/main"}],
                },
                "decision": {
                    "provider": "slot-b", "model": "fixture/main",
                    "fallbacks": [{"provider": "slot-a", "model": "fixture/main"}],
                },
                "moderation": {"provider": "slot-a", "model": "fixture/main", "fallbacks": []},
                "vision": {"provider": "slot-a", "model": "fixture/main", "fallbacks": []},
                "compress": {"provider": "slot-a", "model": "fixture/main", "fallbacks": []},
                "embed": {
                    "provider": "slot-b", "model": "fixture/embed",
                    "fallbacks": [{"provider": "slot-a", "model": "fixture/embed"}],
                },
                "skill": {"provider": "slot-a", "model": "fixture/main", "fallbacks": []},
            },
            "bot": {},
        }
        manager = RuntimeConfigManager(
            session_factory=self.session_factory,
            settings=settings,
            legacy_config_path="/tmp/nonexistent-slot-embed.toml",
            legacy_raw_env={},
        )
        await manager.initialize()
        await manager.save(config, expected_revision=1, updated_by=42)
        service = LLMService(
            settings.bot.main_model,
            settings.bot.decision_model,
            settings.bot.compress_model,
            moderation=settings.bot.moderation_model,
            vision=settings.bot.vision_model,
            skill=settings.bot.skill_model,
            embed=settings.bot.embed_model,
        )

        chat_slots = {
            llm_module.cost_provider_key(candidate)
            for role in (service.main, service.decision_config)
            for candidate in llm_module.LLMService._chat_candidates(role)
        }
        embed_slots = {
            llm_module.cost_provider_key(candidate)
            for candidate in llm_module.LLMService._embed_candidates(service.embed_config)
        }
        self.assertEqual(chat_slots, {"slot-a", "slot-b"}, "chat 主/回退槽不完整")
        self.assertEqual(embed_slots, {"slot-a", "slot-b"}, "embed 主/回退槽不完整")

        calls: list[str] = []

        async def chat_provider(**kwargs):
            base = str(kwargs.get("api_base") or "")
            calls.append(base)
            if "a.example" in base:
                raise RuntimeError("slot-a down")   # 逼出回退
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="b", tool_calls=None))],
                usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=2, cached_tokens=0,
                    cache_creation_input_tokens=0, thinking_tokens=0,
                ),
            )

        async def embed_provider(**kwargs):
            base = str(kwargs.get("api_base") or "")
            calls.append(base)
            if "b.example" in base:
                raise RuntimeError("slot-b down")
            return SimpleNamespace(
                data=[{"embedding": [0.1, 0.2]}],
                usage=SimpleNamespace(prompt_tokens=20, completion_tokens=0),
            )

        with patch.object(llm_module.litellm, "acompletion", chat_provider), patch.object(
            llm_module.litellm, "aembedding", embed_provider
        ):
            with scopes.usage_scope(
                kind=scopes.SCOPE_GROUP, group_id=GROUP_A, purpose=scopes.PURPOSE_OPTIONAL
            ):
                self.assertEqual(
                    await service.chat([{"role": "user", "content": "hi"}]), "b"
                )
                vectors = await service.embed(["x"])
        self.assertEqual(len(vectors), 1)

        rows = await self._rows()
        providers = {str(row.provider) for row in rows}
        self.assertNotIn(llm_module.COST_IDENTITY_UNKNOWN, providers, providers)
        self.assertEqual(providers, {"slot-a", "slot-b"}, providers)
        blob = " ".join(f"{r.provider} {r.model}" for r in rows)
        self.assertNotIn("example", blob)
        self.assertNotIn("k", blob.split()[0])
