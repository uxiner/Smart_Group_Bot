"""定时工具必须有可用的 token 路径。

回归真实事故：裸 `Settings()` **不会**填 `settings.bot.token`（只有 `bot_token` 有值），
而 `create_bot()` 只认前者 → `bot/tools/shop_expire.py` 在容器里抛
`TokenValidationError: Token is invalid!`，VPS 上每 10 分钟的 cron 全部失败，
而 `--dry-run` 因为不建 Bot 完全看不出来。

规则：定时工具要么用 `load_bootstrap_settings()`，要么显式回退到 `settings.bot_token`
——不许只依赖 `settings.bot.token`。
"""

from __future__ import annotations

import pathlib

import pytest

from bot.config import Settings, load_bootstrap_settings
from bot.loader import create_bot

REPO = pathlib.Path(__file__).resolve().parents[1]

# 每个工具：源码里必须能看到其中一种取 token 的方式
TOOL_TOKEN_PATHS = {
    "bot/tools/shop_expire.py": ("load_bootstrap_settings()",),
    "bot/tools/checkin_reminder.py": ("load_bootstrap_settings()", "settings.bot_token"),
    "bot/tools/weekly_report.py": ("load_bootstrap_settings()", "settings.bot_token"),
}

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789abc"


@pytest.fixture()
def bootstrap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("SUPER_ADMIN_ID", "100000001")
    monkeypatch.setenv("CONFIG_MASTER_KEY", "0" * 64)


def test_bare_settings_does_not_fill_bot_token(bootstrap_env: None) -> None:
    """守住这条事实：裸 Settings() 没有 bot.token —— 所以不能拿它直接建 Bot。"""

    assert Settings().bot.token == ""
    assert Settings().bot_token == FAKE_TOKEN


def test_bootstrap_settings_fills_bot_token(bootstrap_env: None) -> None:
    assert load_bootstrap_settings().bot.token == FAKE_TOKEN


def test_create_bot_works_with_bootstrap_settings(bootstrap_env: None) -> None:
    assert create_bot(load_bootstrap_settings()) is not None


@pytest.mark.parametrize("rel,markers", sorted(TOOL_TOKEN_PATHS.items()))
def test_tool_has_a_working_token_path(rel: str, markers: tuple[str, ...]) -> None:
    source = (REPO / rel).read_text(encoding="utf-8")
    assert any(marker in source for marker in markers), f"{rel} 的 token 路径不安全：{markers}"
