"""``/selfcheck`` 离线回归：真实 Router + 真 Dispatcher + 真 Update。

这份文件测的是"会不会伤到别的东西"，不是"函数能跑"：

- **权限**：只有真实 ``settings.super_admin_id`` 能在**私聊**读到详情；未配置、匿名、
  普通用户、机器人一律固定拒绝语，正文自称超管不算数（正文从不参与鉴权）。
- **不泄露**：群聊只说"仅私聊可用"；provider 名称、``error`` 文本、路径、配置值、
  任意异常内容都不出现在回复里；``BUILD_REVISION`` 必须过白名单，否则"未标注"。
- **不越界**：只发 ``SendMessage``
  （连 ``SendVoice``/``GetChat`` 都会让假 session 抛错），
  组件入口不自行开库、不碰模型/搜索/TTS；真实 DB 中间件另测会话与业务零 DML。
- **不吞消息**：非命令聊天、旧 ``/health``、别的 bot 的 ``@mention``、别的命令都
  不会被这个新 router 吃掉。
- **采样异常不崩**：provider 抛异常/返回垃圾 → "状态未知/采样失败"，命令照常回一句，
  不冒充健康、不让 dispatcher 报错；``CancelledError`` 必须原样传播。
"""

from __future__ import annotations

import asyncio
import pathlib
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, Update, User

from bot.db.models import Base, PrivateChatMessage, PrivateChatUsage
from bot.handlers import self_check as handler_module
from bot.middlewares.db import DbSessionMiddleware
from bot.utils import telegram as telegram_helpers
from bot.services import self_check as self_check_service
from bot.services.context_observability import reset_context_observations
from bot.services.resource_health import resource_health_snapshot
from bot.services.tool_observability import reset as reset_tool_observations

BOT_TOKEN = "42:TEST_SELFCHECK"
BOT_USERNAME = "SmartBotProbe"
BOT_ID = 424242
SUPER_ADMIN = 900001
ORDINARY_USER = 900002
GROUP_ID = -1000000000003
EPOCH = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# 假 Telegram：只允许 GetMe / SendMessage，其它任何 API 调用直接炸
# --------------------------------------------------------------------------- #
class RecordingTelegramSession(BaseSession):
    """把 Telegram 拦在本地。别的 API 一律 ``AssertionError``。

    这样"有没有偷偷发语音 / 查群 / 调外部接口"就不是靠人肉 review，而是硬断言。
    """

    ALLOWED = frozenset({"GetMe", "SendMessage"})

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self._next_message_id = 900000

    async def close(self) -> None:  # pragma: no cover - 无网络
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover - 不会被用到
        if False:
            yield b""

    async def make_request(self, bot, method, timeout=None):  # type: ignore[override]
        name = type(method).__name__
        if name not in self.ALLOWED:
            raise AssertionError(f"unexpected Telegram API call: {name}")
        payload = method.model_dump(warnings=False)
        self.calls.append((name, payload))
        if name == "GetMe":
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username=BOT_USERNAME)
        self._next_message_id += 1
        chat_id = int(payload["chat_id"])
        return Message(
            message_id=self._next_message_id,
            date=EPOCH,
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
            text=str(payload.get("text") or ""),
        ).as_(bot)

    def method_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def texts(self) -> list[str]:
        return [
            str(payload.get("text") or "")
            for name, payload in self.calls
            if name == "SendMessage"
        ]

    def joined(self) -> str:
        return "\n".join(self.texts())


def _settings(super_admin_id: int | None = SUPER_ADMIN) -> SimpleNamespace:
    return SimpleNamespace(super_admin_id=super_admin_id, bot=SimpleNamespace())


def _update(
    text: str,
    *,
    user_id: int | None = SUPER_ADMIN,
    chat_type: str = "private",
    chat_id: int | None = None,
) -> Update:
    resolved_chat_id = int(chat_id if chat_id is not None else user_id or 0)
    return Update(
        update_id=1,
        message=Message(
            message_id=1001,
            date=EPOCH,
            chat=Chat(id=resolved_chat_id, type=str(chat_type)),
            from_user=(
                None
                if user_id is None
                else User(id=int(user_id), is_bot=False, first_name="Member")
            ),
            text=str(text),
        ),
    )


