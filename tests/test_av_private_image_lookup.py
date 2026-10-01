"""私聊 ``/av`` 识图反查的处理器级测试（全部 Telegram / LLM 都是 mock）。

覆盖需求里点名的分支：

- 一张真 data URI 走完「识图 → 抽编号 → 查详情 → 回私聊」，并断言**发给私聊**
  （用 message 级 API，绝不 ``bot.send_photo(chat_id=群)``）、**用的是 vision 角色**；
- 模型输出 ``NO_CODE`` / ``NO_VALID_IMAGE_CONTENT`` / 只有演员名 三条分支；
- 视觉调用超时/抛异常 → 回「请直接发番号」，不抛到上层；
- 限流：同一用户第 11 次被拒，且**没有**调用模型/搜索（文字查询与识图共用一个桶）；
- 群里行为未变：``av_enabled`` 关、未授权群仍被拒，群里任何路径都不发照片。
"""

from __future__ import annotations

import asyncio
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, Document, Message, PhotoSize, User

from bot.config import ModelConfig, Settings
from bot.handlers import commands
from bot.services.av_image_lookup import AVPrivateRateLimiter
from bot.services.av_search import AVDetail, AVSearchItem, AVSeed

PRIVATE_CHAT_ID = 900123
GROUP_CHAT_ID = -100987
COVER_URL = "https://pics.dmm.co.jp/mono/movie/adult/sone342/sone342pl.jpg"
DATA_URI_PAYLOAD = b"\xff\xd8\xff\xe0fake-cover-jpeg"

KB = 1024


def _real_message(
    *,
    chat_id: int,
    chat_type: str,
    photo: list[PhotoSize] | None = None,
    document: Document | None = None,
    text: str | None = None,
) -> Message:
    """真 aiogram Message，用来验证路由过滤器（不是 mock）。"""

    return Message(
        message_id=1,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=PRIVATE_CHAT_ID, is_bot=False, first_name="tester"),
        photo=photo,
        document=document,
        text=text,
    )


class _AsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 0
    # 视觉与主模型故意配成两个不同的对象：这样「调的是 vision 角色」才有意义。
    settings.bot.main_model = ModelConfig(model="main-model-for-test")
    settings.bot.vision_model = ModelConfig(model="vision-model-for-test")
    return settings


def _detail() -> AVDetail:
    return AVDetail(
        source="javbus",
        title="SONE-342 新人NO.1 STYLE",
        url="https://www.javbus.com/SONE-342",
        code="SONE-342",
        cover_url=COVER_URL,
        date="2024-05-01",
        actors=["清原みゆう"],
        summary="新人首作。",
        seeds=[AVSeed(title="SONE-342 magnet", magnet="magnet:?xt=urn:btih:abc", size="1.1GB")],
    )


def _search_items() -> list[AVSearchItem]:
    return [
        AVSearchItem(
            source="javbus",
            title="清原みゆう 作品一",
            url="https://www.javbus.com/SONE-342",
            code="SONE-342",
            cover_url=COVER_URL,
        ),
        AVSearchItem(
            source="dmm",
            title="清原みゆう 作品二",
            url="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=sone343/",
            code="SONE-343",
        ),
    ]


class _FakeBot:
    """假 bot：get_file/download_file 都可用；send_photo 一律炸（私聊不该走它）。"""

    def __init__(self, *, payload: bytes = DATA_URI_PAYLOAD, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.download_calls: list[str] = []
        self.send_photo = AsyncMock(
            side_effect=AssertionError("私聊路径不该调用 send_photo(chat_id=...)")
        )
        self.send_chat_action = AsyncMock()

    async def get_file(self, file_id: str):
        if self.fail:
            raise RuntimeError("telegram get_file exploded")
        return SimpleNamespace(
            file_id=file_id,
            file_size=len(self.payload),
            file_path=f"photos/{file_id}.jpg",
        )

    async def download_file(self, file_path: str, destination) -> None:
        self.download_calls.append(file_path)
        destination.write(self.payload)


def _photo_message(
    *,
    chat_type: str = "private",
    chat_id: int = PRIVATE_CHAT_ID,
    user_id: int = PRIVATE_CHAT_ID,
    sizes: tuple[int, ...] = (40 * KB, 90 * KB, 300 * KB),
    caption: str | None = None,
    bot: _FakeBot | None = None,
) -> SimpleNamespace:
    bot = bot or _FakeBot()
    photo = [
        SimpleNamespace(file_id=f"photo-{index}", file_size=size, width=100, height=100)
        for index, size in enumerate(sizes)
    ]
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, title="test"),
        from_user=SimpleNamespace(id=user_id),
        bot=bot,
        photo=photo,
        document=None,
        text=None,
        caption=caption,
        # 私聊路径**绝不**删消息：这里给个 mock 只是为了断言它没被调用。
        delete=AsyncMock(),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=2)),
        edit_text=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        edit_media=AsyncMock(return_value=SimpleNamespace(message_id=4)),
        edit_caption=AsyncMock(return_value=SimpleNamespace(message_id=5)),
    )


