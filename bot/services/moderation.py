from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass

import regex as safe_regex
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import ModerationConfig
from bot.db.models import ModerationExemption, ModerationRule, UserWarning, Violation
from bot.services import llm_metrics
from bot.services.llm import LLMService
from bot.utils.prompts import get_prompt
from bot.utils.security import build_defended_system, clean_text, wrap_untrusted

log = logging.getLogger(__name__)


# Telegram represents a command addressed to a bot as
# ``/command@bot_username``.  Moderation rules are configured against the
# human-readable command/token, so keep the raw message match intact and add
# narrowly parsed aliases for deterministic rules.  Requiring a command at
# the beginning of the text and a token boundary avoids treating ordinary
# paths such as ``/foo/bar`` as Telegram commands.
_TELEGRAM_COMMAND_TOKEN_RE = re.compile(
    r"^\s*/(?P<command>[A-Za-z0-9_]{1,32})"
    r"(?:@(?P<target>[A-Za-z0-9_]{1,32}))?(?=$|\s)",
)

# ---------------------------------------------------------------------------
# 规则扫描范围（scan_scope）
#
# 送审文本是「用户正文 + 机器人注入的引用块 + 机器人生成的图片描述」拼起来的。
# 正则/关键词规则默认（``message``）只应匹配**用户自己写的内容**：
#   * 引文块（``[reply_to_user]``/``[reply_to:text]``/``[reply_to_enriched:*]``/
#     ``[reply_quote]``/``[external_reply*]``）不属于用户自己的话；
#   * ``[image-vision]`` 那段是视觉模型生成的描述（描述购物 App 必然出现
#     「秒杀/优惠券/包邮」），拿它匹配用户的话会造成 1.0 置信度误删（生产事故）。
# 需要时用组合范围放大：
#   ``message`` / ``message+quote`` / ``message+vision`` / ``message+quote+vision``
# 语义（``llm``）规则不受影响：模型始终看到完整文本（含图片描述）。
# ---------------------------------------------------------------------------

SCAN_SCOPE_MESSAGE = "message"
SCAN_SCOPE_QUOTE = "quote"
SCAN_SCOPE_VISION = "vision"
# ModerationVerdict.match_source 的取值（"own" 与范围 token "message" 不同名，
# 调用方用 `match_source != "own"` 判断"违规是不是引用/图片描述引起的"）。
MATCH_SOURCE_OWN = "own"
MATCH_SOURCE_QUOTE = SCAN_SCOPE_QUOTE
MATCH_SOURCE_VISION = SCAN_SCOPE_VISION
MATCH_SOURCE_SEMANTIC = "semantic"
#: 引用/图片描述带来的命中，如果用户本人在**反对/警示**（骗子、别信、举报…），
#: 就不追究——与语义规则 ④(c) 的豁免一致。典型场景：有人引用一条招嫖广告提醒大家
#: "这是骗子别信"，硬正则只看到引文，会把提醒的人当成发广告的。
_OBJECTION_PATTERN = (
    r"(骗[子人]|别信|不要信|勿信|别上[当好]|假(的|货)|诈骗|举报|小心|注意(风险|安全)|"
    r"有风险|坑人|钓鱼|别加|避雷|假的吧)"
)


def _own_text_objects(candidates: tuple[str, ...]) -> bool:
    """用户自己写的正文里，有没有"提示这是骗子/广告"这类反对信号。"""

    for candidate in candidates:
        try:
            if safe_regex.search(
                _OBJECTION_PATTERN,
                candidate,
                flags=safe_regex.IGNORECASE,
                timeout=0.02,
            ) is not None:
                return True
        except (safe_regex.error, TimeoutError):
            continue
    return False
_SCAN_SCOPE_ORDER = (SCAN_SCOPE_MESSAGE, SCAN_SCOPE_QUOTE, SCAN_SCOPE_VISION)
_SCAN_SCOPE_ALIASES = {
    "message": SCAN_SCOPE_MESSAGE,
    "self": SCAN_SCOPE_MESSAGE,
    "own": SCAN_SCOPE_MESSAGE,
    "quote": SCAN_SCOPE_QUOTE,
    "quoted": SCAN_SCOPE_QUOTE,
    "reply": SCAN_SCOPE_QUOTE,
    "quotes": SCAN_SCOPE_QUOTE,
    "vision": SCAN_SCOPE_VISION,
    "image": SCAN_SCOPE_VISION,
    "images": SCAN_SCOPE_VISION,
    "ocr": SCAN_SCOPE_VISION,
}

