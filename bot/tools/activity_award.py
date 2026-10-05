"""每周活跃激励结算入口：算上一个完整自然周的活跃榜、发积分、打印榜单。

用法（容器内）：

    docker exec smart_group_bot python -m bot.tools.activity_award
    docker exec smart_group_bot python -m bot.tools.activity_award --dry-run
    docker exec smart_group_bot python -m bot.tools.activity_award --week 2026-W40
    docker exec smart_group_bot python -m bot.tools.activity_award --group -1000000000002

不传 ``--week`` 就是"最近一个完整自然周"（上周一 ~ 上周日，Asia/Shanghai）。

参与门槛、每天计入上限与奖励向量都来自运行时配置
（``runtime_config.activity.*``，默认 3 天 / 10 条 / 每天 20 条 / 向量
``[25,12,12,4,4,4,4,4,4,4]``）。本工具走真正的 ``RuntimeConfigManager`` 初始化再读，
所以改了 Mini App 里的值之后手动补跑也会用新值。

**重复执行安全**：同一个用户、同一个周只会发一次奖（奖励流水上的幂等键 + 唯一索引），
重复跑既不会重复加分，也不会报错。周报（``bot.tools.weekly_report``）用的是同一个
函数，所以手动补跑和定时周报不会互相打架。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import date

from bot.config import Settings
from bot.db.engine import init_db
from bot.services.runtime_config import RuntimeConfigManager
from bot.services.activity import (
    render_activity_lines,
    settle_weekly_activity,
    week_key,
)
from bot.services.quality_report import authorized_group_ids

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def parse_week(value: str) -> date | None:
    """``2026-W40`` → 那一周的周一；格式不对返回 None（调用方退回"上周"）。"""

    text = str(value or "").strip().upper()
    year_text, separator, week_text = text.partition("-W")
    if not separator:
        return None
    try:
        year, week = int(year_text), int(week_text)
    except ValueError:
        return None
    if not 1 <= week <= 53:
        return None
    try:
        return date.fromisocalendar(year, week, 1)
    except ValueError:
        return None


def _plain(line: str) -> str:
    """命令行输出不需要 Telegram HTML 标签。"""

    return _TAG_RE.sub("", str(line))


async def _settle(
    *,
    week_start: date | None,
    group_ids: list[int],
    dry_run: bool,
) -> int:
    settings = Settings()
    engine, session_factory = await init_db(settings.database_url)
    # 门槛、每天计入上限与奖励向量都是运行时配置：走真正的 RuntimeConfigManager
    # 初始化再读，否则手动补跑会按 schema 默认值算，和 Mini App 里配的对不上。
    runtime_config = RuntimeConfigManager(
        session_factory=session_factory,
        settings=settings,
    )
    await runtime_config.initialize()
    failed = 0
    try:
        async with session_factory() as session:
            targets = list(group_ids) or await authorized_group_ids(session)
            if not targets:
                print("没有授权群，跳过")
                return 0
            for group_id in targets:
                try:
                    result = await settle_weekly_activity(
                        session,
                        group_id=group_id,
                        week_start=week_start,
                        award=not dry_run,
                    )
                    if dry_run:
                        await session.rollback()
                    else:
                        await session.commit()
                except Exception as exc:  # 单群失败不影响其它群
                    await session.rollback()
                    failed += 1
                    log.warning(
                        "weekly activity settle failed | group=%s | %s", group_id, exc
                    )
                    print(f"结算失败 | group={group_id} | {exc}")
                    continue
                mode = "dry-run（只算不发）" if dry_run else "已发奖"
                print(
                    f"==== group={group_id} | {week_key(week_start) if week_start else result.week}"
                    f" | {result.week_start} ~ {result.week_end} | {mode} ===="
                )
                for line in render_activity_lines(result):
                    print(_plain(line))
                print(
                    f"达标 {result.qualified} 人 / 参与 {result.participants} 人 | "
                    f"榜单共 {result.total_points} 分 | 本次入账 {result.awarded_points} 分"
                )
    finally:
        await engine.dispose()
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    dry_run = any(arg in {"--dry-run", "-n"} for arg in args)
    week_start: date | None = None
    group_ids: list[int] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"--week", "-w"} and index + 1 < len(args):
            week_start = parse_week(args[index + 1])
            if week_start is None:
                print(f"--week 格式不对：{args[index + 1]}（应该像 2026-W40）")
                return 1
            index += 2
            continue
        if arg == "--group" and index + 1 < len(args):
            try:
                group_ids.append(int(args[index + 1]))
            except ValueError:
                print(f"--group 不是整数：{args[index + 1]}")
                return 1
            index += 2
            continue
        index += 1
    return asyncio.run(
        _settle(week_start=week_start, group_ids=group_ids, dry_run=dry_run)
    )


if __name__ == "__main__":
    raise SystemExit(main())
