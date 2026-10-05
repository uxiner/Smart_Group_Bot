"""修复批 P1-3 / F-026：``weekly_report`` 单个群出错不再让所有群收不到周报。

``bot/tools/weekly_report.py`` 的结算块有保护、渲染块没有::

    # bot/tools/weekly_report.py:49-65（修前）
    try:
        result = await settle_weekly_activity(session, group_id=group_id, award=not dry_run)
        ...
    except Exception:  # 单群结算失败不影响其它群，也不影响内容块
        await session.rollback()
        log.exception(...)
        boards[group_id] = []
    texts[group_id] = await render_group_quality(   # ← 仍**没有** try/except

于是任何一个群渲染失败，就带着整个 ``for`` 循环（以及所有群）一起炸掉，
**所有群都收不到周报**；而且失败信息只出现在一条未处理异常栈里。

修法：渲染块补上与结算块对齐的 try/except + rollback，该群进 ``failed_groups``
并在 dry-run 输出里如实标注；发送循环只遍历可投递的群。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.tools import weekly_report


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        bot=SimpleNamespace(token="42:TEST"),
        bot_token="",
        super_admin_id=777,
        database_url="sqlite+aiosqlite:///:memory:",
    )


def _row(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        send_message=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )


class WeeklyReportPerGroupIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self, *, broken_group: int, groups: list[int]
    ) -> tuple[int, list[int]]:
        sent_to: list[int] = []

        async def send_message(chat_id, text, **_kwargs):
            sent_to.append(int(chat_id))
            return SimpleNamespace(message_id=1)

        async def render_group_quality(_session, *, group_id, days, activity_lines):
            if group_id == broken_group:
                raise RuntimeError("boom: 这个群的报表炸了")
            return f"群 {group_id} 的周报"

        class _Session:
            """够用的假会话：B-26 的周报占位要走 ``session.execute(...)``。

            合并（P1-2 + P1-3）前这个替身只有 commit/rollback，因为当时发送路径
            不碰数据库；占位逻辑进来之后必须让它能回答「这个 (目标, 周) 还没占过」。
            """

            def __init__(self) -> None:
                self.commit = AsyncMock()
                self.rollback = AsyncMock()

            async def execute(self, *_args, **_kwargs):
                return SimpleNamespace(scalar_one_or_none=lambda: (1,))

        class _Ctx:
            async def __aenter__(self):
                return _Session()

            async def __aexit__(self, *exc):
                return None

        with (
            patch.object(weekly_report, "Settings", lambda: _settings()),
            patch.object(weekly_report, "init_db", new=AsyncMock(return_value=(AsyncMock(), lambda: _Ctx()))),
            patch.object(
                weekly_report,
                "authorized_group_ids",
                new=AsyncMock(return_value=list(groups)),
            ),
            patch.object(
                weekly_report,
                "settle_weekly_activity",
                new=AsyncMock(return_value=SimpleNamespace()),
            ),
            patch.object(weekly_report, "render_activity_lines", return_value=[]),
            patch.object(
                weekly_report, "render_group_quality", new=render_group_quality
            ),
            patch.object(
                weekly_report,
                "render_cost_digest",
                new=AsyncMock(return_value="成本摘要"),
            ),
            patch.object(
                weekly_report, "Bot", return_value=SimpleNamespace(
                    send_message=send_message,
                    session=SimpleNamespace(close=AsyncMock()),
                )
            ),
        ):
            result = await weekly_report._send_reports(7)
        return result, sent_to

    async def test_one_broken_group_does_not_starve_the_others(self) -> None:
        result, sent_to = await self._run(
            broken_group=-200, groups=[-100, -200, -300]
        )
        self.assertEqual(result, 0)
        self.assertEqual(sent_to, [-100, -300, 777])

    async def test_a_single_group_failure_is_reported_not_crashed(self) -> None:
        # 修前：这里是一条未处理异常栈，函数根本走不到发送循环。
        result, sent_to = await self._run(broken_group=-100, groups=[-100])
        self.assertEqual(result, 1)  # 一份都没发出去
        # 只有超管的成本摘要发出去了；没有群收到半截/错误的周报。
        self.assertEqual(sent_to, [777])

    async def test_no_failure_keeps_every_group(self) -> None:
        result, sent_to = await self._run(broken_group=0, groups=[-100, -200])
        self.assertEqual(result, 0)
        self.assertEqual(sent_to, [-100, -200, 777])