# 机器人注入的标记。身份类标记（用户名/ID/频道标题）整行丢弃——它们不是正文，
# 里面的数字/用户名会造成误命中；其余标记只剥掉标记本身，负载算作引文正文。
_VISION_MARKER = "[image-vision]"
_IDENTITY_MARKER_PREFIXES = (
    "[reply_to_user]",
    "[reply_to_chat]",
    "[external_reply_user]",
    "[external_reply_chat]",
)
# 长前缀必须排在短前缀前面（``[external_reply:text]`` 先于 ``[external_reply]``）。
_QUOTE_MARKER_PREFIXES = (
    "[external_reply:text]",
    "[reply_to_text]",
    "[reply_to_caption]",
    "[reply_quote]",
    "[external_reply]",
)
# 类型可变的前缀（``[reply_to:text]``/``[reply_to_enriched:photo]``）：标记在
# 前缀之后的第一个 ``]`` 结束。
_QUOTE_MARKER_OPEN_PREFIXES = (
    "[reply_to_enriched:",
    "[reply_to:",
)


def _classify_moderation_marker(line: str) -> tuple[str, str] | None:
    """识别一行是不是机器人注入的标记，返回 (bucket, payload)。"""

    stripped = line.lstrip()
    if not stripped.startswith("["):
        return None
    for prefix in _IDENTITY_MARKER_PREFIXES:
        if stripped.startswith(prefix):
            return ("drop", stripped[len(prefix):])
    if stripped.startswith(_VISION_MARKER):
        return (SCAN_SCOPE_VISION, stripped[len(_VISION_MARKER):])
    for prefix in _QUOTE_MARKER_PREFIXES:
        if stripped.startswith(prefix):
            return (SCAN_SCOPE_QUOTE, stripped[len(prefix):])
    for prefix in _QUOTE_MARKER_OPEN_PREFIXES:
        if stripped.startswith(prefix):
            end = stripped.find("]", len(prefix))
            payload = stripped[end + 1:] if end >= 0 else ""
            return (SCAN_SCOPE_QUOTE, payload)
    return None


def parse_scan_scope(value: object) -> frozenset[str]:
    """把 ``scan_scope`` 解析成 {message[, quote][, vision]} 集合。

    未知/空值按默认 ``message`` 处理；允许 ``+`` 组合，顺序与大小写无关。
    """

    raw = str(value or "").strip().lower()
    tokens = {token.strip() for token in raw.split("+") if token.strip()}
    if not tokens:
        return frozenset({SCAN_SCOPE_MESSAGE})
    selected = {SCAN_SCOPE_MESSAGE}
    for token in tokens:
        resolved = _SCAN_SCOPE_ALIASES.get(token)
        if resolved is not None:
            selected.add(resolved)
    return frozenset(selected)


def normalize_scan_scope(value: object) -> str:
    """归一化成稳定的存储形式（message / message+quote / ...）。"""

    selected = parse_scan_scope(value)
    return "+".join(token for token in _SCAN_SCOPE_ORDER if token in selected)


@dataclass(frozen=True, slots=True)
class ModerationTextSegments:
    """送审文本按来源拆开：用户正文 / 引文正文 / 图片描述。"""

    own: str
    quote: str
    vision: str

    def scoped(self, *, include_quote: bool, include_vision: bool) -> str:
        """按范围拼出参与正则/关键词匹配的文本。"""

        if not include_quote and not include_vision:
            return self.own
        parts = [self.own]
        if include_quote and self.quote:
            parts.append(self.quote)
        if include_vision and self.vision:
            parts.append(self.vision)
        return "\n".join(part for part in parts if part.strip()) or self.own


