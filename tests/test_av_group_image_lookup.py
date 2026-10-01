"""群内 ``/av`` + 图片：**先删图，再识图**，结果只发文字（封面仍旧只走私聊）。

覆盖需求里点名的几条：

- 调用顺序：``delete`` 必须发生在 ``vision_describe`` **之前**（本需求的核心）；
- 两种入口：图片配文 ``/av`` 与「回复某条图片 + 裸 ``/av``」；
- 删除失败（权限不足 / 超时）仍然继续识图；
- ``groups.settings.av_enabled`` 关 / 群未授权 → **不删任何图**，只回现有提示；
- 群里结果只有文字（绝不 ``answer_photo``/``edit_media``），封面只发发起者私聊；
- 结果不 reply 到已删的那条命令上（独立消息 + 发起者名字）；
- 限流对群内**图片**生效（与私聊共用同一个桶），对群内**文字**查询不生效。

所有 Telegram / LLM 调用都是 mock，不触网。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Chat, Document, Message, PhotoSize, User

from bot.config import ModelConfig, Settings
from bot.handlers import commands
from bot.services.av_image_lookup import AVPrivateRateLimiter
from bot.services.av_search import AVDetail, AVSearchItem, AVSeed

GROUP_CHAT_ID = -100555
USER_ID = 555001
OTHER_USER_ID = 555002
GROUP_MESSAGE_ID = 700
REPLY_MESSAGE_ID = 699
COVER_URL = "https://pics.dmm.co.jp/mono/movie/adult/sone342/sone342pl.jpg"
DATA_URI_PAYLOAD = b"\xff\xd8\xff\xe0fake-group-cover"

KB = 1024


class _AsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 0
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
        )
    ]


class _FakeBot:
    """假 bot：下载可用；``send_photo`` 记录封面私聊；``answer_photo`` 一律炸。"""

    def __init__(self, *, order: list[str] | None = None, me_username: str = "CoolAvBot") -> None:
        self.payload = DATA_URI_PAYLOAD
        self.order = order
        self.download_calls: list[str] = []
        self.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=9))
        self.send_chat_action = AsyncMock()
        self.me = AsyncMock(
            return_value=SimpleNamespace(username=me_username, id=777)
        )
        self.get_me = AsyncMock(
            return_value=SimpleNamespace(username=me_username, id=777)
        )

    async def get_file(self, file_id: str):
        return SimpleNamespace(
            file_id=file_id,
            file_size=len(self.payload),
            file_path=f"photos/{file_id}.jpg",
        )

    async def download_file(self, file_path: str, destination) -> None:
        if self.order is not None:
            self.order.append("download")
        self.download_calls.append(file_path)
        destination.write(self.payload)


def _llm_factory(
    reply: str = "SONE-342 清原みゆう",
    *,
    error: Exception | None = None,
    order: list[str] | None = None,
):
    instances: list[SimpleNamespace] = []

    def _factory(main, decision, compress=None, **kwargs):
        instance = SimpleNamespace(main=main, decision=decision, compress=compress, **kwargs)

        async def _vision(image_url: str, prompt: str) -> str:
            if order is not None:
                order.append("vision")
            if error is not None:
                raise error
            return reply

        instance.vision_describe = AsyncMock(side_effect=_vision)
        instances.append(instance)
        return instance

    return _factory, instances


def _service(
    *,
    detail: AVDetail | None = None,
    results: list[AVSearchItem] | None = None,
):
    return SimpleNamespace(
        enabled=True,
        lookup_by_code=AsyncMock(return_value=detail),
        search=AsyncMock(return_value=list(results or [])),
        fetch_detail=AsyncMock(return_value=detail),
    )


def _photo(file_id: str, size: int) -> SimpleNamespace:
    return SimpleNamespace(file_id=file_id, file_size=size, width=90, height=90)


def _replied_photo(bot: _FakeBot, *, order: list[str] | None = None, user_id: int = OTHER_USER_ID):
    async def _delete():
        if order is not None:
            order.append("delete-reply")

    return SimpleNamespace(
        chat=SimpleNamespace(id=GROUP_CHAT_ID, type="supergroup", title="group"),
        from_user=SimpleNamespace(id=user_id, full_name="别人", username="other"),
        bot=bot,
        message_id=REPLY_MESSAGE_ID,
        photo=[_photo("replied-photo", 45 * KB)],
        document=None,
        caption=None,
        delete=AsyncMock(side_effect=_delete),
    )


def _command_message(
    *,
    bot: _FakeBot | None = None,
    text: str | None = None,
    caption: str | None = None,
    with_photo: bool = False,
    reply_to_message: SimpleNamespace | None = None,
    order: list[str] | None = None,
) -> SimpleNamespace:
    bot = bot or _FakeBot(order=order)
    if order is None and bot.order is not None:
        order = bot.order

    async def _delete():
        if order is not None:
            order.append("delete-command")

    return SimpleNamespace(
        chat=SimpleNamespace(id=GROUP_CHAT_ID, type="supergroup", title="group"),
        from_user=SimpleNamespace(id=USER_ID, full_name="张三", username="zhangsan"),
        bot=bot,
        text=text,
        caption=caption,
        photo=[_photo("command-photo", 60 * KB)] if with_photo else None,
        document=None,
        message_id=GROUP_MESSAGE_ID,
        reply_to_message=reply_to_message,
        delete=AsyncMock(side_effect=_delete),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        answer_photo=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
        edit_text=AsyncMock(return_value=SimpleNamespace(message_id=2)),
        edit_media=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
        edit_caption=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
    )


class _GroupAVTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.limiter = AVPrivateRateLimiter(limit=10, window_seconds=3600.0)
        self._limiter_patch = patch.object(
            commands, "_AV_PRIVATE_RATE_LIMITER", self.limiter
        )
        self._limiter_patch.start()
        self.addCleanup(self._limiter_patch.stop)

    def _run_cmd_av(self, message, *, service=None, llm=None, enabled: bool = True,
                    authorized: bool = True, answer: AsyncMock | None = None):
        """准备 cmd_av 需要的所有 patch；返回 (ExitStack, service, answer, llm_instances)。"""

        service = service if service is not None else _service(detail=_detail())
        llm_factory, llm_instances = llm or _llm_factory()
        answer = answer or AsyncMock()
        stack = contextlib.ExitStack()
        stack.enter_context(
            patch.object(
                commands, "ensure_group_authorized",
                new=AsyncMock(return_value=authorized),
            )
        )
        stack.enter_context(
            patch.object(
                commands,
                "_ensure_group_row",
                new=AsyncMock(return_value=SimpleNamespace(settings={"av_enabled": enabled})),
            )
        )
        stack.enter_context(patch.object(commands, "AVSearchService", return_value=service))
        stack.enter_context(patch.object(commands, "LLMService", new=llm_factory))
        stack.enter_context(
            patch.object(commands, "typing_action", return_value=_AsyncContext())
        )
        stack.enter_context(patch.object(commands, "_answer", new=answer))
        stack.enter_context(
            patch.object(
                commands, "_download_cover_input_file", new=AsyncMock(return_value=None)
            )
        )
        return stack, service, answer, llm_instances


class GroupAVImageEntryPointTests(_GroupAVTestCase):
    """两种触发入口 + 核心的「先删图，再识图」顺序。"""

    async def test_caption_trigger_deletes_photo_before_vision(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service(detail=_detail())
        llm_factory, llm_instances = _llm_factory(order=order)
        answer = AsyncMock()
        context, _, _, _ = self._run_cmd_av(
            message, service=service, llm=(llm_factory, llm_instances), answer=answer
        )

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        # 核心断言：删图是第一步，视觉识别在其后。
        self.assertEqual(order[0], "delete-command")
        self.assertLess(order.index("delete-command"), order.index("vision"))
        message.delete.assert_awaited_once()

        # 一次识图 = 一次 vision，且仍然走 vision 角色。
        self.assertEqual(len(llm_instances), 1)
        self.assertEqual(llm_instances[0].vision_describe.await_count, 1)
        service.lookup_by_code.assert_awaited_once_with("SONE-342")

    async def test_reply_trigger_deletes_both_messages_before_vision(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        replied = _replied_photo(bot, order=order)
        message = _command_message(bot=bot, text="/av", reply_to_message=replied)
        service = _service(detail=_detail())
        llm_factory, llm_instances = _llm_factory(order=order)
        context, _, _, _ = self._run_cmd_av(
            message, service=service, llm=(llm_factory, llm_instances)
        )

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        # 图片消息与 /av 命令行是两条，都要删；而且都在识图之前。
        replied.delete.assert_awaited_once()
        message.delete.assert_awaited_once()
        self.assertLess(order.index("delete-reply"), order.index("vision"))
        self.assertLess(order.index("delete-command"), order.index("vision"))
        service.lookup_by_code.assert_awaited_once_with("SONE-342")

    async def test_reply_trigger_reads_the_replied_photo(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        replied = _replied_photo(bot, order=order)
        message = _command_message(bot=bot, text="/av", reply_to_message=replied)
        context, service, _, _ = self._run_cmd_av(message)

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        # 用的是被回复的那条图（不是命令行本身）。
        self.assertEqual(bot.download_calls, ["photos/replied-photo.jpg"])
        service.lookup_by_code.assert_awaited_once_with("SONE-342")

    async def test_delete_failure_still_runs_vision_and_query(self) -> None:
        def _bad_request() -> TelegramBadRequest:
            return TelegramBadRequest(
                SimpleNamespace(
                    __api_method__="deleteMessage",
                    __url__="https://api.telegram.org/botdeleteMessage",
                ),
                "Bad Request: message can't be deleted",
            )

        for name, error in (
            ("bad_request", _bad_request()),
            ("timeout", asyncio.TimeoutError()),
            ("runtime", RuntimeError("telegram exploded")),
        ):
            with self.subTest(case=name):
                order: list[str] = []
                bot = _FakeBot(order=order)
                replied = _replied_photo(bot, order=order)
                replied.delete = AsyncMock(side_effect=error)
                message = _command_message(bot=bot, text="/av", reply_to_message=replied)
                message.delete = AsyncMock(side_effect=error)
                service = _service(detail=_detail())
                llm_factory, llm_instances = _llm_factory(order=order)
                context, _, _, _ = self._run_cmd_av(
                    message, service=service, llm=(llm_factory, llm_instances)
                )

                with context:
                    await commands.cmd_av(
                        message,
                        session=SimpleNamespace(commit=AsyncMock()),
                        settings=_settings(),
                    )

                # 删除失败 ≠ 中断：识图与查询照跑。
                self.assertEqual(len(llm_instances), 1)
                self.assertEqual(llm_instances[0].vision_describe.await_count, 1)
                service.lookup_by_code.assert_awaited_once_with("SONE-342")

    async def test_reply_to_non_image_keeps_existing_usage_behaviour(self) -> None:
        """``/av`` 回复的是文字消息 → 不是识图请求，不删任何消息。"""

        order: list[str] = []
        bot = _FakeBot(order=order)
        replied_text = SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_CHAT_ID, type="supergroup", title="group"),
            from_user=SimpleNamespace(id=OTHER_USER_ID),
            bot=bot,
            message_id=REPLY_MESSAGE_ID,
            photo=None,
            document=None,
            delete=AsyncMock(),
        )
        message = _command_message(bot=bot, text="/av", reply_to_message=replied_text)
        service = _service(detail=_detail())
        context, _, answer, _ = self._run_cmd_av(message, service=service)

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        message.delete.assert_not_awaited()
        replied_text.delete.assert_not_awaited()
        service.lookup_by_code.assert_not_awaited()
        self.assertIn("AV 查询用法", answer.await_args.args[2])

    async def test_text_query_replying_to_a_photo_is_not_an_image_request(self) -> None:
        """``/av SONE-342`` 回复图片仍按文字查询走（避免误删别人刚发的图）。"""

        order: list[str] = []
        bot = _FakeBot(order=order)
        replied = _replied_photo(bot, order=order)
        message = _command_message(
            bot=bot, text="/av SONE-342", reply_to_message=replied
        )
        service = _service(detail=_detail())
        context, _, _, _ = self._run_cmd_av(message, service=service)

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        replied.delete.assert_not_awaited()
        message.delete.assert_not_awaited()
        service.lookup_by_code.assert_awaited_once_with("SONE-342")

    async def test_plain_group_photo_is_never_an_av_request(self) -> None:
        """普通聊天里的图片：配文不是 /av 就不算请求（也不会被删）。"""

        for name, text, caption, reply in (
            ("no_caption", None, None, None),
            ("other_caption", None, "看看这个", None),
            ("similar_caption", None, "/average 图", None),
            ("text_not_command", "好看", None, None),
        ):
            with self.subTest(case=name):
                bot = _FakeBot()
                replied = _replied_photo(bot) if reply else None
                message = _command_message(
                    bot=bot,
                    text=text,
                    caption=caption,
                    with_photo=True,
                    reply_to_message=replied,
                )
                self.assertIsNone(commands._group_av_image_request(message))

    async def test_router_entrypoint_matches_av_caption_and_reply_forms(self) -> None:
        """路由层确认：``/av`` 配文图片与 ``/av`` 文字都会进 cmd_av。"""

        photo = [PhotoSize(file_id="f", file_unique_id="u", width=90, height=90)]
        fake_bot = SimpleNamespace(
            username="CoolAvBot",
            id=777,
            me=AsyncMock(return_value=SimpleNamespace(username="CoolAvBot", id=777)),
        )
        handler = next(
            handler
            for handler in commands.router.message.handlers
            if getattr(handler.callback, "__name__", "") == "cmd_av"
        )

        def _message(**kwargs) -> Message:
            base = dict(
                message_id=1,
                date=datetime.datetime.now(datetime.timezone.utc),
                chat=Chat(id=GROUP_CHAT_ID, type="supergroup"),
                from_user=User(id=USER_ID, is_bot=False, first_name="张三"),
            )
            base.update(kwargs)
            return Message(**base)

        cases = (
            ("photo_caption_av", _message(photo=photo, caption="/av"), True),
            ("photo_caption_av_mention", _message(photo=photo, caption="/av@CoolAvBot"), True),
            ("photo_caption_other", _message(photo=photo, caption="看看这个"), False),
            ("plain_text_av", _message(text="/av"), True),
            ("plain_text_other", _message(text="好看"), False),
        )
        for name, message, expected in cases:
            with self.subTest(case=name):
                matched = bool(await handler.filters[0].callback(message, fake_bot))
                self.assertEqual(matched, expected)


class GroupAVImageGatingTests(_GroupAVTestCase):
    """未启用 / 未授权：不删任何图片，只回现有提示。"""

    async def test_disabled_group_never_deletes_and_only_prompts(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service(detail=_detail())
        answer = AsyncMock()
        context, _, _, llm_instances = self._run_cmd_av(
            message, service=service, enabled=False, answer=answer
        )

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        message.delete.assert_not_awaited()
        self.assertEqual(order, [])
        self.assertEqual(llm_instances, [])
        service.lookup_by_code.assert_not_awaited()
        service.search.assert_not_awaited()
        self.assertIn("当前群组未启用", answer.await_args.args[2])

    async def test_unauthorized_group_never_deletes_and_only_prompts(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service(detail=_detail())
        context, _, _, llm_instances = self._run_cmd_av(
            message, service=service, authorized=False
        )

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        message.delete.assert_not_awaited()
        self.assertEqual(order, [])
        self.assertEqual(llm_instances, [])
        service.lookup_by_code.assert_not_awaited()

    async def test_globally_disabled_service_never_deletes(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service(detail=_detail())
        service.enabled = False
        answer = AsyncMock()
        context, _, _, llm_instances = self._run_cmd_av(
            message, service=service, answer=answer
        )

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        message.delete.assert_not_awaited()
        self.assertEqual(llm_instances, [])
        self.assertIn("当前已禁用", answer.await_args.args[2])


class GroupAVImageResultTests(_GroupAVTestCase):
    """群里的结果：只有文字、写明是谁问的、封面只走私聊。"""

    async def test_group_result_is_text_only_with_name_and_cover_in_dm(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service(detail=_detail())
        context, _, answer, _ = self._run_cmd_av(message, service=service)

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        # 群里只有文字：没有任何照片 API 指向这个群。
        message.answer_photo.assert_not_awaited()
        message.edit_media.assert_not_awaited()
        message.edit_caption.assert_not_awaited()

        message.answer.assert_awaited_once()
        text = message.answer.await_args.args[0]
        kwargs = message.answer.await_args.kwargs
        self.assertIn("张三", text)
        self.assertIn("的识图结果", text)
        self.assertIn("<code>SONE-342</code>", text)
        self.assertIn(_detail().title, text)
        self.assertTrue(
            kwargs["reply_markup"].inline_keyboard[0][0].callback_data.startswith("avm:")
        )
        # 命令与图片都删了：结果必须是独立消息，绝不 reply 到已删消息上。
        self.assertNotIn("reply_to_message_id", kwargs)
        self.assertNotIn("reply_parameters", kwargs)
        answer.assert_not_awaited()

        # 封面只发发起者私聊（现有行为）。
        bot.send_photo.assert_awaited_once()
        self.assertEqual(int(bot.send_photo.await_args.kwargs["chat_id"]), USER_ID)
        self.assertEqual(bot.send_photo.await_args.kwargs["photo"], COVER_URL)

    async def test_dm_blocked_cover_falls_back_to_deeplink_button_in_group(self) -> None:
        """私聊被拒 → 群里给 ``t.me/<bot>?start=av`` 深链按钮（现有规则照旧）。"""

        order: list[str] = []
        bot = _FakeBot(order=order)
        bot.send_photo = AsyncMock(
            side_effect=TelegramForbiddenError(
                SimpleNamespace(
                    __api_method__="sendPhoto",
                    __url__="https://api.telegram.org/botsendPhoto",
                ),
                "Forbidden: bot can't initiate conversation with a user",
            )
        )
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service(detail=_detail())
        context, _, _, _ = self._run_cmd_av(message, service=service)

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        message.answer_photo.assert_not_awaited()
        bot.send_photo.assert_awaited_once()
        self.assertEqual(int(bot.send_photo.await_args.kwargs["chat_id"]), USER_ID)

        text = message.answer.await_args.args[0]
        kwargs = message.answer.await_args.kwargs
        self.assertIn(commands._AV_COVER_PRIVATE_BLOCKED_HINT, text)
        self.assertIn("张三", text)
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[-1][0].text, "打开私聊")
        self.assertEqual(rows[-1][0].url, "https://t.me/CoolAvBot?start=av")
        self.assertNotIn("reply_to_message_id", kwargs)

    async def test_actor_only_result_uses_search_page_with_name(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service(results=_search_items())
        llm_factory, _instances = _llm_factory(reply="NO_CODE\nACTOR: 清原みゆう")
        context, _, _, _ = self._run_cmd_av(
            message, service=service, llm=(llm_factory, _instances)
        )

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        service.lookup_by_code.assert_not_awaited()
        service.search.assert_awaited_once_with("清原みゆう")
        message.answer_photo.assert_not_awaited()
        text = message.answer.await_args.args[0]
        kwargs = message.answer.await_args.kwargs
        self.assertIn("张三", text)
        self.assertIn("的识图结果", text)
        self.assertIn("AV 搜索结果", text)
        self.assertTrue(kwargs["reply_markup"].inline_keyboard[0][0].callback_data.startswith("avd:"))
        self.assertNotIn("reply_to_message_id", kwargs)
        bot.send_photo.assert_not_awaited()

    async def test_vision_failure_still_deletes_first_and_answers_in_group(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service()
        llm_factory, _instances = _llm_factory(
            error=RuntimeError("vision exploded"), order=order
        )
        answer = AsyncMock()
        context, _, _, _ = self._run_cmd_av(
            message, service=service, llm=(llm_factory, _instances), answer=answer
        )

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        self.assertEqual(order[0], "delete-command")
        self.assertIn("delete-command", order)
        self.assertLess(order.index("delete-command"), order.index("vision"))
        service.lookup_by_code.assert_not_awaited()
        self.assertIn("张三", answer.await_args.args[2])
        self.assertIn(commands._AV_VISION_FAILED_TEXT, answer.await_args.args[2])

    async def test_no_code_result_asks_for_code_in_group(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service()
        llm_factory, instances = _llm_factory(reply="NO_CODE")
        answer = AsyncMock()
        context, _, _, _ = self._run_cmd_av(
            message, service=service, llm=(llm_factory, instances), answer=answer
        )

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        service.lookup_by_code.assert_not_awaited()
        service.search.assert_not_awaited()
        self.assertIn(commands._AV_VISION_NO_CODE_TEXT, answer.await_args.args[2])
        self.assertIn("张三", answer.await_args.args[2])


class GroupAVImageRateLimitTests(_GroupAVTestCase):
    """限流：群内图片与私聊共用一个桶；群内文字查询不受影响。"""

    async def test_group_image_shares_the_private_hourly_bucket(self) -> None:
        for _ in range(10):
            self.assertTrue(self.limiter.allow(USER_ID)[0])

        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        service = _service(detail=_detail())
        llm_factory, llm_instances = _llm_factory(order=order)
        answer = AsyncMock()
        context, _, _, _ = self._run_cmd_av(
            message, service=service, llm=(llm_factory, llm_instances), answer=answer
        )

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        # 超限：不调模型、不搜索；图仍然先被删掉（NSFW 不留群）。
        self.assertEqual(llm_instances, [])
        service.lookup_by_code.assert_not_awaited()
        service.search.assert_not_awaited()
        self.assertEqual(order[0], "delete-command")
        self.assertNotIn("vision", order)
        self.assertIn("太频繁了", answer.await_args.args[2])
        self.assertIn("张三", answer.await_args.args[2])

    async def test_group_image_consumes_one_slot_from_the_shared_bucket(self) -> None:
        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, caption="/av", with_photo=True)
        context, _, _, _ = self._run_cmd_av(message)

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        blocked, _retry = self.limiter.blocked(USER_ID)
        self.assertFalse(blocked)
        # 已经用掉 1 次（9 次之后第 10 次私聊还能过，第 11 次才拦）。
        for _ in range(9):
            self.assertTrue(self.limiter.allow(USER_ID)[0])
        self.assertTrue(self.limiter.blocked(USER_ID)[0])

    async def test_group_text_query_is_not_rate_limited(self) -> None:
        for _ in range(10):
            self.assertTrue(self.limiter.allow(USER_ID)[0])

        order: list[str] = []
        bot = _FakeBot(order=order)
        message = _command_message(bot=bot, text="/av SONE-342", with_photo=False)
        service = _service(detail=_detail())
        context, _, _, llm_instances = self._run_cmd_av(message, service=service)

        with context:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        # 群内文字查询保持现状：不计数、不拦。
        self.assertEqual(llm_instances, [])
        service.lookup_by_code.assert_awaited_once_with("SONE-342")
        message.delete.assert_not_awaited()
        bot.send_photo.assert_awaited_once()
        self.assertEqual(int(bot.send_photo.await_args.kwargs["chat_id"]), USER_ID)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
