"""``/av`` 私聊结果：内联「下载地址」+ 独立「制作信息」块（+ 可选 AI 题材概述）。

规格逐条对应：

- 私聊正文（标题 + 简介之后）内联下载地址：默认 3 条、硬上限 5 条、
  ``av.inline_seed_count=0`` 关闭该块；磁力/大小/日期全部来自**已经抓好的**
  ``AVDetail.seeds``（本功能不发任何新的外部请求）；
- 每条两行：``大小 · 日期 · 标题``（空字段省掉分隔符）+ ``<code>磁力链</code>``；
- 磁力串里的 ``&`` 必须转义成 ``&amp;``，否则 Telegram 会直接报解析错误；
- **只在私聊**：群内文案与加这个功能之前**逐字一致**（回归断言），一个字都不改；
- 磁力链不截断：整条消息超长时按 N→N-1→…→1 减条数，连一条都放不下就整块不出现；
- 制作信息（片商 / 发行商 / 导演 / 系列 / 演员 / 时长 / 类型）单独成块，字段一个不少；
- AI 概述默认关：关闭时**一次模型都不调用**（连 LLMService 都不构造）；
  开启时调用一次、stage=``synopsis``、正文标注「AI 概述，非官方剧情」；
  失败 / 空返回只跳过这一块，绝不让整次查询失败。

所有 Telegram / LLM / 网络调用都是 mock，测试不触网。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from bot.config import Settings
from bot.handlers import commands
from bot.services import llm as llm_module
from bot.services.av_search import AVDetail, AVQuerySessionStore, AVSearchItem, AVSeed
from bot.services.llm import LLMService
from bot.services.runtime_config import AVSettingsConfig, build_legacy_runtime_config

GROUP_CHAT_ID = -10001
SENDER_USER_ID = 123
COVER_URL = "https://www.javbus.com/pics/cover/aqcf_b.jpg"

_APP_JS = Path(__file__).resolve().parents[1] / "bot" / "web" / "static" / "app.js"

#: 群内正文的冻结快照：加内联下载地址前后必须逐字一致（见测试）。
_GROUP_TEXT_SNAPSHOT = (
    "<b>影片详情</b>\n"
    "\n"
    "<blockquote><b>来源</b>　<code>JAVBUS</code>\n"
    "<b>番号</b>　<code>SONE-342</code>\n"
    "<b>发行日期</b>　<code>2024-09-06</code>\n"
    "<b>时长</b>　158分鐘\n"
    "<b>评分</b>　<code>4.2</code>\n"
    "<b>导演</b>　真咲南朋\n"
    "<b>制作商</b>　エスワン ナンバーワンスタイル\n"
    "<b>发行商</b>　S1 NO.1 STYLE\n"
    "<b>系列</b>　●●式メンズエステ\n"
    "<b>演员</b>　清原みゆう\n"
    "<b>类型</b>　巨乳 / 單體作品\n"
    "<b>种子</b>　<code>5</code> 条（点下方按钮浏览）\n"
    '<b>详情页</b>　<a href="https://www.javbus.com/SONE-342">打开详情页</a></blockquote>\n'
    "\n"
    "<b>SONE-342 奇跡のおっぱい</b>\n"
    "\n"
    "这里是简介 &lt;不是标签&gt;"
)


def _settings(
    *,
    inline: int = commands.AV_INLINE_SEED_DEFAULT,
    synopsis: bool = False,
    samples: int = 0,
) -> Settings:
    settings = Settings(_env_file=None)
    settings.bot.auto_delete_seconds = 3
    settings.bot.auto_delete_categories = ["management"]
    settings.av_inline_seed_count = inline
    settings.av_ai_synopsis_enabled = synopsis
    # 样例图与本文件无关：默认关掉，免得后台任务干扰断言。
    settings.av_dm_sample_count = samples
    return settings


SEED_TITLES = ("SONE-342-中文字幕", "SONE-342 本編", "SONE-342 [4K]", "SONE-342 HD", "SONE-342 素人")


def _seeds(count: int = 5, *, magnet_len: int = 0) -> list[AVSeed]:
    seeds: list[AVSeed] = []
    for index in range(count):
        magnet = f"magnet:?xt=urn:btih:{index:040X}&dn=SONE-342&tr=udp%3A%2F%2Ftracker.test%3A80"
        if magnet_len:
            magnet = magnet + "&pad=" + ("x" * max(0, magnet_len - len(magnet)))
        seeds.append(
            AVSeed(
                title=SEED_TITLES[index % len(SEED_TITLES)],
                magnet=magnet,
                size=f"{index + 1}.10GB",
                date=f"2026-04-0{index + 1}",
            )
        )
    return seeds


def _detail(
    *,
    seeds: list[AVSeed] | None = None,
    cover_url: str = "",
    with_fields: bool = True,
) -> AVDetail:
    return AVDetail(
        source="javbus",
        title="SONE-342 奇跡のおっぱい",
        url="https://www.javbus.com/SONE-342",
        code="SONE-342",
        cover_url=cover_url,
        date="2024-09-06",
        runtime="158分鐘",
        score="4.2",
        actors=["清原みゆう"],
        genres=["巨乳", "單體作品"],
        director="真咲南朋",
        studio="エスワン ナンバーワンスタイル",
        publisher="S1 NO.1 STYLE",
        series="●●式メンズエステ",
        summary="这里是简介 <不是标签>",
        seeds=_seeds() if seeds is None else seeds,
    )


def _session(*, detail: AVDetail | None = None, owner_user_id: int = SENDER_USER_ID):
    store = AVQuerySessionStore()
    item = AVSearchItem(
        source="javbus",
        title="SONE-342 奇跡のおっぱい",
        url="https://www.javbus.com/SONE-342",
        code="SONE-342",
        cover_url=COVER_URL,
    )
    av_session = store.create(owner_user_id=owner_user_id, query="SONE-342", results=[item])
    if detail is not None:
        av_session.details[0] = detail
    return av_session


def _bot() -> SimpleNamespace:
    return SimpleNamespace(
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        get_me=AsyncMock(return_value=SimpleNamespace(username="CoolAvBot")),
    )


def _group_message(*, bot) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=GROUP_CHAT_ID, type="supergroup", title="test group"),
        from_user=SimpleNamespace(id=SENDER_USER_ID),
        bot=bot,
        text="/av SONE-342",
        reply_markup=None,
        photo=None,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        answer_photo=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
        edit_text=AsyncMock(return_value=SimpleNamespace(message_id=10)),
        edit_media=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
        edit_caption=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
    )


def _private_message(*, bot, **overrides) -> SimpleNamespace:
    message = SimpleNamespace(
        chat=SimpleNamespace(id=SENDER_USER_ID, type="private", title=""),
        from_user=SimpleNamespace(id=SENDER_USER_ID),
        bot=bot,
        text="/av SONE-342",
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


class AVInlineSeedRenderTests(unittest.TestCase):
    """纯渲染：条数、转义、缺字段、超长缩条。"""

    def test_group_text_is_byte_identical_and_never_inlines_magnets(self) -> None:
        text = commands._build_av_detail_text(_detail())

        self.assertEqual(text, _GROUP_TEXT_SNAPSHOT, "群内文案必须逐字不变")
        self.assertNotIn("下载地址", text)
        self.assertNotIn("magnet:", text)
        self.assertNotIn("制作信息", text)

    def test_private_text_inlines_size_date_title_and_magnet(self) -> None:
        detail = _detail()
        text = commands._build_av_detail_text(detail, private_view=True, inline_seed_count=3)

        self.assertIn("<b>下载地址</b>", text)
        for index in range(3):
            seed = detail.seeds[index]
            self.assertIn(f"{seed.size} · {seed.date} · {seed.title}", text)
            self.assertIn(f"<code>{seed.magnet.replace('&', '&amp;')}</code>", text)
        for index in range(3, 5):
            self.assertNotIn(f"urn:btih:{index:040X}", text, "第 4、5 条留给按钮翻页")

    def test_private_view_defaults_to_three_seeds(self) -> None:
        text = commands._build_av_detail_text(_detail(), private_view=True)

        self.assertEqual(text, commands._build_av_detail_text(
            _detail(), private_view=True, inline_seed_count=3
        ))

    def test_inline_count_three_five_ten_and_zero(self) -> None:
        detail = _detail()

        for count, expected in ((3, 3), (5, 5), (10, 5)):
            with self.subTest(count=count):
                text = commands._build_av_detail_text(
                    detail, private_view=True, inline_seed_count=count
                )
                self.assertEqual(
                    text.count("<code>magnet:"), expected, "硬上限 5，10 必须夹到 5"
                )
                self.assertIn("<b>下载地址</b>", text)

        for count in (0, -1, -10):
            with self.subTest(count=count):
                text = commands._build_av_detail_text(
                    detail, private_view=True, inline_seed_count=count
                )
                self.assertNotIn("<b>下载地址</b>", text)
                self.assertNotIn("magnet:", text)
                # 关掉内联块不动其它内容：按钮那条计数行照旧。
                self.assertIn("<b>种子</b>　<code>5</code> 条（点下方按钮浏览）", text)

    def test_magnet_ampersands_are_escaped(self) -> None:
        detail = _detail(seeds=[AVSeed(
            title="A&B <x>",
            magnet="magnet:?xt=urn:btih:AAAA&dn=a&tr=b",
            size="2.10GB",
            date="2026-04-06",
        )])

        text = commands._build_av_detail_text(detail, private_view=True, inline_seed_count=1)

        self.assertIn("<code>magnet:?xt=urn:btih:AAAA&amp;dn=a&amp;tr=b</code>", text)
        self.assertNotIn("magnet:?xt=urn:btih:AAAA&dn", text, "裸 & 会让 Telegram 解析失败")
        self.assertIn("A&amp;B &lt;x&gt;", text)

    def test_missing_size_date_title_renders_without_crash(self) -> None:
        detail = _detail(seeds=[
            AVSeed(title="", magnet="magnet:?xt=urn:btih:ONE", size="", date=""),
            AVSeed(title="只有标题", magnet="magnet:?xt=urn:btih:TWO", size="1GB", date=""),
            AVSeed(title="", magnet="magnet:?xt=urn:btih:THREE", size="", date="2026-01-01"),
        ])

        text = commands._build_av_detail_text(detail, private_view=True, inline_seed_count=3)

        self.assertIn("Magnet\n<code>magnet:?xt=urn:btih:ONE</code>", text)
        self.assertIn("1GB · 只有标题\n<code>magnet:?xt=urn:btih:TWO</code>", text)
        self.assertIn("2026-01-01 · Magnet\n<code>magnet:?xt=urn:btih:THREE</code>", text)
        self.assertNotIn(" ·  · ", text, "空字段不许留下悬空分隔符")
        self.assertNotIn(" · \n", text)
        self.assertNotIn("\n · ", text)

    def test_empty_seeds_omit_the_download_block(self) -> None:
        text = commands._build_av_detail_text(
            _detail(seeds=[]), private_view=True, inline_seed_count=5
        )

        self.assertNotIn("下载地址", text)
        self.assertNotIn("magnet:", text)
        self.assertIn("<b>种子</b>　无", text)

    def test_seed_without_magnet_is_skipped(self) -> None:
        detail = _detail(seeds=[
            AVSeed(title="坏的", magnet="", size="1GB", date="2026-01-01"),
            AVSeed(title="好的", magnet="magnet:?xt=urn:btih:GOOD", size="2GB", date="2026-01-02"),
        ])

        text = commands._build_av_detail_text(detail, private_view=True, inline_seed_count=5)

        self.assertIn("2GB · 2026-01-02 · 好的", text)
        self.assertNotIn("坏的", text)
        self.assertNotIn("1GB", text)

    def test_long_message_shrinks_the_count_instead_of_truncating_magnets(self) -> None:
        detail = _detail(seeds=_seeds(5, magnet_len=1400))
        text = commands._build_av_detail_text(detail, private_view=True, inline_seed_count=3)

        self.assertIn("下载地址", text)
        self.assertLessEqual(len(text), 4096)
        # 磁力链要么整条出现，要么不出现（截断过的磁力是坏链）。
        self.assertEqual(text.count("<code>magnet:"), 2, "3 条放不下就减到 2 条")
        for seed in detail.seeds[:2]:
            self.assertIn(seed.magnet.replace("&", "&amp;"), text)

    def test_when_even_one_seed_does_not_fit_the_block_disappears(self) -> None:
        detail = _detail(seeds=_seeds(2, magnet_len=3200))
        text = commands._build_av_detail_text(detail, private_view=True, inline_seed_count=3)

        self.assertNotIn("下载地址", text)
        self.assertNotIn("magnet:", text)
        self.assertLessEqual(len(text), commands._AV_INLINE_SEED_BUDGET)
        # 其它块照旧：制作信息与按钮计数行一个都不少。
        self.assertIn("<b>制作信息</b>", text)
        self.assertIn("<b>种子</b>　<code>2</code> 条（点下方按钮浏览）", text)

    def test_making_info_block_keeps_every_field(self) -> None:
        text = commands._build_av_detail_text(_detail(), private_view=True, inline_seed_count=0)

        block = text.split("<b>制作信息</b>", 1)[1]
        for field in ("制作商", "发行商", "导演", "系列", "演员", "时长", "类型"):
            self.assertIn(f"<b>{field}</b>", block)
        self.assertNotIn("<b>时长</b>", text.split("<b>制作信息</b>", 1)[0])


class AVInlineSeedLimitTests(unittest.TestCase):
    def test_limit_reads_settings_with_cap_and_off_switch(self) -> None:
        self.assertEqual(commands._av_inline_seed_limit(None), 3)
        self.assertEqual(commands._av_inline_seed_limit(_settings(inline=3)), 3)
        self.assertEqual(commands._av_inline_seed_limit(_settings(inline=5)), 5)
        self.assertEqual(commands._av_inline_seed_limit(_settings(inline=10)), 5)
        self.assertEqual(commands._av_inline_seed_limit(_settings(inline=0)), 0)
        self.assertEqual(commands._av_inline_seed_limit(_settings(inline=-4)), 0)
        self.assertEqual(
            commands._av_inline_seed_limit(SimpleNamespace(av_inline_seed_count="abc")), 3
        )
        self.assertEqual(
            commands._av_inline_seed_limit(SimpleNamespace(av_inline_seed_count="2")), 2
        )

    def test_hard_cap_is_five(self) -> None:
        self.assertEqual(commands.AV_INLINE_SEED_HARD_CAP, 5)
        self.assertEqual(commands.AV_INLINE_SEED_DEFAULT, 3)


class AVInlineSeedSendTests(unittest.IsolatedAsyncioTestCase):
    """发送路径：私聊有、群内没有（逐字断言）。"""

    async def test_group_send_has_no_download_block(self) -> None:
        bot = _bot()
        message = _group_message(bot=bot)
        detail = _detail()

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
            settings=_settings(),
        )

        self.assertTrue(ok)
        text = message.answer.await_args.args[0]
        self.assertEqual(text.split("\n\n<i>")[0], _GROUP_TEXT_SNAPSHOT)
        self.assertNotIn("下载地址", text)
        self.assertNotIn("magnet:", text)
        self.assertEqual(message.answer.await_count, 1)

    async def test_private_without_cover_answers_with_download_block(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        detail = _detail(cover_url="")

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
            settings=_settings(inline=3),
        )

        self.assertTrue(ok)
        message.answer_photo.assert_not_awaited()
        self.assertEqual(message.answer.await_count, 1)
        text = message.answer.await_args.args[0]
        self.assertIn("<b>下载地址</b>", text)
        self.assertIn(detail.seeds[0].magnet.replace("&", "&amp;"), text)
        self.assertNotIn(detail.seeds[3].magnet.replace("&", "&amp;"), text)

    async def test_private_with_cover_sends_detail_text_after_the_photo(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        detail = _detail(cover_url=COVER_URL)

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
            settings=_settings(inline=2),
        )

        self.assertTrue(ok)
        message.answer_photo.assert_awaited_once()
        caption = message.answer_photo.await_args.kwargs["caption"]
        self.assertNotIn("magnet:", caption, "封面 caption 装不下磁力链")
        self.assertIn("SONE-342", caption)
        self.assertEqual(message.answer.await_count, 1)
        text = message.answer.await_args.args[0]
        self.assertIn("<b>下载地址</b>", text)
        self.assertIn(detail.seeds[0].magnet.replace("&", "&amp;"), text)
        self.assertIn(detail.seeds[1].magnet.replace("&", "&amp;"), text)
        self.assertNotIn(detail.seeds[2].magnet.replace("&", "&amp;"), text)

    async def test_private_cover_with_count_zero_behaves_exactly_as_before(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        detail = _detail(cover_url=COVER_URL)

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
            settings=_settings(inline=0),
        )

        self.assertTrue(ok)
        message.answer_photo.assert_awaited_once()
        message.answer.assert_not_awaited()
        self.assertIn(
            "<code>5</code> 条（可翻页）",
            message.answer_photo.await_args.kwargs["caption"],
        )

    async def test_private_in_place_edit_sends_follow_up_only_when_inline_is_on(self) -> None:
        for inline, expected in ((3, 1), (0, 0)):
            with self.subTest(inline=inline):
                bot = _bot()
                message = _private_message(bot=bot)
                detail = _detail(cover_url=COVER_URL)

                ok = await commands._send_av_detail(
                    message=message,
                    session=_session(detail=detail),
                    result_idx=0,
                    detail=detail,
                    in_place=True,
                    settings=_settings(inline=inline),
                )

                self.assertTrue(ok)
                message.edit_media.assert_awaited_once()
                self.assertEqual(message.answer.await_count, expected)
                if expected:
                    self.assertIn(
                        "<b>下载地址</b>", message.answer.await_args.args[0]
                    )

    async def test_follow_up_failure_does_not_break_the_detail_send(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        message.answer = AsyncMock(side_effect=RuntimeError("flood wait"))
        detail = _detail(cover_url=COVER_URL)

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
            settings=_settings(inline=3),
        )

        self.assertTrue(ok)
        message.answer_photo.assert_awaited_once()

    async def test_group_in_place_edit_never_inlines_magnets(self) -> None:
        bot = _bot()
        message = _group_message(bot=bot)
        detail = _detail()

        ok = await commands._send_av_detail(
            message=message,
            session=_session(detail=detail),
            result_idx=0,
            detail=detail,
            in_place=True,
            settings=_settings(),
        )

        self.assertTrue(ok)
        text = message.edit_text.await_args.args[0]
        self.assertEqual(text.split("\n\n<i>")[0], _GROUP_TEXT_SNAPSHOT)
        self.assertNotIn("magnet:", text)


class AVAiSynopsisTests(unittest.IsolatedAsyncioTestCase):
    """AI 概述：默认关（零调用）、开启调用一次、失败只跳过这一块。"""

    async def test_disabled_never_touches_the_llm(self) -> None:
        detail = _detail(cover_url="")

        for label, make_message, settings in (
            ("private_default", _private_message, _settings(synopsis=False)),
            ("private_no_settings", _private_message, None),
            ("group_default", _group_message, _settings(synopsis=False)),
        ):
            with self.subTest(case=label), patch.object(
                commands, "LLMService"
            ) as llm_cls:
                message = make_message(bot=_bot())
                ok = await commands._send_av_detail(
                    message=message,
                    session=_session(detail=detail),
                    result_idx=0,
                    detail=detail,
                    settings=settings,
                )

                self.assertTrue(ok)
                llm_cls.assert_not_called()
                self.assertNotIn(
                    commands._AV_SYNOPSIS_LABEL, message.answer.await_args.args[0]
                )

        with patch.object(commands, "LLMService") as llm_cls:
            self.assertEqual(
                await commands._build_av_ai_synopsis(detail, _settings(synopsis=False)),
                "",
            )
            llm_cls.assert_not_called()

        self.assertTrue(commands._av_ai_synopsis_enabled(SimpleNamespace(av_ai_synopsis_enabled=True)))
        self.assertFalse(commands._av_ai_synopsis_enabled(None))
        self.assertFalse(commands._av_ai_synopsis_enabled(SimpleNamespace()))
        self.assertTrue(
            commands._av_ai_synopsis_enabled(SimpleNamespace(av_ai_synopsis_enabled="on"))
        )
        self.assertFalse(
            commands._av_ai_synopsis_enabled(SimpleNamespace(av_ai_synopsis_enabled="off"))
        )

    async def test_enabled_calls_the_llm_once_with_synopsis_stage_and_labels_output(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        detail = _detail(cover_url="")
        llm = SimpleNamespace(chat=AsyncMock(return_value="  这是一部以按摩店题材为主的作品。  "))
        settings = _settings(synopsis=True)

        with patch.object(commands, "_av_synopsis_llm", return_value=llm) as factory:
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=settings,
            )

        self.assertTrue(ok)
        factory.assert_called_once()
        llm.chat.assert_awaited_once()
        messages = llm.chat.await_args.args[0]
        self.assertEqual(llm.chat.await_args.kwargs.get("stage"), "synopsis")
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("剧情", system)
        self.assertIn(detail.title, user)
        self.assertIn("巨乳", user)
        self.assertIn("清原みゆう", user)
        self.assertIn("158分鐘", user)

        text = message.answer.await_args.args[0]
        self.assertIn(f"<b>{commands._AV_SYNOPSIS_LABEL}</b>", text)
        self.assertIn("这是一部以按摩店题材为主的作品。", text)
        self.assertNotIn("  这是一部", text)

    async def test_enabled_with_cover_still_shows_the_synopsis(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        detail = _detail(cover_url=COVER_URL, seeds=[])
        llm = SimpleNamespace(chat=AsyncMock(return_value="题材概述。"))
        settings = _settings(synopsis=True, inline=0)

        with patch.object(commands, "_av_synopsis_llm", return_value=llm):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=settings,
            )

        self.assertTrue(ok)
        self.assertEqual(message.answer.await_count, 1)
        self.assertIn(commands._AV_SYNOPSIS_LABEL, message.answer.await_args.args[0])

    async def test_synopsis_failure_is_skipped_and_result_still_returns(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        detail = _detail(cover_url="")

        for error in (RuntimeError("boom"), TimeoutError("slow"), ValueError("bad")):
            with self.subTest(error=type(error).__name__):
                llm = SimpleNamespace(chat=AsyncMock(side_effect=error))
                with patch.object(commands, "_av_synopsis_llm", return_value=llm):
                    ok = await commands._send_av_detail(
                        message=message,
                        session=_session(detail=detail),
                        result_idx=0,
                        detail=detail,
                        settings=_settings(synopsis=True),
                    )

                self.assertTrue(ok, "概述失败绝不能让整次查询失败")
                text = message.answer.await_args.args[0]
                self.assertNotIn(commands._AV_SYNOPSIS_LABEL, text)
                self.assertIn("<b>下载地址</b>", text, "其它块照旧")

    async def test_empty_synopsis_response_is_skipped(self) -> None:
        bot = _bot()
        message = _private_message(bot=bot)
        detail = _detail(cover_url="")

        for value in ("", "   ", "\n\n"):
            with self.subTest(value=repr(value)):
                llm = SimpleNamespace(chat=AsyncMock(return_value=value))
                with patch.object(commands, "_av_synopsis_llm", return_value=llm):
                    ok = await commands._send_av_detail(
                        message=message,
                        session=_session(detail=detail),
                        result_idx=0,
                        detail=detail,
                        settings=_settings(synopsis=True),
                    )

                self.assertTrue(ok)
                self.assertNotIn(
                    commands._AV_SYNOPSIS_LABEL, message.answer.await_args.args[0]
                )

    async def test_synopsis_is_truncated_and_newlines_collapsed(self) -> None:
        llm = SimpleNamespace(chat=AsyncMock(return_value="第一行\n第二行" + "长" * 500))

        with patch.object(commands, "_av_synopsis_llm", return_value=llm):
            text = await commands._build_av_ai_synopsis(_detail(), _settings(synopsis=True))

        self.assertTrue(text)
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), commands._AV_SYNOPSIS_MAX_CHARS + 3)

    def test_payload_only_contains_known_fields(self) -> None:
        payload = commands._build_av_synopsis_input(_detail())

        self.assertIn("标题：", payload)
        self.assertIn("类型：", payload)
        self.assertIn("系列：", payload)
        self.assertIn("演员：", payload)
        self.assertIn("时长：", payload)
        self.assertNotIn("magnet:", payload)
        self.assertNotIn("https://", payload)

    async def test_group_never_gets_the_synopsis_even_when_enabled(self) -> None:
        bot = _bot()
        message = _group_message(bot=bot)
        detail = _detail()
        llm = SimpleNamespace(chat=AsyncMock(return_value="题材概述。"))

        with patch.object(commands, "_av_synopsis_llm", return_value=llm):
            ok = await commands._send_av_detail(
                message=message,
                session=_session(detail=detail),
                result_idx=0,
                detail=detail,
                settings=_settings(synopsis=True),
            )

        self.assertTrue(ok)
        llm.chat.assert_not_awaited()
        text = message.answer.await_args.args[0]
        self.assertNotIn(commands._AV_SYNOPSIS_LABEL, text)
        self.assertEqual(text.split("\n\n<i>")[0], _GROUP_TEXT_SNAPSHOT)


class AVAiSynopsisLlmStageTests(unittest.IsolatedAsyncioTestCase):
    """``stage="synopsis"`` 只改用量统计口径，不换模型端点。"""

    async def test_chat_stage_overrides_the_metric_label_only(self) -> None:
        cfg = llm_module.ModelConfig(model="openai/gpt-4.1", api_key="test-key")
        llm = LLMService(cfg, cfg, cfg)
        completions = AsyncMock(return_value="ok")

        with patch.object(LLMService, "_chat_with_fallbacks", new=completions):
            result = await llm.chat([{"role": "user", "content": "hi"}], stage="synopsis")
            self.assertEqual(result, "ok")
            self.assertEqual(completions.await_args.kwargs["label"], "synopsis")
            candidates = completions.await_args.kwargs["candidates"]
            self.assertEqual(candidates[0].model, "openai/gpt-4.1")

            await llm.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(completions.await_args.kwargs["label"], "main")

    async def test_synopsis_stage_has_a_tight_deadline(self) -> None:
        self.assertIn("synopsis", llm_module._LLM_STAGE_DEADLINES)
        self.assertLessEqual(
            LLMService._stage_deadline_seconds("synopsis"),
            commands._AV_SYNOPSIS_TIMEOUT_SEC * 2,
        )


class AVInlineSeedRuntimeConfigTests(unittest.TestCase):
    """两个新开关都走运行时配置（``av.*``），并有 Web UI 入口。"""

    def test_new_settings_default_to_three_and_off(self) -> None:
        settings = Settings(_env_file=None)

        self.assertEqual(settings.av_inline_seed_count, 3)
        self.assertFalse(settings.av_ai_synopsis_enabled)
        self.assertEqual(AVSettingsConfig().inline_seed_count, 3)
        self.assertFalse(AVSettingsConfig().ai_synopsis_enabled)

    def test_inline_count_bounds_are_validated(self) -> None:
        self.assertEqual(AVSettingsConfig(inline_seed_count=5).inline_seed_count, 5)
        self.assertEqual(AVSettingsConfig(inline_seed_count=0).inline_seed_count, 0)
        with self.assertRaises(ValidationError):
            AVSettingsConfig(inline_seed_count=6)
        with self.assertRaises(ValidationError):
            AVSettingsConfig(inline_seed_count=-1)

    def test_runtime_config_round_trips_the_new_switches(self) -> None:
        legacy = Settings(_env_file=None)
        legacy.av_inline_seed_count = 5
        legacy.av_ai_synopsis_enabled = True

        imported = build_legacy_runtime_config(
            "/tmp/nonexistent-smart-group-bot.toml",
            settings=legacy,
            raw_env={},
        )
        self.assertEqual(imported.av.inline_seed_count, 5)
        self.assertTrue(imported.av.ai_synopsis_enabled)

        target = Settings(_env_file=None)
        imported.apply_to_settings(target, apply_prompts=False)
        self.assertEqual(commands._av_inline_seed_limit(target), 5)
        self.assertTrue(commands._av_ai_synopsis_enabled(target))

    def test_runtime_config_accepts_the_new_av_keys_from_the_web_payload(self) -> None:
        from bot.services.runtime_config import RuntimeConfig

        config = RuntimeConfig.model_validate(
            {"schema_version": 1, "av": {"inline_seed_count": 5, "ai_synopsis_enabled": True}}
        )

        self.assertEqual(config.av.inline_seed_count, 5)
        self.assertTrue(config.av.ai_synopsis_enabled)
        public = config.public_payload()
        self.assertEqual(public["av"]["inline_seed_count"], 5)
        self.assertTrue(public["av"]["ai_synopsis_enabled"])
        self.assertIn("av_synopsis", public["prompts"])
        with self.assertRaises(ValidationError):
            RuntimeConfig.model_validate({"av": {"inline_seed_count": 9}})

    def test_prompt_default_is_loaded_for_the_synopsis_stage(self) -> None:
        from bot.utils.prompts import get_prompt

        self.assertIn("AI", get_prompt("av_synopsis") or "AI")
        self.assertIn("严禁", get_prompt("av_synopsis"))

    def test_settings_ui_exposes_both_new_av_fields(self) -> None:
        source = _APP_JS.read_text(encoding="utf-8")

        self.assertIn('field("av.inline_seed_count"', source)
        self.assertIn('toggle("av.ai_synopsis_enabled"', source)
        self.assertIn('av_synopsis: "AV 题材概述"', source)


if __name__ == "__main__":
    unittest.main()
