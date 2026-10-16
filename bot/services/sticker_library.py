from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import MediaIngestLog, StickerLibraryRecord
from bot.utils.security import clean_text

_ROOT = Path(__file__).resolve().parent.parent.parent
_LEGACY_STICKER_DIR = _ROOT / "memory" / "stickers"
_INVALID_VISION_MARKERS = {"NO_VALID_IMAGE_CONTENT", "NO_VALID_IMAGE"}
_SPACE_RE = re.compile(r"\s+")
_MAX_STICKERS_PER_GROUP = 100

# ---------------------------------------------------------------------------
# 结构化贴纸画像（第一期：群内素材理解 + 可信检索）
#
# 描述串形如 ``画面客观描述 | 情绪=开心 | 场景=庆祝,鼓励 | 梗=熊猫头``。
# 结构化字段是**可选的**：旧库里的纯画面描述照常解析（base=原文，其余字段为空），
# 因此这次改动不需要数据迁移，也不会给旧行编造情绪/梗名。
# ---------------------------------------------------------------------------
_MAX_DESCRIPTION_CHARS = 240
_MAX_ALIASES = 12
_MAX_STICKER_QUERY_CHARS = 32
_MAX_STICKER_DRAFT_CHARS = 128
_MATCH_SCORE_THRESHOLD = 50
_SEGMENT_MATCH_MIN = 2
_RATIO_MATCH_MIN_CHARS = 3
_RATIO_MATCH_THRESHOLD = 0.72

_FIELD_RE = re.compile(r"^(情绪|场景|梗)\s*[=:]\s*(.*)$")
# 查询侧闸门：标点/数字/括号说明这是文章或代码，不是一句情绪表达。
_NON_STICKER_QUERY_RE = re.compile(r"[，。！？、,.!?;；:：\"'“”‘’()（）\[\]【】<>《》0-9]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 情绪词表：匹配顺序 = 这里声明的顺序，保证同一段文字得到稳定结果。
_EMOTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("开心", ("开心", "高兴", "快乐", "大笑", "哈哈大笑", "笑脸", "喜悦", "兴奋", "赞美", "鼓掌", "憨笑")),
    ("无语", ("无语", "摊手", "耸肩", "沉默", "尴尬", "无奈", "扶额", "汗")),
    ("难过", ("难过", "悲伤", "伤心", "委屈", "哭", "沮丧", "低落", "安慰", "抱抱", "心疼")),
    ("生气", ("生气", "愤怒", "恼火", "不爽", "翻白眼")),
    ("惊讶", ("惊讶", "震惊", "吃惊", "目瞪口呆")),
    ("嘲讽", ("嘲讽", "阴阳怪气", "讽刺", "冷笑")),
    ("期待", ("期待", "等不及", "想看")),
    ("鼓励", ("加油", "鼓励", "打气", "支持")),
    ("躺平", ("躺平", "摆烂", "累了")),
)

# 场景词表：同一元组内视为同一场景的同义词。
_SCENE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("庆祝", ("庆祝", "祝贺", "恭喜", "胜利", "赢了", "干杯", "撒花")),
    ("安慰", ("安慰", "抱抱", "摸摸", "别难过", "心疼")),
    ("围观", ("围观", "吃瓜", "看戏", "看热闹", "搬板凳", "吃瓜群众")),
    ("告别", ("告别", "再见", "下班", "散会", "退场", "晚安")),
    ("问候", ("问候", "打招呼", "早上好", "晚上好", "欢迎", "你好")),
    ("道歉", ("道歉", "对不起", "抱歉", "认错")),
    ("感谢", ("感谢", "谢谢", "多谢", "感谢感谢")),
    ("同意", ("同意", "赞成", "附议", "点赞", "支持", "认同", "说得对")),
    ("拒绝", ("拒绝", "不行", "不要", "打住", "住手", "不了")),
    ("吐槽", ("吐槽", "抱怨", "无语", "服了", "离谱")),
    ("催促", ("催促", "赶紧", "快点", "催更")),
    ("睡觉", ("睡觉", "晚安", "困了", "下班")),
)

# 严肃语境词表：出现即视为“不适合发表情”的语境。
_SERIOUS_TERMS: tuple[str, ...] = (
    "警告",
    "处罚",
    "禁言",
    "封禁",
    "踢出",
    "违规",
    "举报",
    "申诉",
    "投诉",
    "报错",
    "错误",
    "失败",
    "故障",
    "宕机",
    "崩溃",
    "修复",
    "排查",
    "配置",
    "接口",
    "数据库",
    "日志",
    "堆栈",
    "异常",
    "部署",
    "上线",
    "更新",
    "重启",
    "支付",
    "转账",
    "退款",
    "订单",
    "密码",
    "账号",
    "验证码",
    "登录",
    "隐私",
    "法律",
    "医疗",
    "诊断",
    "吃药",
    "自杀",
    "危险",
    "报警",
)


