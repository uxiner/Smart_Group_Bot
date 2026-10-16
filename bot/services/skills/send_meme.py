"""第二期：按素材类型发送群内素材（贴纸 / 图片 / 动图）。

与 :mod:`bot.services.skills.send_sticker` 的安全语义完全一致：

* 只能发**本群素材库里已登记**的 ``file_id``（候选匹配或显式 ``file_id`` 都要过
  归属校验），从不接受任意 URL 或库外 ``file_id``。
* 语义不命中就不发；没有候选就不发。
* 发送成功后记账失败只记日志，**绝不重发**。

区别只有一点：发送前按 ``media_type`` 分派到对应的 Telegram 接口，类型不合法
直接拒绝，绝不猜成贴纸。
"""

from __future__ import annotations

import logging

from bot.services.skills.base import SkillContext, SkillRunResult
from bot.services.sticker_library import (
    MEDIA_TYPES,
    MEDIA_TYPE_STICKER,
    normalize_media_type,
    query_has_semantic_evidence,
    sticker_library,
)
from bot.utils.security import clean_text
from bot.utils.telegram import send_material_with_auto_delete

log = logging.getLogger(__name__)


class SendMemeSkill:
    name = "send_meme"
    description = (
        "Send one in-group media material (sticker, photo or animation) that is already "
        "registered in this group's library, using an exact material file_id or a short "
        "semantic query. Never sends material from outside the current group."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Short emotion or scene description for picking suitable material.",
            },
            "media_file_id": {
                "type": "string",
                "description": "Exact library file_id to send when already known.",
            },
            "media_type": {
                "type": "string",
                "enum": list(MEDIA_TYPES),
                "description": "sticker, photo or animation. Defaults to sticker.",
            },
            "delivery_mode": {
                "type": "string",
                "enum": ["reply", "message"],
                "description": "reply replies to the triggering message; message sends as a standalone message.",
                "default": "reply",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict, context: SkillContext) -> SkillRunResult:
        session = context.session
        message = context.message
        if session is None or message is None:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前上下文不支持发送素材",
                error="missing_context",
            )

        group_id = int(getattr(getattr(message, "chat", None), "id", 0) or 0)
        if group_id == 0:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前会话无法定位群组",
                error="missing_group",
            )

        requested_file_id = clean_text(str(arguments.get("media_file_id", "")), max_len=255)
        query = clean_text(str(arguments.get("query", "")), max_len=120)
        raw_media_type = str(arguments.get("media_type", "") or "").strip().lower()
        if raw_media_type and raw_media_type not in MEDIA_TYPES:
            # Sending the wrong media type is worse than sending nothing.
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="不支持的素材类型",
                error="invalid_media_type",
            )
        requested_type = normalize_media_type(raw_media_type or MEDIA_TYPE_STICKER)
        allowed_types = (requested_type,)
        delivery_mode = clean_text(str(arguments.get("delivery_mode", "reply")), max_len=16).lower()
        if delivery_mode not in {"reply", "message"}:
            delivery_mode = "reply"

        picked_source = "explicit"
        picked_description = ""
        media_file_id = requested_file_id
        if not media_file_id:
            pick_query = query or context.current_user_text
            # Tool-level guard: text that cannot honestly justify a semantic
            # lookup is never turned into a material pick.
            if query_has_semantic_evidence(pick_query):
                picked = await sticker_library.pick_sticker(
                    session,
                    group_id,
                    query=pick_query,
                    fallback_pool=context.default_sticker_file_ids,
                    media_types=allowed_types,
                )
                media_file_id = (picked.file_id or "").strip()
                picked_source = getattr(picked, "source", "") or "none"
                picked_description = clean_text(
                    getattr(picked, "description", "") or "", max_len=120
                )
                if media_file_id and getattr(picked, "source", "") == "library_match":
                    requested_type = normalize_media_type(
                        getattr(picked, "media_type", requested_type)
                    )

        if not media_file_id:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前没有可用素材",
                error="no_material",
            )

        # Ownership gate: the chosen file_id must be registered for THIS group.
        owned = await sticker_library.lookup_owned_material(session, group_id, media_file_id)
        if owned is not None:
            owned_type = normalize_media_type(getattr(owned, "media_type", None))
            if raw_media_type and owned_type != requested_type:
                # The caller asserted a type that contradicts the library. Never
                # quietly send the other kind: sending the wrong media type is
                # worse than sending nothing.
                log.info(
                    "[%s] send_meme rejected media_type mismatch | file_id=%s "
                    "requested=%s owned=%s",
                    group_id,
                    media_file_id[:32],
                    requested_type,
                    owned_type,
                )
                return SkillRunResult(
                    ok=False,
                    skill=self.name,
                    summary="该素材类型与素材库记录不一致",
                    error="media_type_mismatch",
                )
            # No explicit type given: trust the library record over the default.
            requested_type = owned_type
        if picked_source == "explicit" and owned is None:
            # An explicit file_id the library does not know is never sendable.
            log.info(
                "[%s] send_meme rejected unowned material | file_id=%s",
                group_id,
                media_file_id[:32],
            )
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="该素材不属于本群素材库",
                error="material_not_owned",
            )
        if requested_type not in MEDIA_TYPES:
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="不支持的素材类型",
                error="invalid_media_type",
            )

        # ``pick_sticker`` may have read/imported the library. Persist that
        # snapshot and return its connection before Telegram I/O.
        try:
            await session.commit()
        except Exception as exc:
            log.exception("[%s] media snapshot commit failed", group_id)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="当前无法读取素材库",
                error=str(exc),
            )

        try:
            await send_material_with_auto_delete(
                message,
                file_id=media_file_id,
                media_type=requested_type,
                delivery_mode=delivery_mode,
                auto_delete_seconds=context.auto_delete_media_seconds,
                on_delivery=context.delivery_callback,
            )
        except Exception as exc:
            log.exception("[%s] send_meme skill failed", group_id)
            return SkillRunResult(
                ok=False,
                skill=self.name,
                summary="素材发送失败",
                error=str(exc),
            )

        # Delivery is already externally visible. Bookkeeping must use a fresh,
        # short transaction and must never turn a successful send into a tool
        # failure that could make the model retry the side effect.
        try:
            if context.session_factory is not None:
                async with context.session_factory() as mark_session:
                    await sticker_library.mark_sent(mark_session, group_id, media_file_id)
                    await mark_session.commit()
            else:
                await sticker_library.mark_sent(session, group_id, media_file_id)
        except Exception:
            log.exception(
                "[%s] sent material bookkeeping failed | file_id=%s",
                group_id,
                media_file_id[:32],
            )

        context.handled = True
        context.sticker_sent = True
        context.sticker_file_id = media_file_id
        context.suppress_followup_text = True
        return SkillRunResult(
            ok=True,
            skill=self.name,
            summary="",
            payload={
                "media_file_id": media_file_id,
                "media_type": requested_type,
                "query": query,
                "source": picked_source,
                "description": picked_description,
                "delivery_mode": delivery_mode,
            },
        )