def split_moderation_text(text: str) -> ModerationTextSegments:
    """把送审文本拆成 用户正文 / 引文正文 / 图片描述 三段。

    没有任何标记时按整体返回 ``own``（保证既有行为逐字节不变，``^``/``$``
    锚定与 Telegram 命令别名都不受影响）。多行负载会跟着它所属的标记走
    （``[image-vision]`` 之后的描述可以很多行，直到下一个标记为止）。
    """

    raw = str(text or "")
    if "[" not in raw:
        return ModerationTextSegments(own=raw, quote="", vision="")

    own_lines: list[str] = []
    quote_lines: list[str] = []
    vision_lines: list[str] = []
    buckets: dict[str, list[str]] = {
        SCAN_SCOPE_MESSAGE: own_lines,
        SCAN_SCOPE_QUOTE: quote_lines,
        SCAN_SCOPE_VISION: vision_lines,
    }
    bucket = SCAN_SCOPE_MESSAGE
    found_marker = False
    for line in raw.split("\n"):
        classified = _classify_moderation_marker(line)
        if classified is None:
            # "drop" 段（身份标记）的续行同样不算正文。
            if bucket != "drop":
                buckets[bucket].append(line)
            continue
        found_marker = True
        kind, payload = classified
        if kind == "drop":
            bucket = "drop"
            continue
        bucket = kind
        payload = payload.strip()
        if payload:
            buckets[kind].append(payload)

    if not found_marker:
        return ModerationTextSegments(own=raw, quote="", vision="")

    def _render(lines: list[str]) -> str:
        return "\n".join(lines).strip()

    return ModerationTextSegments(
        own=_render(own_lines),
        quote=_render(quote_lines),
        vision=_render(vision_lines),
    )


def _moderation_match_candidates(text: str) -> tuple[str, ...]:
    """Return raw text plus safe aliases for a leading Telegram command."""

    raw = str(text or "")
    candidates: list[str] = [raw]
    command_match = _TELEGRAM_COMMAND_TOKEN_RE.match(raw)
    if command_match is None:
        return (raw,)

    command = command_match.group("command") or ""
    target = command_match.group("target") or ""
    if target:
        candidates.append(f"{command}@{target}")
    if command:
        candidates.append(command)
    if target:
        candidates.append(target)

    # Keep candidate order stable while avoiding duplicate regex evaluations.
    return tuple(dict.fromkeys(candidates))


@dataclass(slots=True)
class ModerationVerdict:
    """LLM verdict for one message.

    confidence is only meaningful when violated=True. Missing or invalid
    confidence is treated as inconclusive low confidence, never upgraded into
    a direct punishment.
    """

    violated: bool
    reason: str
    rule: ModerationRule | None
    conclusive: bool
    confidence: float = 0.0
    rules_fingerprint: str = ""
    # 命中来源：``own``（用户自己的正文）/``quote``（被引用/转发的正文）/
    # ``vision``（机器人生成的图片描述）/``semantic``（语义规则，无法细分）/
    # ""（未知，含测试替身与恢复路径）。调用方据此判断「违规是不是引用内容引起的」。
    match_source: str = ""
    # True 表示这次命中来自本地确定性规则（关键词 / 正则），``confidence``
    # 只是"规则命中即视为确定"的占位 1.0，不是模型给的置信度。
    # 阈值判定照旧读 ``confidence``（行为不变），但落库必须写 NULL：
    # 报表要算"模型有多确定"，把规则命中的 1.0 记进去会直接拉高置信度分布。
    deterministic: bool = False


def _parse_confidence(value: object) -> tuple[float, bool]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0, False
    try:
        confidence = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0, False
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return 0.0, False
    return confidence, True


def _strip_markdown_fence(text: str) -> str:
    payload = (text or "").strip()
    if payload.startswith("```"):
        payload = re.sub(r"^```(?:json)?", "", payload, flags=re.IGNORECASE).strip()
        payload = re.sub(r"```$", "", payload).strip()
    return payload


