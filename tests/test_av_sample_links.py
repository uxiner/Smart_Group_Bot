"""详情页 ``class="sample-box"`` → 「番号的其他图片」列表（只解析，不发图）。

实测事实（线上容器、服务自己的 ``_fetch_text``，同一套 UA/Referer）：

- javbus 详情页 ``class="sample-box"`` 的链接有 10 条，形如
  ``https://pics.dmm.co.jp/digital/video/sone00342/sone00342jp-1.jpg``；
- 同页还有 ``https://www.javbus.com/pics/sample/<id>_N.jpg`` 的本地缩略图
  （实测只有 4KB，太糊）——**必须丢弃**，不许拿它凑数；
- ``class="bigImage"`` 的封面选择器仍然是 1 条，与现有代码一致。

本文件覆盖规格要求的解析用例：真实结构详情页取图（含 ``_abs_url`` 相对路径补全）、
去重保序、优先 dmm 档并丢弃 ``pics/sample/`` 缩略图、解析不到时空列表且不影响其他字段。
全程不触网。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from bot.services.av_search import (
    AVDetail,
    AVSearchService,
    _extract_sample_urls,
)

PAGE_URL = "https://www.javbus.com/SONE-342"
DMM_HOST = "https://pics.dmm.co.jp"
DMM_SAMPLE = DMM_HOST + "/digital/video/sone00342/sone00342jp-{index}.jpg"
DMM_COVER = DMM_HOST + "/mono/movie/adult/sone00342/sone00342pl.jpg"
LOCAL_THUMB = "https://www.javbus.com/pics/sample/abc_{index}.jpg"


def _make_service() -> AVSearchService:
    settings = SimpleNamespace(
        av_http_timeout_sec=15.0,
        av_max_results=18,
        av_enabled=True,
        av_javbus_base_url="https://www.javbus.com",
        av_madouqu_base_url="https://madouqu.com",
        av_dmm_base_url="https://www.dmm.co.jp",
        av_fc2_base_url="https://adult.contents.fc2.com",
    )
    return AVSearchService(settings)


def _sample_anchors() -> str:
    """按真实 javbus 结构拼一段 sample 区：10 张 dmm 图 + 缩略图 + 重复 + 干扰项。"""

    parts: list[str] = []
    for index in range(1, 11):
        parts.append(
            f'<a class="sample-box" href="{DMM_SAMPLE.format(index=index)}">'
            f'<div class="photo-frame"><img src="{LOCAL_THUMB.format(index=index)}"></div></a>'
        )
    # 本地缩略图直接挂在 sample-box 的 href 上 → 必须丢。
    parts.append(
        f'<a class="sample-box" href="{LOCAL_THUMB.format(index=99)}">thumb</a>'
    )
    parts.append('<a class="sample-box" href="/pics/sample/abc_98.jpg">relative thumb</a>')
    # 属性顺序颠倒 + 协议相对路径（_abs_url 补全）。
    parts.append(
        f'<a href="//pics.dmm.co.jp/digital/video/sone00342/sone00342jp-11.jpg" '
        f'class="sample-box big">protocol-relative</a>'
    )
    # 重复项（去重保序）。
    parts.append(
        f'<a class="sample-box" href="{DMM_SAMPLE.format(index=3)}">duplicate</a>'
    )
    # 没有 sample-box 的链接不许进来。
    parts.append(
        f'<a href="{DMM_HOST}/digital/video/sone00342/other.jpg">not a sample</a>'
    )
    return "".join(parts)


def _detail_html(*, sample_anchors: str = "") -> str:
    return f"""<html><head>