def _ordered_keywords(groups: tuple[tuple[str, tuple[str, ...]], ...]) -> tuple[str, ...]:
    """Return every surface keyword ordered by descending length.

    Longer phrases win so ``哈哈大笑`` is not shadowed by ``大笑`` and the
    inferred label stays stable for a given text.
    """
    seen: list[str] = []
    for _label, words in groups:
        for word in words:
            if word not in seen:
                seen.append(word)
    return tuple(sorted(seen, key=lambda w: (-len(w), w)))


_ALL_STICKER_KEYWORDS = _ordered_keywords(_EMOTION_GROUPS) + _ordered_keywords(_SCENE_GROUPS)


def _strip_structure_syntax(text: str) -> str:
    """Remove characters that would let untrusted text forge structured fields."""
    return (text or "").replace("|", " ").replace("=", " ")


def _infer_fields(text: str) -> tuple[str, tuple[str, ...]]:
    """Infer (emotion, scenes) from text using the closed label vocabularies.

    Never invents a meme name: that requires an explicit ``梗=`` field, which
    only a real understanding step may supply.
    """
    haystack = text or ""
    if not haystack or not _CJK_RE.search(haystack):
        return "", ()

    emotion = ""
    for label, words in _EMOTION_GROUPS:
        if any(word in haystack for word in words):
            emotion = label
            break

    scenes: list[str] = []
    for label, words in _SCENE_GROUPS:
        if label in scenes:
            continue
        if any(word in haystack for word in words):
            scenes.append(label)
    return emotion, tuple(scenes)


def _clean_scene_list(value: str | None) -> list[str]:
    items: list[str] = []
    for part in str(value or "").replace("/", ",").replace("，", ",").split(","):
        item = part.strip()
        if item and item not in items:
            items.append(item)
    return items


@dataclass(frozen=True, slots=True)
class StickerProfile:
    """Structured view of one sticker's description."""

    base: str = ""
    emotion: str = ""
    scenes: tuple[str, ...] = ()
    meme: str = ""

    @property
    def is_structured(self) -> bool:
        return bool(self.emotion or self.scenes or self.meme)


# ---------------------------------------------------------------------------
# 素材类型（第二期：素材库从「贴纸」扩成 sticker/photo/animation）
#
# ``media_type`` 只有这三个值是合法的。旧库没有这一列，读出来是默认值
# ``sticker``；任何未知/空值也一律按 ``sticker`` 处理——绝不猜成"可能是图片"，
# 因为猜错会导致用错误的 Telegram 接口发送。
# ---------------------------------------------------------------------------
MEDIA_TYPE_STICKER = "sticker"
MEDIA_TYPE_PHOTO = "photo"
MEDIA_TYPE_ANIMATION = "animation"
MEDIA_TYPES: tuple[str, ...] = (
    MEDIA_TYPE_STICKER,
    MEDIA_TYPE_PHOTO,
    MEDIA_TYPE_ANIMATION,
)


def normalize_media_type(value: Any) -> str:
    """Coerce a stored/requested media type to one of :data:`MEDIA_TYPES`.

    Unknown and missing values degrade to ``sticker`` because that is what
    every pre-phase-2 row is.
    """
    candidate = str(value or "").strip().lower()
    if candidate in MEDIA_TYPES:
        return candidate
    return MEDIA_TYPE_STICKER


@dataclass(frozen=True, slots=True)
class MediaMaterial:
    """Type-aware view of one library row, used for sending and for auditing."""

    file_id: str = ""
    media_type: str = MEDIA_TYPE_STICKER
    mime_type: str = ""
    width: int = 0
    height: int = 0
    description: str = ""

    @property
    def is_sendable(self) -> bool:
        return bool(self.file_id) and self.media_type in MEDIA_TYPES


def parse_media_material(row: StickerLibraryRecord) -> MediaMaterial:
    """Build a :class:`MediaMaterial` from a library row without trusting it."""
    return MediaMaterial(
        file_id=(getattr(row, "file_id", None) or "").strip(),
        media_type=normalize_media_type(getattr(row, "media_type", None)),
        mime_type=(getattr(row, "mime_type", None) or "").strip(),
        width=max(0, int(getattr(row, "width", 0) or 0)),
        height=max(0, int(getattr(row, "height", 0) or 0)),
        description=(getattr(row, "description", None) or "").strip(),
    )