def _extract_balanced_object(text: str) -> str | None:
    """Extract first balanced JSON object from text, ignoring braces in strings."""
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _decode_json_fragment(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value


def _extract_field_by_regex(payload: str, key: str) -> str:
    # Support escaped quote content inside JSON strings.
    pattern = rf'"{re.escape(key)}"\s*:\s*"((?:[^"\\]|\\.)*)"'
    m = re.search(pattern, payload, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return _decode_json_fragment(m.group(1)).strip()


def _salvage_moderation_fields(payload: str) -> dict | None:
    data: dict = {}

    bool_match = re.search(
        r'"(?:violated|violation)"\s*:\s*(true|false)\b(?!\s*/)',
        payload,
        flags=re.IGNORECASE,
    )
    if bool_match:
        data["violated"] = bool_match.group(1).lower() == "true"

    rid_match = re.search(
        r'"rule_id"\s*:\s*(null|-?\d+)',
        payload,
        flags=re.IGNORECASE,
    )
    if rid_match:
        rid_raw = rid_match.group(1).lower()
        data["rule_id"] = None if rid_raw == "null" else int(rid_raw)

    reason = _extract_field_by_regex(payload, "reason")
    if reason:
        data["reason"] = reason

    rule = _extract_field_by_regex(payload, "rule")
    if rule:
        data["rule"] = rule

    conf_match = re.search(
        r'"confidence"\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?=[,}]|$)',
        payload,
        flags=re.IGNORECASE,
    )
    if conf_match:
        data["confidence"] = float(conf_match.group(1))

    # Moderation decision must at least contain violated boolean.
    if "violated" not in data:
        return None
    return data


def _parse_moderation_json(raw: str) -> dict | None:
    payload = _strip_markdown_fence(raw)
    candidate = _extract_balanced_object(payload)
    if candidate:
        payload = candidate
    elif "{" in payload:
        # LLM can return truncated object; keep from first "{" for regex salvage.
        payload = payload[payload.find("{") :]

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return _salvage_moderation_fields(payload)

    if not isinstance(data, dict):
        return None
    return data


class ModerationService:
    def __init__(self, config: ModerationConfig, llm: LLMService) -> None:
        self.config = config
        self.llm = llm

    async def is_user_exempt(self, session: AsyncSession, group_id: int, user_id: int) -> bool:
        stmt = select(ModerationExemption.id).where(
            ModerationExemption.group_id == group_id,
            ModerationExemption.user_id == user_id,
        )
        with session.no_autoflush:
            result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def check_rules(
        self, session: AsyncSession, group_id: int, text: str, *, context: str = ""
    ) -> tuple[bool, str, ModerationRule | None]:
        """使用 LLM 基于群规则判定，返回(是否违规, 原因, 命中规则)。"""
        verdict = await self.evaluate(session, group_id, text, context=context)
        return verdict.violated, verdict.reason, verdict.rule

    async def check_rules_verbose(
        self, session: AsyncSession, group_id: int, text: str, *, context: str = ""
    ) -> tuple[bool, str, ModerationRule | None, bool]:
        """同 check_rules，但额外返回判定是否可信（conclusive）。"""
        verdict = await self.evaluate(session, group_id, text, context=context)
        return verdict.violated, verdict.reason, verdict.rule, verdict.conclusive

    async def evaluate(
        self,
        session: AsyncSession,
        group_id: int,
        text: str,
        *,
        context: str = "",
    ) -> ModerationVerdict:
        """Evaluate deterministic rules locally and semantic rules with the LLM.

        审核模型输出不可解析时按不违规处理，但 conclusive=False，
        调用方不应据此写入"已审查通过"类缓存。

        ``context`` 是这条消息之前的群内对话（见 ``bot.services.moderation_context``）。
        本地正则只匹配消息本身，上下文只交给语义规则——否则上下文里的广告词会算到别人头上。
        """
        stmt = select(ModerationRule).where(
            ModerationRule.group_id == group_id,
            ModerationRule.enabled == True,
        ).order_by(ModerationRule.id)
        with session.no_autoflush:
            result = await session.execute(stmt)
        loaded_rules = list(result.scalars().all())
        # Keep a detached immutable-in-practice snapshot.  Ending the read
        # transaction may expire ORM rows on session factories configured with
        # expire_on_commit=True; accessing those rows during/after a slow LLM
        # call would otherwise silently check a connection back out or raise
        # MissingGreenlet.
        rules = [
            ModerationRule(
                id=int(rule.id),
                group_id=int(rule.group_id),
                rule_type=str(rule.rule_type or ""),
                pattern=str(rule.pattern or ""),
                action=str(rule.action or "warn"),
                enabled=bool(rule.enabled),
                scan_scope=(
                    str(rule.scan_scope)
                    if getattr(rule, "scan_scope", None) is not None
                    else ""
                ),
            )
            for rule in loaded_rules
        ]
        rules_raw = "\x1e".join(
            "\x1f".join(
                str(value)
                for value in (rule.id, rule.rule_type, rule.pattern, rule.action)
            )
            for rule in rules
        )
        rules_fingerprint = hashlib.sha256(
            rules_raw.encode("utf-8")
        ).hexdigest()[:16]

        def make_verdict(**kwargs: object) -> ModerationVerdict:
            return ModerationVerdict(
                **kwargs,
                rules_fingerprint=rules_fingerprint,
            )

        # A SELECT starts a SQLite transaction and keeps a pooled connection
        # checked out.  End that read-only transaction before regex/LLM work so
        # a slow provider cannot starve the database pool.
        commit = getattr(session, "commit", None)
        if callable(commit):
            await commit()

        if not rules:
            log.info("审核通过 (无启用规则)")
            return make_verdict(violated=False, reason="", rule=None, conclusive=True)

        deterministic_inconclusive = False
        llm_rules: list[ModerationRule] = []
        normalized_text = text or ""
        segments = split_moderation_text(normalized_text)
        # 用户自己正文的匹配视图：用来判断一次命中到底是"用户自己写的"，
        # 还是"引用/图片描述带来的"（见 ModerationVerdict.match_source）。
        own_candidates = _moderation_match_candidates(segments.own)
        own_folded = tuple(candidate.casefold() for candidate in own_candidates)
        scoped_cache: dict[tuple[bool, bool], tuple[tuple[str, ...], tuple[str, ...]]] = {}

        def scoped_candidates(
            include_quote: bool, include_vision: bool
        ) -> tuple[tuple[str, ...], tuple[str, ...]]:
            key = (include_quote, include_vision)
            cached = scoped_cache.get(key)
            if cached is None:
                if not include_quote and not include_vision:
                    source = segments.own
                else:
                    source = segments.scoped(
                        include_quote=include_quote,
                        include_vision=include_vision,
                    )
                candidates = _moderation_match_candidates(source)
                cached = (
                    candidates,
                    tuple(candidate.casefold() for candidate in candidates),
                )
                scoped_cache[key] = cached
            return cached

        def bucket_candidates(bucket: str) -> tuple[str, ...]:
            if bucket == SCAN_SCOPE_QUOTE:
                return _moderation_match_candidates(segments.quote)
            if bucket == SCAN_SCOPE_VISION:
                return _moderation_match_candidates(segments.vision)
            return own_candidates

        # 用户本人是不是在警示骗子/反对广告（引用/图片描述命中时用来豁免）。
        own_objects = _own_text_objects(own_candidates)

        regex_deadline = time.perf_counter() + 0.1

        def regex_hits(pattern: str, candidates: tuple[str, ...]) -> bool:
            for candidate in candidates:
                remaining = regex_deadline - time.perf_counter()
                if remaining <= 0:
                    return False
                try:
                    if safe_regex.search(
                        pattern,
                        candidate,
                        flags=safe_regex.IGNORECASE,
                        timeout=min(0.02, remaining),
                    ) is not None:
                        return True
                except (safe_regex.error, TimeoutError):
                    return False
            return False

        def deterministic_match_source(
            pattern: str,
            *,
            include_quote: bool,
            include_vision: bool,
            keyword_folded: str = "",
        ) -> str:
            """命中已经发生；定位它来自哪一段（own / quote / vision）。"""

            if not include_quote and not include_vision:
                return MATCH_SOURCE_OWN
            if keyword_folded:
                own_hit = any(keyword_folded in candidate for candidate in own_folded)
            else:
                own_hit = regex_hits(pattern, own_candidates)
            if own_hit:
                return MATCH_SOURCE_OWN
            for bucket, enabled in (
                (SCAN_SCOPE_QUOTE, include_quote),
                (SCAN_SCOPE_VISION, include_vision),
            ):
                if not enabled:
                    continue
                if keyword_folded:
                    hit = any(
                        keyword_folded in candidate.casefold()
                        for candidate in bucket_candidates(bucket)
                    )
                else:
                    hit = regex_hits(pattern, bucket_candidates(bucket))
                if hit:
                    return bucket
            # 组合文本命中但单段都不命中（例如 ``^``/``$`` 跨段锚定）：按范围
            # 里最先包含的额外段归类，至少不会把引用命中误标成 own。
            return SCAN_SCOPE_QUOTE if include_quote else SCAN_SCOPE_VISION

        for rule in rules:
            rule_type = (rule.rule_type or "keyword").strip().lower()
            pattern = (rule.pattern or "").strip()
            scope = parse_scan_scope(rule.scan_scope)
            include_quote = SCAN_SCOPE_QUOTE in scope
            include_vision = SCAN_SCOPE_VISION in scope
            if rule_type == "keyword":
                if not pattern:
                    deterministic_inconclusive = True
                    log.warning(
                        "empty keyword moderation rule ignored: group=%s rule=%s",
                        group_id,
                        rule.id,
                    )
                    continue
                folded_pattern = pattern.casefold()
                _candidates, folded_candidates = scoped_candidates(
                    include_quote, include_vision
                )
                if any(
                    folded_pattern in candidate for candidate in folded_candidates
                ):
                    source = deterministic_match_source(
                        pattern,
                        include_quote=include_quote,
                        include_vision=include_vision,
                        keyword_folded=folded_pattern,
                    )
                    if source != MATCH_SOURCE_OWN and own_objects:
                        log.info(
                            "审核豁免 (命中来自 %s，但本人在警示): group=%s rule_id=%s",
                            source,
                            group_id,
                            rule.id,
                        )
                    else:
                        log.info(
                            "审核命中本地关键词: group=%s rule_id=%s source=%s",
                            group_id,
                            rule.id,
                            source,
                        )
                        return make_verdict(
                            violated=True,
                            reason="命中关键词规则",
                        rule=rule,
                        conclusive=True,
                        confidence=1.0,
                        match_source=source,
                        deterministic=True,
                    )
                continue
            if rule_type == "regex":
                if not pattern:
                    deterministic_inconclusive = True
                    log.warning(
                        "empty regex moderation rule ignored: group=%s rule=%s",
                        group_id,
                        rule.id,
                    )
                    continue
                match_candidates, _folded_candidates = scoped_candidates(
                    include_quote, include_vision
                )
                for candidate in match_candidates:
                    remaining_regex_budget = regex_deadline - time.perf_counter()
                    if remaining_regex_budget <= 0:
                        deterministic_inconclusive = True
                        log.warning(
                            "regex moderation total budget exhausted: group=%s rule=%s",
                            group_id,
                            rule.id,
                        )
                        break
                    try:
                        matched = safe_regex.search(
                            pattern,
                            candidate,
                            flags=safe_regex.IGNORECASE,
                            timeout=min(0.02, remaining_regex_budget),
                        )
                    except (safe_regex.error, TimeoutError) as exc:
                        deterministic_inconclusive = True
                        log.warning(
                            "regex moderation rule invalid or timed out: group=%s rule=%s error=%s",
                            group_id,
                            rule.id,
                            exc,
                        )
                        break
                    if matched is not None:
                        source = deterministic_match_source(
                            pattern,
                            include_quote=include_quote,
                            include_vision=include_vision,
                        )
                        if source != MATCH_SOURCE_OWN and own_objects:
                            log.info(
                                "审核豁免 (命中来自 %s，但本人在警示): group=%s rule_id=%s",
                                source,
                                group_id,
                                rule.id,
                            )
                            # 跳出候选循环，继续下一条规则（这条不追究）。
                            break
                        log.info(
                            "审核命中本地正则: group=%s rule_id=%s source=%s",
                            group_id,
                            rule.id,
                            source,
                        )
                        return make_verdict(
                            violated=True,
                            reason="命中正则规则",
                            rule=rule,
                            conclusive=True,
                            confidence=1.0,
                            match_source=source,
                            deterministic=True,
                        )
                continue
            # Unknown legacy rule types are treated as semantic rules rather
            # than silently ignored.
            llm_rules.append(rule)

        if not llm_rules:
            log.info("审核通过 (检查了 %d 条本地规则)", len(rules))
            return make_verdict(
                violated=False,
                reason="",
                rule=None,
                conclusive=not deterministic_inconclusive,
            )

        rules_payload = [
            {
                "id": r.id,
                "rule_type": r.rule_type,
                "rule": r.pattern,
                "action": r.action,
            }
            for r in llm_rules
        ]
        rules_json = json.dumps(rules_payload, ensure_ascii=False, indent=2)

        system_prompt = build_defended_system(
            get_prompt("moderation").format(rules_json=rules_json)
        )
        clean_context = clean_text(context or "", max_len=1200)
        if clean_context:
            # 上下文同样是群成员写的，按不可信内容包裹，防止它夹带指令
            user_input = (
                f"{wrap_untrusted('群内上下文', clean_context, max_len=1200)}\n\n"
                f"{wrap_untrusted('待审核消息', clean_text(text, max_len=1200), max_len=1200)}"
            )
        else:
            user_input = wrap_untrusted(
                "待审核消息", clean_text(text, max_len=1200), max_len=1200
            )
        try:
            llm_raw = await self.llm.moderation(system_prompt, user_input)
        except Exception:
            log.exception("审核模型调用失败；本地规则已完成检查")
            return make_verdict(
                violated=False,
                reason="",
                rule=None,
                conclusive=False,
            )
        data = _parse_moderation_json(llm_raw)

        if not data:
            raw_text = llm_raw or ""
            escaped = raw_text.replace("\r", "\\r").replace("\n", "\\n")
            preview_limit = 500
            preview_truncated = len(escaped) > preview_limit
            preview = escaped[:preview_limit]
            # 统计口径：模型"有输出但不是合法 JSON"。这和"空响应"是两回事，
            # 成本看板里分开计，才能看出是模型被截断还是根本没出声。
            llm_metrics.record("moderation", parse_errors=1)
            log.warning(
                "审核模型输出不可解析，按不违规处理: response_len=%d preview_truncated=%s preview=%s",
                len(raw_text),
                preview_truncated,
                preview,
            )
            return make_verdict(violated=False, reason="", rule=None, conclusive=False)

        violated_value = data.get("violated", data.get("violation"))
        if not isinstance(violated_value, bool):
            log.warning("审核模型 violated 字段无效，按不违规处理")
            return make_verdict(
                violated=False,
                reason="",
                rule=None,
                conclusive=False,
            )
        violated = violated_value
        reason = clean_text(str(data.get("reason", "")).strip(), max_len=120)
        confidence, confidence_valid = _parse_confidence(data.get("confidence"))

        hit_rule: ModerationRule | None = None
        rid = data.get("rule_id")
        if rid is not None:
            try:
                rid_int = int(rid)
                hit_rule = next((r for r in llm_rules if r.id == rid_int), None)
            except (TypeError, ValueError):
                hit_rule = None

        if violated and not hit_rule:
            rule_text = str(data.get("rule", "")).strip()
            if rule_text:
                hit_rule = next(
                    (r for r in llm_rules if (r.pattern or "").strip() == rule_text),
                    None,
                )

        if violated:
            if not reason:
                reason = "命中群规（AI判定）"
            log.info(
                "审核命中: group=%s rule_id=%s confidence=%.2f reason=%s",
                group_id,
                hit_rule.id if hit_rule else None,
                confidence,
                reason,
            )
            return make_verdict(
                violated=True,
                reason=reason,
                rule=hit_rule,
                conclusive=confidence_valid,
                confidence=confidence,
                match_source=MATCH_SOURCE_SEMANTIC,
            )

        log.info("审核通过 (检查了 %d 条语义规则, AI判定)", len(llm_rules))
        return make_verdict(
            violated=False,
            reason="",
            rule=None,
            conclusive=not deterministic_inconclusive,
        )

    def is_high_confidence(self, verdict: ModerationVerdict) -> bool:
        return bool(
            verdict.conclusive
            and verdict.confidence >= self.config.high_confidence_threshold
        )

    async def count_rule_hits(
        self,
        session: AsyncSession,
        group_id: int,
        user_id: int,
        rule_id: int | None,
    ) -> int:
        """该用户在本群本规则下已经记录的命中次数（不含本次，只读）。

        ``violations`` 里每一行都是一次真实命中，所以行数就是累计命中次数。
        ``rule_id`` 为 NULL（未标注规则/NSFW 守卫）时按 IS NULL 归组，
        避免把它们混进任何具体规则。
        """

        conditions = [
            Violation.group_id == int(group_id),
            Violation.user_id == int(user_id),
            Violation.rule_id.is_(None) if rule_id is None else Violation.rule_id == int(rule_id),
        ]
        total = (
            await session.execute(
                select(func.count()).select_from(Violation).where(*conditions)
            )
        ).scalar()
        return int(total or 0)

    async def record_violation(
        self,
        session: AsyncSession,
        group_id: int,
        user_id: int,
        text: str,
        action: str,
        rule: ModerationRule | None = None,
        *,
        source_message_id: int | None = None,
        confidence: float | None = None,
        verdict_reason: str = "",
    ) -> Violation:
        rule_id = int(rule.id) if rule is not None and rule.id is not None else None
        # 纯观测列：该用户在本群本规则下的第几次命中（含本次）。与 warn_threshold
        # 的 UserWarning 计数器无关，不参与任何质询/警告/封禁判定。
        # 计数前先 flush：这个 session 关掉了 autoflush，同一事务里"上一条命中"
        # 还在 identity map 里没落库，不 flush 就会漏数（次数依赖调用方何时 commit
        # 是隐式约定，不该被观测列继承）。
        # 注意：ban 计数路径（_apply_counted_moderation_ban）随后会把它覆盖成
        # add_warning 的返回值——那条链路的幂等重放依赖该值，不能动。
        await session.flush()
        hit_count = await self.count_rule_hits(session, group_id, user_id, rule_id) + 1
        values = {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "rule_id": rule_id,
            "message_text": str(text or "")[:500],
            "action_taken": str(action or "warn")[:32],
            "confidence": None if confidence is None else round(float(confidence), 3),
            "warning_count": hit_count,
            "verdict_reason": str(verdict_reason or "")[:120],
        }
        normalized_source = int(source_message_id or 0)
        if normalized_source <= 0:
            v = Violation(**values)
            session.add(v)
            setattr(v, "_source_event_created", True)
            return v

        values["source_message_id"] = normalized_source
        inserted_id = (
            await session.execute(
                sqlite_insert(Violation)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=[
                        Violation.group_id,
                        Violation.source_message_id,
                    ]
                )
                .returning(Violation.id)
            )
        ).scalar_one_or_none()
        created = inserted_id is not None
        v = await session.scalar(
            select(Violation).where(
                Violation.group_id == int(group_id),
                Violation.source_message_id == normalized_source,
            )
        )
        if v is None:
            raise RuntimeError("failed to persist or reload idempotent violation event")
        setattr(v, "_source_event_created", created)
        return v

    async def add_warning(
        self, session: AsyncSession, group_id: int, user_id: int
    ) -> tuple[int, bool]:
        """增加警告次数，返回(当前次数, 是否应封禁)。"""
        threshold = max(1, int(self.config.warn_threshold))

        async def increment_existing() -> tuple[int, bool] | None:
            next_count = UserWarning.count + 1
            result = await session.execute(
                update(UserWarning)
                .where(
                    UserWarning.group_id == group_id,
                    UserWarning.user_id == user_id,
                )
                .values(
                    count=next_count,
                    is_banned=case(
                        (next_count >= threshold, True),
                        else_=UserWarning.is_banned,
                    ),
                )
                .returning(UserWarning.count, UserWarning.is_banned)
            )
            row = result.first()
            if row is None:
                return None
            return int(row[0]), bool(row[1])

        incremented = await increment_existing()
        if incremented is not None:
            return incremented

        should_ban = threshold <= 1
        try:
            async with session.begin_nested():
                warning = UserWarning(
                    group_id=group_id,
                    user_id=user_id,
                    count=1,
                    is_banned=should_ban,
                )
                session.add(warning)
                await session.flush()
            return 1, should_ban
        except IntegrityError:
            incremented = await increment_existing()
            if incremented is None:
                raise
            return incremented