def _llm_factory(reply: str = "SONE-342 清原みゆう", error: Exception | None = None):
    """假的 LLMService 工厂：记录构造参数，instance 只有 vision_describe。

    故意**不**提供 chat/complete 之类的方法：一旦代码把角色串成 main，测试会直接炸。
    """

    instances: list[SimpleNamespace] = []

    def _factory(main, decision, compress=None, **kwargs):
        instance = SimpleNamespace(
            main=main,
            decision=decision,
            compress=compress,
            **kwargs,
        )
        instance.vision_describe = AsyncMock(return_value=reply, side_effect=error)
        instances.append(instance)
        return instance

    return _factory, instances


def _service(
    *,
    detail: AVDetail | None = None,
    results: list[AVSearchItem] | None = None,
    fetch_detail: AVDetail | None = None,
):
    return SimpleNamespace(
        enabled=True,
        lookup_by_code=AsyncMock(return_value=detail),
        search=AsyncMock(return_value=list(results or [])),
        fetch_detail=AsyncMock(return_value=fetch_detail),
    )


class PrivateAVImageLookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # 限流器是模块级单例：每个用例换成干净的，避免用例之间互相计数。
        self.limiter = AVPrivateRateLimiter(limit=10, window_seconds=3600.0)
        self._limiter_patch = patch.object(
            commands, "_AV_PRIVATE_RATE_LIMITER", self.limiter
        )
        self._limiter_patch.start()
        self.addCleanup(self._limiter_patch.stop)

    async def _run_image_handler(self, message, *, service=None, llm=None, settings=None):
        service = service if service is not None else _service()
        llm_factory, llm_instances = llm or _llm_factory()
        answer = AsyncMock()
        with (
            patch.object(commands, "AVSearchService", return_value=service),
            patch.object(commands, "LLMService", new=llm_factory),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=answer),
        ):
            await commands.on_private_av_image(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=settings or _settings(),
            )
        return SimpleNamespace(
            service=service, answer=answer, llm_instances=llm_instances
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def test_photo_with_code_reads_image_and_sends_detail_to_private_chat(self) -> None:
        order: list[str] = []
        detail = _detail()
        data_uri_seen: list[str] = []

        class _OrderedBot(_FakeBot):
            async def get_file(self, file_id: str):
                order.append("download")
                return await super().get_file(file_id)

        bot = _OrderedBot()
        message = _photo_message(bot=bot)
        message.answer_photo = AsyncMock(
            side_effect=lambda *args, **kwargs: (
                order.append("answer_photo") or SimpleNamespace(message_id=2)
            )
        )
        service = _service(detail=detail)

        def _factory(main, decision, compress=None, **kwargs):
            instance = SimpleNamespace(main=main, decision=decision, **kwargs)

            async def _vision(image_url: str, prompt: str) -> str:
                order.append("vision")
                data_uri_seen.append(image_url)
                return "SONE-342 清原みゆう"

            instance.vision_describe = AsyncMock(side_effect=_vision)
            return instance

        answer = AsyncMock()
        db_session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: order.append("commit"))
        )
        with (
            patch.object(commands, "AVSearchService", return_value=service),
            patch.object(commands, "LLMService", new=_factory),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=answer),
        ):
            await commands.on_private_av_image(
                message, session=db_session, settings=_settings()
            )

        # 1) 挑的是「最大且 ≤100KB」那档（90KB），不是 len-2 的 40KB、也不是 300KB。
        self.assertEqual(bot.download_calls, ["photos/photo-1.jpg"])

        # 2) 传进模型的是真 data URI（base64 本地下载，不是远端 URL）。
        self.assertEqual(len(data_uri_seen), 1)
        self.assertTrue(data_uri_seen[0].startswith("data:image/jpeg;base64,"))
        self.assertNotIn(COVER_URL, data_uri_seen[0])

        # 3) 用的是 vision 角色（构造时 vision=settings.bot.vision_model），
        #    而且只调了一次 vision_describe，没有别的模型用途。
        self.assertEqual(len(service.lookup_by_code.await_args_list), 1)
        self.assertEqual(service.lookup_by_code.await_args.args[0], "SONE-342")

        # 4) 命中编号 → 详情回在私聊：message 级 answer_photo（chat 就是私聊），
        #    并且没有 send_photo(chat_id=...) 这种群/direct 发送。
        self.assertEqual(message.chat.type, "private")
        message.answer_photo.assert_awaited_once()
        photo_kwargs = message.answer_photo.await_args.kwargs
        self.assertEqual(photo_kwargs["photo"], COVER_URL)
        self.assertIn("SONE-342", photo_kwargs["caption"])
        self.assertIn(detail.title[:10], photo_kwargs["caption"])
        self.assertIsNotNone(photo_kwargs["reply_markup"])
        self.assertTrue(
            photo_kwargs["reply_markup"].inline_keyboard[0][0].callback_data.startswith("avm:")
        )
        bot.send_photo.assert_not_awaited()

        # 私聊路径不删任何消息（「先删图」只是群内规则）。
        message.delete.assert_not_awaited()

        # 5) Telegram I/O 之前先 commit（不跨网络调用持 SQLite 写锁）。
        self.assertEqual(order[0], "commit")
        self.assertLess(order.index("commit"), order.index("download"))
        self.assertLess(order.index("commit"), order.index("vision"))
        self.assertLess(order.index("commit"), order.index("answer_photo"))

        # 6) 整条链路没有别的话术。
        answer.assert_not_awaited()

    async def test_vision_role_is_selected_not_main_model(self) -> None:
        settings = _settings()
        llm_factory, llm_instances = _llm_factory(reply="SONE-342")
        ctor = Mock(side_effect=llm_factory)
        message = _photo_message()

        with (
            patch.object(commands, "AVSearchService", return_value=_service(detail=_detail())),
            patch.object(commands, "LLMService", new=ctor),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=AsyncMock()),
        ):
            await commands.on_private_av_image(
                message, session=SimpleNamespace(commit=AsyncMock()), settings=settings
            )

        ctor.assert_called_once()
        self.assertEqual(len(llm_instances), 1)
        instance = llm_instances[0]
        self.assertIs(instance.vision, settings.bot.vision_model)
        self.assertIs(instance.main, settings.bot.main_model)
        self.assertNotEqual(
            settings.bot.vision_model.model, settings.bot.main_model.model
        )
        self.assertEqual(instance.vision_describe.await_count, 1)
        # 假实例上根本没有 chat/complete：串成 main 会 AttributeError。
        self.assertFalse(hasattr(instance, "chat"))
        self.assertFalse(hasattr(instance, "complete"))

    async def test_private_cover_upload_fallback_used_when_url_send_fails(self) -> None:
        """私聊封面沿用现有顺序：先发 URL，BadRequest 后下载再上传。"""

        bot = _FakeBot()
        message = _photo_message(bot=bot)
        attempts = {"count": 0}
        file_obj = object()

        def _answer_photo(*args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TelegramBadRequest(
                    SimpleNamespace(
                        __api_method__="sendPhoto",
                        __url__="https://api.telegram.org/botsendPhoto",
                    ),
                    "Bad Request: failed to get HTTP URL content",
                )
            return SimpleNamespace(message_id=9)

        message.answer_photo = AsyncMock(side_effect=_answer_photo)

        with (
            patch.object(commands, "AVSearchService", return_value=_service(detail=_detail())),
            patch.object(commands, "LLMService", new=_llm_factory()[0]),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=AsyncMock()),
            patch.object(
                commands,
                "_download_cover_input_file",
                new=AsyncMock(return_value=file_obj),
            ) as download,
        ):
            await commands.on_private_av_image(
                message, session=SimpleNamespace(commit=AsyncMock()), settings=_settings()
            )

        self.assertEqual(message.answer_photo.await_count, 2)
        download.assert_awaited_once()
        first, second = message.answer_photo.await_args_list
        self.assertEqual(first.kwargs["photo"], COVER_URL)
        self.assertIs(second.kwargs["photo"], file_obj)

    async def test_document_image_runs_the_same_lookup_flow(self) -> None:
        bot = _FakeBot()
        message = _photo_message(bot=bot)
        message.photo = None
        message.document = SimpleNamespace(
            file_id="doc-cover", mime_type="image/png", file_size=80 * KB
        )
        service = _service(detail=_detail())

        result = await self._run_image_handler(message, service=service)

        self.assertEqual(bot.download_calls, ["photos/doc-cover.jpg"])
        service.lookup_by_code.assert_awaited_once_with("SONE-342")
        message.answer_photo.assert_awaited_once()
        result.answer.assert_not_awaited()

    async def test_non_image_document_is_ignored_silently(self) -> None:
        bot = _FakeBot()
        message = _photo_message(bot=bot)
        message.photo = None
        message.document = SimpleNamespace(
            file_id="pdf", mime_type="application/pdf", file_size=80 * KB
        )
        service = _service(detail=_detail())

        result = await self._run_image_handler(message, service=service)

        self.assertEqual(bot.download_calls, [])
        self.assertEqual(result.llm_instances, [])
        service.lookup_by_code.assert_not_awaited()
        result.answer.assert_not_awaited()
        message.answer.assert_not_awaited()

    # ------------------------------------------------------------------
    # 三条「没有编号」的分支
    # ------------------------------------------------------------------

    async def test_no_code_with_actor_falls_back_to_actor_search(self) -> None:
        message = _photo_message()
        service = _service(results=_search_items())

        result = await self._run_image_handler(
            message,
            service=service,
            llm=_llm_factory(reply="NO_CODE\nACTOR: 清原みゆう"),
        )

        service.lookup_by_code.assert_not_awaited()
        service.search.assert_awaited_once_with("清原みゆう")
        message.answer_photo.assert_not_awaited()
        text = message.answer.await_args.args[0]
        kwargs = message.answer.await_args.kwargs
        self.assertIn("AV 搜索结果", text)
        self.assertIn("清原みゆう", text)
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertTrue(rows[0][0].callback_data.startswith("avd:"))
        result.answer.assert_not_awaited()

    async def test_no_code_marker_asks_for_the_code_directly(self) -> None:
        message = _photo_message()
        service = _service()

        result = await self._run_image_handler(
            message, service=service, llm=_llm_factory(reply="NO_CODE")
        )

        service.lookup_by_code.assert_not_awaited()
        service.search.assert_not_awaited()
        message.answer_photo.assert_not_awaited()
        message.answer.assert_not_awaited()
        result.answer.assert_awaited_once()
        text = result.answer.await_args.args[2]
        self.assertIn(commands._AV_VISION_NO_CODE_TEXT, text)
        self.assertIn("请直接把番号发给我", text)

    async def test_unreadable_image_marker_answers_without_search(self) -> None:
        message = _photo_message()
        service = _service()

        result = await self._run_image_handler(
            message,
            service=service,
            llm=_llm_factory(reply="NO_VALID_IMAGE_CONTENT"),
        )

        service.lookup_by_code.assert_not_awaited()
        service.search.assert_not_awaited()
        result.answer.assert_awaited_once()
        text = result.answer.await_args.args[2]
        self.assertIn(commands._AV_VISION_NO_CONTENT_TEXT, text)

    async def test_code_hit_but_lookup_returns_nothing_falls_back_to_search(self) -> None:
        message = _photo_message()
        service = _service(detail=None, results=_search_items())

        with (
            patch.object(commands, "AVSearchService", return_value=service),
            patch.object(commands, "LLMService", new=_llm_factory(reply="SONE-342")[0]),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=AsyncMock()),
        ):
            await commands.on_private_av_image(
                message, session=SimpleNamespace(commit=AsyncMock()), settings=_settings()
            )

        service.lookup_by_code.assert_awaited_once_with("SONE-342")
        service.search.assert_awaited_once_with("SONE-342")
        self.assertIn("AV 搜索结果", message.answer.await_args.args[0])

    # ------------------------------------------------------------------
    # 失败降级
    # ------------------------------------------------------------------

    async def test_vision_timeout_and_exception_never_escape(self) -> None:
        for name, error in (
            ("timeout", asyncio.TimeoutError()),
            ("runtime", RuntimeError("vision exploded")),
        ):
            with self.subTest(case=name):
                message = _photo_message()
                service = _service()

                result = await self._run_image_handler(
                    message, service=service, llm=_llm_factory(error=error)
                )

                service.lookup_by_code.assert_not_awaited()
                service.search.assert_not_awaited()
                message.answer_photo.assert_not_awaited()
                result.answer.assert_awaited_once()
                text = result.answer.await_args.args[2]
                self.assertIn(commands._AV_VISION_FAILED_TEXT, text)

    async def test_hanging_vision_call_hits_the_hard_deadline(self) -> None:
        """视觉调用卡住不返回时，硬超时到点必须回复，而不是把处理器挂住。"""

        message = _photo_message()
        service = _service()
        answer = AsyncMock()

        async def _hang(*args, **kwargs):
            await asyncio.sleep(30)

        instance = SimpleNamespace(vision_describe=AsyncMock(side_effect=_hang))

        with (
            patch.object(commands, "AVSearchService", return_value=service),
            patch.object(commands, "LLMService", return_value=instance),
            patch.object(commands, "AV_VISION_TIMEOUT_SEC", 0.01),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=answer),
        ):
            await asyncio.wait_for(
                commands.on_private_av_image(
                    message,
                    session=SimpleNamespace(commit=AsyncMock()),
                    settings=_settings(),
                ),
                timeout=2.0,
            )

        service.lookup_by_code.assert_not_awaited()
        answer.assert_awaited_once()
        self.assertIn(commands._AV_VISION_FAILED_TEXT, answer.await_args.args[2])

    async def test_image_download_failure_degrades_to_ask_for_code(self) -> None:
        message = _photo_message(bot=_FakeBot(fail=True))
        service = _service()

        result = await self._run_image_handler(
            message, service=service, llm=_llm_factory()
        )

        self.assertEqual(result.llm_instances, [])
        service.lookup_by_code.assert_not_awaited()
        result.answer.assert_awaited_once()
        self.assertIn(commands._AV_VISION_FAILED_TEXT, result.answer.await_args.args[2])

    async def test_oversized_photo_is_rejected_before_any_model_call(self) -> None:
        message = _photo_message(sizes=(6 * 1024 * KB,))
        service = _service()

        result = await self._run_image_handler(
            message, service=service, llm=_llm_factory()
        )

        self.assertEqual(result.llm_instances, [])
        service.lookup_by_code.assert_not_awaited()
        result.answer.assert_awaited_once()
        self.assertIn(commands._AV_IMAGE_TOO_LARGE_TEXT, result.answer.await_args.args[2])

    async def test_empty_model_output_asks_for_code(self) -> None:
        message = _photo_message()

        result = await self._run_image_handler(
            message, service=_service(), llm=_llm_factory(reply="   ")
        )

        result.answer.assert_awaited_once()
        self.assertIn(commands._AV_VISION_FAILED_TEXT, result.answer.await_args.args[2])

    async def test_llm_construction_failure_degrades_to_ask_for_code(self) -> None:
        message = _photo_message()
        service = _service()
        answer = AsyncMock()

        def _boom(*args, **kwargs):
            raise RuntimeError("vision endpoint not configured")

        with (
            patch.object(commands, "AVSearchService", return_value=service),
            patch.object(commands, "LLMService", new=_boom),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=answer),
        ):
            await commands.on_private_av_image(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        service.lookup_by_code.assert_not_awaited()
        answer.assert_awaited_once()
        self.assertIn(commands._AV_VISION_FAILED_TEXT, answer.await_args.args[2])

    # ------------------------------------------------------------------
    # 私聊按钮触发的外部抓取：超限的人不许再触发（只查不计数）
    # ------------------------------------------------------------------

    def _private_callback(self, *, with_details: bool = False):
        item = AVSearchItem(
            source="javbus",
            title="清原みゆう 作品一",
            url="https://www.javbus.com/SONE-342",
            code="SONE-342",
        )
        av_session = commands._AV_SESSION_STORE.create(
            owner_user_id=PRIVATE_CHAT_ID, query="清原みゆう", results=[item]
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=PRIVATE_CHAT_ID, type="private", title=""),
            photo=None,
            bot=_FakeBot(),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            edit_text=AsyncMock(return_value=SimpleNamespace(message_id=2)),
            edit_media=AsyncMock(return_value=SimpleNamespace(message_id=3)),
            edit_caption=AsyncMock(return_value=SimpleNamespace(message_id=4)),
        )
        callback = SimpleNamespace(
            data=f"avd:{av_session.token}:0",
            message=message,
            from_user=SimpleNamespace(id=PRIVATE_CHAT_ID),
            answer=AsyncMock(),
        )
        return callback, message, av_session

    async def test_private_detail_callback_is_blocked_once_over_the_limit(self) -> None:
        limiter = AVPrivateRateLimiter(limit=1, window_seconds=3600.0)
        self.assertTrue(limiter.allow(PRIVATE_CHAT_ID)[0])
        callback, message, _session = self._private_callback()
        service = _service(detail=_detail())

        with (
            patch.object(commands, "_AV_PRIVATE_RATE_LIMITER", limiter),
            patch.object(commands, "AVSearchService", return_value=service) as service_cls,
        ):
            await commands.on_av_detail_select(
                callback,
                settings=_settings(),
                session=SimpleNamespace(commit=AsyncMock()),
            )

        service_cls.assert_not_called()
        service.fetch_detail.assert_not_awaited()
        message.edit_text.assert_not_awaited()
        text = callback.answer.await_args.args[0]
        self.assertIn("太频繁了", text)

    async def test_private_detail_callback_fetches_when_within_the_limit(self) -> None:
        callback, _message, _session = self._private_callback()
        service = _service(detail=_detail())

        with patch.object(commands, "AVSearchService", return_value=service):
            await commands.on_av_detail_select(
                callback,
                settings=_settings(),
                session=SimpleNamespace(commit=AsyncMock()),
            )

        # 额度没用完时，点按钮照常抓详情（而且不消耗配额）。
        service.fetch_detail.assert_awaited_once()
        self.assertEqual(
            commands._AV_PRIVATE_RATE_LIMITER.blocked(PRIVATE_CHAT_ID), (False, 0)
        )
        for call in callback.answer.await_args_list:
            if call.args:
                self.assertNotIn("太频繁了", call.args[0])

    async def test_group_detail_callback_is_never_rate_limited(self) -> None:
        item = AVSearchItem(
            source="javbus",
            title="清原みゆう 作品一",
            url="https://www.javbus.com/SONE-342",
            code="SONE-342",
        )
        av_session = commands._AV_SESSION_STORE.create(
            owner_user_id=PRIVATE_CHAT_ID, query="q", results=[item]
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_CHAT_ID, type="supergroup", title="group"),
            photo=None,
            bot=_FakeBot(),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            edit_text=AsyncMock(return_value=SimpleNamespace(message_id=2)),
            edit_media=AsyncMock(return_value=SimpleNamespace(message_id=3)),
            edit_caption=AsyncMock(return_value=SimpleNamespace(message_id=4)),
        )
        callback = SimpleNamespace(
            data=f"avd:{av_session.token}:0",
            message=message,
            from_user=SimpleNamespace(id=PRIVATE_CHAT_ID),
            answer=AsyncMock(),
        )
        service = _service(detail=_detail())
        exhausted = AVPrivateRateLimiter(limit=1, window_seconds=3600.0)
        self.assertTrue(exhausted.allow(PRIVATE_CHAT_ID)[0])

        with (
            patch.object(commands, "_AV_PRIVATE_RATE_LIMITER", exhausted),
            patch.object(
                commands, "is_group_authorized", new=AsyncMock(return_value=True)
            ),
            patch.object(
                commands,
                "_ensure_group_row",
                new=AsyncMock(return_value=SimpleNamespace(settings={"av_enabled": True})),
            ),
            patch.object(commands, "AVSearchService", return_value=service),
        ):
            await commands.on_av_detail_select(
                callback,
                settings=_settings(),
                session=SimpleNamespace(commit=AsyncMock()),
            )

        service.fetch_detail.assert_awaited_once()
        for call in callback.answer.await_args_list:
            if call.args:
                self.assertNotIn("太频繁了", call.args[0])

    # ------------------------------------------------------------------
    # 限流
    # ------------------------------------------------------------------

    async def test_eleventh_image_call_is_rejected_without_model_or_search(self) -> None:
        service = _service(detail=_detail())
        llm_factory, llm_instances = _llm_factory(reply="SONE-342 清原みゆう")
        answer = AsyncMock()

        with (
            patch.object(commands, "AVSearchService", return_value=service) as service_cls,
            patch.object(commands, "LLMService", new=llm_factory),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=answer),
        ):
            for _ in range(10):
                await commands.on_private_av_image(
                    _photo_message(),
                    session=SimpleNamespace(commit=AsyncMock()),
                    settings=_settings(),
                )
            self.assertEqual(service.lookup_by_code.await_count, 10)
            self.assertEqual(len(llm_instances), 10)

            await commands.on_private_av_image(
                _photo_message(),
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        # 第 11 次：没有新的模型调用、没有新的搜索、没有新服务对象。
        self.assertEqual(len(llm_instances), 10)
        self.assertEqual(service.lookup_by_code.await_count, 10)
        self.assertEqual(service.search.await_count, 0)
        self.assertEqual(service_cls.call_count, 10)
        self.assertEqual(answer.await_count, 1)
        text = answer.await_args.args[2]
        self.assertIn("太频繁了", text)
        self.assertIn("分钟后再试", text)

    async def test_text_query_shares_the_same_hourly_bucket(self) -> None:
        for _ in range(10):
            self.assertTrue(self.limiter.allow(PRIVATE_CHAT_ID)[0])

        service = _service(detail=_detail())
        answer = AsyncMock()
        message = SimpleNamespace(
            chat=SimpleNamespace(id=PRIVATE_CHAT_ID, type="private", title=""),
            from_user=SimpleNamespace(id=PRIVATE_CHAT_ID),
            text="/av SONE-342",
            bot=_FakeBot(),
        )

        with (
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ),
            patch.object(commands, "AVSearchService", return_value=service) as service_cls,
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=answer),
        ):
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        service_cls.assert_not_called()
        service.lookup_by_code.assert_not_awaited()
        service.search.assert_not_awaited()
        self.assertIn("太频繁了", answer.await_args.args[2])

    async def test_private_text_query_is_open_to_regular_users(self) -> None:
        service = _service(detail=_detail())
        message = SimpleNamespace(
            chat=SimpleNamespace(id=PRIVATE_CHAT_ID, type="private", title=""),
            from_user=SimpleNamespace(id=PRIVATE_CHAT_ID),
            text="/av SONE-342",
            bot=_FakeBot(),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=2)),
        )
        settings = _settings()
        settings.super_admin_id = 999999  # 普通用户，不是最高管理员

        with (
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ),
            patch.object(commands, "AVSearchService", return_value=service),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=AsyncMock()) as answer,
        ):
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=settings,
            )

        service.lookup_by_code.assert_awaited_once_with("SONE-342")
        message.answer_photo.assert_awaited_once()
        self.assertEqual(message.answer_photo.await_args.kwargs["photo"], COVER_URL)
        message.bot.send_photo.assert_not_awaited()
        answer.assert_not_awaited()

    async def test_private_usage_text_mentions_the_hourly_quota(self) -> None:
        answer = AsyncMock()
        message = SimpleNamespace(
            chat=SimpleNamespace(id=PRIVATE_CHAT_ID, type="private", title=""),
            from_user=SimpleNamespace(id=PRIVATE_CHAT_ID),
            text="/av",
            bot=_FakeBot(),
        )

        with (
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ),
            patch.object(commands, "_answer", new=answer),
        ):
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        text = answer.await_args.args[2]
        self.assertIn("私聊可以直接发图片", text)
        self.assertIn("每人每小时最多 10 次", text)
        self.assertNotIn("私聊仅最高管理员可查询", text)


class GroupAVBehaviourUnchangedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.limiter = AVPrivateRateLimiter(limit=10, window_seconds=3600.0)
        self._limiter_patch = patch.object(
            commands, "_AV_PRIVATE_RATE_LIMITER", self.limiter
        )
        self._limiter_patch.start()
        self.addCleanup(self._limiter_patch.stop)

    def _group_message(
        self,
        *,
        text: str | None = "/av SONE-342",
        caption: str | None = None,
        with_photo: bool = True,
    ) -> SimpleNamespace:
        bot = _FakeBot()
        return SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_CHAT_ID, type="supergroup", title="group"),
            from_user=SimpleNamespace(id=PRIVATE_CHAT_ID),
            bot=bot,
            text=text,
            caption=caption,
            photo=(
                [SimpleNamespace(file_id="group-photo", file_size=40 * KB)]
                if with_photo
                else None
            ),
            document=None,
            message_id=42,
            delete=AsyncMock(),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            answer_photo=AsyncMock(
                side_effect=AssertionError("群内不许出现照片")
            ),
            edit_text=AsyncMock(return_value=SimpleNamespace(message_id=2)),
            edit_media=AsyncMock(side_effect=AssertionError("群内不许出现照片")),
            edit_caption=AsyncMock(side_effect=AssertionError("群内不许出现照片")),
        )

    async def test_group_usage_text_is_byte_identical_to_before(self) -> None:
        """群里 ``/av``（无参数）的用法文案一个字都没改。"""

        legacy = (
            "<b>AV 查询用法</b>\n"
            "1. /av WANZ-530（按番号直查并展示详情+种子）\n"
            "2. /av 推川悠里（按演员名查询并弹出可选列表）\n"
            "3. /av 人妻 NTR（按关键词查询并弹出可选列表）\n\n"
            "支持来源：JAVBUS / MADOUQU / DMM / FC2\n"
            "支持 FC2 编号：/av FC2-PPV-4863846\n\n"
            "默认状态：<b>关闭</b>（每个群独立）\n"
            "需最高管理员在目标群发送 /av enable 后可使用\n\n"
            "私聊仅最高管理员可查询。\n\n"
            "<b>最高管理员命令（群内）</b>\n"
            "4. /av enable（启用本群 AV 查询）\n"
            "5. /av disable（停用本群 AV 查询）"
        )
        message = self._group_message(text="/av", with_photo=False)
        answer = AsyncMock()

        with (
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ),
            patch.object(commands, "AVSearchService") as service_cls,
            patch.object(commands, "_answer", new=answer),
        ):
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        service_cls.assert_not_called()
        message.delete.assert_not_awaited()
        self.assertEqual(answer.await_args.args[2], legacy)

    async def test_disabled_group_still_rejected_without_service_calls(self) -> None:
        # 图片配文 /av：群开关关着时既不识图，也绝不删用户的图。
        message = self._group_message(text=None, caption="/av")
        answer = AsyncMock()

        with (
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ),
            patch.object(
                commands,
                "_ensure_group_row",
                new=AsyncMock(return_value=SimpleNamespace(settings={"av_enabled": False})),
            ),
            patch.object(commands, "AVSearchService") as service_cls,
            patch.object(commands, "_answer", new=answer),
        ):
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        service_cls.assert_not_called()
        message.answer_photo.assert_not_awaited()
        message.bot.send_photo.assert_not_awaited()
        message.delete.assert_not_awaited()
        self.assertIn("当前群组未启用", answer.await_args.args[2])

    async def test_unauthorized_group_still_rejected(self) -> None:
        message = self._group_message(text=None, caption="/av")

        with (
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=False)
            ),
            patch.object(commands, "AVSearchService") as service_cls,
            patch.object(commands, "_answer", new=AsyncMock()) as answer,
        ):
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        service_cls.assert_not_called()
        answer.assert_not_awaited()
        message.answer_photo.assert_not_awaited()
        message.delete.assert_not_awaited()

    async def test_enabled_group_query_never_sends_photo_to_the_group(self) -> None:
        # 纯文字 /av：真实群里这种消息没有 photo（text 与 caption 互斥）。
        message = self._group_message(with_photo=False)
        service = _service(detail=_detail())
        message.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=8))

        with (
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            ),
            patch.object(
                commands,
                "_ensure_group_row",
                new=AsyncMock(return_value=SimpleNamespace(settings={"av_enabled": True})),
            ),
            patch.object(commands, "AVSearchService", return_value=service),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=AsyncMock()),
        ):
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        message.answer_photo.assert_not_awaited()
        message.edit_media.assert_not_awaited()
        # 封面只允许发给发起者本人（现有群内行为，一个字没改）。
        message.bot.send_photo.assert_awaited_once()
        self.assertEqual(
            int(message.bot.send_photo.await_args.kwargs["chat_id"]), PRIVATE_CHAT_ID
        )
        text = message.answer.await_args.args[0]
        self.assertIn("<code>SONE-342</code>", text)

    async def test_group_photo_message_never_enters_private_image_path(self) -> None:
        """群里的照片消息即便落到私聊处理器上，也必须直接返回。"""

        message = self._group_message(text=None)
        llm_factory, llm_instances = _llm_factory()

        with (
            patch.object(commands, "AVSearchService") as service_cls,
            patch.object(commands, "LLMService", new=llm_factory),
            patch.object(commands, "_answer", new=AsyncMock()) as answer,
        ):
            await commands.on_private_av_image(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        self.assertEqual(llm_instances, [])
        service_cls.assert_not_called()
        answer.assert_not_awaited()
        message.answer.assert_not_awaited()
        message.answer_photo.assert_not_awaited()
        message.bot.send_photo.assert_not_awaited()

    async def test_group_private_photo_handler_only_matches_private_chat(self) -> None:
        """路由过滤器本身也只挂在私聊上（群消息由 group 路由处理）。"""

        private_names = [
            getattr(handler.callback, "__name__", "")
            for handler in commands.router.message.handlers
        ]
        self.assertIn("on_private_av_image", private_names)
        self.assertLess(
            private_names.index("on_private_av_image"),
            private_names.index("cmd_av"),
            "识图处理器必须在 cmd_av 前面，带 /av 说明的图片才走识图",
        )

    async def test_router_filter_matches_only_private_images(self) -> None:
        """用真 aiogram Message 跑一遍过滤器：只有「私聊 + 图片」才会命中。"""

        photo = [
            PhotoSize(file_id="f", file_unique_id="u", width=90, height=90)
        ]
        cases = (
            ("private_photo", _real_message(chat_id=1, chat_type="private", photo=photo), True),
            (
                "private_image_document",
                _real_message(
                    chat_id=1,
                    chat_type="private",
                    document=Document(
                        file_id="d", file_unique_id="u", mime_type="image/png"
                    ),
                ),
                True,
            ),
            (
                "private_pdf_document",
                _real_message(
                    chat_id=1,
                    chat_type="private",
                    document=Document(
                        file_id="d", file_unique_id="u", mime_type="application/pdf"
                    ),
                ),
                False,
            ),
            (
                "group_photo",
                _real_message(chat_id=-100, chat_type="supergroup", photo=photo),
                False,
            ),
            ("private_text", _real_message(chat_id=1, chat_type="private", text="/av"), False),
        )

        handler = next(
            handler
            for handler in commands.router.message.handlers
            if getattr(handler.callback, "__name__", "") == "on_private_av_image"
        )
        for name, message, expected in cases:
            with self.subTest(case=name):
                matched = True
                for filter_object in handler.filters:
                    if not filter_object.callback(message):
                        matched = False
                        break
                self.assertEqual(matched, expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
