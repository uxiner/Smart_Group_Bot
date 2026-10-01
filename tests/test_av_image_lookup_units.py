"""私聊识图反查：挑图规则、data URI、编号/演员名抽取、内存限流的单元测试。

这些用例覆盖需求里最容易写错的两点：

- 挑图**不是**群内那套固定的 ``photo[len-2]``（那张图 211KB，base64 后 275KB，
  实测撞 ``LLM request exceeds configured context budget | stage=vision``）；
- 模型只会回「编号 + 演员名」这类极短文本，抽取必须容错（``NO_CODE``、
  ``NO_VALID_IMAGE_CONTENT``、``ACTOR: 名字``、纯名字）。

全部 Telegram 调用都是假 bot，不触网。
"""

from __future__ import annotations

import asyncio
import base64
import unittest
from types import SimpleNamespace

from bot.services.av_image_lookup import (
    AV_PHOTO_PREFERRED_MAX_BYTES,
    AV_PHOTO_RETRY_MAX_BYTES,
    AV_VISION_MAX_IMAGE_BYTES,
    AVPrivateRateLimiter,
    av_image_size_kb,
    build_av_image_data_uri,
    build_av_image_data_uri_for,
    extract_av_actor,
    extract_av_code,
    pick_av_photo_size,
    rate_limit_minutes,
    run_with_hard_deadline,
    select_av_image_file,
)

KB = 1024


def _photo(file_id: str, size: int) -> SimpleNamespace:
    return SimpleNamespace(file_id=file_id, file_size=size)


class _FakeBot:
    """只实现 get_file / download_file 的假 bot；download 时把字节塞进 buffer。"""

    def __init__(self, payload: bytes, *, remote_size: int | None = None) -> None:
        self.payload = payload
        self.remote_size = len(payload) if remote_size is None else remote_size
        self.get_file_calls: list[str] = []
        self.download_calls: list[str] = []

    async def get_file(self, file_id: str):
        self.get_file_calls.append(file_id)
        return SimpleNamespace(
            file_id=file_id,
            file_size=self.remote_size,
            file_path=f"photos/{file_id}.jpg",
        )

    async def download_file(self, file_path: str, destination) -> None:
        self.download_calls.append(file_path)
        destination.write(self.payload)


