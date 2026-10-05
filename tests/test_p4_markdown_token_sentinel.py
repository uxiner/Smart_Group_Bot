"""P4-7（A-19）：``md_to_html`` 的内部占位符不能被输入伪造触发 ``IndexError``。

现象：``_render_inline_markdown`` 用 ``\\x00tgmd{index}\\x00`` 把已渲染的代码段/
链接**暂存**起来，最后再正则反替换回去。前缀写死，于是正文里只要出现
``\\x00tgmd9\\x00``（用户自己打出来，或模型原样吐回来），反替换就会去取
``tokens[9]`` —— 越界 → ``IndexError``，整条消息的渲染直接炸掉。

改法：占位符前缀**按输入动态选取**（``_markdown_token_prefix``），保证输入里
不可能存在它，于是只有渲染器自己塞进去的占位符会被命中。

「正常 Markdown 渲染结果必须逐字节不变」由既有用例
（``tests/test_telegram_send.py`` 的 ``test_markdown_renderer_*``）与本文件的
``test_normal_rendering_is_byte_identical_*`` 一起钉住。
"""

from __future__ import annotations

import unittest

from bot.utils import telegram

#: 一批覆盖各种 Markdown 结构的正常输入（含中文/标点/围栏代码块）。
NORMAL_CASES = (
    "**加粗** *斜体* __也是加粗__ ~~删除~~ ||剧透||",
    "标题行\n普通行\n\n> 引用一\n> 引用二\n\n普通行二",
    "`行内代码` 与 [链接](https://example.com/a?b=1&c=2)",
    "# 一级标题\n## 二级标题",
    "```yaml\nkey: 1\n# comment\n- item\n```",
    "说明\n\n~~~python\nprint(1)\n~~~\n\n结束",
    "a | b ||c|| `a || b`",
    "emoji 🎉 与 & < > \" ' 全部原样",
    "```\nplain\n```",
    "行内 `code` 后紧跟 **粗体** 与 ||spoiler||",
)


class MarkdownTokenForgeryTests(unittest.TestCase):
    def test_forged_placeholder_does_not_raise(self) -> None:
        for forged in ("\x00tgmd0\x00", "\x00tgmd7\x00", "hi \x00tgmd9\x00 there"):
            with self.subTest(forged=repr(forged)):
                rendered = telegram.md_to_html(forged)
                self.assertIsInstance(rendered, str)

    def test_forged_placeholder_is_kept_as_literal_text(self) -> None:
        """伪造出来的占位符不会被当成真占位符替换掉（也不该让它 IndexError）。"""

        rendered = telegram.md_to_html("a\x00tgmd3\x00b")
        self.assertIn("a", rendered)
        self.assertIn("b", rendered)
        # 渲染成功即可；这里只要求没有把这段文本「变成别的东西」。
        self.assertNotIn("<code>", rendered)

    def test_forged_placeholder_inside_code_span_still_renders_the_code(self) -> None:
        rendered = telegram.md_to_html("`\x00tgmd0\x00`")
        self.assertIn("<code>", rendered)
        self.assertIn("</code>", rendered)

    def test_forged_placeholder_next_to_real_markup_keeps_both(self) -> None:
        rendered = telegram.md_to_html("**粗**\x00tgmd5\x00`code`")
        self.assertIn("<b>粗</b>", rendered)
        self.assertIn("<code>code</code>", rendered)

    def test_repeated_forgery_across_a_long_message(self) -> None:
        payload = ("\x00tgmd0\x00" + "字" * 50) * 20
        rendered = telegram.md_to_html(payload)
        self.assertEqual(rendered.count("字"), 20 * 50)

    def test_forged_placeholder_with_many_digits(self) -> None:
        rendered = telegram.md_to_html("\x00tgmd999999\x00")
        self.assertIsInstance(rendered, str)

    def test_forged_placeholder_inside_a_fenced_block(self) -> None:
        rendered = telegram.md_to_html("```\n\x00tgmd0\x00\n```")
        self.assertIn("<pre><code>", rendered)


class MarkdownTokenPrefixTests(unittest.TestCase):
    def test_the_base_prefix_is_used_for_ordinary_text(self) -> None:
        self.assertEqual(telegram._markdown_token_prefix("普通正文"), "\x00tgmd")

    def test_a_prefix_present_in_the_input_is_extended(self) -> None:
        self.assertEqual(telegram._markdown_token_prefix("含 \x00tgmd 的正文"), "\x00tgmdx")
        self.assertEqual(
            telegram._markdown_token_prefix("含 \x00tgmdx 的正文"),
            "\x00tgmdxx",
        )

    def test_the_chosen_prefix_never_occurs_in_the_input(self) -> None:
        for text in ("\x00tgmd", "\x00tgmdx\x00tgmd", "abc\x00tgmd" * 30, "无"):
            with self.subTest(text=repr(text[:24])):
                prefix = telegram._markdown_token_prefix(text)
                self.assertNotIn(prefix, text)
                self.assertTrue(prefix.startswith("\x00tgmd"))

    def test_the_pattern_only_matches_the_chosen_prefix(self) -> None:
        pattern = telegram._markdown_token_pattern("\x00tgmdx")
        self.assertIsNotNone(pattern.match("\x00tgmdx3\x00"))
        self.assertIsNone(pattern.match("\x00tgmd3\x00"))


class NormalRenderingUnchangedTests(unittest.TestCase):
    def test_normal_rendering_is_byte_identical_for_every_case(self) -> None:
        """正常输入的渲染结果逐字钉死（含代码段/链接的占位符往返）。"""

        expected = {
            "**加粗** *斜体* __也是加粗__ ~~删除~~ ||剧透||": (
                "<b>加粗</b> <i>斜体</i> <b>也是加粗</b> <s>删除</s> "
                "<tg-spoiler>剧透</tg-spoiler>"
            ),
            "`行内代码` 与 [链接](https://example.com/a?b=1&c=2)": (
                "<code>行内代码</code> 与 "
                '<a href="https://example.com/a?b=1&amp;c=2">链接</a>'
            ),
            "a | b ||c|| `a || b`": "a | b <tg-spoiler>c</tg-spoiler> <code>a || b</code>",
            "emoji 🎉 与 & < > \" ' 全部原样": "emoji 🎉 与 &amp; &lt; &gt; &quot; &#x27; 全部原样",
            "> 引用一\n> 引用二": "<blockquote>引用一\n引用二</blockquote>",
            "```yaml\nkey: 1\n# comment\n- item\n```": (
            '<pre><code class="language-yaml">key: 1\n# comment\n- item\n</code></pre>'
        ),
        }
        for source, rendered in expected.items():
            with self.subTest(source=source):
                self.assertEqual(telegram.md_to_html(source), rendered)

    def test_no_placeholder_or_nul_byte_survives_rendering(self) -> None:
        for source in NORMAL_CASES:
            with self.subTest(source=source):
                rendered = telegram.md_to_html(source)
                self.assertNotIn("\x00", rendered)
                self.assertNotIn("tgmd", rendered)

    def test_every_normal_case_renders_without_error(self) -> None:
        for source in NORMAL_CASES:
            with self.subTest(source=source):
                self.assertIsInstance(telegram.md_to_html(source), str)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