<meta property="og:image" content="{DMM_HOST}/mono/movie/adult/sone00342/sone00342ps.jpg">
<meta name="description" content="サンプル作品の説明文">
</head><body>
<h3>SONE-342 サンプル作品タイトル</h3>
<p><span class="header">識別碼:</span> <a href="{PAGE_URL}">SONE-342</a></p>
<p><span class="header">發行日期:</span> 2024-01-01</p>
<a class="bigImage" href="{DMM_COVER}"><img src="{DMM_COVER}"></a>
<div id="sample-waterfall">{sample_anchors}</div>
</body></html>"""


class AVSampleUrlParserTests(unittest.TestCase):
    def test_detail_page_sample_urls_in_page_order(self) -> None:
        svc = _make_service()
        html = _detail_html(sample_anchors=_sample_anchors())

        detail = svc._parse_javbus_detail(html, PAGE_URL)

        self.assertIsNotNone(detail)
        assert detail is not None
        expected = [DMM_SAMPLE.format(index=index) for index in range(1, 12)]
        self.assertEqual(detail.sample_urls, expected)

    def test_local_sample_thumbnails_are_dropped(self) -> None:
        svc = _make_service()
        html = _detail_html(sample_anchors=_sample_anchors())

        detail = svc._parse_javbus_detail(html, PAGE_URL)

        assert detail is not None
        for url in detail.sample_urls:
            self.assertNotIn("/pics/sample/", url, "4KB 本地缩略图不该出现在样例图里")

    def test_dmm_cdn_is_preferred_but_other_hosts_survive(self) -> None:
        anchors = (
            '<a class="sample-box" href="https://cdn.example.com/a-1.jpg">other</a>'
            f'<a class="sample-box" href="{DMM_SAMPLE.format(index=1)}">dmm</a>'
            f'<a class="sample-box" href="{DMM_HOST}/digital/video/sone00342/sone00342jp-2.jpg">dmm2</a>'
            '<a class="sample-box" href="https://cdn.example.com/a-2.jpg">other2</a>'
        )
        urls = _extract_sample_urls(anchors, PAGE_URL)

        self.assertEqual(
            urls,
            [
                DMM_SAMPLE.format(index=1),
                DMM_HOST + "/digital/video/sone00342/sone00342jp-2.jpg",
                "https://cdn.example.com/a-1.jpg",
                "https://cdn.example.com/a-2.jpg",
            ],
            "dmm CDN 那档要排前面，其余保序保留（只是优先，不是唯一来源）",
        )

    def test_relative_href_is_resolved_against_page_url(self) -> None:
        anchors = (
            '<a class="sample-box" href="/digital/video/x/x-1.jpg">relative</a>'
            '<a class="sample-box" href="digital/video/x/x-2.jpg">relative2</a>'
        )
        urls = _extract_sample_urls(anchors, PAGE_URL)

        self.assertEqual(
            urls,
            [
                "https://www.javbus.com/digital/video/x/x-1.jpg",
                "https://www.javbus.com/digital/video/x/x-2.jpg",
            ],
        )

    def test_duplicates_are_removed_keeping_first_occurrence(self) -> None:
        anchors = (
            f'<a class="sample-box" href="{DMM_SAMPLE.format(index=2)}">a</a>'
            f'<a class="sample-box" href="{DMM_SAMPLE.format(index=1)}">b</a>'
            f'<a class="sample-box" href="{DMM_SAMPLE.format(index=2)}">dup</a>'
        )
        urls = _extract_sample_urls(anchors, PAGE_URL)

        self.assertEqual(
            urls,
            [DMM_SAMPLE.format(index=2), DMM_SAMPLE.format(index=1)],
        )

    def test_no_sample_box_yields_empty_list(self) -> None:
        self.assertEqual(_extract_sample_urls("", PAGE_URL), [])
        self.assertEqual(
            _extract_sample_urls('<div class="bigImage">only cover</div>', PAGE_URL),
            [],
        )
        self.assertEqual(
            _extract_sample_urls(
                f'<a href="{DMM_SAMPLE.format(index=1)}">no sample-box class</a>',
                PAGE_URL,
            ),
            [],
        )

    def test_missing_sample_block_keeps_every_other_detail_field(self) -> None:
        svc = _make_service()
        html = _detail_html(sample_anchors="")

        detail = svc._parse_javbus_detail(html, PAGE_URL)

        assert detail is not None
        self.assertEqual(detail.sample_urls, [])
        self.assertEqual(detail.code, "SONE-342")
        self.assertIn("サンプル作品タイトル", detail.title)
        self.assertEqual(detail.cover_url, DMM_COVER)
        self.assertEqual(detail.date, "2024-01-01")
        self.assertEqual(detail.source, "javbus")
        self.assertEqual(detail.url, PAGE_URL)

    def test_av_detail_defaults_to_empty_sample_urls(self) -> None:
        detail = AVDetail(
            source="dmm",
            title="t",
            url="https://example.invalid/x",
            code="ABC-1",
        )

        self.assertEqual(detail.sample_urls, [])


if __name__ == "__main__":
    unittest.main()