class PickPhotoSizeTests(unittest.TestCase):
    def test_preferred_limit_defaults_to_150kb_not_100kb(self) -> None:
        """新上限 150KB：140KB 那档必须赢过 90KB（旧的 100KB 规则会选 90KB）。"""

        self.assertEqual(AV_PHOTO_PREFERRED_MAX_BYTES, 150 * KB)

        photo = [
            _photo("90kb", 90 * KB),
            _photo("140kb", 140 * KB),
            _photo("300kb", 300 * KB),
        ]

        picked = pick_av_photo_size(photo)

        self.assertIsNotNone(picked)
        self.assertEqual(picked.file_id, "140kb")

    def test_prefers_largest_size_within_the_preferred_limit(self) -> None:
        photo = [
            _photo("tiny", 20 * KB),
            _photo("ok-big", 90 * KB),
            _photo("ok-mid", 60 * KB),
            _photo("huge", 300 * KB),
        ]

        picked = pick_av_photo_size(photo)

        self.assertIsNotNone(picked)
        self.assertEqual(picked.file_id, "ok-big")

    def test_custom_limit_is_honoured(self) -> None:
        """上限可传参：升级重试用 190KB 就能挑到「更大的一档」。"""

        self.assertEqual(AV_PHOTO_RETRY_MAX_BYTES, 190 * KB)

        photo = [
            _photo("primary", 142 * KB),
            _photo("retry", 188 * KB),
            _photo("too-big", 300 * KB),
        ]

        self.assertEqual(
            pick_av_photo_size(photo, preferred_max_bytes=AV_PHOTO_RETRY_MAX_BYTES).file_id,
            "retry",
        )
        # 默认（150KB）仍然是首选那档。
        self.assertEqual(pick_av_photo_size(photo).file_id, "primary")
        # 收得更紧时同样生效。
        self.assertEqual(
            pick_av_photo_size(photo, preferred_max_bytes=100 * KB).file_id,
            "primary",
        )

    def test_falls_back_to_smallest_when_every_size_is_oversized(self) -> None:
        photo = [
            _photo("bigger", 400 * KB),
            _photo("smallest-of-bad", 211 * KB),
            _photo("mid", 250 * KB),
        ]

        picked = pick_av_photo_size(photo)

        self.assertIsNotNone(picked)
        self.assertEqual(picked.file_id, "smallest-of-bad")

    def test_oversized_fallback_also_applies_to_a_custom_limit(self) -> None:
        photo = [
            _photo("bigger", 400 * KB),
            _photo("smallest-of-bad", 211 * KB),
        ]

        picked = pick_av_photo_size(photo, preferred_max_bytes=190 * KB)

        self.assertIsNotNone(picked)
        self.assertEqual(picked.file_id, "smallest-of-bad")

    def test_rule_is_not_the_group_len_minus_two_index(self) -> None:
        """反例：群内 ``len(photo)-2`` 会取 300KB 那档，这里必须取 30KB 那档。"""

        photo = [
            _photo("ok", 30 * KB),
            _photo("group-pick", 300 * KB),
            _photo("largest", 500 * KB),
        ]

        self.assertEqual(photo[len(photo) - 2].file_id, "group-pick")
        picked = pick_av_photo_size(photo)

        self.assertIsNotNone(picked)
        self.assertEqual(picked.file_id, "ok")

    def test_boundary_size_equal_to_limit_is_preferred(self) -> None:
        photo = [
            _photo("exact", AV_PHOTO_PREFERRED_MAX_BYTES),
            _photo("over", AV_PHOTO_PREFERRED_MAX_BYTES + 1),
        ]

        self.assertEqual(pick_av_photo_size(photo).file_id, "exact")

    def test_missing_file_size_counts_as_within_budget(self) -> None:
        photo = [
            _photo("unknown", 0),
            _photo("known", 50 * KB),
        ]

        self.assertEqual(pick_av_photo_size(photo).file_id, "known")

    def test_missing_file_size_is_not_treated_as_oversized_for_a_custom_limit(self) -> None:
        photo = [
            _photo("unknown", 0),
            _photo("huge", 300 * KB),
        ]

        self.assertEqual(
            pick_av_photo_size(photo, preferred_max_bytes=190 * KB).file_id, "unknown"
        )

    def test_empty_photo_list_returns_none(self) -> None:
        self.assertIsNone(pick_av_photo_size([]))
        self.assertIsNone(pick_av_photo_size(None))

    def test_size_kb_is_ceiling_rounded(self) -> None:
        self.assertEqual(av_image_size_kb(0), 0)
        self.assertEqual(av_image_size_kb(1), 1)
        self.assertEqual(av_image_size_kb(142 * KB), 142)
        self.assertEqual(av_image_size_kb(142 * KB + 1), 143)


