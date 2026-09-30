"""群健康周报：把上一周的审核质量与活跃度发进每个授权群。

用法（容器内）：

    docker exec smart_group_bot-bot-1 python -m bot.tools.weekly_report [天数] [--dry-run]

由定时任务调用。发不出去只打印错误，不影响机器人本体。
"""

from __future__ import annotations

import asyncio
import sys

import logging

from aiogram import Bot

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.quality_report import authorized_group_ids, render_group_quality
log = logging.getLogger(__name__)

DEFAULT_DAYS = 7


async def _send_reports(days: int, *, dry_run: bool = False) -> int:
    settings = Settings()
    token = str(getattr(settings.bot, "token", "") or settings.bot_token or "").strip()
    if not token:
        print("没有配置 bot token，无法发送周报")
        return 1
    engine, session_factory = await init_db(settings.database_url)
    sent = 0
    try:
        async with session_factory() as session:
            group_ids = await authorized_group_ids(session)
            texts = {
                group_id: await render_group_quality(session, group_id=group_id, days=days)
                for group_id in group_ids
            }
        if not group_ids:
            print("没有授权群，跳过")
            return 0
        if dry_run:
            # 只渲染不发送：验证文案/HTML 正确性时用它，别拿真群当试验场
            for group_id in group_ids:
                print(f"---- dry-run | group={group_id} ----")
                print(texts[group_id].replace("审核质量 · 近", "群健康周报 · 近", 1))
            return 0
        bot = Bot(token)
        try:
            for group_id in group_ids:
                text = texts[group_id].replace(
                    "审核质量 · 近", "群健康周报 · 近", 1
                )
                try:
                    await bot.send_message(group_id, text, parse_mode="HTML")
                    sent += 1
                    print(f"已发送周报 | group={group_id}")
                except Exception as exc:  # 单个群失败不影响其它群
                    log.warning("weekly report send failed | group=%s | %s", group_id, exc)
                    print(f"发送失败 | group={group_id} | {exc}")
        finally:
            await bot.session.close()
    finally:
        await engine.dispose()
    return 0 if sent else 1


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    dry_run = any(arg in {"--dry-run", "-n"} for arg in args)
    numbers = [arg for arg in args if not arg.startswith("-")]
    days = DEFAULT_DAYS
    if numbers:
        try:
            days = max(1, min(90, int(numbers[0])))
        except ValueError:
            days = DEFAULT_DAYS
    return asyncio.run(_send_reports(days, dry_run=dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
