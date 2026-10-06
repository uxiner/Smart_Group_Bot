import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.handlers import group


def bind_group_ops(**overrides):
    """把 ``group_ops`` 字段绑进运行时配置（返回 stack 上下文管理器）。"""

    from contextlib import contextmanager

    from bot.config import Settings
    from bot.services import policy_runtime

    @contextmanager
    def _bind():
        previous = policy_runtime.bound_settings()
        settings = previous or Settings(_env_file=None)
        settings = settings.model_copy(deep=True)
        unknown = set(overrides) - set(settings.group_ops.model_fields)
        if unknown:
            raise AssertionError(f"不是 group_ops 的字段名：{sorted(unknown)}")
        settings.group_ops = settings.group_ops.model_copy(update=overrides)
        policy_runtime.bind(settings)
        try:
            yield
        finally:
            if previous is None:
                policy_runtime.unbind()
            else:
                policy_runtime.bind(previous)

    return _bind()



def _image_message(*, file_size: int = 3, mime: str = "image/png") -> SimpleNamespace:
    bot = SimpleNamespace(
        token="123456:TOP_SECRET_TOKEN",
        get_file=AsyncMock(
            return_value=SimpleNamespace(file_path="documents/image.png", file_size=file_size)
        ),
        download_file=AsyncMock(),
    )
    return SimpleNamespace(
        photo=None,
        document=SimpleNamespace(
            file_id="image-file-id",
            mime_type=mime,
            file_size=file_size,
        ),
        animation=None,
        sticker=None,
        bot=bot,
    )


class GroupMediaSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_declared_oversize_image_is_rejected_before_telegram_download(self) -> None:
        message = _image_message(file_size=101)
        with bind_group_ops(vision_image_max_bytes=100):
            data_uri = await group._build_telegram_image_data_uri(message)

        self.assertEqual(data_uri, "")
        message.bot.get_file.assert_not_awaited()
        message.bot.download_file.assert_not_awaited()

    async def test_actual_download_limit_is_enforced_even_if_metadata_lies(self) -> None:
        message = _image_message(file_size=3)

        async def write_too_much(_path, *, destination):
            destination.write(b"12345")

        message.bot.download_file.side_effect = write_too_much
        with bind_group_ops(vision_image_max_bytes=4):
            data_uri = await group._build_telegram_image_data_uri(message)

        self.assertEqual(data_uri, "")

    async def test_vision_uses_data_uri_only_and_never_exposes_bot_token(self) -> None:
        message = _image_message(file_size=3)

        async def write_image(_path, *, destination):
            destination.write(b"png")

        message.bot.download_file.side_effect = write_image
        llm = SimpleNamespace(vision_describe=AsyncMock(return_value=""))

        text, vision = await group._append_image_context(
            message,
            llm,
            "[document]",
            "document",
        )

        self.assertEqual(text, "[document]")
        self.assertEqual(vision, "")
        llm.vision_describe.assert_awaited_once()
        image_input = llm.vision_describe.await_args.args[0]
        self.assertTrue(image_input.startswith("data:image/png;base64,"))
        self.assertNotIn(message.bot.token, image_input)

    async def test_telegram_download_failure_degrades_to_text_only(self) -> None:
        message = _image_message(file_size=3)
        message.bot.get_file.side_effect = RuntimeError("telegram unavailable")
        llm = SimpleNamespace(vision_describe=AsyncMock())

        text, vision = await group._append_image_context(
            message,
            llm,
            "caption",
            "document_caption",
        )

        self.assertEqual((text, vision), ("caption", ""))
        llm.vision_describe.assert_not_awaited()

    async def test_vision_timeout_degrades_to_text_only(self) -> None:
        message = _image_message(file_size=3)

        async def write_image(_path, *, destination):
            destination.write(b"png")

        message.bot.download_file.side_effect = write_image
        llm = SimpleNamespace(vision_describe=AsyncMock(return_value="late"))

        async def timeout(awaitable, *, timeout_seconds):
            del timeout_seconds
            awaitable.close()
            raise TimeoutError

        with patch(
            "bot.handlers.group._await_hard_deadline",
            new=timeout,
        ):
            text, vision = await group._append_image_context(
                message,
                llm,
                "caption",
                "document_caption",
            )

        self.assertEqual((text, vision), ("caption", ""))

    async def test_svg_document_is_not_sent_to_vision_provider(self) -> None:
        message = _image_message(file_size=3, mime="image/svg+xml")

        data_uri = await group._build_telegram_image_data_uri(message)

        self.assertEqual(data_uri, "")
        message.bot.get_file.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
