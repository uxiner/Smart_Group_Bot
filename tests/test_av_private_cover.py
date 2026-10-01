"""群内 ``/av`` 只发文字，封面走私聊（NSFW 封面不进社区群）。

覆盖规格里要求的几条：
- **核心断言**：群内路径没有任何照片调用指向群 chat（照片只允许指向发起者私聊）；
- 封面确实发到发起者私聊（``chat_id == 发起者 user_id``，且私聊那条没有按钮）；
- 私聊被拒（``TelegramForbiddenError``）→ 群里换成 ``t.me/<bot>?start=av`` 深链按钮
  （用户名运行时 ``get_me()`` 取；取不到用户名退化成纯文本），并且**依然**没有照片指向群；
- 群里的文字结果（标题/番号/详情）与按钮照旧存在；
- 私聊里最高管理员的诊断入口行为不变；
- ``av_enabled`` 关闭的既有拒绝行为不变，且拒绝路径也不发照片。

所有 Telegram 调用都是 mock，测试不触网。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.config import Settings
from bot.handlers import commands
from bot.services.av_search import AVDetail, AVQuerySessionStore, AVSearchItem, AVSeed

GROUP_CHAT_ID = -10001
SENDER_USER_ID = 123
COVER_URL = "https://pics.dmm.co.jp/mono/movie/adult/wanz530/wanz530pl.jpg"


def _settings() -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 3
    settings.bot.auto_delete_categories = ["management"]
    return settings


class _AsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _detail(*, cover_url: str = COVER_URL) -> AVDetail:
    return AVDetail(
        source="dmm",
        title="推川ゆうりの凄テクを我慢できれば生★中出しSEX！",
        url="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=wanz530/",
        code="WANZ-530",
        cover_url=cover_url,
        date="2016/09/01",
        summary="第28弾は、ホルスタイン系むっちむちGカップ。",
        seeds=[
            AVSeed(
                title="WANZ-530 magnet",
                magnet="magnet:?xt=urn:btih:abc",
                size="1.2GB",
                date="2016/09/02",
            )
        ],
    )


def _session(*, owner_user_id: int = SENDER_USER_ID, detail: AVDetail | None = None):
    store = AVQuerySessionStore()
    item = AVSearchItem(
        source="dmm",
        title="推川ゆうりの凄テクを我慢できれば生★中出しSEX！",
        url="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=wanz530/",
        code="WANZ-530",
        cover_url=COVER_URL,
    )
    av_session = store.create(owner_user_id=owner_user_id, query="WANZ-530", results=[item])
    if detail is not None:
        av_session.details[0] = detail
    return av_session


def _bot(*, send_photo_error: Exception | None = None, me=None, me_error: Exception | None = None):
    async def _get_me():
        if me_error is not None:
            raise me_error
        return me

    return SimpleNamespace(
        send_photo=AsyncMock(side_effect=send_photo_error),
        get_me=AsyncMock(side_effect=_get_me),
    )


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(
        SimpleNamespace(
            __api_method__="sendPhoto",
            __url__="https://api.telegram.org/botsendPhoto",
        ),
        "Forbidden: bot can't initiate conversation with a user",
    )


def _bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(
        SimpleNamespace(
            __api_method__="sendPhoto",
            __url__="https://api.telegram.org/botsendPhoto",
        ),
        "Bad Request: failed to get HTTP URL content",
    )


def _group_message(*, bot, **overrides) -> SimpleNamespace:
    """群消息 mock：任何指向群 chat 的照片 API 都会直接炸测试。"""

    message = SimpleNamespace(
        chat=SimpleNamespace(id=GROUP_CHAT_ID, type="supergroup", title="test group"),
        from_user=SimpleNamespace(id=SENDER_USER_ID),
        bot=bot,
        text="/av WANZ-530",
        reply_markup=None,
        photo=None,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        answer_photo=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
        edit_text=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        edit_media=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
        edit_caption=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
    )
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


class AVGroupCoverPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def _assert_no_group_photo(self, message, bot, *, group_chat_id: int = GROUP_CHAT_ID) -> None:
        """核心断言：没有任何照片调用指向群 chat。"""

        message.answer_photo.assert_not_awaited()
        message.edit_media.assert_not_awaited()
        message.edit_caption.assert_not_awaited()
        for call in bot.send_photo.await_args_list:
            self.assertNotEqual(
                int(call.kwargs.get("chat_id", 0)),
                group_chat_id,
                "封面被发到了群里",
            )

    async def test_group_query_sends_cover_to_sender_dm_and_keeps_text_in_group(self) -> None:
        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        message = _group_message(bot=bot)
        detail = _detail()

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
        )

        self.assertTrue(ok)
        self._assert_no_group_photo(message, bot)

        # 封面进的是发起者私聊，而且只有一张图：没有按钮、没有分页。
        bot.send_photo.assert_awaited_once()
        photo_call = bot.send_photo.await_args
        self.assertEqual(photo_call.kwargs["chat_id"], SENDER_USER_ID)
        self.assertEqual(photo_call.kwargs["photo"], COVER_URL)
        self.assertNotIn("reply_markup", photo_call.kwargs)
        self.assertIn("WANZ-530", photo_call.kwargs["caption"])

        # 文字详情与按钮照旧留在群里。
        text = message.answer.await_args.args[0]
        kwargs = message.answer.await_args.kwargs
        self.assertIn("<code>WANZ-530</code>", text)
        self.assertIn(detail.title, text)
        self.assertIn("第28弾", text)
        self.assertIn(commands._AV_COVER_SENT_HINT, text)
        self.assertNotIn("t.me/", text)
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertTrue(rows[0][0].callback_data.startswith("avm:"))
        self.assertEqual(rows[0][0].text, "浏览种子 (1)")

    async def test_group_path_never_targets_group_chat_with_photo(self) -> None:
        """正常/被拒/取不到用户名/其它失败：四种结果都不许把图发群里。"""

        cases = (
            ("dm_sent", None, SimpleNamespace(username="CoolAvBot"), None),
            ("dm_blocked", _forbidden(), SimpleNamespace(username="CoolAvBot"), None),
            ("dm_blocked_no_username", _forbidden(), None, RuntimeError("getMe failed")),
            ("dm_failed", RuntimeError("boom"), SimpleNamespace(username="CoolAvBot"), None),
        )
        for name, send_photo_error, me, me_error in cases:
            with self.subTest(case=name):
                bot = _bot(
                    send_photo_error=send_photo_error,
                    me=me,
                    me_error=me_error,
                )
                message = _group_message(bot=bot)
                detail = _detail()
                with patch.object(
                    commands,
                    "_download_cover_input_file",
                    new=AsyncMock(return_value=None),
                ):
                    ok = await commands._send_av_detail(
                        message=message,
                        session=_session(detail=detail),
                        result_idx=0,
                        detail=detail,
                    )

                self.assertTrue(ok)
                self._assert_no_group_photo(message, bot)
                text = message.answer.await_args.args[0]
                kwargs = message.answer.await_args.kwargs
                self.assertIn(detail.title, text)
                self.assertIn("<code>WANZ-530</code>", text)
                self.assertIsNotNone(kwargs["reply_markup"])

    async def test_group_upload_fallback_also_goes_to_sender_dm(self) -> None:
        """URL 发被拒（BadRequest）→ 下载后上传，依然只发私聊。"""

        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        attempts = {"count": 0}

        def _send_photo(**kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise _bad_request()
            return SimpleNamespace(message_id=7)

        bot.send_photo = AsyncMock(side_effect=_send_photo)
        message = _group_message(bot=bot)
        detail = _detail()
        file_obj = object()

        with patch.object(
            commands,
            "_download_cover_input_file",
            new=AsyncMock(return_value=file_obj),
        ) as download:
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
            )

        self.assertTrue(ok)
        self._assert_no_group_photo(message, bot)
        download.assert_awaited_once()
        self.assertEqual(bot.send_photo.await_count, 2)
        first, second = bot.send_photo.await_args_list
        self.assertEqual(first.kwargs["chat_id"], SENDER_USER_ID)
        self.assertEqual(first.kwargs["photo"], COVER_URL)
        self.assertEqual(second.kwargs["chat_id"], SENDER_USER_ID)
        self.assertIs(second.kwargs["photo"], file_obj)
        self.assertIn(commands._AV_COVER_SENT_HINT, message.answer.await_args.args[0])

    async def test_forbidden_dm_posts_deeplink_button_in_group(self) -> None:
        bot = _bot(send_photo_error=_forbidden(), me=SimpleNamespace(username="CoolAvBot"))
        message = _group_message(bot=bot)
        detail = _detail()

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
        )

        self.assertTrue(ok)
        self._assert_no_group_photo(message, bot)
        text = message.answer.await_args.args[0]
        kwargs = message.answer.await_args.kwargs
        self.assertIn(commands._AV_COVER_PRIVATE_BLOCKED_HINT, text)
        self.assertIn("文本结果照旧留在群里", text)
        self.assertIn(detail.title, text)

        rows = kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[-1][0].text, "打开私聊")
        self.assertEqual(rows[-1][0].url, "https://t.me/CoolAvBot?start=av")
        # 原来的按钮一行都没少。
        self.assertTrue(rows[0][0].callback_data.startswith("avm:"))
        self.assertEqual(rows[1][0].url, detail.url)

    async def test_forbidden_dm_without_username_degrades_to_plain_text(self) -> None:
        bot = _bot(
            send_photo_error=_forbidden(),
            me=None,
            me_error=RuntimeError("getMe failed"),
        )
        message = _group_message(bot=bot)
        detail = _detail()

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
        )

        self.assertTrue(ok)
        self._assert_no_group_photo(message, bot)
        text = message.answer.await_args.args[0]
        kwargs = message.answer.await_args.kwargs
        self.assertIn(commands._AV_COVER_PRIVATE_BLOCKED_HINT, text)
        urls = [
            button.url
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.url
        ]
        self.assertEqual(urls, [detail.url], "取不到用户名时不该拼出深链按钮")

    async def test_group_detail_without_cover_sends_no_dm_and_no_hint(self) -> None:
        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        message = _group_message(bot=bot)
        detail = _detail(cover_url="")

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
        )

        self.assertTrue(ok)
        self._assert_no_group_photo(message, bot)
        bot.send_photo.assert_not_awaited()
        text = message.answer.await_args.args[0]
        self.assertNotIn("封面", text)

    async def test_group_in_place_detail_edits_text_not_media(self) -> None:
        """``avd:`` 回调在群里就地编辑成文字详情，不再 edit_media 换封面。"""

        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        message = _group_message(bot=bot)
        detail = _detail()

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
            in_place=True,
        )

        self.assertTrue(ok)
        self._assert_no_group_photo(message, bot)
        message.answer.assert_not_awaited()
        text = message.edit_text.await_args.args[0]
        kwargs = message.edit_text.await_args.kwargs
        self.assertIn(detail.title, text)
        self.assertIn("<code>WANZ-530</code>", text)
        self.assertIn(commands._AV_COVER_SENT_HINT, text)
        self.assertTrue(kwargs["reply_markup"].inline_keyboard[0][0].callback_data.startswith("avm:"))
        bot.send_photo.assert_awaited_once()
        self.assertEqual(bot.send_photo.await_args.kwargs["chat_id"], SENDER_USER_ID)

    async def test_group_in_place_detail_adds_deeplink_when_dm_blocked(self) -> None:
        bot = _bot(send_photo_error=_forbidden(), me=SimpleNamespace(username="CoolAvBot"))
        message = _group_message(bot=bot)
        detail = _detail()

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
            in_place=True,
        )

        self.assertTrue(ok)
        self._assert_no_group_photo(message, bot)
        text = message.edit_text.await_args.args[0]
        kwargs = message.edit_text.await_args.kwargs
        self.assertIn(commands._AV_COVER_PRIVATE_BLOCKED_HINT, text)
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertEqual(rows[-1][0].url, "https://t.me/CoolAvBot?start=av")

    async def test_group_seed_paging_never_edits_media(self) -> None:
        """``avm:`` 回调：即便手里是历史遗留的图片消息，群内也改回文字。"""

        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        message = _group_message(bot=bot, photo=SimpleNamespace(file_id="legacy-photo"))

        ok = await commands._edit_av_seed_in_place(
            message=message,
            detail=_detail(),
            text="<b>种子列表</b>",
            keyboard=None,
        )

        self.assertTrue(ok)
        message.edit_text.assert_awaited_once()
        message.edit_caption.assert_not_awaited()
        message.edit_media.assert_not_awaited()
        self._assert_no_group_photo(message, bot)

    async def test_private_diagnostic_entry_keeps_photo_with_buttons(self) -> None:
        """私聊里最高管理员的 ``/av`` 诊断入口行为不变：私聊照旧发图 + 按钮。"""

        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        message = SimpleNamespace(
            chat=SimpleNamespace(id=SENDER_USER_ID, type="private", title=""),
            from_user=SimpleNamespace(id=SENDER_USER_ID),
            bot=bot,
            text="/av WANZ-530",
            answer=AsyncMock(),
            answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=7)),
            edit_text=AsyncMock(),
        )
        detail = _detail()

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
        )

        self.assertTrue(ok)
        message.answer_photo.assert_awaited_once()
        self.assertEqual(message.answer_photo.await_args.kwargs["photo"], COVER_URL)
        self.assertIsNotNone(message.answer_photo.await_args.kwargs["reply_markup"])
        bot.send_photo.assert_not_awaited()

    async def test_group_detail_callback_keeps_text_in_group_and_dms_cover(self) -> None:
        detail = _detail()
        av_session = _session(detail=detail)
        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        message = _group_message(bot=bot)
        callback = SimpleNamespace(
            data=f"avd:{av_session.token}:0",
            message=message,
            from_user=SimpleNamespace(id=SENDER_USER_ID),
            answer=AsyncMock(),
        )
        db_session = SimpleNamespace(commit=AsyncMock())

        with (
            patch.object(commands._AV_SESSION_STORE, "get", return_value=av_session),
            patch(
                "bot.handlers.commands.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.commands._ensure_group_row",
                new=AsyncMock(return_value=SimpleNamespace(settings={"av_enabled": True})),
            ),
        ):
            await commands.on_av_detail_select(
                callback,
                settings=_settings(),
                session=db_session,
            )

        self._assert_no_group_photo(message, bot)
        message.edit_text.assert_awaited_once()
        text = message.edit_text.await_args.args[0]
        kwargs = message.edit_text.await_args.kwargs
        self.assertIn(detail.title, text)
        self.assertTrue(kwargs["reply_markup"].inline_keyboard[0][0].callback_data.startswith("avm:"))
        bot.send_photo.assert_awaited_once()
        self.assertEqual(bot.send_photo.await_args.kwargs["chat_id"], SENDER_USER_ID)
        callback.answer.assert_awaited_once_with("已更新详情")

    async def test_group_search_paging_still_edits_text_only(self) -> None:
        detail = _detail()
        av_session = _session(detail=detail)
        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        message = _group_message(bot=bot)
        callback = SimpleNamespace(
            data=f"avs:{av_session.token}:0",
            message=message,
            from_user=SimpleNamespace(id=SENDER_USER_ID),
            answer=AsyncMock(),
        )
        db_session = SimpleNamespace(commit=AsyncMock())

        with (
            patch.object(commands._AV_SESSION_STORE, "get", return_value=av_session),
            patch(
                "bot.handlers.commands.is_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.commands._ensure_group_row",
                new=AsyncMock(return_value=SimpleNamespace(settings={"av_enabled": True})),
            ),
        ):
            await commands.on_av_search_paging(
                callback,
                settings=_settings(),
                session=db_session,
            )

        message.edit_text.assert_awaited_once()
        text = message.edit_text.await_args.args[0]
        kwargs = message.edit_text.await_args.kwargs
        self.assertIn("AV 搜索结果", text)
        self.assertTrue(kwargs["reply_markup"].inline_keyboard[0][0].callback_data.startswith("avd:"))
        self._assert_no_group_photo(message, bot)
        bot.send_photo.assert_not_awaited()

    async def test_disabled_group_av_never_sends_photo_or_dm(self) -> None:
        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        message = _group_message(bot=bot)
        db_session = SimpleNamespace(commit=AsyncMock())

        with (
            patch(
                "bot.handlers.commands.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.commands._ensure_group_row",
                new=AsyncMock(return_value=SimpleNamespace(settings={"av_enabled": False})),
            ),
            patch("bot.handlers.commands.AVSearchService") as service_cls,
            patch("bot.handlers.commands._answer", new=AsyncMock()) as answer_mock,
        ):
            await commands.cmd_av(message, session=db_session, settings=_settings())

        service_cls.assert_not_called()
        bot.send_photo.assert_not_awaited()
        message.answer_photo.assert_not_awaited()
        self.assertIn("当前群组未启用", answer_mock.await_args.args[2])

    async def test_group_code_lookup_commits_before_telegram_io(self) -> None:
        order: list[str] = []
        bot = _bot(me=SimpleNamespace(username="CoolAvBot"))
        bot.send_photo = AsyncMock(side_effect=lambda **kwargs: order.append("photo"))
        message = _group_message(bot=bot)
        message.answer = AsyncMock(
            side_effect=lambda *args, **kwargs: (
                order.append("answer") or SimpleNamespace(message_id=1)
            )
        )
        db_session = SimpleNamespace(
            commit=AsyncMock(side_effect=lambda: order.append("commit")),
            in_transaction=lambda: False,
        )
        detail = _detail()
        service = SimpleNamespace(
            enabled=True,
            search=AsyncMock(),
            lookup_by_code=AsyncMock(
                side_effect=lambda query: order.append("lookup") or detail
            ),
        )

        with (
            patch(
                "bot.handlers.commands.ensure_group_authorized",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "bot.handlers.commands._ensure_group_row",
                new=AsyncMock(return_value=SimpleNamespace(settings={"av_enabled": True})),
            ),
            patch("bot.handlers.commands.AVSearchService", return_value=service),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
            patch("bot.handlers.commands._answer", new=AsyncMock()),
        ):
            await commands.cmd_av(message, session=db_session, settings=_settings())

        self.assertEqual(order[0], "commit")
        self.assertLess(order.index("commit"), order.index("lookup"))
        self.assertLess(order.index("commit"), order.index("photo"))
        self.assertEqual(bot.send_photo.await_args.kwargs["chat_id"], SENDER_USER_ID)
        self._assert_no_group_photo(message, bot)
        text = message.answer.await_args.args[0]
        self.assertIn("<code>WANZ-530</code>", text)
        self.assertIn(commands._AV_COVER_SENT_HINT, text)


if __name__ == "__main__":
    unittest.main()
