"""Load LLM prompt templates from prompt/ directory."""
from __future__ import annotations

import re
from pathlib import Path

from bot.utils.bot_identity import build_bot_identity_context
from bot.utils.project_info import build_bot_project_info_context

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"

_PROMPT_FILES = {
    "decision": "decision.md",
    "moderation": "moderation.md",
    "casual": "casual.md",
    "manage_intent": "manage_intent.md",
    "compress": "compress.md",
    "skill_tools": "skill_tools_v2.md",
    "sticker_decision": "sticker_decision.md",
    "reply_mode": "reply_mode.md",
    "persona": "persona.md",
    "proactive_topic": "proactive_topic.md",
    "style_distill": "style_distill.md",
    "av_synopsis": "av_synopsis.md",
}


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8").strip()


def load_prompt_defaults() -> dict[str, str]:
    return {key: _load(filename) for key, filename in _PROMPT_FILES.items()}


_RUNTIME_PROMPTS = load_prompt_defaults()


def get_prompt(name: str) -> str:
    key = str(name or "").strip().lower()
    if key not in _PROMPT_FILES:
        raise KeyError(f"unknown prompt: {name}")
    return _RUNTIME_PROMPTS.get(key) or _load(_PROMPT_FILES[key])


def set_runtime_prompts(values: dict[str, str]) -> None:
    for key in _PROMPT_FILES:
        value = str((values or {}).get(key) or "").strip()
        _RUNTIME_PROMPTS[key] = value or _load(_PROMPT_FILES[key])


# ---------------------------------------------------------------------------
# 群内「成人文字放开」指令块
#
# 权威正文只有一份：``prompt/persona.md`` 末尾的 ``[Content Boundaries]`` 小节。
# 默认人设里必须先把这一节摘掉，否则等于对所有群都放开了成人文字；只有群内开启
# ``groups.settings.av_enabled`` 时，才由 ``build_content_boundaries_context``
# 把这**同一段文本**作为独立指令块按群注入回复链路。
# ---------------------------------------------------------------------------

_CONTENT_BOUNDARIES_SECTION = "Content Boundaries"


def _persona_section_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^\[{re.escape(name)}\]\s*$.*?(?=^\[[^\]\n]+\]\s*$|\Z)",
        re.MULTILINE | re.DOTALL,
    )


def extract_persona_section(persona: str, name: str) -> str:
    """取出 ``[name]`` 小节（含标题行）；找不到返回空串。"""
    match = _persona_section_pattern(name).search(str(persona or ""))
    return match.group(0).strip() if match else ""


def strip_persona_section(persona: str, name: str) -> str:
    """删掉 ``[name]`` 小节（含标题行与紧随的空白）；找不到时原样返回。"""
    text = str(persona or "")
    pattern = _persona_section_pattern(name)
    if not pattern.search(text):
        return text
    return pattern.sub("", text).strip()


#: 文字放开指令块：直接取自 ``prompt/persona.md`` 的 ``[Content Boundaries]``。
CONTENT_BOUNDARIES_BLOCK: str = extract_persona_section(
    _load("persona.md"), _CONTENT_BOUNDARIES_SECTION
)


def build_content_boundaries_context() -> str:
    """返回成人文字放开指令块（调用方负责按群开关决定是否注入）。"""
    return CONTENT_BOUNDARIES_BLOCK


def with_persona(task_prompt: str) -> str:
    # 默认人设里永远不含 ``[Content Boundaries]``：该节只在群内开启 /av 时，
    # 由调用方以独立系统指令块按群注入（见 ``build_content_boundaries_context``）。
    persona = strip_persona_section(
        get_prompt("persona").strip(), _CONTENT_BOUNDARIES_SECTION
    )
    project_info = build_bot_project_info_context().strip()
    identity = build_bot_identity_context().strip()
    task = (task_prompt or "").strip()
    parts = [part for part in (persona, project_info, identity) if part]
    if task:
        parts.append(f"[TASK_PROMPT]\n{task}")
    return "\n\n".join(parts)


# Compatibility constants retain the repository defaults. Runtime consumers
# call ``get_prompt`` so Mini App updates take effect without a restart.
DECISION_SYSTEM: str = _RUNTIME_PROMPTS["decision"]
MODERATION_SYSTEM: str = _RUNTIME_PROMPTS["moderation"]
CASUAL_SYSTEM: str = _RUNTIME_PROMPTS["casual"]
MANAGE_INTENT_SYSTEM: str = _RUNTIME_PROMPTS["manage_intent"]
COMPRESS_SYSTEM: str = _RUNTIME_PROMPTS["compress"]
SKILL_TOOL_SYSTEM: str = _RUNTIME_PROMPTS["skill_tools"]
STICKER_DECISION_SYSTEM: str = _RUNTIME_PROMPTS["sticker_decision"]
REPLY_MODE_SYSTEM: str = _RUNTIME_PROMPTS["reply_mode"]
PERSONA_SYSTEM: str = _RUNTIME_PROMPTS["persona"]
PROACTIVE_TOPIC_SYSTEM: str = _RUNTIME_PROMPTS["proactive_topic"]
STYLE_DISTILL_SYSTEM: str = _RUNTIME_PROMPTS["style_distill"]