class SelectImageFileTests(unittest.TestCase):
    def test_photo_message_uses_the_rule_and_jpeg_mime(self) -> None:
        message = SimpleNamespace(
            photo=[_photo("a", 200 * KB), _photo("b", 40 * KB)],
            document=None,
        )

        info = select_av_image_file(message)

        self.assertEqual(info, ("b", "image/jpeg", 40 * KB))

    def test_photo_message_custom_limit_picks_the_bigger_tier(self) -> None:
        """同一张图：默认上限挑 142KB 档，升级上限挑 188KB 档（file_id 不同）。"""

        message = SimpleNamespace(
            photo=[
                _photo("primary", 142 * KB),
                _photo("retry", 188 * KB),
                _photo("too-big", 300 * KB),
            ],
            document=None,
        )

        self.assertEqual(
            select_av_image_file(message), ("primary", "image/jpeg", 142 * KB)
        )
        self.assertEqual(
            select_av_image_file(
                message, preferred_max_bytes=AV_PHOTO_RETRY_MAX_BYTES
            ),
            ("retry", "image/jpeg", 188 * KB),
        )

    def test_document_is_unaffected_by_the_limit(self) -> None:
        """文档只有一档，上限对它没有意义（升级重试自然也不会换档）。"""

        message = SimpleNamespace(
            photo=None,
            document=SimpleNamespace(
                file_id="doc", mime_type="image/png", file_size=900 * KB
            ),
        )

        self.assertEqual(
            select_av_image_file(message, preferred_max_bytes=190 * KB),
            ("doc", "image/png", 900 * KB),
        )

    def test_document_with_image_mime_is_supported(self) -> None:
        message = SimpleNamespace(
            photo=None,
            document=SimpleNamespace(file_id="doc", mime_type="image/png", file_size=1234),
        )

        self.assertEqual(select_av_image_file(message), ("doc", "image/png", 1234))

    def test_document_with_charset_suffix_is_normalized(self) -> None:
        message = SimpleNamespace(
            photo=None,
            document=SimpleNamespace(
                file_id="doc", mime_type="image/WebP; charset=binary", file_size=9
            ),
        )

        self.assertEqual(select_av_image_file(message), ("doc", "image/webp", 9))

    def test_non_image_document_is_ignored(self) -> None:
        message = SimpleNamespace(
            photo=None,
            document=SimpleNamespace(file_id="pdf", mime_type="application/pdf", file_size=9),
        )

        self.assertIsNone(select_av_image_file(message))

    def test_plain_text_message_is_ignored(self) -> None:
        self.assertIsNone(select_av_image_file(SimpleNamespace(photo=None, document=None)))


