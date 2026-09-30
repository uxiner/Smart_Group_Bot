"""积分商店到期清理入口：清掉到期的自定义头衔、取消到期的置顶。

用法（容器内）：

    docker exec smart_group_bot-bot-1 python -m bot.tools.shop_expire
    docker exec smart_group_bot-bot-1 python -m bot.tools.shop_expire --dry-run
    docker exec smart_group_bot-bot-1 python -m bot.tools.shop_expire --no-notify

**重复执行安全（幂等）**：这里扫的是 ``member_entitlements`` 里 ``expires_at <= now``
的行，撤下动作（把 tag 设成空字符串 / 取消置顶）本身就是幂等的，而且只有动作跑过之后
才删行——进程重启、重复执行、一次有多个到期项都能正确处理。群里有几条到期就处理几条。

``--dry-run`` 只打印"打算做什么"，既不调 Telegram 也不改数据库，用来运维自检。
机器人进程内还有一个常驻的 ``ShopExpiryService`` 跑同一套逻辑（默认 5 分钟一轮），
所以这个 CLI 主要是给"想立刻看一眼/补一次"的场景用的。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from bot.config import Settings
from bot.db.engine import init_db
from bot.loader import create_bot
from bot.services.point_shop import expire_due_entitlements

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 200


def _print_outcome(outcome: object) -> bool:
    """打印一条处理结果；返回是否失败。"""

    item = getattr(outcome, "item", None)
    ok = bool(getattr(outcome, "ok", False))
    detail = str(getattr(outcome, "detail", "") or "")
    notified = bool(getattr(outcome, "notified", False))
    action = str(getattr(outcome, "action", "") or "")
    group_id = int(getattr(item, "group_id", 0) or 0)
    user_id = int(getattr(item, "user_id", 0) or 0)
    label = str(getattr(item, "label", getattr(item, "kind", "?")))
    expires_at = getattr(item, "expires_at", None)
    when = expires_at.strftime("%Y-%m-%d %H:%M") if expires_at else "?"
    if action == "dry-run":
        print(f"[DRY ] group={group_id} user={user_id} | 到期 {when} | {detail or label}")
        return False
    mark = "OK  " if ok else "FAIL"
    tail = "" if notified else " | 提醒未发出"
    print(f"[{mark}] group={group_id} user={user_id} | 到期 {when} | {detail or label}{tail}")
    return not ok


async def _expire(*, dry_run: bool, notify: bool, limit: int) -> int:
    settings = Settings()
    engine, session_factory = await init_db(settings.database_url)
    bot = None
    try:
        if not dry_run:
            bot = create_bot(settings)
        async with session_factory() as session:
            outcomes = await expire_due_entitlements(
                session,
                bot=bot,
                dry_run=dry_run,
                notify=notify,
                limit=limit,
            )
    except Exception as exc:
        log.warning("shop expiry run failed | %s", exc, exc_info=True)
        print(f"到期清理失败：{exc}")
        return 1
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:  # 关不掉连接不该改变退出码
                log.warning("shop expiry bot session close failed", exc_info=True)
        await engine.dispose()

    if not outcomes:
        print("没有到期的权益，什么都不用做。")
        return 0

    mode = "dry-run（只打印，不动 Telegram、不改数据库）" if dry_run else "已执行"
    print(f"==== 到期权益 {len(outcomes)} 条 | {mode} ====")
    failed = 0
    for outcome in outcomes:
        if _print_outcome(outcome):
            failed += 1
    print(
        f"处理完成：{len(outcomes)} 条，其中 Telegram 侧失败 {failed} 条"
        "（失败的行也已删除，重跑不会重复处理）。"
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    dry_run = any(arg in {"--dry-run", "-n"} for arg in args)
    notify = not any(arg in {"--no-notify", "--quiet"} for arg in args)
    limit = DEFAULT_LIMIT
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--limit" and index + 1 < len(args):
            try:
                limit = max(1, int(args[index + 1]))
            except ValueError:
                print(f"--limit 不是整数：{args[index + 1]}")
                return 1
            index += 2
            continue
        index += 1
    return asyncio.run(_expire(dry_run=dry_run, notify=notify, limit=limit))


if __name__ == "__main__":
    raise SystemExit(main())