def parse_profile(description: str) -> StickerProfile:
    """Parse ``描述 | 情绪=.. | 场景=..`` and stay compatible with legacy text."""
    raw = str(description or "").strip()
    if not raw:
        return StickerProfile()

    head, separator, tail = raw.partition("|")
    base = head.strip()
    if not separator:
        return StickerProfile(base=raw)

    emotion = ""
    meme = ""
    scenes: list[str] = []
    for chunk in tail.split("|"):
        match = _FIELD_RE.match(chunk.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key == "情绪":
            emotion = value
        elif key == "场景":
            scenes = _clean_scene_list(value)
        elif key == "梗":
            meme = value

    return StickerProfile(
        base=base or raw,
        emotion=emotion,
        scenes=tuple(scenes),
        meme=meme,
    )


def infer_profile(text: str) -> StickerProfile:
    """Build an annotated profile from untrusted description text."""
    if not text:
        return StickerProfile()
    profile = parse_profile(text)
    base = profile.base or str(text).strip()
    if profile.is_structured:
        return StickerProfile(
            base=base,
            emotion=profile.emotion,
            scenes=profile.scenes,
            meme=profile.meme,
        )
    emotion, scenes = _infer_fields(base)
    return StickerProfile(base=base, emotion=emotion, scenes=scenes, meme="")


def render_profile(profile: StickerProfile) -> str:
    """Render a profile to the canonical description string.

    The plain base description always comes first so unresolved legacy readers
    still see a usable objective description.
    """
    base = _strip_structure_syntax(profile.base).strip()
    parts = [base] if base else []
    if profile.emotion:
        parts.append(f"情绪={_strip_structure_syntax(profile.emotion).strip()}")
    if profile.scenes:
        joined = ",".join(
            _strip_structure_syntax(x).strip() for x in profile.scenes if str(x).strip()
        )
        if joined:
            parts.append(f"场景={joined}")
    if profile.meme:
        parts.append(f"梗={_strip_structure_syntax(profile.meme).strip()}")
    return " | ".join(part for part in parts if part)


def fallback_sticker_description(emoji: str, set_name: str) -> str:
    """Objective, always-available description for a sticker seen in a group.

    Used when vision cannot describe the sticker at all. It intentionally
    contains no emotion/scene/meme token: an unlabelled sticker must stay
    unmatchable by semantic query rather than become a random candidate.
    """
    clean_emoji = clean_text(str(emoji or ""), max_len=32)
    clean_set = clean_text(str(set_name or ""), max_len=120)
    if clean_emoji and clean_set:
        return f"贴纸 {clean_emoji}（来自 {clean_set}）"
    if clean_emoji:
        return f"贴纸 {clean_emoji}"
    if clean_set:
        return f"来自 {clean_set} 的贴纸"
    return "群聊贴纸"


def is_serious_context(*, user_text: str, assistant_reply: str = "") -> bool:
    """True when the material is serious/technical rather than light banter."""
    for source in (user_text, assistant_reply):
        text = clean_text(str(source or ""), max_len=600)
        if not text:
            continue
        collapsed = _SPACE_RE.sub("", text)
        if any(term in collapsed for term in _SERIOUS_TERMS):
            return True
    return False


def query_has_semantic_evidence(query: str) -> bool:
    """Whether ``query`` could honestly justify a semantic sticker lookup.

    Long paragraphs, warnings, and technical text are rejected so the caller
    can never fall back to a random pick.
    """
    text = clean_text(str(query or ""), max_len=400).strip()
    if not text:
        return False
    collapsed = _SPACE_RE.sub("", text)
    if not collapsed or not _CJK_RE.search(collapsed):
        return False
    if not any(keyword in collapsed for keyword in _ALL_STICKER_KEYWORDS):
        return False
    if len(collapsed) > _MAX_STICKER_QUERY_CHARS:
        return False
    if _NON_STICKER_QUERY_RE.search(collapsed):
        return False
    if is_serious_context(user_text=collapsed):
        return False
    return True



def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(text: str) -> str:
    s = (text or "").lower().strip()
    s = _SPACE_RE.sub("", s)
    return s


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _dt_rank(dt: datetime | None) -> float:
    if not dt:
        return 0.0
    try:
        return float(dt.timestamp())
    except Exception:
        return 0.0


@dataclass(slots=True)
class StickerPick:
    file_id: str = ""
    score: int = 0
    source: str = "none"
    description: str = ""
    media_type: str = MEDIA_TYPE_STICKER
    mime_type: str = ""


class StickerLibrary:
    """Per-group sticker store backed by database with legacy JSON import."""

    def __init__(self, legacy_dir: Path | None = None) -> None:
        self.legacy_dir = legacy_dir or _LEGACY_STICKER_DIR
        self._checked_groups: set[int] = set()

    def _legacy_path(self, group_id: int) -> Path:
        return self.legacy_dir / f"{group_id}.json"

    async def _ensure_legacy_import(self, session: AsyncSession, group_id: int) -> None:
        if group_id in self._checked_groups:
            return

        exists_stmt = (
            select(StickerLibraryRecord.id)
            .where(StickerLibraryRecord.group_id == group_id)
            .limit(1)
        )
        exists = (await session.execute(exists_stmt)).scalar_one_or_none()
        if exists is not None:
            await self._trim_group_records(session, group_id)
            self._checked_groups.add(group_id)
            return

        path = self._legacy_path(group_id)
        if not path.exists():
            self._checked_groups.add(group_id)
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._checked_groups.add(group_id)
            return
        if not isinstance(data, dict):
            self._checked_groups.add(group_id)
            return

        rows = data.get("stickers")
        if not isinstance(rows, list) or not rows:
            self._checked_groups.add(group_id)
            return

        imported_file_ids: set[str] = set()
        for item in rows:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("file_id", "")).strip()
            if not file_id or file_id in imported_file_ids:
                continue
            imported_file_ids.add(file_id)

            aliases_raw = item.get("aliases", [])
            if isinstance(aliases_raw, list):
                aliases = [str(x).strip() for x in aliases_raw if str(x).strip()]
            else:
                aliases = []

            session.add(
                StickerLibraryRecord(
                    group_id=group_id,
                    file_id=file_id,
                    emoji=str(item.get("emoji", "")).strip(),
                    set_name=str(item.get("set_name", "")).strip(),
                    description=clean_text(str(item.get("description", "")), max_len=240),
                    aliases=aliases,
                    seen_count=max(0, int(item.get("seen_count", 0) or 0)),
                    sent_count=max(0, int(item.get("sent_count", 0) or 0)),
                    source=str(item.get("source", "legacy_json")).strip() or "legacy_json",
                    created_at=_parse_iso_datetime(item.get("created_at")),
                    last_seen_at=_parse_iso_datetime(item.get("last_seen_at")),
                    last_sent_at=_parse_iso_datetime(item.get("last_sent_at")),
                )
            )

        await session.flush()
        await self._trim_group_records(session, group_id)
        self._checked_groups.add(group_id)

    @staticmethod
    def _is_valid_vision_description(text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False
        return all(marker not in normalized for marker in _INVALID_VISION_MARKERS)

    @staticmethod
    def _build_auto_description(emoji: str, set_name: str, vision_description: str) -> str:
        vd = clean_text(vision_description, max_len=120)
        if StickerLibrary._is_valid_vision_description(vd):
            return vd
        return fallback_sticker_description(emoji, set_name)

    @staticmethod
    def _record_segments(record: StickerLibraryRecord) -> tuple[str, ...]:
        profile = parse_profile(str(record.description or ""))
        tokens: list[str] = []
        if profile.base:
            tokens.append(profile.base)
        if profile.emotion:
            tokens.append(profile.emotion)
        tokens.extend(profile.scenes)
        if profile.meme:
            tokens.append(profile.meme)
        for alias in record.aliases or []:
            text = str(alias or "").strip()
            if text:
                tokens.append(text)
        return tuple(dict.fromkeys(tokens))

    @staticmethod
    def _tokens_match(query: str, record: StickerLibraryRecord) -> bool:
        """True only on an explicit emotion/scene/meme/alias hit.

        Deliberately no random or popularity-based path: a miss must stay a miss
        so the bot can decide to send nothing at all.
        """
        normalized_query = _normalize(query)
        if not normalized_query:
            return False

        profile = parse_profile(str(record.description or ""))
        labeled = {value for value in (profile.emotion, profile.meme) if value}
        labeled.update(profile.scenes)

        container_tokens = [
            str(x or "").strip() for x in (record.aliases or []) if str(x or "").strip()
        ]
        container_tokens.extend(value for value in (profile.emotion, profile.meme) if value)
        container_tokens.extend(profile.scenes)

        if any(
            _normalize(token) == normalized_query or _normalize(token) in normalized_query
            for token in container_tokens
        ):
            return True

        query_groups: dict[str, list[str]] = {}
        for label, words in (*_EMOTION_GROUPS, *_SCENE_GROUPS):
            query_groups.setdefault(label, []).extend(words)
        for word in labeled:
            if word not in query_groups:
                query_groups[word] = [word]
        for words in query_groups.values():
            if any(_normalize(word) in normalized_query for word in words):
                return True

        # Short unlabeled free text still matches, but only against a segment
        # that is long enough to be meaningful for both sides.
        for segment in StickerLibrary._record_segments(record):
            normalized_segment = _normalize(segment)
            if len(normalized_segment) < _RATIO_MATCH_MIN_CHARS:
                continue
            if len(normalized_query) < _RATIO_MATCH_MIN_CHARS:
                continue
            if (
                SequenceMatcher(None, normalized_query, normalized_segment).ratio()
                >= _RATIO_MATCH_THRESHOLD
            ):
                return True
        return False

    @staticmethod
    def _score(query: str, record: StickerLibraryRecord) -> float:
        """Legacy-style similarity score kept for diagnostics and callers."""
        q = _normalize(query)
        if not q:
            return 0.0
        best = 0.0
        for segment in StickerLibrary._record_segments(record):
            normalized_segment = _normalize(segment)
            if not normalized_segment:
                continue
            ratio = SequenceMatcher(None, q, normalized_segment).ratio() * 100.0
            if q in normalized_segment:
                ratio += 20.0
            if ratio > best:
                best = ratio
        return round(best, 2)

    @staticmethod
    def _row_to_dict(row: StickerLibraryRecord) -> dict[str, Any]:
        return {
            "file_id": row.file_id,
            "emoji": row.emoji,
            "set_name": row.set_name,
            "description": row.description,
            "aliases": list(row.aliases or []),
            "seen_count": row.seen_count,
            "sent_count": row.sent_count,
            "source": row.source,
            "media_type": normalize_media_type(getattr(row, "media_type", None)),
            "mime_type": (getattr(row, "mime_type", None) or "").strip(),
            "width": max(0, int(getattr(row, "width", 0) or 0)),
            "height": max(0, int(getattr(row, "height", 0) or 0)),
            "file_unique_id": (getattr(row, "file_unique_id", None) or "").strip(),
        }

    @staticmethod
    async def _group_row_count(session: AsyncSession, group_id: int) -> int:
        stmt = select(func.count(StickerLibraryRecord.id)).where(
            StickerLibraryRecord.group_id == group_id
        )
        return int((await session.execute(stmt)).scalar_one() or 0)

    async def total_sent_count(self, session: AsyncSession, group_id: int) -> int:
        await self._ensure_legacy_import(session, group_id)
        stmt = select(func.coalesce(func.sum(StickerLibraryRecord.sent_count), 0)).where(
            StickerLibraryRecord.group_id == group_id
        )
        total = (await session.execute(stmt)).scalar_one_or_none()
        return max(0, int(total or 0))

    async def _trim_group_records(self, session: AsyncSession, group_id: int) -> None:
        stmt = (
            select(StickerLibraryRecord)
            .where(StickerLibraryRecord.group_id == group_id)
            .order_by(
                StickerLibraryRecord.last_seen_at.desc(),
                StickerLibraryRecord.last_sent_at.desc(),
                StickerLibraryRecord.sent_count.desc(),
                StickerLibraryRecord.seen_count.desc(),
                StickerLibraryRecord.id.desc(),
            )
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if len(rows) <= _MAX_STICKERS_PER_GROUP:
            return
        for stale in rows[_MAX_STICKERS_PER_GROUP:]:
            await session.delete(stale)
        await session.flush()

    async def learn_from_message(
        self,
        session: AsyncSession,
        group_id: int,
        message: Message,
        *,
        vision_description: str = "",
    ) -> dict[str, Any] | None:
        """Synchronous sticker ingest.

        Kept as the in-process equivalent of one background ingest job so the
        sticker path stays testable and callable outside the worker. The
        canonical production path is now
        :func:`bot.services.media_ingestion.ingest_media_job`, which normalizes
        identically and is shared by sticker/photo/animation.
        """
        await self._ensure_legacy_import(session, group_id)

        sticker = getattr(message, "sticker", None)
        if not sticker:
            return None
        file_id = (getattr(sticker, "file_id", None) or "").strip()
        if not file_id:
            return None

        emoji = (getattr(sticker, "emoji", None) or "").strip()
        set_name = (getattr(sticker, "set_name", None) or "").strip()
        raw_description = self._build_auto_description(emoji, set_name, vision_description)
        return await self.upsert_material(
            session,
            group_id,
            file_id=file_id,
            raw_description=raw_description,
            media_type=MEDIA_TYPE_STICKER,
            mime_type="",
            width=0,
            height=0,
            file_unique_id=(getattr(sticker, "file_unique_id", None) or "").strip(),
            emoji=emoji,
            set_name=set_name,
            source="group_message",
        )

    async def upsert_material(
        self,
        session: AsyncSession,
        group_id: int,
        *,
        file_id: str,
        raw_description: str,
        media_type: str = MEDIA_TYPE_STICKER,
        mime_type: str = "",
        width: int = 0,
        height: int = 0,
        file_unique_id: str = "",
        emoji: str = "",
        set_name: str = "",
        source: str = "group_message",
    ) -> dict[str, Any] | None:
        """Insert or refresh one library row, preserving legacy behaviour.

        Matching order is ``file_id`` first (exact, cheap, indexed) and then
        ``file_unique_id``. The second pass matters for ``photo`` messages:
        Telegram gives one visual image several sizes with different
        ``file_id`` but the *same* ``file_unique_id``, so without it a single
        photo would create a row per size.
        """
        fid = (file_id or "").strip()
        if not fid:
            return None

        normalized_type = normalize_media_type(media_type)
        unique_id = (file_unique_id or "").strip()
        profile = infer_profile(raw_description or "")
        description = clean_text(render_profile(profile), max_len=_MAX_DESCRIPTION_CHARS)
        now = _now_utc()

        stmt = select(StickerLibraryRecord).where(
            StickerLibraryRecord.group_id == group_id,
            StickerLibraryRecord.file_id == fid,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None and unique_id:
            stmt = select(StickerLibraryRecord).where(
                StickerLibraryRecord.group_id == group_id,
                StickerLibraryRecord.file_unique_id == unique_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()

        if row is None:
            row_count = await self._group_row_count(session, group_id)
            if row_count >= _MAX_STICKERS_PER_GROUP:
                await self._trim_group_records(session, group_id)
                return None
            row = StickerLibraryRecord(
                group_id=group_id,
                file_id=fid,
                emoji=emoji,
                set_name=set_name,
                description=description,
                aliases=self._structured_aliases(profile),
                seen_count=1,
                sent_count=0,
                source=source or "group_message",
                media_type=normalized_type,
                mime_type=(mime_type or "").strip(),
                width=max(0, int(width or 0)),
                height=max(0, int(height or 0)),
                file_unique_id=unique_id,
                created_at=now,
                last_seen_at=now,
            )
            try:
                # Savepoint, not transaction: a concurrent worker inserting the
                # same file_id must not destroy the caller's outer transaction.
                # ``session.add`` is INSIDE the savepoint on purpose: opening a
                # savepoint autoflushes pending state, so a pending row added
                # outside it would raise before the SAVEPOINT exists and leave
                # the session unusable instead of cleanly rolled back. Exiting
                # ``begin_nested`` on the error already rolled back to the
                # savepoint, so no outer ``session.rollback()`` is allowed here
                # - it would discard the caller's pending work.
                async with session.begin_nested():
                    session.add(row)
                    await session.flush()
            except IntegrityError:
                # Lost the race. Re-read the row the winner created and fall
                # through to the normal refresh path so no duplicate row and no
                # lost peer row can exist.
                stmt = select(StickerLibraryRecord).where(
                    StickerLibraryRecord.group_id == group_id,
                    StickerLibraryRecord.file_id == fid,
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None and unique_id:
                    stmt = select(StickerLibraryRecord).where(
                        StickerLibraryRecord.group_id == group_id,
                        StickerLibraryRecord.file_unique_id == unique_id,
                    )
                    row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
            else:
                return self._row_to_dict(row)

        # Refresh the identity/sending metadata; never downgrade a known value
        # to empty, and never flip a stored media_type on a guess.
        row.emoji = emoji or row.emoji
        row.set_name = set_name or row.set_name
        row.last_seen_at = now
        row.seen_count = max(0, int(row.seen_count or 0)) + 1
        if not (getattr(row, "file_unique_id", None) or "").strip() and unique_id:
            row.file_unique_id = unique_id
        if (mime_type or "").strip():
            row.mime_type = mime_type.strip()
        if int(width or 0) > 0:
            row.width = int(width)
        if int(height or 0) > 0:
            row.height = int(height)
        if normalized_type != MEDIA_TYPE_STICKER:
            # A real photo/animation send is strictly richer than the
            # "sticker" default; only upgrade, never downgrade.
            row.media_type = normalized_type
        old_desc = (row.description or "").strip()
        if description and description != old_desc:
            aliases = list(row.aliases or [])
            # Keep the previous description searchable, but never let a
            # structured string pollute the alias list.
            old_profile = parse_profile(old_desc)
            if old_desc and not old_profile.is_structured and old_desc not in aliases:
                aliases.append(old_desc)
            for token in self._structured_aliases(profile):
                if token not in aliases:
                    aliases.append(token)
            row.aliases = aliases[:_MAX_ALIASES]
            row.description = description

        return self._row_to_dict(row)

    @staticmethod
    def _structured_aliases(profile: StickerProfile) -> list[str]:
        """Searchable tokens derived from a profile's structured fields only."""
        tokens: list[str] = []
        if profile.emotion:
            tokens.append(profile.emotion)
        for scene in profile.scenes:
            if scene not in tokens:
                tokens.append(scene)
        if profile.meme and profile.meme not in tokens:
            tokens.append(profile.meme)
        return tokens[:_MAX_ALIASES]

    async def mark_sent(self, session: AsyncSession, group_id: int, file_id: str) -> None:
        await self._ensure_legacy_import(session, group_id)

        fid = (file_id or "").strip()
        if not fid:
            return
        now = _now_utc()

        stmt = select(StickerLibraryRecord).where(
            StickerLibraryRecord.group_id == group_id,
            StickerLibraryRecord.file_id == fid,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row_count = await self._group_row_count(session, group_id)
            if row_count >= _MAX_STICKERS_PER_GROUP:
                await self._trim_group_records(session, group_id)
                return
            row = StickerLibraryRecord(
                group_id=group_id,
                file_id=fid,
                emoji="",
                set_name="",
                description="默认贴纸",
                aliases=[],
                seen_count=0,
                sent_count=1,
                source="skill_send",
                created_at=now,
                last_seen_at=now,
                last_sent_at=now,
            )
            session.add(row)
            return
        else:
            row.sent_count = max(0, int(row.sent_count or 0)) + 1
            row.last_sent_at = now

    async def pick_sticker(
        self,
        session: AsyncSession,
        group_id: int,
        *,
        query: str = "",
        fallback_pool: list[str] | None = None,
        media_types: tuple[str, ...] | None = None,
    ) -> StickerPick:
        await self._ensure_legacy_import(session, group_id)

        stmt = select(StickerLibraryRecord).where(StickerLibraryRecord.group_id == group_id)
        rows = list((await session.execute(stmt)).scalars().all())
        allowed = {normalize_media_type(x) for x in media_types} if media_types else set(MEDIA_TYPES)
        fallback = [x.strip() for x in (fallback_pool or []) if x and x.strip()]

        q = clean_text(query, max_len=200).strip()
        if q and query_has_semantic_evidence(q) and rows:
            best_score = -1.0
            best: StickerLibraryRecord | None = None
            for row in rows:
                if normalize_media_type(getattr(row, "media_type", None)) not in allowed:
                    continue
                if not self._tokens_match(q, row):
                    continue
                score = self._score(q, row)
                if score > best_score:
                    best_score = score
                    best = row
            if best is not None and best_score >= _MATCH_SCORE_THRESHOLD:
                media = parse_media_material(best)
                return StickerPick(
                    file_id=media.file_id,
                    score=int(best_score),
                    source="library_match",
                    description=media.description,
                    media_type=media.media_type,
                    mime_type=media.mime_type,
                )
        if q:
            # A non-empty query that did not honestly match must never fall
            # through to a random library pick.
            return StickerPick(source="no_semantic_match")

        if fallback:
            return StickerPick(file_id=random.choice(fallback), score=0, source="fallback_pool", description="")
        return StickerPick(source="no_semantic_match")

    async def list_candidates(
        self,
        session: AsyncSession,
        group_id: int,
        *,
        limit: int = 20,
        fallback_pool: list[str] | None = None,
        media_types: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        await self._ensure_legacy_import(session, group_id)

        stmt = (
            select(StickerLibraryRecord)
            .where(StickerLibraryRecord.group_id == group_id)
            .order_by(
                StickerLibraryRecord.last_seen_at.desc(),
                StickerLibraryRecord.last_sent_at.desc(),
                StickerLibraryRecord.sent_count.desc(),
                StickerLibraryRecord.seen_count.desc(),
                StickerLibraryRecord.id.desc(),
            )
        )
        rows = list((await session.execute(stmt)).scalars().all())

        allowed = {normalize_media_type(x) for x in media_types} if media_types else set(MEDIA_TYPES)
        normalized_limit = max(1, min(int(limit or 20), 50))
        out: list[dict[str, Any]] = []
        used: set[str] = set()
        for row in rows:
            fid = (row.file_id or "").strip()
            if not fid or fid in used:
                continue
            media_type = normalize_media_type(getattr(row, "media_type", None))
            if media_type not in allowed:
                continue
            used.add(fid)
            out.append(
                {
                    "file_id": fid,
                    "emoji": (row.emoji or "").strip(),
                    "set_name": (row.set_name or "").strip(),
                    "description": clean_text(str(row.description or ""), max_len=180),
                    "seen_count": int(row.seen_count or 0),
                    "sent_count": int(row.sent_count or 0),
                    "source": (row.source or "").strip() or "group_message",
                    "media_type": media_type,
                    "mime_type": (getattr(row, "mime_type", None) or "").strip(),
                    "width": max(0, int(getattr(row, "width", 0) or 0)),
                    "height": max(0, int(getattr(row, "height", 0) or 0)),
                }
            )
            if len(out) >= normalized_limit:
                break

        for fid in [x.strip() for x in (fallback_pool or []) if x and x.strip()]:
            if fid in used:
                continue
            used.add(fid)
            out.append(
                {
                    "file_id": fid,
                    "emoji": "",
                    "set_name": "",
                    "description": "默认贴纸",
                    "seen_count": 0,
                    "sent_count": 0,
                    "source": "fallback_pool",
                    "media_type": MEDIA_TYPE_STICKER,
                    "mime_type": "",
                    "width": 0,
                    "height": 0,
                }
            )
            if len(out) >= normalized_limit:
                break

        return out

    async def lookup_owned_material(
        self,
        session: AsyncSession,
        group_id: int,
        file_id: str,
    ) -> MediaMaterial | None:
        """Return the library material for ``file_id`` **in this group**, else None.

        This is the ownership gate: a ``file_id`` that is not registered for the
        current group is never sendable, even if the model asks for it.
        """
        fid = (file_id or "").strip()
        if not fid:
            return None
        await self._ensure_legacy_import(session, group_id)
        stmt = select(StickerLibraryRecord).where(
            StickerLibraryRecord.group_id == group_id,
            StickerLibraryRecord.file_id == fid,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return parse_media_material(row)

    # ------------------------------------------------------------------
    # 异步入库幂等账本（第三期）
    #
    # ``media_ingest_log`` 的唯一索引建在 ``(group_id, file_unique_id)`` 上：
    # 后台 worker 重复收到同一条消息、Telegram 重投 update、或并发跑两个
    # worker，都只有第一个能拿到 True。
    # ------------------------------------------------------------------
    @staticmethod
    async def has_ingested(session: AsyncSession, group_id: int, file_unique_id: str) -> bool:
        unique_id = (file_unique_id or "").strip()
        if not unique_id:
            # 没有稳定 media 标识时不能声称"处理过"，否则会永久漏掉素材。
            return False
        stmt = (
            select(MediaIngestLog.id)
            .where(
                MediaIngestLog.group_id == group_id,
                MediaIngestLog.file_unique_id == unique_id,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    @staticmethod
    async def claim_ingest(
        session: AsyncSession,
        group_id: int,
        file_unique_id: str,
        *,
        media_type: str = "",
        telegram_message_id: int = 0,
        status: str = "ok",
        reason: str = "",
    ) -> bool:
        """Atomically claim ``(group_id, file_unique_id)``.

        Returns ``True`` only for the caller that actually created the row. The
        insert is flushed immediately so a concurrent worker's identical insert
        hits the unique index instead of silently winning later.
        """
        unique_id = (file_unique_id or "").strip()
        if not unique_id:
            return False
        now = _now_utc()
        row = MediaIngestLog(
            group_id=group_id,
            file_unique_id=unique_id,
            media_type=normalize_media_type(media_type) if media_type else "",
            telegram_message_id=max(0, int(telegram_message_id or 0)),
            status=(status or "ok").strip()[:16] or "ok",
            reason=(reason or "").strip()[:120],
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            # Another worker already claimed this image; that is a normal
            # outcome, not an error worth logging as a failure.
            await session.rollback()
            return False
        return True

    @staticmethod
    async def forget_ingest(
        session: AsyncSession,
        group_id: int,
        file_unique_id: str,
    ) -> None:
        """Drop one claim so the next attempt may retry (used on hard failure)."""
        unique_id = (file_unique_id or "").strip()
        if not unique_id:
            return
        stmt = select(MediaIngestLog).where(
            MediaIngestLog.group_id == group_id,
            MediaIngestLog.file_unique_id == unique_id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            await session.delete(row)
            await session.flush()

    @staticmethod
    async def finalize_ingest(
        session: AsyncSession,
        group_id: int,
        file_unique_id: str,
        *,
        status: str,
        reason: str = "",
    ) -> None:
        """Write the terminal status of an already-claimed ingest."""
        unique_id = (file_unique_id or "").strip()
        if not unique_id:
            return
        stmt = select(MediaIngestLog).where(
            MediaIngestLog.group_id == group_id,
            MediaIngestLog.file_unique_id == unique_id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return
        row.status = (status or "ok").strip()[:16] or "ok"
        row.reason = (reason or "").strip()[:120]
        row.updated_at = _now_utc()
        await session.flush()


sticker_library = StickerLibrary()