class BuildDataUriTests(unittest.IsolatedAsyncioTestCase):
    async def test_downloads_and_encodes_base64_data_uri(self) -> None:
        payload = b"\xff\xd8fake-jpeg-bytes"
        bot = _FakeBot(payload)
        message = SimpleNamespace(
            bot=bot,
            photo=[_photo("cover", 20 * KB)],
            document=None,
        )

        data_uri = await build_av_image_data_uri(message)

        self.assertTrue(data_uri.startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(data_uri.split(",", 1)[1]), payload)
        self.assertEqual(bot.get_file_calls, ["cover"])
        self.assertEqual(bot.download_calls, ["photos/cover.jpg"])

    async def test_declared_oversize_image_is_rejected_without_download(self) -> None:
        bot = _FakeBot(b"x" * 10)
        message = SimpleNamespace(
            bot=bot,
            photo=[_photo("huge", AV_VISION_MAX_IMAGE_BYTES + 1)],
            document=None,
        )

        self.assertEqual(await build_av_image_data_uri(message), "")
        self.assertEqual(bot.get_file_calls, [])

    async def test_custom_limit_can_download_the_bigger_tier(self) -> None:
        bot = _FakeBot(b"\xff\xd8retry-tier")
        message = SimpleNamespace(
            bot=bot,
            photo=[_photo("primary", 142 * KB), _photo("retry", 188 * KB)],
            document=None,
        )

        self.assertEqual(bot.download_calls, [])
        await build_av_image_data_uri(message, preferred_max_bytes=190 * KB)

        self.assertEqual(bot.get_file_calls, ["retry"])
        self.assertEqual(bot.download_calls, ["photos/retry.jpg"])

    async def test_build_for_an_explicit_file_id_uses_that_tier(self) -> None:
        """重试要按**已知**的一档下载：``build_av_image_data_uri_for`` 不再自己挑图。"""

        payload = b"\xff\xd8explicit-tier"
        bot = _FakeBot(payload)
        message = SimpleNamespace(
            bot=bot,
            photo=[_photo("primary", 142 * KB), _photo("retry", 188 * KB)],
            document=None,
        )

        data_uri = await build_av_image_data_uri_for(
            message, "retry", "image/png", declared_size=188 * KB
        )

        self.assertTrue(data_uri.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(data_uri.split(",", 1)[1]), payload)
        self.assertEqual(bot.get_file_calls, ["retry"])

    async def test_build_for_rejects_oversize_declared_size_without_download(self) -> None:
        bot = _FakeBot(b"x" * 10)
        message = SimpleNamespace(
            bot=bot,
            photo=[_photo("huge", AV_VISION_MAX_IMAGE_BYTES + 1)],
            document=None,
        )

        self.assertEqual(
            await build_av_image_data_uri_for(
                message,
                "huge",
                "image/jpeg",
                declared_size=AV_VISION_MAX_IMAGE_BYTES + 1,
            ),
            "",
        )
        self.assertEqual(bot.get_file_calls, [])

    async def test_build_for_empty_file_id_returns_empty(self) -> None:
        bot = _FakeBot(b"x" * 10)
        message = SimpleNamespace(bot=bot, photo=[_photo("a", KB)], document=None)

        self.assertEqual(await build_av_image_data_uri_for(message, ""), "")
        self.assertEqual(bot.get_file_calls, [])

    async def test_remote_oversize_image_is_rejected(self) -> None:
        bot = _FakeBot(b"small", remote_size=AV_VISION_MAX_IMAGE_BYTES + 1)
        message = SimpleNamespace(bot=bot, photo=[_photo("liar", 10)], document=None)

        self.assertEqual(await build_av_image_data_uri(message), "")
        self.assertEqual(bot.download_calls, [])

    async def test_download_failure_only_logs_and_returns_empty(self) -> None:
        class _BoomBot(_FakeBot):
            async def download_file(self, file_path, destination) -> None:
                raise RuntimeError("network down")

        bot = _BoomBot(b"payload")
        message = SimpleNamespace(bot=bot, photo=[_photo("cover", 20 * KB)], document=None)

        self.assertEqual(await build_av_image_data_uri(message), "")


class ExtractCodeTests(unittest.TestCase):
    def test_reads_code_and_actor_from_vision_output(self) -> None:
        self.assertEqual(extract_av_code("SONE-342 清原みゆう"), "SONE-342")
        self.assertEqual(extract_av_code("SSIS-001 葵つかさ 乙白さやか"), "SSIS-001")

    def test_normalizes_relaxed_code_shapes(self) -> None:
        self.assertEqual(extract_av_code("作品编号：sone 342"), "SONE-342")
        self.assertEqual(extract_av_code("SSIS_001"), "SSIS-001")

    def test_prefers_fc2_code(self) -> None:
        self.assertEqual(extract_av_code("FC2-PPV-4863846"), "FC2-PPV-4863846")
        self.assertEqual(extract_av_code("fc2ppv4863846"), "FC2-PPV-4863846")

    def test_no_code_markers_return_empty(self) -> None:
        self.assertEqual(extract_av_code("NO_CODE"), "")
        self.assertEqual(extract_av_code("NO_VALID_IMAGE_CONTENT"), "")
        self.assertEqual(extract_av_code(""), "")

    def test_description_without_code_returns_empty(self) -> None:
        self.assertEqual(extract_av_code("一名女性站在窗边，光线很好。"), "")


class ExtractActorTests(unittest.TestCase):
    def test_actor_line_is_parsed_when_code_is_missing(self) -> None:
        self.assertEqual(extract_av_actor("NO_CODE\nACTOR: 清原みゆう"), "清原みゆう")
        self.assertEqual(extract_av_actor("演员：葵つかさ"), "葵つかさ")

    def test_bare_name_fallback(self) -> None:
        self.assertEqual(extract_av_actor("NO_CODE 清原みゆう"), "清原みゆう")

    def test_long_description_is_not_treated_as_actor(self) -> None:
        self.assertEqual(
            extract_av_actor("NO_VALID_IMAGE_CONTENT"),
            "",
        )
        self.assertEqual(
            extract_av_actor("这是一张很普通的生活照，画面里没有任何文字信息"),
            "",
        )
        self.assertEqual(extract_av_actor(""), "")

    def test_code_only_output_has_no_actor(self) -> None:
        self.assertEqual(extract_av_actor("SONE-342"), "")


class HardDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_value_when_the_call_finishes_in_time(self) -> None:
        async def _quick() -> str:
            await asyncio.sleep(0)
            return "SONE-342"

        self.assertEqual(
            await run_with_hard_deadline(_quick(), timeout_seconds=1.0), "SONE-342"
        )

    async def test_raises_timeout_even_when_the_child_ignores_cancellation(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _stubborn() -> str:
            started.set()
            # 模拟「拖着不响应取消」的调用：硬超时必须照样到点返回，不能一直等。
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.wait_for(release.wait(), timeout=1.0)
            return "late"

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(
                run_with_hard_deadline(_stubborn(), timeout_seconds=0.01),
                timeout=2.0,
            )
        self.assertTrue(started.is_set())
        release.set()

    async def test_child_exception_propagates(self) -> None:
        async def _boom() -> str:
            raise RuntimeError("vision exploded")

        with self.assertRaises(RuntimeError):
            await run_with_hard_deadline(_boom(), timeout_seconds=1.0)


class RateLimiterTests(unittest.TestCase):
    def test_eleventh_call_in_the_same_window_is_rejected(self) -> None:
        clock = SimpleNamespace(now=1000.0)
        limiter = AVPrivateRateLimiter(
            limit=10, window_seconds=3600.0, clock=lambda: clock.now
        )

        for index in range(10):
            allowed, retry_after = limiter.allow(42)
            self.assertTrue(allowed, f"第 {index + 1} 次不该被拒")
            self.assertEqual(retry_after, 0)

        allowed, retry_after = limiter.allow(42)
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)
        self.assertGreaterEqual(rate_limit_minutes(retry_after), 1)

    def test_each_user_has_an_independent_bucket(self) -> None:
        clock = SimpleNamespace(now=0.0)
        limiter = AVPrivateRateLimiter(
            limit=2, window_seconds=3600.0, clock=lambda: clock.now
        )

        self.assertTrue(limiter.allow(1)[0])
        self.assertTrue(limiter.allow(1)[0])
        self.assertFalse(limiter.allow(1)[0])
        self.assertTrue(limiter.allow(2)[0])

    def test_window_slides_after_one_hour(self) -> None:
        clock = SimpleNamespace(now=0.0)
        limiter = AVPrivateRateLimiter(
            limit=1, window_seconds=3600.0, clock=lambda: clock.now
        )

        self.assertTrue(limiter.allow(7)[0])
        self.assertFalse(limiter.allow(7)[0])
        clock.now = 3600.0
        self.assertTrue(limiter.allow(7)[0])

    def test_blocked_check_does_not_consume_quota(self) -> None:
        clock = SimpleNamespace(now=0.0)
        limiter = AVPrivateRateLimiter(
            limit=2, window_seconds=3600.0, clock=lambda: clock.now
        )

        self.assertEqual(limiter.blocked(5), (False, 0))
        self.assertEqual(limiter.blocked(5), (False, 0))
        self.assertEqual(limiter.blocked(5), (False, 0))
        # 三次只查不计数，配额一点没少。
        self.assertTrue(limiter.allow(5)[0])
        self.assertTrue(limiter.allow(5)[0])
        self.assertTrue(limiter.blocked(5)[0])

    def test_unknown_user_is_never_blocked(self) -> None:
        limiter = AVPrivateRateLimiter(limit=1)
        self.assertEqual(limiter.allow(0), (True, 0))
        self.assertEqual(limiter.blocked(0), (False, 0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
