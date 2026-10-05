#!/usr/bin/env python3
"""Regenerate the synthetic UI fixture for tools/settings-ui-harness.

Run from the repository root with a Python 3.12 environment that has the
project dependencies installed::

    LITELLM_MODE=PRODUCTION python tools/settings-ui-harness/make_fixture.py

The produced JSON is 100% synthetic: the runtime configuration comes from the
project's own default ``RuntimeConfig`` (no .env, no database, no network) and
every group / resource row is invented here. It is committed so the harness can
run with the standard library alone.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("LITELLM_MODE", "PRODUCTION")

from bot.services.runtime_config import RuntimeConfig  # noqa: E402
from bot.web.settings_api import _public_group_settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "fixtures" / "settings.json"

# Prompts are trimmed to short synthetic text: the fixture exists to exercise
# layout, not to ship a copy of the real templates.
SYNTHETIC_PROMPTS = {
    "decision": "你是一个群聊助手。先判断这条消息是否需要回复，再生成回复。",
    "moderation": "你是群规审核器。逐条比对规则，输出判定与置信度。",
    "casual": "你是群里的日常聊天搭子，语气自然、简短。",
    "manage_intent": "判断消息里是否包含管理意图，并抽取目标动作与对象。",
    "compress": "把给定的历史对话压缩成不超过指定长度的摘要。",
    "skill_tools": "根据用户问题选择合适的技能工具，必要时并行调用。",
    "sticker_decision": "判断当前话题是否适合用贴纸回应。",
    "reply_mode": "决定本轮应该文字回复、调用工具还是保持沉默。",
    "persona": "你使用的固定人设与语气设定。",
    "proactive_topic": "结合群近期话题，给出一个自然的主动发言切入点。",
    "style_distill": "从样本中提炼该用户的表达风格特征。",
    "av_synopsis": "用中性、不剧透的语言概括条目主题。",
}


def default_group_settings() -> dict:
    return _public_group_settings(RuntimeConfig().storage_payload())


def build() -> dict:
    config = RuntimeConfig().public_payload()
    config["prompts"] = dict(SYNTHETIC_PROMPTS)
    config["models"]["providers"] = [
        {
            "name": "night-crystal",
            "provider": "openai_compatible",
            "api_key": "",
            "api_base": "https://api.example.invalid/v1",
            "stream": True,
            "chat_endpoint": "auto",
        },
        {
            "name": "icefall",
            "provider": "anthropic",
            "api_key": "",
            "api_base": "",
            "stream": False,
            "chat_endpoint": "auto",
        },
    ]
    config["models"]["main"] = {
        "provider": "night-crystal",
        "model": "fixture-large-1",
        "fallbacks": [
            {"provider": "icefall", "model": "fixture-large-2", "request_params": {}},
        ],
        "temperature": 0.7,
        "max_tokens": 2048,
        "timeout_sec": 12.0,
        "total_deadline_sec": 0.0,
        "request_params": {},
    }
    config["models"]["vision"] = {
        "provider": "icefall",
        "model": "fixture-vision-1",
        "fallbacks": [],
        "temperature": 0.2,
        "max_tokens": 1024,
        "timeout_sec": 12.0,
        "total_deadline_sec": 0.0,
        "request_params": {},
    }
    config["bot"]["proactive_task_brief"] = "结合群里的最新话题自然地开口，避免刷屏。"
    config["tts"]["app_key"] = ""
    config["tts"]["access_key"] = ""
    config["verification"]["turnstile_secret_key"] = ""
    config["verification"]["hcaptcha_secret_key"] = ""
    config["movie_info"]["tmdb_read_access_token"] = ""
    config["movie_info"]["imdb_api_key"] = ""
    config["movie_info"]["imdb_aws_access_key_id"] = ""
    config["movie_info"]["imdb_aws_secret_access_key"] = ""
    config["movie_info"]["imdb_aws_session_token"] = ""

    base = default_group_settings()
    long_welcome = (
        "欢迎来到本群！请先阅读群公告并在入群 5 分钟内完成昵称标注，"
        "否则可能会被提醒或临时禁言。这里是用于布局验证的一段较长的欢迎语，"
        "用来确认多行文本不会撑破移动端卡片。"
    )
    groups = [
        {
            "id": -1001234567890,
            "title": "夜蓝综合交流群 · 这是一个刻意很长的群名用来验证标题截断",
            "revision": "7",
            "settings": {
                **base,
                "welcome_message": long_welcome,
                "welcome_buttons": [
                    {"label": "阅读群规", "payload": "/rules"},
                    {"label": "申请发言", "payload": "/apply"},
                ],
                "join_verification_enabled": True,
                "join_verification_provider": "turnstile",
                "patrol_enabled": True,
                "raid_guard_enabled": True,
                "call_admin_enabled": True,
                "call_admin_targets": [111111111, 222222222],
                "vote_ban_enabled": True,
                "proactive_enabled": True,
                "proactive_task_brief": "关注群里的技术讨论，必要时补充一句背景。",
                "mimic_target_user_id": 987654321,
                "mimic_target_user_name": "fixture_user",
                "default_permissions": None,
            },
        },
        {
            "id": -1009876543210,
            "title": "极简测试群",
            "revision": "2",
            "settings": {**base, "mute_all_replies": True, "tts_mode": "reply"},
        },
        {
            "id": -1005550001111,
            "title": "空数据群",
            "revision": "1",
            "settings": {**base},
        },
    ]

    return {
        "session": {
            "user_id": 42,
            "can_manage_global": True,
            "display_name": "Fixture Admin",
        },
        "group_admin_session": {
            "user_id": 4242,
            "can_manage_global": False,
            "display_name": "Fixture Group Admin",
        },
        "settings": {
            "revision": 12,
            "config": config,
            "configured_secrets": [
                "verification.turnstile_secret_key",
                "providers.night-crystal.api_key",
                "movie_info.tmdb_read_access_token",
            ],
            "bootstrap": {
                "public_base_url": "https://miniapp.example.invalid",
                "listen_host": "127.0.0.1",
                "listen_port": 8788,
                "database_url": "sqlite+aiosqlite:////app/data/fixture.db",
                "master_key_configured": True,
            },
            "restart_required_paths": ["bot.parse_mode"],
        },
        "groups": {"groups": groups},
        "authorized_groups": {
            "authorized_groups": [
                {"group_id": group["id"], "title": group["title"]} for group in groups
            ],
        },
        "admins": {
            "admins": [
                {
                    "user_id": 111111111,
                    "display_name": "Fixture Admin One",
                    "username": "fixture_one",
                },
                {"user_id": 222222222, "display_name": "Fixture Admin Two", "username": ""},
            ],
        },
        "global_bans": {
            "global_bans": [
                {
                    "user_id": 700000001,
                    "reason": "fixture reason / 合成数据 / 仅用于布局验证",
                    "source": "manual",
                    "created_at": "2026-01-02T03:04:05+00:00",
                },
                {"user_id": 700000002, "reason": "", "source": "manual", "created_at": "2026-01-03T00:00:00+00:00"},
            ]
        },
        "global_exemptions": {
            "global_exemptions": [
                {"user_id": 700000003, "created_at": "2026-01-04T00:00:00+00:00"},
            ]
        },
        "resources": {
            "1001234567890": {
                "rules": [
                    {"id": 1, "rule_type": "keyword", "pattern": "加群送饮料", "action": "warn", "enabled": True, "scan_scope": "message"},
                    {"id": 2, "rule_type": "regex", "pattern": r"https?://\S+", "action": "delete", "enabled": True, "scan_scope": "message+quote"},
                    {"id": 3, "rule_type": "image", "pattern": "nsfw", "action": "ban", "enabled": False, "scan_scope": "message+vision"},
                ],
                "memories": [
                    {"id": 10, "content": "群规：每周三晚 8 点开语音房。", "created_by": 42, "created_at": "2026-01-05T10:00:00+00:00", "updated_at": "2026-01-05T10:00:00+00:00"},
                    {"id": 11, "content": "管理员 @fixture_one 负责新人接待。", "created_by": 111111111, "created_at": "2026-01-06T10:00:00+00:00", "updated_at": "2026-01-06T10:00:00+00:00"},
                ],
                "warnings": [
                    {"user_id": 800000001, "count": 3, "is_banned": False, "display_name": "Fixture Warned User", "username": "warned"},
                    {"user_id": 800000002, "count": 7, "is_banned": True, "display_name": "", "username": ""},
                ],
                "bans": [{"user_id": 800000002, "is_banned": True, "display_name": "", "username": ""}],
                "exemptions": [{"user_id": 800000003, "created_at": "2026-01-07T10:00:00+00:00", "display_name": "Fixture Exempt", "username": "exempt"}],
                "reply_mutes": [{"user_id": 800000004, "created_at": "2026-01-08T10:00:00+00:00", "display_name": "Fixture Mute", "username": "mute"}],
                "keyword_replies": [
                    {
                        "id": 20,
                        "keyword": "菜单",
                        "match_type": "exact",
                        "reply_text": "这里是我们群的功能菜单。",
                        "buttons": [{"label": "群规", "payload": "/rules"}],
                        "pin_message": False,
                        "auto_delete": False,
                        "disable_link_preview": True,
                        "enabled": True,
                    }
                ],
                "scheduled_messages": [
                    {
                        "id": 30,
                        "text": "早上好，今天是本周群公告日。",
                        "buttons": [],
                        "schedule_type": "daily",
                        "schedule_time": "09:00",
                        "interval_minutes": 60,
                        "pin_message": False,
                        "unpin_previous": False,
                        "auto_delete": False,
                        "disable_link_preview": True,
                        "enabled": True,
                    }
                ],
            },
            "9876543210": {"rules": [], "memories": [], "warnings": [], "bans": [], "exemptions": [], "reply_mutes": [], "keyword_replies": [], "scheduled_messages": []},
            "5550001111": {"rules": [], "memories": [], "warnings": [], "bans": [], "exemptions": [], "reply_mutes": [], "keyword_replies": [], "scheduled_messages": []},
        },
        "telegram_admins": {
            "admins": [
                {"user_id": 111111111, "display_name": "Fixture Admin One", "username": "fixture_one", "is_super_admin": False},
                {"user_id": 42, "display_name": "Fixture Super Admin", "username": "fixture_super", "is_super_admin": True},
            ]
        },
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = build()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
