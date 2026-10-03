"""封面之外，番号的样例图**只**补发到发起者私聊（默认 4 张、最多 5 张）。

规格要求逐条对应：

- 默认 4 张；``sample_urls`` 给 10 条时只发 4 张；显式配置 5 张就发 5 张；0 = 关闭；
- 一律下载字节后上传（``Referer`` = 详情页），不把外部 URL 交给 Telegram 抓；
- 逐张 best-effort：单张下载/发送失败只记日志、继续下一张；全失败也不动文字结果码；
- **仅私聊**：``private_blocked`` 时一张都不发，且群内文案/结果码不变；
- 群内文字**不被样例图阻塞**：``_send_av_detail`` 返回时样例图任务还没开始跑，
  群里已经能看到文字；样例图在独立后台任务里发；
- 两个入口共用同一个发送函数（群里/私聊点开详情 与 识图反查成功那条）；
- 全程零照片指向群 chat。

所有 Telegram / 网络调用都是 mock，测试不触网。
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from pydantic import ValidationError

from bot.config import Settings
from bot.handlers import commands
from bot.services.av_search import AVDetail, AVQuerySessionStore, AVSearchItem, AVSeed
from bot.services.runtime_config import AVSettingsConfig, build_legacy_runtime_config

GROUP_CHAT_ID = -10001
SENDER_USER_ID = 123
COVER_URL = "https://pics.dmm.co.jp/mono/movie/adult/wanz530/wanz530pl.jpg"
SAMPLE_URLS = [
    f"https://pics.dmm.co.jp/digital/video/wanz530/wanz530jp-{index}.jpg"
    for index in range(1, 11)
]


def _settings(*, samples: int = commands.AV_DM_SAMPLE_DEFAULT) -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 3
    settings.bot.auto_delete_categories = ["management"]
    settings.av_dm_sample_count = samples
    return settings


class _AsyncContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _detail(
    *,
    cover_url: str = COVER_URL,
    sample_urls: list[str] | None = None,
) -> AVDetail:
    return AVDetail(
        source="javbus",
        title="推川ゆうりの凄テクを我慢できれば生★中出しSEX！",
        url="https://www.javbus.com/WANZ-530",
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
        sample_urls=list(SAMPLE_URLS if sample_urls is None else sample_urls),
    )


def _session(*, owner_user_id: int = SENDER_USER_ID, detail: AVDetail | None = None):
    store = AVQuerySessionStore()
    item = AVSearchItem(
        source="javbus",
        title="推川ゆうりの凄テクを我慢できれば生★中出しSEX！",
        url="https://www.javbus.com/WANZ-530",
        code="WANZ-530",
        cover_url=COVER_URL,
    )
    av_session = store.create(owner_user_id=owner_user_id, query="WANZ-530", results=[item])
    if detail is not None:
        av_session.details[0] = detail
    return av_session


def _forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(
        SimpleNamespace(
            __api_method__="sendPhoto",
            __url__="https://api.telegram.org/botsendPhoto",
        ),
        "Forbidden: bot can't initiate conversation with a user",
    )


def _retry_after() -> TelegramRetryAfter:
    return TelegramRetryAfter(
        SimpleNamespace(
            __api_method__="sendPhoto",
            __url__="https://api.telegram.org/botsendPhoto",
        ),
        "Too Many Requests: retry after 5",
        5,
    )


def _bot(
    *,
    cover_error: Exception | None = None,
    full_failures: bool = False,
    sample_send_failures: set[int] | None = None,
    me=SimpleNamespace(username="CoolAvBot"),
) -> SimpleNamespace:
    """``send_photo`` 的第 1 次调用是封面，之后都是样例图。

    直接调 ``_send_av_samples_to_sender_dm`` 时没有封面，第 1 次调用就是第 1 张样例图。
    """

    sample_send_failures = sample_send_failures or set()
    state = {"count": 0}

    async def _send_photo(**_kwargs):
        index = state["count"]
        state["count"] += 1
        if index == 0 and cover_error is not None:
            raise cover_error
        if full_failures:
            raise RuntimeError("sample send boom")
        if index in sample_send_failures:
            raise RuntimeError(f"sample {index} send boom")
        return SimpleNamespace(message_id=index + 1)

    return SimpleNamespace(
        send_photo=AsyncMock(side_effect=_send_photo),
        get_me=AsyncMock(return_value=me),
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


def _private_message(*, bot, **overrides) -> SimpleNamespace:
    message = SimpleNamespace(
        chat=SimpleNamespace(id=SENDER_USER_ID, type="private", title=""),
        from_user=SimpleNamespace(id=SENDER_USER_ID),
        bot=bot,
        text="/av WANZ-530",
        reply_markup=None,
        photo=None,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=11)),
        edit_text=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        edit_media=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        edit_caption=AsyncMock(return_value=SimpleNamespace(message_id=10)),
    )
    for key, value in overrides.items():
        setattr(message, key, value)
    return message


class AVSampleDmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

    def _assert_no_group_photo(self, message, bot, *, group_chat_id: int = GROUP_CHAT_ID) -> None:
        """核心断言：没有任何照片调用指向群 chat（扩展到了样例图路径）。"""

        message.answer_photo.assert_not_awaited()
        message.edit_media.assert_not_awaited()
        message.edit_caption.assert_not_awaited()
        for call in bot.send_photo.await_args_list:
            self.assertNotEqual(
                int(call.kwargs.get("chat_id", 0)),
                group_chat_id,
                "有照片被发到了群里",
            )

    # ------------------------------------------------------------------
    # 条数：默认 4 / 上限 5 / 0 = 关闭
    # ------------------------------------------------------------------

    def test_sample_limit_reads_settings_with_cap_and_off_switch(self) -> None:
        self.assertEqual(commands._av_dm_sample_limit(None), 4)
        self.assertEqual(commands._av_dm_sample_limit(_settings(samples=4)), 4)
        self.assertEqual(commands._av_dm_sample_limit(_settings(samples=5)), 5)
        self.assertEqual(commands._av_dm_sample_limit(_settings(samples=99)), 5)
        self.assertEqual(commands._av_dm_sample_limit(_settings(samples=0)), 0)
        self.assertEqual(commands._av_dm_sample_limit(_settings(samples=-3)), 0)
        broken = SimpleNamespace(av_dm_sample_count="abc")
        self.assertEqual(commands._av_dm_sample_limit(broken), 4)
        text = SimpleNamespace(av_dm_sample_count="3")
        self.assertEqual(commands._av_dm_sample_limit(text), 3)

    async def _direct_send(self, *, detail: AVDetail, limit: int, bot) -> int:
        with patch.object(
            commands,
            "_download_av_image_input_file",
            new=AsyncMock(return_value=SimpleNamespace()),
        ):
            return await commands._send_av_samples_to_sender_dm(
                bot=bot,
                sender_user_id=SENDER_USER_ID,
                detail=detail,
                limit=limit,
            )

    async def test_direct_send_default_limit_is_four_of_ten(self) -> None:
        bot = _bot()

        sent = await self._direct_send(
            detail=_detail(),
            limit=commands._av_dm_sample_limit(_settings()),
            bot=bot,
        )

        self.assertEqual(sent, 4)
        self.assertEqual(bot.send_photo.await_count, 4)
        for call in bot.send_photo.await_args_list:
            self.assertEqual(call.kwargs["chat_id"], SENDER_USER_ID)
            self.assertNotIsInstance(call.kwargs["photo"], str)

    async def test_direct_send_explicit_five_sends_five(self) -> None:
        bot = _bot()

        sent = await self._direct_send(detail=_detail(), limit=5, bot=bot)

        self.assertEqual(sent, 5)
        self.assertEqual(bot.send_photo.await_count, 5)

    async def test_direct_send_zero_sends_nothing(self) -> None:
        bot = _bot()

        sent = await self._direct_send(detail=_detail(), limit=0, bot=bot)

        self.assertEqual(sent, 0)
        bot.send_photo.assert_not_awaited()

    async def test_direct_send_rejects_non_positive_chat_id(self) -> None:
        bot = _bot()

        sent = await commands._send_av_samples_to_sender_dm(
            bot=bot,
            sender_user_id=-10001,
            detail=_detail(),
            limit=4,
        )

        self.assertEqual(sent, 0)
        bot.send_photo.assert_not_awaited()

    async def test_downloads_use_detail_page_referer_and_upload_bytes(self) -> None:
        bot = _bot()
        files = [SimpleNamespace(name=f"sample-{index}") for index in range(1, 5)]
        download = AsyncMock(side_effect=files)

        with patch.object(commands, "_download_av_image_input_file", new=download):
            sent = await commands._send_av_samples_to_sender_dm(
                bot=bot,
                sender_user_id=SENDER_USER_ID,
                detail=_detail(),
                limit=4,
            )

        self.assertEqual(sent, 4)
        self.assertEqual(
            [call.args[0] for call in download.await_args_list],
            SAMPLE_URLS[:4],
        )
        for call in download.await_args_list:
            self.assertEqual(call.kwargs.get("referer"), "https://www.javbus.com/WANZ-530")
        sent_photos = [call.kwargs["photo"] for call in bot.send_photo.await_args_list]
        self.assertEqual(sent_photos, files, "必须发下载后的字节，不是外部 URL")
        for call in bot.send_photo.await_args_list:
            self.assertEqual(call.kwargs["chat_id"], SENDER_USER_ID)

    # ------------------------------------------------------------------
    # best-effort：单张失败继续、全失败不影响文字
    # ------------------------------------------------------------------

    async def test_failed_download_is_skipped_and_later_images_still_sent(self) -> None:
        bot = _bot()

        async def _download(url, referer="", **_kwargs):
            if url == SAMPLE_URLS[1]:
                return None
            return SimpleNamespace(name=url)

        with patch.object(
            commands, "_download_av_image_input_file", new=AsyncMock(side_effect=_download)
        ):
            sent = await commands._send_av_samples_to_sender_dm(
                bot=bot,
                sender_user_id=SENDER_USER_ID,
                detail=_detail(),
                limit=4,
            )

        self.assertEqual(sent, 3)
        self.assertEqual(bot.send_photo.await_count, 3)

    async def test_failed_send_continues_with_the_next_image(self) -> None:
        bot = _bot(sample_send_failures={2})

        sent = await self._direct_send(detail=_detail(), limit=4, bot=bot)

        self.assertEqual(sent, 3)
        self.assertEqual(bot.send_photo.await_count, 4)

    async def test_all_sample_sends_failing_reports_zero_without_raising(self) -> None:
        bot = _bot(full_failures=True)

        sent = await self._direct_send(detail=_detail(), limit=4, bot=bot)

        self.assertEqual(sent, 0)
        self.assertEqual(bot.send_photo.await_count, 4, "每张都试过，绝不重试同一张")

    async def test_forbidden_stops_further_samples(self) -> None:
        bot = _bot()
        calls = {"count": 0}

        async def _send_photo(**_kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise _forbidden()
            return SimpleNamespace(message_id=calls["count"])

        bot.send_photo = AsyncMock(side_effect=_send_photo)

        sent = await self._direct_send(detail=_detail(), limit=4, bot=bot)

        self.assertEqual(sent, 1)
        self.assertEqual(bot.send_photo.await_count, 2)

    async def test_retry_after_stops_further_samples(self) -> None:
        bot = _bot()
        calls = {"count": 0}

        async def _send_photo(**_kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise _retry_after()
            return SimpleNamespace(message_id=calls["count"])

        bot.send_photo = AsyncMock(side_effect=_send_photo)

        sent = await self._direct_send(detail=_detail(), limit=4, bot=bot)

        self.assertEqual(sent, 1, "被限流时不许把剩下的张数硬顶着全撞一遍")
        self.assertEqual(bot.send_photo.await_count, 2)

    async def test_no_valid_urls_sends_nothing(self) -> None:
        bot = _bot()

        sent = await self._direct_send(
            detail=_detail(sample_urls=["not-a-url", "", "ftp://x/y.jpg"]),
            limit=4,
            bot=bot,
        )

        self.assertEqual(sent, 0)
        bot.send_photo.assert_not_awaited()

    # ------------------------------------------------------------------
    # 群内：文字先出，样例图在后台任务里；私聊被拒时一张都不发
    # ------------------------------------------------------------------

    async def test_group_text_is_not_blocked_by_sample_uploads(self) -> None:
        order: list[str] = []
        bot = _bot()
        bot.send_photo = AsyncMock(
            side_effect=lambda **kwargs: (
                order.append(f"photo:{kwargs['chat_id']}") or SimpleNamespace(message_id=1)
            )
        )
        message = _group_message(bot=bot)
        message.answer = AsyncMock(
            side_effect=lambda *args, **kwargs: (
                order.append("group_text") or SimpleNamespace(message_id=10)
            )
        )
        download = AsyncMock(
            side_effect=lambda url, referer="", **kwargs: (
                order.append("download_sample") or SimpleNamespace(name=url)
            )
        )
        detail = _detail()

        with patch.object(commands, "_download_av_image_input_file", new=download):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=_settings(),
            )

            # 群内文字已经发出，样例图还一张没动（在独立后台任务里）。
            self.assertTrue(ok)
            self.assertEqual(order, [f"photo:{SENDER_USER_ID}", "group_text"])
            self.assertEqual(download.await_count, 0)
            self.assertTrue(
                commands._AV_SAMPLE_DM_TASKS,
                "样例图必须在独立后台任务里，而不是阻塞群内文字",
            )

            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self._assert_no_group_photo(message, bot)
        self.assertEqual(download.await_count, 4)
        self.assertEqual(order.count("download_sample"), 4)
        self.assertEqual(bot.send_photo.await_count, 5, "1 张封面 + 4 张样例图")

        # 群内文字照旧（提示 + 按钮），没有因为样例图变化。
        text = message.answer.await_args.args[0]
        self.assertIn(commands._AV_COVER_SENT_HINT, text)
        self.assertIn("<code>WANZ-530</code>", text)

        sample_calls = bot.send_photo.await_args_list[1:]
        for call in sample_calls:
            self.assertEqual(call.kwargs["chat_id"], SENDER_USER_ID)
            self.assertNotIn("reply_markup", call.kwargs)
        self.assertIn("其他图片", str(sample_calls[0].kwargs.get("caption") or ""))

    async def test_configured_five_samples_are_all_sent_after_group_text(self) -> None:
        bot = _bot()
        message = _group_message(bot=bot)
        detail = _detail()
        download = AsyncMock(side_effect=lambda url, referer="", **kwargs: SimpleNamespace())

        with patch.object(commands, "_download_av_image_input_file", new=download):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=_settings(samples=5),
            )
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertTrue(ok)
        self.assertEqual(download.await_count, 5)
        self.assertEqual(bot.send_photo.await_count, 6)
        self._assert_no_group_photo(message, bot)

    async def test_zero_disables_samples_and_keeps_old_behaviour(self) -> None:
        bot = _bot()
        message = _group_message(bot=bot)
        detail = _detail()
        download = AsyncMock(side_effect=AssertionError("关掉后不许下载样例图"))

        with patch.object(commands, "_download_av_image_input_file", new=download):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=_settings(samples=0),
            )
            await commands.flush_av_sample_dm_tasks(timeout_seconds=0.2)

        self.assertTrue(ok)
        download.assert_not_awaited()
        bot.send_photo.assert_awaited_once()
        self.assertEqual(bot.send_photo.await_args.kwargs["chat_id"], SENDER_USER_ID)
        self.assertEqual(commands._AV_SAMPLE_DM_TASKS, set())
        self._assert_no_group_photo(message, bot)

    async def test_private_blocked_sends_zero_samples_and_keeps_result_code(self) -> None:
        detail = _detail()

        # 结果码本身不变（直接调那个函数看返回值）。
        outcome = await commands._send_av_cover_to_sender_dm(
            bot=_bot(cover_error=_forbidden()),
            sender_user_id=SENDER_USER_ID,
            detail=detail,
        )
        self.assertEqual(outcome, commands._AV_COVER_PRIVATE_BLOCKED)

        bot = _bot(cover_error=_forbidden())
        message = _group_message(bot=bot)
        download = AsyncMock(side_effect=AssertionError("被拒时不许下载样例图"))

        with patch.object(commands, "_download_av_image_input_file", new=download):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=_settings(),
            )
            await commands.flush_av_sample_dm_tasks(timeout_seconds=0.2)

        self.assertTrue(ok)
        download.assert_not_awaited()
        bot.send_photo.assert_awaited_once()  # 只有那次被拒的封面尝试
        self.assertEqual(commands._AV_SAMPLE_DM_TASKS, set())
        self._assert_no_group_photo(message, bot)
        text = message.answer.await_args.args[0]
        self.assertIn(commands._AV_COVER_PRIVATE_BLOCKED_HINT, text)
        self.assertNotIn(commands._AV_COVER_SENT_HINT, text)

    async def test_all_downloads_failing_does_not_change_text_result(self) -> None:
        bot = _bot()
        message = _group_message(bot=bot)
        detail = _detail()
        download = AsyncMock(return_value=None)

        with patch.object(commands, "_download_av_image_input_file", new=download):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=_settings(),
            )
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertTrue(ok)
        self.assertEqual(download.await_count, 4)
        bot.send_photo.assert_awaited_once()  # 只有封面
        text = message.answer.await_args.args[0]
        self.assertIn(commands._AV_COVER_SENT_HINT, text)
        self._assert_no_group_photo(message, bot)

    async def test_sample_task_exceptions_never_escape(self) -> None:
        bot = _bot()
        message = _group_message(bot=bot)
        detail = _detail()

        with patch.object(
            commands,
            "_send_av_samples_to_sender_dm",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=_settings(),
            )
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertTrue(ok)
        self.assertEqual(commands._AV_SAMPLE_DM_TASKS, set())
        self.assertIn(commands._AV_COVER_SENT_HINT, message.answer.await_args.args[0])

    async def test_group_in_place_edit_sends_samples_after_group_text(self) -> None:
        bot = _bot()
        message = _group_message(bot=bot)
        detail = _detail()
        download = AsyncMock(side_effect=lambda url, referer="", **kwargs: SimpleNamespace())

        with patch.object(commands, "_download_av_image_input_file", new=download):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                in_place=True,
                settings=_settings(),
            )
            self.assertEqual(download.await_count, 0, "就地编辑路径也不许等样例图")
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertTrue(ok)
        message.answer.assert_not_awaited()
        self.assertIn(commands._AV_COVER_SENT_HINT, message.edit_text.await_args.args[0])
        self.assertEqual(download.await_count, 4)
        self._assert_no_group_photo(message, bot)

    async def test_group_in_place_private_blocked_sends_no_samples(self) -> None:
        bot = _bot(cover_error=_forbidden())
        message = _group_message(bot=bot)
        detail = _detail()
        download = AsyncMock(side_effect=AssertionError("被拒时不许下载样例图"))

        with patch.object(commands, "_download_av_image_input_file", new=download):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                in_place=True,
                settings=_settings(),
            )

        self.assertTrue(ok)
        download.assert_not_awaited()
        self.assertIn(
            commands._AV_COVER_PRIVATE_BLOCKED_HINT,
            message.edit_text.await_args.args[0],
        )
        self._assert_no_group_photo(message, bot)

    # ------------------------------------------------------------------
    # 私聊：封面之后补发（共用同一个发送函数）
    # ------------------------------------------------------------------

    async def test_private_text_lookup_sends_samples_after_cover(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        detail = _detail()
        download = AsyncMock(side_effect=lambda url, referer="", **kwargs: SimpleNamespace())

        with patch.object(commands, "_download_av_image_input_file", new=download):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=_settings(),
            )
            message.answer_photo.assert_awaited_once()
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertTrue(ok)
        self.assertEqual(message.answer_photo.await_args.kwargs["photo"], COVER_URL)
        self.assertEqual(download.await_count, 4)
        self.assertEqual(bot.send_photo.await_count, 4)
        for call in bot.send_photo.await_args_list:
            self.assertEqual(call.kwargs["chat_id"], SENDER_USER_ID)

    async def test_private_in_place_detail_also_sends_samples(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        detail = _detail()
        download = AsyncMock(side_effect=lambda url, referer="", **kwargs: SimpleNamespace())

        with patch.object(commands, "_download_av_image_input_file", new=download):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                in_place=True,
                settings=_settings(),
            )
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertTrue(ok)
        message.edit_media.assert_awaited_once()
        self.assertEqual(download.await_count, 4)
        self.assertEqual(bot.send_photo.await_count, 4)

    # ------------------------------------------------------------------
    # 识图反查成功那条入口（私聊 /av + 图片、群内 /av + 图片）
    # ------------------------------------------------------------------

    async def _image_lookup(self, message, *, header: str, order: list[str] | None = None):
        detail = _detail()
        service = SimpleNamespace(
            enabled=True,
            lookup_by_code=AsyncMock(return_value=detail),
            search=AsyncMock(return_value=[]),
        )
        with (
            patch("bot.handlers.commands.AVSearchService", return_value=service),
            patch("bot.handlers.commands.typing_action", return_value=_AsyncContext()),
        ):
            await commands._send_av_query_result(
                message,
                _settings(),
                query="WANZ-530",
                header=header,
            )
        return detail

    async def test_group_image_lookup_gets_samples_after_group_text(self) -> None:
        order: list[str] = []
        bot = _bot()
        bot.send_photo = AsyncMock(
            side_effect=lambda **kwargs: (
                order.append(f"photo:{kwargs['chat_id']}") or SimpleNamespace(message_id=1)
            )
        )
        message = _group_message(bot=bot)
        message.answer = AsyncMock(
            side_effect=lambda *args, **kwargs: (
                order.append("group_text") or SimpleNamespace(message_id=10)
            )
        )
        download = AsyncMock(
            side_effect=lambda url, referer="", **kwargs: (
                order.append("download_sample") or SimpleNamespace(name=url)
            )
        )

        with patch.object(commands, "_download_av_image_input_file", new=download):
            await self._image_lookup(message, header="<b>甲</b> 的识图结果")
            self.assertEqual(order, [f"photo:{SENDER_USER_ID}", "group_text"])
            self.assertEqual(download.await_count, 0)
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertEqual(download.await_count, 4)
        self.assertEqual(bot.send_photo.await_count, 5)
        self._assert_no_group_photo(message, bot)
        self.assertIn("识图结果", message.answer.await_args.args[0])

    async def test_private_image_lookup_gets_samples_after_cover(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        download = AsyncMock(side_effect=lambda url, referer="", **kwargs: SimpleNamespace())

        with patch.object(commands, "_download_av_image_input_file", new=download):
            await self._image_lookup(message, header="")
            message.answer_photo.assert_awaited_once()
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertEqual(download.await_count, 4)
        self.assertEqual(bot.send_photo.await_count, 4)
        for call in bot.send_photo.await_args_list:
            self.assertEqual(call.kwargs["chat_id"], SENDER_USER_ID)

    # ------------------------------------------------------------------
    # 「群里永远没有照片」扩展到全部新路径
    # ------------------------------------------------------------------

    async def test_every_group_path_keeps_photos_out_of_the_group(self) -> None:
        cases = (
            ("dm_sent", None, _settings()),
            ("dm_blocked", _forbidden(), _settings()),
            ("dm_failed", RuntimeError("boom"), _settings()),
            ("samples_off", None, _settings(samples=0)),
            ("samples_five", None, _settings(samples=5)),
        )
        for name, cover_error, settings in cases:
            with self.subTest(case=name):
                bot = _bot(cover_error=cover_error)
                message = _group_message(bot=bot)
                detail = _detail()
                download = AsyncMock(return_value=SimpleNamespace())
                with patch.object(
                    commands, "_download_av_image_input_file", new=download
                ), patch.object(
                    commands,
                    "_download_cover_input_file",
                    new=AsyncMock(return_value=None),
                ):
                    ok = await commands._send_av_detail(
                        message=message,
                        session=_session(detail=detail),
                        result_idx=0,
                        detail=detail,
                        settings=settings,
                    )
                    await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

                self.assertTrue(ok)
                self._assert_no_group_photo(message, bot)
                self.assertIn(detail.title, message.answer.await_args.args[0])


class AVSampleRuntimeConfigTests(unittest.TestCase):
    """可调的条数开关：默认 4、0=关、超过 5 直接拒绝（运行时配置也要带得动）。"""

    def test_sample_count_bounds_are_validated(self) -> None:
        self.assertEqual(AVSettingsConfig().dm_sample_count, 4)
        with self.assertRaises(ValidationError):
            AVSettingsConfig(dm_sample_count=6)
        with self.assertRaises(ValidationError):
            AVSettingsConfig(dm_sample_count=-1)
        self.assertEqual(AVSettingsConfig(dm_sample_count=0).dm_sample_count, 0)

    def test_runtime_config_round_trips_the_sample_count(self) -> None:
        legacy = Settings(_env_file=None)
        legacy.av_dm_sample_count = 5

        imported = build_legacy_runtime_config(
            "/tmp/nonexistent-smart-group-bot.toml",
            settings=legacy,
            raw_env={},
        )
        self.assertEqual(imported.av.dm_sample_count, 5)

        target = Settings(_env_file=None)
        imported.apply_to_settings(target, apply_prompts=False)
        self.assertEqual(target.av_dm_sample_count, 5)
        self.assertEqual(commands._av_dm_sample_limit(target), 5)


class AVSampleGroupImageEntryTests(unittest.IsolatedAsyncioTestCase):
    """群内「/av + 图片」端到端：先删图，群内只文字，样例图只在私聊。"""

    async def asyncTearDown(self) -> None:
        await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

    async def test_group_image_lookup_end_to_end_sends_samples_only_to_dm(self) -> None:
        order: list[str] = []
        bot = _bot()
        bot.send_photo = AsyncMock(
            side_effect=lambda **kwargs: (
                order.append(f"photo:{kwargs['chat_id']}") or SimpleNamespace(message_id=1)
            )
        )
        message = _group_message(bot=bot, text="/av")
        message.photo = [SimpleNamespace(file_id="photo-1", file_size=50000)]
        message.caption = None
        message.delete = AsyncMock()
        message.answer = AsyncMock(
            side_effect=lambda *args, **kwargs: (
                order.append("group_text") or SimpleNamespace(message_id=10)
            )
        )
        detail = _detail()
        service = SimpleNamespace(
            enabled=True,
            lookup_by_code=AsyncMock(return_value=detail),
            search=AsyncMock(return_value=[]),
        )
        download = AsyncMock(
            side_effect=lambda url, referer="", **kwargs: (
                order.append("download_sample") or SimpleNamespace(name=url)
            )
        )

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
            patch.object(
                commands,
                return_value=("photo-1", "image/jpeg", 50000),
            ),
            patch.object(
                commands,
                new=AsyncMock(
                    return_value=commands._AVImageVisionOutcome(150, 0, "WANZ-530", False, False)
                ),
            ),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_download_av_image_input_file", new=download),
        ):
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )
            # 图片先删（顺序要求没动）；群内文字已经出去，样例图还没开始。
            message.delete.assert_awaited()
            self.assertEqual(order, [f"photo:{SENDER_USER_ID}", "group_text"])
            self.assertEqual(download.await_count, 0)
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertEqual(download.await_count, 4)
        self.assertEqual(bot.send_photo.await_count, 5)
        self._assert_no_group_photo(message, bot)
        self.assertIn("的识图结果", message.answer.await_args.args[0])

    def _assert_no_group_photo(self, message, bot, *, group_chat_id: int = GROUP_CHAT_ID) -> None:
        message.answer_photo.assert_not_awaited()
        message.edit_media.assert_not_awaited()
        message.edit_caption.assert_not_awaited()
        for call in bot.send_photo.await_args_list:
            self.assertNotEqual(
                int(call.kwargs.get("chat_id", 0)),
                group_chat_id,
                "有照片被发到了群里",
            )


class AVSampleTaskSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

    def test_schedule_rejects_when_nothing_to_send(self) -> None:
        bot = _bot()
        self.assertFalse(
            commands._schedule_av_samples_dm(
                bot=bot,
                sender_user_id=SENDER_USER_ID,
                detail=_detail(sample_urls=[]),
                settings=_settings(),
            )
        )
        self.assertFalse(
            commands._schedule_av_samples_dm(
                bot=bot,
                sender_user_id=SENDER_USER_ID,
                detail=_detail(),
                settings=_settings(samples=0),
            )
        )
        self.assertFalse(
            commands._schedule_av_samples_dm(
                bot=bot,
                sender_user_id=0,
                detail=_detail(),
                settings=_settings(),
            )
        )
        self.assertEqual(commands._AV_SAMPLE_DM_TASKS, set())

    async def test_scheduled_task_is_tracked_then_released(self) -> None:
        bot = _bot()
        started = asyncio.Event()
        release = asyncio.Event()

        async def _send(**_kwargs):
            started.set()
            await release.wait()

        with patch.object(commands, "_send_av_samples_to_sender_dm", new=_send):
            scheduled = commands._schedule_av_samples_dm(
                bot=bot,
                sender_user_id=SENDER_USER_ID,
                detail=_detail(),
                settings=_settings(),
            )
            self.assertTrue(scheduled)
            await started.wait()
            self.assertEqual(len(commands._AV_SAMPLE_DM_TASKS), 1)
            release.set()
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)

        self.assertEqual(commands._AV_SAMPLE_DM_TASKS, set())

    async def test_task_capacity_limit_skips_instead_of_piling_up(self) -> None:
        bot = _bot()
        release = asyncio.Event()

        async def _send(**_kwargs):
            await release.wait()

        with patch.object(commands, "_send_av_samples_to_sender_dm", new=_send), patch.object(
            commands, "_AV_SAMPLE_DM_TASK_LIMIT", 1
        ):
            self.assertTrue(
                commands._schedule_av_samples_dm(
                    bot=bot,
                    sender_user_id=SENDER_USER_ID,
                    detail=_detail(),
                    settings=_settings(),
                )
            )
            while not commands._AV_SAMPLE_DM_TASKS:
                await asyncio.sleep(0)
            self.assertFalse(
                commands._schedule_av_samples_dm(
                    bot=bot,
                    sender_user_id=SENDER_USER_ID,
                    detail=_detail(),
                    settings=_settings(),
                )
            )
            release.set()
            await commands.flush_av_sample_dm_tasks(timeout_seconds=2.0)


if __name__ == "__main__":
    unittest.main()