# --------------------------------------------------------------------------- #
# 真实 Dispatcher 夹具
# --------------------------------------------------------------------------- #
class SelfCheckCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self_check_service.reset_cache()
        reset_tool_observations()
        reset_context_observations()
        self.errors: list[str] = []
        self.session_factory_calls = 0
        self.provider_calls = 0

    async def asyncTearDown(self) -> None:
        # 分发期间的未处理异常会被 errors 处理器吞成"什么都没发"，
        # 那样负断言会假绿 —— 所以这里必须为空。
        self.assertEqual(self.errors, [], "feed_update 期间出现了未处理异常")
        self.assertEqual(
            self.session_factory_calls, 0, "裸 router 组件不应自行打开数据库会话"
        )
        self_check_service.reset_cache()

    def _make_dispatcher(self, settings=None) -> Dispatcher:
        dispatcher = Dispatcher()

        def _session_factory():
            self.session_factory_calls += 1
            raise AssertionError("self check must not open a database session")

        # 产品 router 是模块级单例，aiogram 的 include_router 是单亲的：
        # 每个测试挂载前先解除上一次的父子关系（不改 router 内容）。
        handler_module.router._parent_router = None  # noqa: SLF001 - 测试夹具
        dispatcher.include_router(handler_module.router)
        dispatcher["settings"] = settings or _settings()
        dispatcher["session_factory"] = _session_factory

        async def _record_error(event) -> None:
            self.errors.append(f"{type(event.exception).__name__}: {event.exception}")

        dispatcher.errors.register(_record_error)
        return dispatcher

    async def _feed(
        self,
        text: str,
        *,
        user_id: int | None = SUPER_ADMIN,
        chat_type: str = "private",
        chat_id: int | None = None,
        settings=None,
    ) -> RecordingTelegramSession:
        session = RecordingTelegramSession()
        bot = Bot(token=BOT_TOKEN, session=session)
        dispatcher = self._make_dispatcher(settings)
        await dispatcher.feed_update(
            bot,
            _update(text, user_id=user_id, chat_type=chat_type, chat_id=chat_id),
        )
        return session

    # -- 采样桩 ---------------------------------------------------------- #
    def _stub(self, snapshot: dict) -> None:
        def _provider():
            self.provider_calls += 1
            return snapshot

        patcher = patch.object(
            self_check_service, "resource_health_snapshot", _provider
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _failing_stub(self, error: BaseException) -> None:
        def _provider():
            self.provider_calls += 1
            raise error

        patcher = patch.object(
            self_check_service, "resource_health_snapshot", _provider
        )
        patcher.start()
        self.addCleanup(patcher.stop)


def _healthy_snapshot() -> dict:
    return {
        "ok": True,
        "fatal": False,
        "system": {
            "ok": True,
            "rss_bytes": 300 * 1024 * 1024,
            "swap_bytes": 0,
            "threads": 42,
            "fd_count": 128,
            "fd_soft_limit": 4096,
            "cgroup_memory_ratio": 0.61,
            "cgroup_pids_ratio": 0.02,
            "event_loop_lag_seconds": 0.01,
            "max_event_loop_lag_seconds": 0.03,
            "data_disk": {
                "path": "data",
                "free_bytes": 41 * 1024 * 1024 * 1024,
                "total_bytes": 200 * 1024 * 1024 * 1024,
                "free_ratio": 0.2,
            },
        },
        "resources": {
            "queue": {"ok": True},
            "worker": {"ok": True},
        },
    }


# --------------------------------------------------------------------------- #
# 1. 超管私聊：正常读到白名单详情
# --------------------------------------------------------------------------- #
class SuperAdminPrivateTests(SelfCheckCase):
    async def test_super_admin_private_gets_whitelisted_report(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed("/selfcheck")

        texts = session.texts()
        self.assertEqual(len(texts), 1, session.method_names())
        body = texts[0]
        self.assertIn("自检（本地只读快照）", body)
        self.assertIn("Asia/Shanghai", body)
        self.assertIn("运行时长", body)
        # 计时起点必须写明，不能冒充容器/服务启动时间
        self.assertIn("非容器/服务启动时间", body)
        self.assertIn("版本", body)
        self.assertIn("总体：本地健康", body)
        self.assertIn("组件：已注册 2 个 · 异常 0 个", body)
        # 安全资源数值
        self.assertIn("RSS 300 MiB", body)
        self.assertIn("线程 42", body)
        self.assertIn("fd 128/4096", body)
        self.assertIn("数据盘剩余", body)
        # 已接入真实观测源；本用例重置后暂无事件，不能冒充成功或健康。
        for scope in ("group_skill", "private_search", "private_tts"):
            self.assertIn(f"{scope}：暂无观测/未知", body)
        self.assertIn("上下文：暂无观测/未知", body)
        self.assertIn("工具观测（本进程", body)
        self.assertIn("上下文装配（全局", body)
        # 免责声明
        self.assertIn(self_check_service.DISCLAIMER, body)
        # 只发了 SendMessage
        self.assertEqual(
            [n for n in session.method_names() if n != "GetMe"], ["SendMessage"]
        )

    async def test_super_admin_mention_suffix_is_accepted(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed(f"/selfcheck@{BOT_USERNAME}")
        self.assertEqual(len(session.texts()), 1, session.method_names())
        self.assertIn("总体：", session.joined())

    async def test_command_with_arguments_still_handled(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed("/selfcheck now")
        self.assertEqual(len(session.texts()), 1, session.method_names())

    async def test_other_bot_mention_is_not_handled(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed("/selfcheck@SomeOtherBot")
        self.assertEqual(session.texts(), [], session.method_names())
        self.assertEqual(self.provider_calls, 0)

    async def test_report_is_built_from_the_real_process_snapshot(self) -> None:
        """: 真 provider（空 provider 表）也能跑通，不依赖网络/DB。"""

        with patch.dict("bot.services.resource_health._PROVIDERS", {}, clear=True):
            self_check_service.reset_cache()
            session = await self._feed("/selfcheck")

        self.assertEqual(len(session.texts()), 1, session.method_names())
        body = session.joined()
        self.assertIn("总体：", body)
        self.assertIn("组件：已注册 0 个", body)
        self.assertEqual(session.method_names().count("SendMessage"), 1)


# --------------------------------------------------------------------------- #
# 2. 拒绝：普通用户 / 未配置 / 匿名 / 机器人 / 正文自称超管
# --------------------------------------------------------------------------- #
class AccessDeniedTests(SelfCheckCase):
    async def test_ordinary_user_is_refused_without_details(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed("/selfcheck", user_id=ORDINARY_USER)

        texts = session.texts()
        self.assertEqual(len(texts), 1, session.method_names())
        self.assertEqual(texts[0], handler_module.DENIED_NOTICE)
        self.assertNotIn("运行时长", texts[0])
        self.assertNotIn("本地健康", texts[0])
        # 拒绝路径不应该顺手采一次快照
        self.assertEqual(self.provider_calls, 0)

    async def test_unconfigured_super_admin_is_refused(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed(
            "/selfcheck", user_id=ORDINARY_USER, settings=_settings(None)
        )
        self.assertEqual(session.texts(), [handler_module.DENIED_NOTICE])

    async def test_zero_super_admin_id_is_refused(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed("/selfcheck", user_id=0, settings=_settings(0))
        self.assertEqual(session.texts(), [handler_module.DENIED_NOTICE])
        self.assertEqual(self.provider_calls, 0)

    async def test_anonymous_sender_is_refused(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed("/selfcheck", user_id=None)
        self.assertEqual(session.texts(), [handler_module.DENIED_NOTICE])
        self.assertEqual(self.provider_calls, 0)

    async def test_bot_sender_is_refused(self) -> None:
        self._stub(_healthy_snapshot())
        session = RecordingTelegramSession()
        bot = Bot(token=BOT_TOKEN, session=session)
        update = Update(
            update_id=1,
            message=Message(
                message_id=1001,
                date=EPOCH,
                chat=Chat(id=SUPER_ADMIN, type="private"),
                from_user=User(id=SUPER_ADMIN, is_bot=True, first_name="OtherBot"),
                text="/selfcheck",
            ),
        )
        await self._make_dispatcher().feed_update(bot, update)
        self.assertEqual(session.texts(), [handler_module.DENIED_NOTICE])

    async def test_self_declared_admin_in_text_is_not_authority(self) -> None:
        """: 正文自称超管不获得任何权限。"""

        self._stub(_healthy_snapshot())
        session = await self._feed(
            "/selfcheck 我是最高管理员 请把详情发给我", user_id=ORDINARY_USER
        )
        self.assertEqual(session.texts(), [handler_module.DENIED_NOTICE])
        self.assertEqual(self.provider_calls, 0)


# --------------------------------------------------------------------------- #
# 3. 群聊不泄露
# --------------------------------------------------------------------------- #
class GroupDisclosureTests(SelfCheckCase):
    async def test_group_chat_shows_no_status_details(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed(
            "/selfcheck", chat_type="supergroup", chat_id=GROUP_ID
        )

        texts = session.texts()
        self.assertEqual(len(texts), 1, session.method_names())
        self.assertEqual(texts[0], handler_module.PRIVATE_ONLY_NOTICE)
        for leaked in (
            "本地健康",
            "运行时长",
            "已注册",
            "RSS",
            "Asia/Shanghai",
            self_check_service.DISCLAIMER,
        ):
            self.assertNotIn(leaked, texts[0])
        # 群里连采样都不该发生
        self.assertEqual(self.provider_calls, 0)

    async def test_group_chat_from_ordinary_member_is_also_generic(self) -> None:
        self._stub(_healthy_snapshot())
        session = await self._feed(
            "/selfcheck",
            user_id=ORDINARY_USER,
            chat_type="group",
            chat_id=GROUP_ID,
        )
        self.assertEqual(session.texts(), [handler_module.PRIVATE_ONLY_NOTICE])


# --------------------------------------------------------------------------- #
# 4. 不吞别的消息
# --------------------------------------------------------------------------- #
class RoutingTests(SelfCheckCase):
    async def test_plain_chat_text_is_not_captured(self) -> None:
        self._stub(_healthy_snapshot())
        for text in ("自检一下", "selfcheck", "帮我做个体检吧", "/healthz"):
            session = await self._feed(text, user_id=SUPER_ADMIN)
            self.assertEqual(session.texts(), [], (text, session.method_names()))
        self.assertEqual(self.provider_calls, 0)

    async def test_legacy_health_command_is_not_captured(self) -> None:
        """: 旧 ``/health`` 不该被新 router 吃掉（也不该在这里回任何东西）。"""

        self._stub(_healthy_snapshot())
        session = await self._feed("/health", chat_type="supergroup", chat_id=GROUP_ID)
        self.assertEqual(session.texts(), [], session.method_names())
        self.assertEqual(self.provider_calls, 0)

    async def test_lookalike_commands_are_not_captured(self) -> None:
        self._stub(_healthy_snapshot())
        for text in ("/selfchecker", "/self_checks", "/selfcheck2"):
            session = await self._feed(text)
            self.assertEqual(session.texts(), [], (text, session.method_names()))
        self.assertEqual(self.provider_calls, 0)

    def test_router_is_included_before_commands_in_main(self) -> None:
        """: 接线断言 —— 产品入口必须在 commands 之前挂这个 router。"""

        source = pathlib.Path("bot/__main__.py").read_text(encoding="utf-8")
        self.assertIn("dp.include_router(self_check.router)", source)
        self.assertLess(
            source.index("dp.include_router(self_check.router)"),
            source.index("dp.include_router(commands.router)"),
            "self_check.router 必须注册在 commands.router 之前",
        )

    def test_handler_module_exposes_a_real_router(self) -> None:
        self.assertIsInstance(handler_module.router, Router)
        self.assertEqual(handler_module.router.name, "self_check")
        self.assertTrue(callable(handler_module.cmd_selfcheck))
        self.assertTrue(callable(handler_module.build_self_check_message))


# --------------------------------------------------------------------------- #
# 5. 采样异常 / 恶意载荷
# --------------------------------------------------------------------------- #
class SamplingRobustnessTests(SelfCheckCase):
    async def test_provider_exception_reports_unknown_status_without_leaking(
        self,
    ) -> None:
        self._failing_stub(
            RuntimeError("boom /etc/secret/token=abc at provider 'evil_queue'")
        )
        session = await self._feed("/selfcheck")

        texts = session.texts()
        self.assertEqual(len(texts), 1, session.method_names())
        body = texts[0]
        self.assertIn(self_check_service.SAMPLING_FAILED_STATUS, body)
        self.assertNotIn("本地健康", body)
        self.assertNotIn("本地异常", body)
        self.assertNotIn("组件：已注册", body)
        for leaked in (
            "boom",
            "/etc/secret",
            "token=abc",
            "evil_queue",
            "RuntimeError",
        ):
            self.assertNotIn(leaked, body)
        # 免责声明仍在：即使采样失败，也不让读者以为这是外部探测结果
        self.assertIn(self_check_service.DISCLAIMER, body)

    async def test_non_mapping_snapshot_reports_unknown_status(self) -> None:
        with patch.object(
            self_check_service, "resource_health_snapshot", lambda: ["not", "a", "map"]
        ):
            session = await self._feed("/selfcheck")

        body = session.joined()
        self.assertIn(self_check_service.SAMPLING_FAILED_STATUS, body)
        self.assertNotIn("本地健康", body)

    async def test_malicious_provider_payload_is_not_echoed(self) -> None:
        snapshot = {
            "ok": True,
            "fatal": False,
            "system": {
                "ok": True,
                "rss_bytes": 1024 * 1024,
                "threads": 7,
                "fd_count": 3,
                "data_disk": {
                    "path": "/srv/secret-mount",
                    "free_bytes": 12 * 1024 * 1024 * 1024,
                    "free_ratio": 0.5,
                },
                "secret_env": "OPENAI_API_KEY=sk-should-not-leak",
            },
            "resources": {
                "evil_provider_name": {
                    "ok": False,
                    "error": "SECRET /etc/passwd boom",
                },
            },
        }
        with patch.object(
            self_check_service, "resource_health_snapshot", lambda: snapshot
        ):
            session = await self._feed("/selfcheck")

        body = session.joined()
        for leaked in (
            "SECRET",
            "/etc/passwd",
            "evil_provider_name",
            "OPENAI_API_KEY",
            "sk-should-not-leak",
            "/srv/secret-mount",
        ):
            self.assertNotIn(leaked, body)
        # 但"有 1 个组件异常"这件事必须还在，只是不点名
        self.assertIn("组件：已注册 1 个 · 异常 1 个", body)
        self.assertIn("总体：本地异常", body)

    def test_build_revision_whitelist(self) -> None:
        self.assertEqual(
            self_check_service.safe_build_revision({"BUILD_REVISION": "v1.2.3-rc1"}),
            "v1.2.3-rc1",
        )
        self.assertEqual(
            self_check_service.safe_build_revision({"APP_VERSION": "2026.10.30"}),
            "2026.10.30",
        )
        # 非法/可疑值一律"未标注"，并回落到下一个白名单字段
        self.assertEqual(
            self_check_service.safe_build_revision(
                {"BUILD_REVISION": "<script>alert(1)</script>"}
            ),
            self_check_service.VERSION_UNKNOWN,
        )
        self.assertEqual(
            self_check_service.safe_build_revision({"BUILD_REVISION": "/etc/passwd"}),
            self_check_service.VERSION_UNKNOWN,
        )
        self.assertEqual(
            self_check_service.safe_build_revision({"BUILD_REVISION": "x" * 200}),
            self_check_service.VERSION_UNKNOWN,
        )
        self.assertEqual(
            self_check_service.safe_build_revision(
                {"BUILD_REVISION": "bad value", "APP_VERSION": "v9"}
            ),
            "v9",
        )
        self.assertEqual(
            self_check_service.safe_build_revision({}),
            self_check_service.VERSION_UNKNOWN,
        )

    def test_malicious_build_revision_env_is_not_echoed(self) -> None:
        report = self_check_service.build_report(
            snapshot_provider=_healthy_snapshot,
            environ={"BUILD_REVISION": "9<script>/etc/shadow"},
        )
        text = self_check_service.render_self_check_text(report)
        self.assertIn(self_check_service.VERSION_UNKNOWN, text)
        self.assertNotIn("script", text)
        self.assertNotIn("/etc/shadow", text)

    def test_report_never_contains_scary_values_verbatim(self) -> None:
        """: 时间戳仅接受固定格式，恶意 HTML 不以原文或转义形式回显。"""

        report = self_check_service.build_report(snapshot_provider=_healthy_snapshot)
        tampered = dict(report)
        tampered["checked_at"] = "<b>2026-10-30</b>"
        text = self_check_service.render_self_check_text(tampered)
        self.assertIn("时间 未知（Asia/Shanghai）", text)
        self.assertNotIn("<b>2026-10-30</b>", text)
        self.assertNotIn("&lt;b&gt;2026-10-30&lt;/b&gt;", text)


# --------------------------------------------------------------------------- #
# 6. 只读纯度：不开库、不碰模型/TTS/搜索
# --------------------------------------------------------------------------- #
class ReadOnlyPurityTests(SelfCheckCase):
    async def test_only_send_message_reaches_telegram(self) -> None:
        """: 假 session 会对任何非白名单 API 抛错，这里再显式断言一次。"""

        self._stub(_healthy_snapshot())
        session = await self._feed("/selfcheck")
        self.assertEqual(
            [name for name in session.method_names() if name != "GetMe"],
            ["SendMessage"],
        )

    def test_handler_and_service_import_no_model_tts_or_search_boundary(self) -> None:
        banned = (
            "LLMService",
            "answer_with_search",
            "DoubaoTTSService",
            "deliver_private_reply",
            "av_search",
        )
        for module in (handler_module, self_check_service):
            for name in banned:
                self.assertFalse(
                    hasattr(module, name),
                    f"{module.__name__} 不应持有 {name}",
                )


# --------------------------------------------------------------------------- #
# 7. 节流/缓存有界
# --------------------------------------------------------------------------- #
class ThrottleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self_check_service.reset_cache()

    def tearDown(self) -> None:
        self_check_service.reset_cache()

    def test_repeated_calls_within_ttl_use_a_single_snapshot(self) -> None:
        calls: list[int] = []

        def provider():
            calls.append(1)
            return _healthy_snapshot()

        clock = [1000.0]

        def monotonic():
            return clock[0]

        first = self_check_service.self_check_report(
            snapshot_provider=provider,
            monotonic=monotonic,
            use_cache=True,
        )
        clock[0] += 1.0
        second = self_check_service.self_check_report(
            snapshot_provider=provider,
            monotonic=monotonic,
            use_cache=True,
        )
        self.assertEqual(len(calls), 1, "TTL 内重复调用不应重复采样")
        self.assertEqual(first["build_revision"], second["build_revision"])
        self.assertIsNot(first, second, "缓存命中要给出独立的 dict 副本")

    def test_cache_expires_after_ttl(self) -> None:
        calls: list[int] = []

        def provider():
            calls.append(1)
            return _healthy_snapshot()

        clock = [1000.0]

        def monotonic():
            return clock[0]

        self_check_service.self_check_report(
            snapshot_provider=provider, monotonic=monotonic, use_cache=True
        )
        clock[0] += self_check_service.CACHE_TTL_SECONDS + 0.5
        self_check_service.self_check_report(
            snapshot_provider=provider, monotonic=monotonic, use_cache=True
        )
        self.assertEqual(len(calls), 2, "过期后必须重新采样")

    def test_cache_is_a_bounded_single_slot(self) -> None:
        def provider():
            return _healthy_snapshot()

        clock = [1000.0]
        for _ in range(50):
            clock[0] += self_check_service.CACHE_TTL_SECONDS + 1.0
            self_check_service.self_check_report(
                snapshot_provider=provider,
                monotonic=lambda: clock[0],
                use_cache=True,
            )
        # 单槽缓存：键永远不会随调用次数增长
        self.assertLessEqual(len(self_check_service._CACHE), 2)
        self.assertEqual(set(self_check_service._CACHE), {"monotonic", "report"})

    def test_reason_is_documented_constant(self) -> None:
        self.assertEqual(self_check_service.NOT_OBSERVED_REASON, "本轮未接入/暂无观测")


# --------------------------------------------------------------------------- #
# 8. 取消语义
# --------------------------------------------------------------------------- #
class CancellationTests(SelfCheckCase):
    def test_cancelled_error_propagates_from_service(self) -> None:
        def provider():
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            self_check_service.build_report(snapshot_provider=provider)

    async def test_cancelled_error_propagates_through_the_handler(self) -> None:
        """: 取消不能被 `except Exception`/`BaseException` 吞成"自检完成"。"""

        def provider():
            raise asyncio.CancelledError()

        session = RecordingTelegramSession()
        bot = Bot(token=BOT_TOKEN, session=session)
        dispatcher = self._make_dispatcher()
        with patch.object(self_check_service, "resource_health_snapshot", provider):
            with self.assertRaises(asyncio.CancelledError):
                await dispatcher.feed_update(bot, _update("/selfcheck"))
        self.assertEqual(session.texts(), [], "取消时不应发出任何消息")

    def test_no_base_exception_swallowing_in_source(self) -> None:
        source = pathlib.Path("bot/services/self_check.py").read_text(encoding="utf-8")
        self.assertNotIn("except BaseException", source)
        self.assertNotIn("except (Exception, BaseException)", source)


class RetentionTelegramSession(RecordingTelegramSession):
    """允许记录真实 helper 的按钮编辑，以便正控能证明探针有效。"""

    async def make_request(self, bot, method, timeout=None):
        if type(method).__name__ == "EditMessageReplyMarkup":
            self.calls.append(
                (type(method).__name__, method.model_dump(warnings=False))
            )
            return True
        return await super().make_request(bot, method, timeout)


class CountingScheduler:
    """安装到生产 scheduler seam；不替换 helper 或 scheduling 函数。"""

    def __init__(self):
        self.durable_calls = []

    async def enqueue_durable(self, **kwargs):
        self.durable_calls.append(kwargs)
        return True


def _retention_settings(mode):
    settings = _settings()
    settings.bot = SimpleNamespace(
        auto_delete_categories=[] if mode == "off" else ["management"],
        auto_delete_category_mode={"management": mode},
        auto_delete_category_seconds={"management": 90},
        auto_delete_seconds=90,
    )
    return settings


async def _drain_new_helper_tasks(previous):
    tasks = telegram_helpers._TELEGRAM_BACKGROUND_TASKS - previous
    if tasks:
        await asyncio.gather(*tasks)


class RetentionReadOnlyTests(SelfCheckCase):
    async def _check_mode(self, mode, expected_seconds):
        settings = _retention_settings(mode)
        # 确认测试配置真正生效，不能把空 settings.bot 当作 timer/button 负控。
        self.assertEqual(
            telegram_helpers.configured_auto_delete_seconds(settings, "management"),
            expected_seconds,
        )
        self._stub(_healthy_snapshot())
        scheduler = CountingScheduler()
        previous_scheduler = telegram_helpers._TELEGRAM_CLEANUP_SCHEDULER
        previous_tasks = set(telegram_helpers._TELEGRAM_BACKGROUND_TASKS)
        telegram_helpers.configure_telegram_cleanup_scheduler(scheduler)
        try:
            session = RetentionTelegramSession()
            bot = Bot(token=BOT_TOKEN, session=session)
            dispatcher = self._make_dispatcher(settings)
            for update in (
                _update("/selfcheck"),
                _update("/selfcheck", user_id=ORDINARY_USER),
                _update("/selfcheck", chat_type="supergroup", chat_id=GROUP_ID),
            ):
                await dispatcher.feed_update(bot, update)
            await _drain_new_helper_tasks(previous_tasks)
            self.assertEqual(session.method_names(), ["SendMessage"] * 3)
            self.assertEqual(scheduler.durable_calls, [])
            self.assertTrue(
                all(p.get("reply_markup") is None for _, p in session.calls)
            )
            self.assertEqual(
                telegram_helpers._TELEGRAM_BACKGROUND_TASKS - previous_tasks, set()
            )
        finally:
            telegram_helpers.configure_telegram_cleanup_scheduler(previous_scheduler)

    async def test_management_timer_does_not_enqueue_or_edit(self):
        await self._check_mode("timer", 90)

    async def test_management_button_does_not_enqueue_or_edit(self):
        await self._check_mode("button", telegram_helpers.AUTO_DELETE_BUTTON_SENTINEL)

    async def test_management_off_does_not_enqueue_or_edit(self):
        await self._check_mode("off", 0)

    async def test_real_helper_positive_controls_exercise_scheduler_and_button(self):
        """直接调用同一真实 helper：timer 写 seam、button 发编辑；探针非假绿。"""
        scheduler = CountingScheduler()
        previous_scheduler = telegram_helpers._TELEGRAM_CLEANUP_SCHEDULER
        telegram_helpers.configure_telegram_cleanup_scheduler(scheduler)
        try:
            for mode, expected_methods, expected_enqueues in (
                ("timer", ["SendMessage"], 1),
                ("button", ["SendMessage", "EditMessageReplyMarkup"], 0),
                ("off", ["SendMessage"], 0),
            ):
                with self.subTest(mode=mode):
                    scheduler.durable_calls.clear()
                    session = RetentionTelegramSession()
                    bot = Bot(token=BOT_TOKEN, session=session)
                    message = _update("/probe").message.as_(bot)
                    previous_tasks = set(telegram_helpers._TELEGRAM_BACKGROUND_TASKS)
                    await telegram_helpers.answer_with_auto_delete(
                        message,
                        "probe",
                        auto_delete_seconds=telegram_helpers.configured_auto_delete_seconds(
                            _retention_settings(mode), "management"
                        ),
                    )
                    await _drain_new_helper_tasks(previous_tasks)
                    self.assertEqual(session.method_names(), expected_methods)
                    self.assertEqual(len(scheduler.durable_calls), expected_enqueues)
        finally:
            telegram_helpers.configure_telegram_cleanup_scheduler(previous_scheduler)


class ParentReviewRegressionTests(SelfCheckCase):
    async def test_fatal_true_overrides_ok_true_in_dispatcher_response(self):
        snapshot = _healthy_snapshot()
        snapshot["fatal"] = True
        self._stub(snapshot)
        session = await self._feed("/selfcheck")
        self.assertIn("总体：本地异常", session.joined())
        self.assertNotIn("本地健康", session.joined())

    async def test_sampling_exception_logs_type_without_body_or_traceback(self):
        secret = "SECRET /etc/private token=never_echo"
        self._failing_stub(RuntimeError(secret))
        with self.assertLogs(self_check_service.log, level="WARNING") as capture:
            session = await self._feed("/selfcheck")
        self.assertIn(self_check_service.SAMPLING_FAILED_STATUS, session.joined())
        self.assertIn("RuntimeError", "\n".join(capture.output))
        self.assertNotIn(secret, "\n".join(capture.output))
        for record in capture.records:
            self.assertIsNone(record.exc_info)
            self.assertIsNone(record.exc_text)
            self.assertNotIn(secret, record.getMessage())

    async def test_render_exception_logs_type_without_body_or_traceback(self):
        secret = "SECRET render /etc/private token=never_echo"
        with patch.object(
            handler_module, "render_self_check_text", side_effect=ValueError(secret)
        ):
            with self.assertLogs(handler_module.log, level="WARNING") as capture:
                session = await self._feed("/selfcheck")
        self.assertEqual(session.texts(), [handler_module.UNAVAILABLE_NOTICE])
        self.assertIn("ValueError", "\n".join(capture.output))
        self.assertNotIn(secret, "\n".join(capture.output))
        self.assertTrue(all(record.exc_info is None for record in capture.records))


class DbMiddlewareReadOnlyTests(SelfCheckCase):
    async def test_real_db_middleware_opens_session_but_no_business_dml(self):
        """真实中间件与隔离内存 SQLite；不是完整生产 DurableInbox 集成验收。"""
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
        )
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            session.add_all(
                [
                    PrivateChatUsage(
                        user_id=SUPER_ADMIN, usage_date="2026-10-30", messages=7
                    ),
                    PrivateChatUsage(user_id=0, usage_date="2026-10-30", messages=19),
                    PrivateChatMessage(
                        user_id=SUPER_ADMIN,
                        role="user",
                        content="synthetic seed",
                        message_key="u:seed",
                    ),
                ]
            )
            await session.commit()

        async def database_rows():
            async with engine.connect() as connection:
                return {
                    table.name: (await connection.execute(select(table))).all()
                    for table in Base.metadata.sorted_tables
                }

        before = await database_rows()
        self.assertEqual(len(before["private_chat_usage"]), 2)
        self.assertEqual(len(before["private_chat_messages"]), 1)
        dml = []
        commits = []
        opened_sessions = []

        def capture_sql(conn, cursor, statement, parameters, context, executemany):
            verb = statement.lstrip().split(None, 1)[0].upper()
            if (
                context.isinsert
                or context.isupdate
                or context.isdelete
                or verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
            ):
                dml.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", capture_sql)
        self.addCleanup(
            event.remove, engine.sync_engine, "before_cursor_execute", capture_sql
        )

        class ObservedAsyncSession(AsyncSession):
            async def __aenter__(self):
                opened_sessions.append(self)
                return await super().__aenter__()

            async def commit(self):
                commits.append(self)
                return await super().commit()

        observed_factory = async_sessionmaker(
            engine, class_=ObservedAsyncSession, expire_on_commit=False, autoflush=False
        )
        self._stub(_healthy_snapshot())
        session = RecordingTelegramSession()
        bot = Bot(token=BOT_TOKEN, session=session)
        dispatcher = self._make_dispatcher(_retention_settings("timer"))
        dispatcher["session_factory"] = observed_factory
        # 与生产一样由中间件创建、注入并提交 session；未改变中间件实现。
        dispatcher.message.outer_middleware(DbSessionMiddleware(observed_factory))
        for _ in range(2):
            await dispatcher.feed_update(bot, _update("/selfcheck"))
        self.assertEqual(len(opened_sessions), 2)
        self.assertEqual(commits, opened_sessions)
        self.assertTrue(all(isinstance(s, AsyncSession) for s in opened_sessions))
        self.assertEqual(session.method_names(), ["SendMessage", "SendMessage"])
        self.assertEqual(self.provider_calls, 1, "生产命令路径也必须命中单槽 TTL 缓存")
        self.assertEqual(dml, [], "不能增加业务、配额、聊天历史或删除队列 DML")
        self.assertEqual(
            await database_rows(), before, "所有业务行（包括已种入配额）必须原样不变"
        )


class RealSnapshotTests(unittest.IsolatedAsyncioTestCase):
    """: 真 provider 的可调用性（不 mock）。"""

    def test_real_snapshot_yields_a_usable_report(self) -> None:
        self_check_service.reset_cache()
        with patch.dict("bot.services.resource_health._PROVIDERS", {}, clear=True):
            report = self_check_service.self_check_report(
                snapshot_provider=resource_health_snapshot, use_cache=False
            )
        self.assertTrue(report["sampling_ok"])
        self.assertIn(report["overall_ok"], (True, False, None))
        self.assertIsInstance(report["registered_components"], int)
        self.assertIsInstance(report["uptime_seconds"], float)
        text = self_check_service.render_self_check_text(report)
        self.assertIn("Asia/Shanghai", text)
        self_check_service.reset_cache()
