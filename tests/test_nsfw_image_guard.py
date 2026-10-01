"""群内公开发 NSFW 图片 → 删图 + @警告 + 质询。

覆盖需求点名的边界：

- 判定复用审核链路那**一次**视觉调用（提示词追加结构化要求），绝不新增模型调用；
- 只有模型明确回 ``NSFW_YES`` 才处置：``NSFW_NO`` / ``NSFW_UNKNOWN`` / 标记缺失 /
  模型拒答 / 视觉失败一律什么都不做；
- 处置顺序固定：删除 → 群里 @当事人警告（独立一条、2 分钟后自动删除）→ 质询；
  任何一步失败都只记日志并继续——**删图失败也照样警告并质询**；
- **记账失败不挡删图**：违规记录落库失败（SQLITE_BUSY / 磁盘/DB 异常）时照样
  删图 + 警告 + 质询，只跳过 ``notice_sent_at`` 写回，并在日志里写明「记账失败」；
- 贴纸不碰；带 ``/av`` 的图片不碰（已有「先删图再识图」流程负责）；
- 管理员/群主沿用现有审核豁免；手动豁免成员同样不处置；
- 同一条消息重复投递只处置一次（幂等）；
- 运行时开关 ``moderation.nsfw_image_guard_enabled`` 关闭后不判定也不处置；
- 原有图片描述/OCR 提示词与描述文本没有被破坏。

所有 Telegram / LLM 调用都是 mock，不触网。
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import ModelConfig, Settings
from bot.handlers import group
from bot.services.moderation import ModerationVerdict

GROUP_ID = -10001
USER_ID = 42
OTHER_USER_ID = 43
MESSAGE_ID = 777
WARNING_SENT_MESSAGE_ID = 999

NSFW_YES_TEXT = "NSFW_YES 画面中出现裸露的性器官。"
NSFW_NO_TEXT = "NSFW_NO 一只坐在键盘上的猫。"
NSFW_UNKNOWN_TEXT = "NSFW_UNKNOWN 画面过于模糊。"
REFUSAL_TEXT = "抱歉，我无法判断这张图片。"
IMAGE_DESCRIPTION = "图中有一个杯子"
ORIGINAL_VISION_PROMPT = (
    "Please describe key information in this image, prioritizing visible text (OCR) and main objects. "
    "Respond briefly in Chinese within 30 words. "
    "If no useful content can be identified, reply exactly: NO_VALID_IMAGE_CONTENT."
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _moderation_settings(**overrides) -> SimpleNamespace:
    values = {
        "enabled": True,
        "warn_threshold": 3,
        "high_confidence_threshold": 0.9,
        "challenge_timeout_seconds": 600,
        "bot_screening_enabled": True,
        "bot_screening_message_count": 5,
        "nsfw_image_guard_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _settings(**moderation_overrides) -> SimpleNamespace:
    return SimpleNamespace(
        super_admin_id=1,
        bot=SimpleNamespace(
            main_model="",
            decision_model="",
            compress_model="",
            moderation_model="",
            vision_model="",
            embed_model="",
            max_context_tokens=0,
            auto_delete_seconds=0,
            parse_mode="HTML",
        ),
        moderation=_moderation_settings(**moderation_overrides),
        skill_sticker_file_ids="",
    )


def _user(user_id: int = USER_ID, *, username: str | None = "member") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        is_bot=False,
        username=username,
        full_name="Member",
    )


async def _write_image(_path, *, destination) -> None:
    destination.write(b"\xff\xd8\xff\xe0nsfw")


def _photo_message(
    *,
    caption: str | None = None,
    user_id: int = USER_ID,
    username: str | None = "member",
    message_id: int = MESSAGE_ID,
) -> SimpleNamespace:
    conversation: list[str] = []
    bot = SimpleNamespace(
        me=AsyncMock(return_value=SimpleNamespace(username="selfbot", id=1)),
        get_file=AsyncMock(
            return_value=SimpleNamespace(file_path="photos/x.jpg", file_size=3)
        ),
        download_file=AsyncMock(side_effect=_write_image),
    )

    async def delete():
        conversation.append("delete")

    async def answer(_text, **_kwargs):
        conversation.append("warn")
        return SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_ID),
            message_id=WARNING_SENT_MESSAGE_ID,
        )

    message = SimpleNamespace(
        message_id=message_id,
        date=None,
        chat=SimpleNamespace(id=GROUP_ID, type="supergroup", title="测试群"),
        from_user=_user(user_id, username=username),
        sender_chat=None,
        photo=[SimpleNamespace(file_id="photo-file-id", file_size=3)],
        document=None,
        animation=None,
        sticker=None,
        video=None,
        video_note=None,
        audio=None,
        contact=None,
        voice=None,
        text=None,
        caption=caption,
        reply_to_message=None,
        delete=AsyncMock(side_effect=delete),
        answer=AsyncMock(side_effect=answer),
        bot=bot,
    )
    message.conversation = conversation
    return message


def _sticker_message() -> SimpleNamespace:
    message = _photo_message()
    message.photo = None
    message.sticker = SimpleNamespace(
        file_id="sticker-file-id",
        file_size=3,
        is_animated=False,
        is_video=False,
        emoji="🙂",
        thumbnail=None,
    )
    return message


def _verdict() -> ModerationVerdict:
    return ModerationVerdict(
        violated=False, reason="", rule=None, conclusive=True, confidence=0.0
    )


def _fake_moderation_service(
    *,
    exempt: bool = False,
    event_created: bool = True,
    exempt_error: bool = False,
    record_error: bool = False,
    record_side_effect=None,
) -> SimpleNamespace:
    async def is_user_exempt(_session, _group_id, _user_id):
        if exempt_error:
            # 只让本功能这次查询失败：豁免检查失败后流程要照常继续（不处置），
            # 后面的文本审核会再查一次，那次必须正常返回。
            if not state["exempt_error_raised"]:
                state["exempt_error_raised"] = True
                raise RuntimeError("exemption lookup failed")
        return exempt

    async def record_violation(*_args, **_kwargs):
        if record_error:
            raise RuntimeError("db write failed")
        return SimpleNamespace(
            id=321,
            notice_sent_at=None,
            _source_event_created=event_created,
        )

    record_mock = AsyncMock(
        side_effect=record_side_effect
        if record_side_effect is not None
        else record_violation
    )
    state = {"exempt_error_raised": False}
    return SimpleNamespace(
        is_user_exempt=AsyncMock(side_effect=is_user_exempt),
        record_violation=record_mock,
        evaluate=AsyncMock(return_value=_verdict()),
        is_high_confidence=lambda _verdict: True,
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(),
        flush=AsyncMock(),
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=1)),
    )


def _vision_llm(vision_text: str) -> SimpleNamespace:
    return SimpleNamespace(vision_describe=AsyncMock(return_value=vision_text))


async def _run_group_message(
    *,
    vision_text: str,
    message: SimpleNamespace | None = None,
    moderation_service: SimpleNamespace | None = None,
    settings: SimpleNamespace | None = None,
    session: SimpleNamespace | None = None,
    fresh_side_effect=(True, False),
    tg_admin: bool = False,
    challenge=None,
):
    """跑一遍 ``on_group_message``：Telegram / LLM 全部 mock，不触网。

    默认让第二次 ``_fresh_group_authorized_for_moderation``（文本审核分支里那次）
    返回 False，好在没有处置时把流程停住，不进入回复流水线。
    """
    message = message or _photo_message()
    moderation_service = moderation_service or _fake_moderation_service()
    llm = _vision_llm(vision_text)
    settings = settings or _settings()
    session = session or _session()

    if challenge is None:
        async def challenge(**_kwargs):
            message.conversation.append("challenge")
            return True

        challenge_mock = AsyncMock(side_effect=challenge)
    else:
        challenge_mock = challenge

    patchers = [
        patch(
            "bot.handlers.group.ensure_group_authorized",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "bot.handlers.group._fresh_group_authorized_for_moderation",
            new=AsyncMock(side_effect=list(fresh_side_effect)),
        ),
        patch(
            "bot.handlers.group._record_group_activity_cas",
            # mute_all_replies=True 让"没有处置"的用例在审核分支之后干净收尾，
            # 不进入回复流水线（本文件只关心 NSFW 处置这一段）。
            new=AsyncMock(return_value={"mute_all_replies": True}),
        ),
        patch(
            "bot.handlers.group._is_user_admin_cached",
            new=AsyncMock(return_value=tg_admin),
        ),
        patch("bot.handlers.group._best_effort_commit", new=AsyncMock()),
        patch(
            "bot.handlers.group.memory_holder",
            SimpleNamespace(get_optional=lambda: None),
        ),
        patch("bot.handlers.group.LLMService", return_value=llm),
        patch("bot.handlers.group.ModerationService", return_value=moderation_service),
        patch(
            "bot.handlers.group.build_moderation_context",
            new=AsyncMock(return_value=([], False)),
        ),
        patch(
            "bot.handlers.group.sticker_library",
            SimpleNamespace(learn_from_message=AsyncMock(return_value=None)),
        ),
        patch("bot.handlers.group.begin_moderation_challenge", new=challenge_mock),
    ]
    with ExitStack() as stack:
        for patcher in patchers:
            stack.enter_context(patcher)
        await group.on_group_message(message, session=session, settings=settings)
    return message, llm, moderation_service, session, challenge_mock


# ---------------------------------------------------------------------------
# 标记解析
# ---------------------------------------------------------------------------


class NsfwMarkerParsingTests(unittest.TestCase):
    def test_yes_no_unknown_are_parsed_only_at_the_start(self) -> None:
        self.assertEqual(group._parse_nsfw_marker(NSFW_YES_TEXT), "NSFW_YES")
        self.assertEqual(group._parse_nsfw_marker(NSFW_NO_TEXT), "NSFW_NO")
        self.assertEqual(group._parse_nsfw_marker(NSFW_UNKNOWN_TEXT), "NSFW_UNKNOWN")
        self.assertEqual(group._parse_nsfw_marker("  \n nsfw_yes 描述"), "NSFW_YES")

    def test_missing_refusal_and_mid_text_markers_do_not_count(self) -> None:
        for text in (
            "",
            None,
            REFUSAL_TEXT,
            "图中的 NSFW_YES 只是文字",
            "这是一个杯子",
            "NSFW_YESONSET",  # 前缀相似但不是标记
        ):
            with self.subTest(text=text):
                self.assertEqual(group._parse_nsfw_marker(text), "")

    def test_strip_keeps_every_description_character(self) -> None:
        self.assertEqual(
            group._strip_nsfw_marker(f"NSFW_YES {IMAGE_DESCRIPTION}"),
            IMAGE_DESCRIPTION,
        )
        self.assertEqual(group._strip_nsfw_marker("NSFW_NO"), "")
        # 没有标记的文本原样返回（贴纸库/记忆归档依赖这段描述）
        self.assertEqual(
            group._strip_nsfw_marker(IMAGE_DESCRIPTION), IMAGE_DESCRIPTION
        )


# ---------------------------------------------------------------------------
# 适用范围（贴纸 / /av / 开关 / 非图片文档）
# ---------------------------------------------------------------------------


class NsfwGuardScopeTests(unittest.TestCase):
    def test_images_are_in_scope_and_stickers_are_not(self) -> None:
        photo = _photo_message()
        self.assertTrue(group._nsfw_image_guard_applies(photo, "photo", _settings()))
        self.assertTrue(
            group._nsfw_image_guard_applies(photo, "photo_caption", _settings())
        )

        sticker = _sticker_message()
        # 贴纸确实是审核链路认的图片类型，但本功能必须放过它
        self.assertIsNotNone(group._extract_image_file_info(sticker))
        for msg_type in ("sticker", "text", "video", "video_caption", "voice"):
            with self.subTest(msg_type=msg_type):
                self.assertFalse(
                    group._nsfw_image_guard_applies(sticker, msg_type, _settings())
                )

    def test_non_image_document_is_out_of_scope(self) -> None:
        message = _photo_message()
        message.photo = None
        message.document = SimpleNamespace(
            file_id="doc", mime_type="application/pdf", file_size=3
        )
        self.assertIsNone(group._extract_image_file_info(message))
        self.assertFalse(
            group._nsfw_image_guard_applies(message, "document", _settings())
        )

    def test_av_images_are_left_to_the_existing_av_flow(self) -> None:
        for caption in ("/av", "/av WANZ-530", "/av@selfbot", " /av  人妻"):
            with self.subTest(caption=caption):
                message = _photo_message(caption=caption)
                self.assertTrue(group._is_group_av_image_message(message))
                self.assertFalse(
                    group._nsfw_image_guard_applies(
                        message, "photo_caption", _settings()
                    )
                )

        plain = _photo_message(caption="普通的图片说明")
        self.assertFalse(group._is_group_av_image_message(plain))
        self.assertTrue(
            group._nsfw_image_guard_applies(plain, "photo_caption", _settings())
        )
        # /average 不是 /av 命令
        sneaky = _photo_message(caption="/average 5")
        self.assertFalse(group._is_group_av_image_message(sneaky))

    def test_runtime_switch_disables_scope(self) -> None:
        message = _photo_message()
        self.assertFalse(
            group._nsfw_image_guard_applies(
                message, "photo", _settings(nsfw_image_guard_enabled=False)
            )
        )
        # 审核总开关关闭时本功能同样不生效
        self.assertFalse(
            group._nsfw_image_guard_applies(message, "photo", _settings(enabled=False))
        )


# ---------------------------------------------------------------------------
# 视觉提示词与描述文本
# ---------------------------------------------------------------------------


class NsfwVisionPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_appends_nsfw_requirement_and_keeps_descriptions(self) -> None:
        message = _photo_message()
        llm = _vision_llm(f"NSFW_YES {IMAGE_DESCRIPTION}")

        text, vision = await group._append_image_context(
            message, llm, "[image]", "photo", nsfw_guard=True
        )

        prompt = llm.vision_describe.await_args.args[1]
        self.assertTrue(prompt.startswith(ORIGINAL_VISION_PROMPT))
        self.assertIn("NSFW_YES", prompt)
        self.assertIn("NSFW_UNKNOWN", prompt)
        self.assertIn("明确露骨色情内容", prompt)
        # 只调一次视觉模型（成本红线）
        self.assertEqual(llm.vision_describe.await_count, 1)
        # 描述一字不少，标记不进正文；原始输出留给调用方判定
        self.assertEqual(text, f"[image]\n[image-vision]\n{IMAGE_DESCRIPTION}")
        self.assertNotIn("NSFW_", text)
        self.assertEqual(vision, f"NSFW_YES {IMAGE_DESCRIPTION}")

    async def test_prompt_is_unchanged_without_the_guard(self) -> None:
        message = _photo_message()
        llm = _vision_llm(f"NSFW_YES {IMAGE_DESCRIPTION}")

        text, vision = await group._append_image_context(
            message, llm, "[image]", "photo"
        )

        self.assertEqual(llm.vision_describe.await_args.args[1], ORIGINAL_VISION_PROMPT)
        # 旧行为一字不改：正文原样带上模型输出
        self.assertEqual(
            text, f"[image]\n[image-vision]\nNSFW_YES {IMAGE_DESCRIPTION}"
        )
        self.assertEqual(vision, f"NSFW_YES {IMAGE_DESCRIPTION}")

    async def test_marker_only_reply_does_not_add_empty_vision_block(self) -> None:
        message = _photo_message()
        llm = _vision_llm("NSFW_YES")

        text, vision = await group._append_image_context(
            message, llm, "[image]", "photo", nsfw_guard=True
        )

        self.assertEqual(text, "[image]")
        self.assertEqual(vision, "NSFW_YES")

    async def test_vision_failure_reports_nothing(self) -> None:
        message = _photo_message()
        llm = SimpleNamespace(
            vision_describe=AsyncMock(side_effect=RuntimeError("model down"))
        )

        text, vision = await group._append_image_context(
            message, llm, "[image]", "photo", nsfw_guard=True
        )

        self.assertEqual((text, vision), ("[image]", ""))


# ---------------------------------------------------------------------------
# 处置动作
# ---------------------------------------------------------------------------


class NsfwDisposalTests(unittest.IsolatedAsyncioTestCase):
    async def test_hit_deletes_warns_and_challenges_in_order(self) -> None:
        message, llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT
        )

        self.assertEqual(message.conversation, ["delete", "warn", "challenge"])
        message.delete.assert_awaited_once()
        # 只调一次视觉模型：判定复用审核链路那次调用
        self.assertEqual(llm.vision_describe.await_count, 1)

        # ② 群里 @当事人：独立一条消息 + 明确 @ + 原因
        warn_text = message.answer.await_args.args[0]
        self.assertTrue(warn_text.startswith("@member"), warn_text)
        self.assertIn("裸露/色情图片", warn_text)
        self.assertIn("已删除", warn_text)
        # 默认净化会把 @handle 拆成零宽字符，这里必须没被拆
        self.assertNotIn("\u200b", warn_text)
        # 独立一条：不回复、不引用那张已经被删掉的图
        warn_kwargs = message.answer.await_args.kwargs
        self.assertNotIn("reply_to_message_id", warn_kwargs)
        self.assertNotIn("reply_parameters", warn_kwargs)

        # ③ 质询：复用现有质询，不给「花积分免除」入口
        self.assertEqual(challenge.await_args.kwargs["user_id"], USER_ID)
        self.assertEqual(challenge.await_args.kwargs["rule_action"], "ban")
        self.assertFalse(challenge.await_args.kwargs["allow_points_skip"])
        self.assertIn("色情", challenge.await_args.kwargs["reason"])

        # 事件落库：原因与来源（source_message_id 做幂等键）
        self.assertEqual(moderation.record_violation.await_args.args[4], "nsfw_image")
        self.assertEqual(
            moderation.record_violation.await_args.kwargs["source_message_id"],
            MESSAGE_ID,
        )
        self.assertIn("nsfw-image", moderation.record_violation.await_args.args[3])

    async def test_warning_is_auto_deleted_after_two_minutes(self) -> None:
        with patch(
            "bot.utils.telegram.schedule_message_auto_delete_durable",
            new=AsyncMock(return_value=True),
        ) as durable:
            await _run_group_message(vision_text=NSFW_YES_TEXT)

        durable.assert_awaited_once()
        sent, seconds = durable.await_args.args
        self.assertEqual(seconds, 120)
        self.assertEqual(sent.message_id, WARNING_SENT_MESSAGE_ID)
        self.assertEqual(group._NSFW_IMAGE_WARNING_AUTO_DELETE_SECONDS, 120)

    async def test_negative_markers_and_failures_do_nothing(self) -> None:
        for vision_text in (
            NSFW_NO_TEXT,
            NSFW_UNKNOWN_TEXT,
            REFUSAL_TEXT,
            IMAGE_DESCRIPTION,
            "",
        ):
            with self.subTest(vision_text=vision_text):
                message, _llm, moderation, _session_mock, challenge = (
                    await _run_group_message(vision_text=vision_text)
                )
                self.assertEqual(message.conversation, [])
                message.delete.assert_not_awaited()
                message.answer.assert_not_awaited()
                challenge.assert_not_awaited()
                moderation.record_violation.assert_not_awaited()

    async def test_delete_failure_still_warns_and_challenges(self) -> None:
        message = _photo_message()
        message.delete = AsyncMock(side_effect=RuntimeError("no rights"))

        message, _llm, moderation, session, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT, message=message
        )

        self.assertEqual(message.conversation, ["warn", "challenge"])
        message.answer.assert_awaited_once()
        challenge.assert_awaited_once()
        moderation.record_violation.assert_awaited_once()
        # 记账成功才占住幂等键：删图失败之前违规记录已经落库（不回归）
        session.commit.assert_awaited()

    async def test_warning_failure_still_challenges(self) -> None:
        message = _photo_message()
        message.answer = AsyncMock(side_effect=RuntimeError("chat restricted"))

        message, _llm, _moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT, message=message
        )

        # 警告发不出去也要继续质询
        self.assertEqual(message.conversation, ["delete", "challenge"])
        challenge.assert_awaited_once()

    async def test_challenge_failure_does_not_escape(self) -> None:
        challenge = AsyncMock(side_effect=RuntimeError("verification unavailable"))

        message, _llm, moderation, _session_mock, _challenge = (
            await _run_group_message(vision_text=NSFW_YES_TEXT, challenge=challenge)
        )

        self.assertEqual(message.conversation, ["delete", "warn"])
        moderation.record_violation.assert_awaited_once()

    async def test_owner_and_tg_admin_keep_the_existing_exemption(self) -> None:
        owner = _photo_message(user_id=1, username="owner")
        owner, _llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT, message=owner
        )
        self.assertEqual(owner.conversation, [])
        moderation.record_violation.assert_not_awaited()
        challenge.assert_not_awaited()

        admin = _photo_message(user_id=OTHER_USER_ID, username="admin")
        admin, _llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT, message=admin, tg_admin=True
        )
        self.assertEqual(admin.conversation, [])
        moderation.record_violation.assert_not_awaited()
        challenge.assert_not_awaited()

    async def test_manually_exempt_member_is_not_disposed(self) -> None:
        message, _llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT,
            moderation_service=_fake_moderation_service(exempt=True),
        )

        self.assertEqual(message.conversation, [])
        moderation.record_violation.assert_not_awaited()
        challenge.assert_not_awaited()

    async def test_exemption_lookup_failure_disposes_nothing(self) -> None:
        message, _llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT,
            moderation_service=_fake_moderation_service(exempt_error=True),
        )

        self.assertEqual(message.conversation, [])
        moderation.record_violation.assert_not_awaited()
        challenge.assert_not_awaited()

    async def test_sender_chat_identity_is_skipped(self) -> None:
        message = _photo_message(username=None)
        message.sender_chat = SimpleNamespace(id=GROUP_ID, title="Group")
        message.from_user = None

        message, _llm, moderation, _session_mock, _challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT, message=message
        )

        self.assertEqual(message.conversation, [])
        moderation.record_violation.assert_not_awaited()

    async def test_duplicate_delivery_is_disposed_only_once(self) -> None:
        message = _photo_message()
        moderation = _fake_moderation_service(
            record_side_effect=[
                SimpleNamespace(id=1, notice_sent_at=None, _source_event_created=True),
                SimpleNamespace(id=1, notice_sent_at=None, _source_event_created=False),
            ]
        )

        async def challenge(**_kwargs):
            message.conversation.append("challenge")
            return True

        challenge_mock = AsyncMock(side_effect=challenge)
        for _ in range(2):
            (
                _message,
                _llm,
                _moderation,
                session,
                _challenge,
            ) = await _run_group_message(
                vision_text=NSFW_YES_TEXT,
                message=message,
                moderation_service=moderation,
                challenge=challenge_mock,
            )

        # 第二次是「已有违规记录」：一条 Telegram 动作都不许重复
        self.assertEqual(message.conversation, ["delete", "warn", "challenge"])
        message.delete.assert_awaited_once()
        message.answer.assert_awaited_once()
        challenge_mock.assert_awaited_once()
        self.assertEqual(moderation.record_violation.await_count, 2)
        # 重复投递只是回滚掉这次的多余事务，不占新键也不写回
        session.rollback.assert_awaited_once()
        session.commit.assert_not_awaited()

    async def test_record_failure_still_deletes_warns_and_challenges(self) -> None:
        """记账失败（SQLITE_BUSY / 磁盘异常）不许换来「图留在群里」。

        这是本功能的第一目的：NSFW 图尽快离开群。违规记录只用于留痕与幂等，
        写不进去也要照样删图 → 警告 → 质询，且日志里要写明「记账失败」。
        """
        with self.assertLogs("bot.handlers.group", level="ERROR") as logs:
            message, _llm, moderation, session, challenge = await _run_group_message(
                vision_text=NSFW_YES_TEXT,
                moderation_service=_fake_moderation_service(record_error=True),
            )

        # 核心断言：删图 + 警告 + 质询三者都发生，顺序与正常路径完全一致
        self.assertEqual(message.conversation, ["delete", "warn", "challenge"])
        message.delete.assert_awaited_once()
        message.answer.assert_awaited_once()
        challenge.assert_awaited_once()
        # 记账确实尝试过并失败，且失败被明确写进日志（便于排查）
        moderation.record_violation.assert_awaited_once()
        self.assertTrue(
            any("记账失败" in line for line in logs.output),
            logs.output,
        )
        # 没有 violation 行 → 跳过 notice_sent_at 写回：不因此再 commit，
        # 也不因此把质询炸掉（质询自身在这个用例里是 mock）
        session.commit.assert_not_awaited()
        # 已经处置过就必须结束本条消息：不许落回文本审核再走一遍
        moderation.evaluate.assert_not_awaited()
        # 质询仍然不给「花积分免除」入口
        self.assertEqual(challenge.await_args.kwargs["rule_action"], "ban")
        self.assertFalse(challenge.await_args.kwargs["allow_points_skip"])

    async def test_record_failure_with_delete_failure_still_warns_and_challenges(
        self,
    ) -> None:
        """记账失败 + 删图也失败：警告与质询仍然要发出去。"""
        message = _photo_message()
        message.delete = AsyncMock(side_effect=RuntimeError("no rights"))

        message, _llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT,
            message=message,
            moderation_service=_fake_moderation_service(record_error=True),
        )

        self.assertEqual(message.conversation, ["warn", "challenge"])
        message.answer.assert_awaited_once()
        challenge.assert_awaited_once()
        moderation.record_violation.assert_awaited_once()

    async def test_warning_failure_still_challenges_when_record_failed(self) -> None:
        """记账失败 + 警告失败：质询照发，异常不外泄。"""
        message = _photo_message()
        message.answer = AsyncMock(side_effect=RuntimeError("chat restricted"))

        message, _llm, _moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT,
            message=message,
            moderation_service=_fake_moderation_service(record_error=True),
        )

        self.assertEqual(message.conversation, ["delete", "challenge"])
        challenge.assert_awaited_once()

    async def test_rollback_failure_after_record_failure_still_disposes(self) -> None:
        """清失败事务的回滚也失败时，删图/警告/质询一个都不能少。"""
        session = _session()
        session.rollback = AsyncMock(side_effect=RuntimeError("rollback failed"))

        message, _llm, _moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT,
            session=session,
            moderation_service=_fake_moderation_service(record_error=True),
        )

        self.assertEqual(message.conversation, ["delete", "warn", "challenge"])
        message.delete.assert_awaited_once()
        challenge.assert_awaited_once()

    async def test_duplicate_skips_every_action_even_if_rollback_fails(self) -> None:
        """已有违规记录：一条 Telegram 动作都不做，回滚失败也不改变这一点。"""
        session = _session()
        session.rollback = AsyncMock(side_effect=RuntimeError("rollback failed"))

        message, _llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT,
            session=session,
            moderation_service=_fake_moderation_service(event_created=False),
        )

        self.assertEqual(message.conversation, [])
        message.delete.assert_not_awaited()
        message.answer.assert_not_awaited()
        challenge.assert_not_awaited()
        # 已经算处置过：不许落回文本审核再走一遍
        moderation.evaluate.assert_not_awaited()
        session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# 与 on_group_message 的接线：贴纸 / /av / 开关
# ---------------------------------------------------------------------------


class NsfwGuardWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_sticker_is_never_sent_for_nsfw_judgement(self) -> None:
        message, llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT, message=_sticker_message()
        )

        self.assertEqual(llm.vision_describe.await_count, 1)
        prompt = llm.vision_describe.await_args.args[1]
        self.assertNotIn("NSFW_YES", prompt)
        self.assertEqual(message.conversation, [])
        moderation.record_violation.assert_not_awaited()
        challenge.assert_not_awaited()

    async def test_av_image_is_never_disposed(self) -> None:
        message, llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT, message=_photo_message(caption="/av WANZ-530")
        )

        # 「/av + 图片」由 commands.py 的先删图再识图流程负责，这里不碰
        prompt = llm.vision_describe.await_args.args[1]
        self.assertNotIn("NSFW_YES", prompt)
        self.assertEqual(message.conversation, [])
        moderation.record_violation.assert_not_awaited()
        challenge.assert_not_awaited()

    async def test_switch_off_means_no_judgement_and_no_extra_model_call(self) -> None:
        message, llm, moderation, _session_mock, challenge = await _run_group_message(
            vision_text=NSFW_YES_TEXT,
            settings=_settings(nsfw_image_guard_enabled=False),
        )

        # 开关关闭：提示词里没有 NSFW 要求（不做判定），处置一个都没有，
        # 而且视觉调用仍然只有审核链路那一次（没有为本功能多调模型）。
        self.assertEqual(llm.vision_describe.await_count, 1)
        prompt = llm.vision_describe.await_args.args[1]
        self.assertNotIn("NSFW_YES", prompt)
        self.assertEqual(prompt, ORIGINAL_VISION_PROMPT)
        self.assertEqual(message.conversation, [])
        moderation.record_violation.assert_not_awaited()
        challenge.assert_not_awaited()


# ---------------------------------------------------------------------------
# 配置接线
# ---------------------------------------------------------------------------


class NsfwGuardConfigTests(unittest.TestCase):
    def test_runtime_config_exposes_the_switch_and_defaults_to_on(self) -> None:
        from bot.services.runtime_config import ModerationSettingsConfig, RuntimeConfig

        self.assertTrue(ModerationSettingsConfig().nsfw_image_guard_enabled)

        config = RuntimeConfig()
        self.assertTrue(config.moderation.nsfw_image_guard_enabled)

        settings = Settings(_env_file=None, bot_token="42:TEST", super_admin_id=42)
        config.moderation.nsfw_image_guard_enabled = False
        config.apply_to_settings(settings)
        self.assertFalse(settings.moderation.nsfw_image_guard_enabled)

    def test_settings_default_keeps_the_guard_on(self) -> None:
        settings = Settings(_env_file=None, bot_token="42:TEST", super_admin_id=42)
        self.assertTrue(settings.moderation.nsfw_image_guard_enabled)
        self.assertIsInstance(settings.bot.main_model, ModelConfig)


if __name__ == "__main__":
    unittest.main()
