"""识图升级重试：首次没读出编号 → 用**更大的一档**再试一次（最多两次）。

覆盖需求点名的分支：

- 首次没读出编号 → 第二次用**不同的 file_id** 再调一次 vision，命中后正常查详情；
- 首次就读出编号 → **只调一次** vision（不浪费第二次）；
- 没有更大的档可选（两次 file_id 相同）→ **不重试**；
- 两次都读不到 → 走演员名 / 提示兜底，且**总共只有两次**调用；
- 重试路径下群里仍然是**先删图，再识图**；
- 日志里带上档位 KB 与是否升级。

所有 Telegram 与 LLM 调用都是 mock，不触网、不真调模型。
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import ModelConfig, Settings
from bot.handlers import commands
from bot.services.av_image_lookup import AVPrivateRateLimiter
from bot.services.av_search import AVDetail, AVSearchItem, AVSeed

PRIVATE_CHAT_ID = 900123
GROUP_CHAT_ID = -100987
USER_ID = 900123
COVER_URL = "https://pics.dmm.co.jp/mono/movie/adult/sone342/sone342pl.jpg"

KB = 1024

#: 首选档 142KB（≤150KB）、升级档 188KB（≤190KB）、外加一档 300KB 不该被选中。
#: 这两个数字就是需求里给的日志例子 ``档位=142KB … 升级重试 188KB``。
PRIMARY_KB = 142
RETRY_KB = 188
TIERS_WITH_RETRY = (40 * KB, PRIMARY_KB * KB, RETRY_KB * KB, 300 * KB)
#: 两档挑出来是同一档（没有更大的档可选）。
TIERS_WITHOUT_RETRY = (40 * KB, 90 * KB, 300 * KB)


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
    """假 bot：每档 payload 不同（用来证明第二次用的是**另一档**）。"""

    def __init__(self, *, fail_file_ids: tuple[str, ...] = ()) -> None:
        self.fail_file_ids = set(fail_file_ids)
        self.get_file_calls: list[str] = []
        self.download_calls: list[str] = []
        #: 封面只允许发发起者私聊；私聊识图路径用 message.answer_photo，不走这里。
        self.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=9))
        self.send_chat_action = AsyncMock()

    def _payload(self, file_id: str) -> bytes:
        return f"\xff\xd8jpeg-bytes-of-{file_id}".encode()

    async def get_file(self, file_id: str):
        self.get_file_calls.append(file_id)
        if file_id in self.fail_file_ids:
            raise RuntimeError("telegram get_file exploded")
        return SimpleNamespace(
            file_id=file_id,
            file_size=len(self._payload(file_id)),
            file_path=f"photos/{file_id}.jpg",
        )

    async def download_file(self, file_path: str, destination) -> None:
        self.download_calls.append(file_path)
        file_id = file_path.split("/")[-1].removesuffix(".jpg")
        destination.write(self._payload(file_id))


def _llm_factory(replies: list[str], *, order: list[str] | None = None):
    """按顺序返回 ``replies`` 的假 LLMService 工厂；记录每次 vision 的 data URI。"""

    queue = list(replies)
    instances: list[SimpleNamespace] = []
    data_uris: list[str] = []

    def _factory(main, decision, compress=None, **kwargs):
        instance = SimpleNamespace(main=main, decision=decision, compress=compress, **kwargs)

        async def _vision(image_url: str, prompt: str) -> str:
            data_uris.append(image_url)
            if order is not None:
                order.append("vision")
            return queue.pop(0) if queue else ""

        instance.vision_describe = AsyncMock(side_effect=_vision)
        instances.append(instance)
        return instance

    return _factory, instances, data_uris


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
    return SimpleNamespace(file_id=file_id, file_size=size, width=100, height=100)


def _private_message(
    *, sizes: tuple[int, ...] = TIERS_WITH_RETRY, bot: _FakeBot | None = None
) -> SimpleNamespace:
    bot = bot or _FakeBot()
    return SimpleNamespace(
        chat=SimpleNamespace(id=PRIVATE_CHAT_ID, type="private", title=""),
        from_user=SimpleNamespace(id=USER_ID),
        bot=bot,
        photo=[
            _photo(f"photo-{index}", size) for index, size in enumerate(sizes)
        ],
        document=None,
        text=None,
        caption=None,
        delete=AsyncMock(),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        answer_photo=AsyncMock(return_value=SimpleNamespace(message_id=2)),
        edit_text=AsyncMock(return_value=SimpleNamespace(message_id=3)),
        edit_media=AsyncMock(return_value=SimpleNamespace(message_id=4)),
        edit_caption=AsyncMock(return_value=SimpleNamespace(message_id=5)),
    )


def _decode(data_uri: str) -> bytes:
    return base64.b64decode(data_uri.split(",", 1)[1])


class _EscalationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.limiter = AVPrivateRateLimiter(limit=10, window_seconds=3600.0)
        self._limiter_patch = patch.object(
            commands, "_AV_PRIVATE_RATE_LIMITER", self.limiter
        )
        self._limiter_patch.start()
        self.addCleanup(self._limiter_patch.stop)

    async def _run_private(self, message, *, service=None, llm=None, settings=None):
        service = service if service is not None else _service(detail=_detail())
        llm = llm if llm is not None else _llm_factory(["SONE-342 清原みゆう"])
        answer = AsyncMock()
        with (
            patch.object(commands, "AVSearchService", return_value=service),
            patch.object(commands, "LLMService", new=llm[0]),
            patch.object(commands, "typing_action", return_value=_AsyncContext()),
            patch.object(commands, "_answer", new=answer),
        ):
            await commands.on_private_av_image(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=settings or _settings(),
            )
        return service, answer

    def _run_group(self, message, *, service=None, llm=None, order=None):
        """准备 ``cmd_av`` 需要的全部 patch；返回 (ExitStack, service, answer)。"""

        service = service if service is not None else _service(detail=_detail())
        llm = llm if llm is not None else _llm_factory(["SONE-342 清原みゆう"], order=order)
        answer = AsyncMock()
        stack = contextlib.ExitStack()
        stack.enter_context(
            patch.object(
                commands, "ensure_group_authorized", new=AsyncMock(return_value=True)
            )
        )
        stack.enter_context(
            patch.object(
                commands,
                "_ensure_group_row",
                new=AsyncMock(
                    return_value=SimpleNamespace(settings={"av_enabled": True})
                ),
            )
        )
        stack.enter_context(patch.object(commands, "AVSearchService", return_value=service))
        stack.enter_context(patch.object(commands, "LLMService", new=llm[0]))
        stack.enter_context(
            patch.object(commands, "typing_action", return_value=_AsyncContext())
        )
        stack.enter_context(patch.object(commands, "_answer", new=answer))
        stack.enter_context(
            patch.object(
                commands, "_download_cover_input_file", new=AsyncMock(return_value=None)
            )
        )
        return stack, service, answer

    def _group_message(
        self,
        *,
        bot: _FakeBot | None = None,
        sizes: tuple[int, ...] = TIERS_WITH_RETRY,
        text: str | None = None,
        caption: str | None = "/av",
        with_photo: bool = True,
        order: list[str] | None = None,
    ) -> SimpleNamespace:
        bot = bot or _FakeBot()

        async def _delete() -> None:
            if order is not None:
                order.append("delete-command")

        return SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_CHAT_ID, type="supergroup", title="group"),
            from_user=SimpleNamespace(id=USER_ID, full_name="张三", username="zhangsan"),
            bot=bot,
            text=text,
            caption=caption,
            photo=(
                [_photo(f"photo-{index}", size) for index, size in enumerate(sizes)]
                if with_photo
                else None
            ),
            document=None,
            message_id=700,
            reply_to_message=None,
            delete=AsyncMock(side_effect=_delete),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=1)),
            answer_photo=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
            edit_text=AsyncMock(return_value=SimpleNamespace(message_id=2)),
            edit_media=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
            edit_caption=AsyncMock(side_effect=AssertionError("群里不许出现照片")),
        )


class EscalationRetryTests(_EscalationTestCase):
    """首次未读出编号 → 升级一档重试一次。"""

    async def test_retry_uses_a_bigger_tier_and_hits_the_code(self) -> None:
        bot = _FakeBot()
        message = _private_message(bot=bot)
        service = _service(detail=_detail())
        llm = _llm_factory(["NO_CODE", "SONE-342 清原みゆう"])

        with self.assertLogs(commands.log, level="INFO") as captured:
            service, answer = await self._run_private(message, service=service, llm=llm)

        _factory, instances, data_uris = llm

        # 两次 vision：第一次 142KB 档，第二次 188KB 档（file_id 不同）。
        self.assertEqual(len(instances), 2)
        self.assertEqual(len(data_uris), 2)
        self.assertEqual(
            bot.download_calls, ["photos/photo-1.jpg", "photos/photo-2.jpg"]
        )
        self.assertNotEqual(_decode(data_uris[0]), _decode(data_uris[1]))

        # 第二次读出的编号照常查详情，兜底话术一次都没用上。
        service.lookup_by_code.assert_awaited_once_with("SONE-342")
        answer.assert_not_awaited()
        message.answer_photo.assert_awaited_once()
        self.assertEqual(message.answer_photo.await_args.kwargs["photo"], COVER_URL)
        # 私聊不删消息（「先删图」只是群内规则），也不走 bot.send_photo。
        message.delete.assert_not_awaited()
        bot.send_photo.assert_not_awaited()

        # 日志：档位 KB + 升级 + 是否命中（需求点名的可观测性）。
        text = "\n".join(captured.output)
        self.assertIn("档位=142KB", text)
        self.assertIn("升级重试 188KB", text)
        self.assertIn("命中", text)

    async def test_first_try_hit_never_calls_vision_twice(self) -> None:
        bot = _FakeBot()
        message = _private_message(bot=bot)
        service = _service(detail=_detail())
        llm = _llm_factory(["SONE-342 清原みゆう"])

        with self.assertLogs(commands.log, level="INFO") as captured:
            await self._run_private(message, service=service, llm=llm)

        _factory, instances, data_uris = llm
        # 有更大的档可选，但第一次就读到了编号 → 绝不浪费第二次。
        self.assertEqual(len(data_uris), 1)
        self.assertEqual(len(instances), 1)
        self.assertEqual(bot.download_calls, ["photos/photo-1.jpg"])
        service.lookup_by_code.assert_awaited_once_with("SONE-342")
        self.assertIn("升级=否", "\n".join(captured.output))

    async def test_same_file_id_is_not_retried(self) -> None:
        bot = _FakeBot()
        message = _private_message(sizes=TIERS_WITHOUT_RETRY, bot=bot)
        service = _service()
        llm = _llm_factory(["NO_CODE", "SONE-342 清原みゆう"])

        with self.assertLogs(commands.log, level="INFO") as captured:
            service, answer = await self._run_private(message, service=service, llm=llm)

        _factory, instances, data_uris = llm
        # 两档挑出来都是 photo-1（300KB > 190KB 上限）→ 不重试，第二次回复没被消费。
        self.assertEqual(bot.download_calls, ["photos/photo-1.jpg"])
        self.assertEqual(len(data_uris), 1)
        self.assertEqual(len(instances), 1)
        service.lookup_by_code.assert_not_awaited()
        service.search.assert_not_awaited()

        # 直接走原有兜底：提示直接发番号。
        answer.assert_awaited_once()
        self.assertIn(commands._AV_VISION_NO_CODE_TEXT, answer.await_args.args[2])
        self.assertIn("不重试", "\n".join(captured.output))

    async def test_two_failures_ask_for_code_with_exactly_two_calls(self) -> None:
        bot = _FakeBot()
        message = _private_message(bot=bot)
        service = _service()
        llm = _llm_factory(["NO_CODE", "NO_CODE"])

        service, answer = await self._run_private(message, service=service, llm=llm)

        _factory, instances, data_uris = llm
        self.assertEqual(len(data_uris), 2)
        self.assertEqual(
            bot.download_calls, ["photos/photo-1.jpg", "photos/photo-2.jpg"]
        )
        service.lookup_by_code.assert_not_awaited()
        service.search.assert_not_awaited()
        answer.assert_awaited_once()
        self.assertIn(commands._AV_VISION_NO_CODE_TEXT, answer.await_args.args[2])

    async def test_retry_actor_only_output_falls_back_to_actor_search(self) -> None:
        bot = _FakeBot()
        message = _private_message(bot=bot)
        service = _service(results=_search_items())
        llm = _llm_factory(["NO_CODE", "NO_CODE\nACTOR: 清原みゆう"])

        with self.assertLogs(commands.log, level="INFO") as captured:
            service, answer = await self._run_private(message, service=service, llm=llm)

        _factory, _instances, data_uris = llm
        self.assertEqual(len(data_uris), 2)
        service.lookup_by_code.assert_not_awaited()
        service.search.assert_awaited_once_with("清原みゆう")
        answer.assert_not_awaited()
        # 「命中」指的是读出**编号**：这次第二次只读到演员名 → 记「仍未读出」。
        self.assertIn("升级重试 188KB → 仍未读出", "\n".join(captured.output))

    async def test_empty_retry_output_reuses_the_first_actor(self) -> None:
        bot = _FakeBot()
        message = _private_message(bot=bot)
        service = _service(results=_search_items())
        # 第一次读出演员名、第二次（升级档）调用失败/空输出 → 退回第一次的输出。
        llm = _llm_factory(["NO_CODE\nACTOR: 清原みゆう", ""])

        service, answer = await self._run_private(message, service=service, llm=llm)

        _factory, _instances, data_uris = llm
        self.assertEqual(len(data_uris), 2)
        service.search.assert_awaited_once_with("清原みゆう")
        answer.assert_not_awaited()

    async def test_retry_download_failure_reuses_the_first_text(self) -> None:
        # 第二档（photo-2）get_file 直接失败 → 不再有第三次调用，退回第一次的输出。
        bot = _FakeBot(fail_file_ids=("photo-2",))
        message = _private_message(bot=bot)
        service = _service(results=_search_items())
        llm = _llm_factory(["NO_CODE\nACTOR: 清原みゆう", "SONE-342"])

        with self.assertLogs(commands.log, level="INFO") as captured:
            service, answer = await self._run_private(message, service=service, llm=llm)

        _factory, _instances, data_uris = llm
        self.assertEqual(len(data_uris), 1)
        service.search.assert_awaited_once_with("清原みゆう")
        answer.assert_not_awaited()
        self.assertIn("升级重试 188KB 下载失败", "\n".join(captured.output))

    async def test_retry_does_not_consume_a_second_quota_slot(self) -> None:
        limit_one = AVPrivateRateLimiter(limit=1, window_seconds=3600.0)
        bot = _FakeBot()
        message = _private_message(bot=bot)
        llm = _llm_factory(["NO_CODE", "SONE-342"])

        with patch.object(commands, "_AV_PRIVATE_RATE_LIMITER", limit_one):
            await self._run_private(message, llm=llm)

        _factory, _instances, data_uris = llm
        # 一次用户请求（哪怕内部重试两次）只算一次配额。
        self.assertEqual(len(data_uris), 2)
        self.assertEqual(limit_one.blocked(USER_ID), (True, 3600))


class EscalationGroupOrderTests(_EscalationTestCase):
    """群内重试路径：顺序与既有约束一个字都不变（先删图，再识图）。"""

    async def test_group_escalation_still_deletes_before_any_vision(self) -> None:
        order: list[str] = []
        bot = _FakeBot()
        message = self._group_message(bot=bot, order=order, caption="/av")
        service = _service(detail=_detail())
        llm = _llm_factory(["NO_CODE", "SONE-342 清原みゆう"], order=order)

        with self.assertLogs(commands.log, level="INFO") as captured:
            stack, service, answer = self._run_group(
                message, service=service, llm=llm, order=order
            )
            with stack:
                await commands.cmd_av(
                    message,
                    session=SimpleNamespace(commit=AsyncMock()),
                    settings=_settings(),
                )

        # 删图仍是第一步，且发生在两次 vision 之前。
        self.assertEqual(order[0], "delete-command")
        self.assertLess(order.index("delete-command"), order.index("vision"))
        message.delete.assert_awaited_once()

        _factory, instances, data_uris = llm
        self.assertEqual(len(data_uris), 2)
        self.assertEqual(
            bot.download_calls, ["photos/photo-1.jpg", "photos/photo-2.jpg"]
        )
        # 第二次读出的编号用于查询，结果仍然只发文字（抬头带发起者）。
        service.lookup_by_code.assert_awaited_once_with("SONE-342")
        message.answer_photo.assert_not_awaited()
        message.edit_media.assert_not_awaited()
        # 封面依旧只发发起者私聊（群里一个字没改）。
        bot.send_photo.assert_awaited_once()
        self.assertEqual(int(bot.send_photo.await_args.kwargs["chat_id"]), USER_ID)
        self.assertIn("张三", message.answer.await_args.args[0])
        self.assertIn("<code>SONE-342</code>", message.answer.await_args.args[0])
        answer.assert_not_awaited()

        text = "\n".join(captured.output)
        self.assertIn("档位=142KB", text)
        self.assertIn("升级重试 188KB", text)
        self.assertIn("命中", text)

    async def test_group_without_a_bigger_tier_is_not_retried(self) -> None:
        order: list[str] = []
        bot = _FakeBot()
        message = self._group_message(
            bot=bot, order=order, sizes=TIERS_WITHOUT_RETRY, caption="/av"
        )
        service = _service()
        llm = _llm_factory(["NO_CODE", "SONE-342"], order=order)

        stack, service, answer = self._run_group(
            message, service=service, llm=llm, order=order
        )
        with stack:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        _factory, _instances, data_uris = llm
        self.assertEqual(order[0], "delete-command")
        self.assertEqual(len(data_uris), 1)
        self.assertEqual(bot.download_calls, ["photos/photo-1.jpg"])
        service.lookup_by_code.assert_not_awaited()
        service.search.assert_not_awaited()
        self.assertIn(commands._AV_VISION_NO_CODE_TEXT, answer.await_args.args[2])
        self.assertIn("张三", answer.await_args.args[2])


class OversizedTierHonestyTests(_EscalationTestCase):
    """超出实测安全带（190KB）的档位失败时，必须明说「换一张更小的图」。"""

    async def test_private_oversized_tier_failure_asks_for_a_smaller_image(self) -> None:
        bot = _FakeBot()
        message = _private_message(sizes=(211 * KB,), bot=bot)
        service = _service()
        # 211KB → base64 ≈275KB → token 预算跳过：模型侧表现为空输出。
        llm = _llm_factory([""])

        with self.assertLogs(commands.log, level="INFO") as captured:
            service, answer = await self._run_private(message, service=service, llm=llm)

        _factory, _instances, data_uris = llm
        self.assertEqual(len(data_uris), 1)
        service.lookup_by_code.assert_not_awaited()
        answer.assert_awaited_once()
        text = answer.await_args.args[2]
        self.assertIn(commands._AV_IMAGE_TOO_LARGE_TEXT, text)
        self.assertIn("请发小一点的图", text)
        self.assertNotIn(commands._AV_VISION_FAILED_TEXT, text)
        self.assertIn("档位=211KB", "\n".join(captured.output))

    async def test_private_failure_within_the_safety_band_keeps_generic_text(self) -> None:
        bot = _FakeBot()
        message = _private_message(sizes=TIERS_WITHOUT_RETRY, bot=bot)
        service = _service()
        llm = _llm_factory([""])

        service, answer = await self._run_private(message, service=service, llm=llm)

        _factory, _instances, data_uris = llm
        # 90KB 这一档在安全带内：失败就是失败，不该说「图太大」。
        self.assertEqual(len(data_uris), 1)
        answer.assert_awaited_once()
        self.assertIn(commands._AV_VISION_FAILED_TEXT, answer.await_args.args[2])
        self.assertNotIn(commands._AV_IMAGE_TOO_LARGE_TEXT, answer.await_args.args[2])

    async def test_group_oversized_tier_still_deletes_first_then_asks_for_smaller(self) -> None:
        order: list[str] = []
        bot = _FakeBot()
        message = self._group_message(
            bot=bot, order=order, sizes=(211 * KB,), caption="/av"
        )
        service = _service()
        llm = _llm_factory([""], order=order)

        stack, service, answer = self._run_group(
            message, service=service, llm=llm, order=order
        )
        with stack:
            await commands.cmd_av(
                message,
                session=SimpleNamespace(commit=AsyncMock()),
                settings=_settings(),
            )

        _factory, _instances, data_uris = llm
        self.assertEqual(order[0], "delete-command")
        self.assertLess(order.index("delete-command"), order.index("vision"))
        self.assertEqual(len(data_uris), 1)
        service.lookup_by_code.assert_not_awaited()
        self.assertIn(commands._AV_IMAGE_TOO_LARGE_TEXT, answer.await_args.args[2])
        self.assertIn("张三", answer.await_args.args[2])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
