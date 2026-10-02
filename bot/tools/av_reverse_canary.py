"""反查可用性金丝雀：每天验一次「帧级反查还能不能认出那张已知的帧」。

**为什么必须有它**：反查没有第二家可换（同类站点要么被 Cloudflare 挡在门外、
要么是商业 API），所以上游一旦改名/改字段/换接口，我们这边只会表现为
「永远查不到」——用户不抱怨就没人知道。这个工具拿一张**答案已知**的帧去探：
命中就静默（只打一行日志），异常才私聊最高管理员。

判定口径（实测过）：那张帧正常时是 `REAL-195 98.9%`，所以
「认出来了」+「相似度 ≥ 90」两个条件同时满足才算健康——
只认出来但相似度塌到 80 出头，说明索引质量在退化，也该报警。

用法（容器内）：

    docker exec smart_group_bot-bot-1 python -m bot.tools.av_reverse_canary
    docker exec smart_group_bot-bot-1 python -m bot.tools.av_reverse_canary --dry-run
    docker exec smart_group_bot-bot-1 python -m bot.tools.av_reverse_canary --no-notify

参考图放 `data/av_reverse_canary_frame.webp`（在 gitignore 的 data/ 下，**不进仓库**）；
第一次运行会自动从上游抓一张存下来，之后就一直用它。

退出码：0 = 健康；1 = 异常（cron 里可以据此再兜一层）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import aiohttp

from bot.config import load_bootstrap_settings
from bot.loader import create_bot
from bot.services.av_image_reverse import (
    AVScanHit,
    AV_SCAN_TIMEOUT_SEC,
    resolve_av_reverse_provider,
    search_av_image,
)

log = logging.getLogger(__name__)

#: 参考帧的正确答案与最低相似度（实测健康值是 98.9%）。
EXPECTED_CODE = "REAL-195"
EXPECTED_MIN_SIMILARITY = 90.0
#: 参考图来源（上游自己的命中帧缩略图；真相是 REAL-195）。
REFERENCE_URL = "https://avscan.cc/thumb/KXVOOqm1HsmoI4oz/REAL-195/REAL-195_02-59-19.webp"
REFERENCE_REFERER = "https://avscan.cc/"
REFERENCE_PATH = Path("data/av_reverse_canary_frame.webp")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def evaluate_canary(
    hits: list[AVScanHit],
    *,
    expected_code: str = EXPECTED_CODE,
    min_similarity: float = EXPECTED_MIN_SIMILARITY,
) -> tuple[bool, str]:
    """纯判定：把探针结果翻成「健康/异常 + 人话原因」。"""

    if not hits:
        return False, "上游没返回任何候选（可能改了接口/字段，或索引不可用）"

    top = hits[0]
    if top.code.strip().upper() != expected_code.strip().upper():
        return (
            False,
            f"认错了：期望 {expected_code}，实际 {top.code}（相似度 {top.similarity:.2f}）",
        )
    if top.similarity < min_similarity:
        return (
            False,
            f"认出来了但相似度只有 {top.similarity:.2f}（低于 {min_similarity:.0f}）：索引质量可能退化",
        )
    return True, f"{top.code} {top.similarity:.2f}%（{top.frames} 帧命中）"


async def _fetch(url: str, *, timeout: float) -> bytes:
    headers = {"User-Agent": _UA, "Referer": REFERENCE_REFERER}
    client_timeout = aiohttp.ClientTimeout(total=max(5.0, timeout))
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"参考图下载失败 HTTP={resp.status}")
            return await resp.read()


async def _load_reference(*, timeout: float, refresh: bool) -> bytes:
    """取参考图：优先本地缓存文件，缺失/要求刷新时从上游抓一份存下。"""

    if REFERENCE_PATH.exists() and not refresh:
        data = REFERENCE_PATH.read_bytes()
        if data:
            return data
    data = await _fetch(REFERENCE_URL, timeout=timeout)
    try:
        REFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        REFERENCE_PATH.write_bytes(data)
    except OSError as exc:  # 只读环境/权限问题不该让探测失败
        log.warning("参考图写盘失败（不影响探测）| error=%s", exc)
    return data


async def _notify(text: str) -> bool:
    """私聊最高管理员；失败只记日志（金丝雀绝不因为通知失败而崩）。"""

    settings = load_bootstrap_settings()
    admin_id = int(getattr(settings, "super_admin_id", 0) or 0)
    if admin_id <= 0:
        log.warning("没有配置 super_admin_id，无法私聊告警")
        return False
    bot = create_bot(settings)
    try:
        await bot.send_message(admin_id, text, parse_mode="HTML")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("金丝雀告警发送失败 | error=%s", exc)
        return False
    finally:
        session = getattr(bot, "session", None)
        if session is not None:
            await session.close()


async def _probe(*, dry_run: bool, refresh: bool) -> tuple[bool, str]:
    settings = load_bootstrap_settings()
    provider = resolve_av_reverse_provider(settings)
    if provider is None:
        # F-025：没有显式配置 endpoint 就没有可以探测的第三方入口。
        return False, "未配置 av_reverse_endpoint：默认不外发用户图片，金丝雀跳过"
    try:
        timeout = float(getattr(settings, "av_reverse_timeout_sec", AV_SCAN_TIMEOUT_SEC))
    except (TypeError, ValueError):
        timeout = AV_SCAN_TIMEOUT_SEC

    try:
        image = await _load_reference(timeout=timeout, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        return False, f"参考图取不到：{type(exc).__name__}: {exc}"

    try:
        hits = await search_av_image(
            image,
            endpoint=provider.endpoint,
            timeout_seconds=timeout,
            field_name=provider.field_name,
            referer=provider.referer,
            filename="canary.webp",
            content_type="image/webp",
        )
    except Exception as exc:  # noqa: BLE001 - 探测失败也要给出人话
        return False, f"请求失败：{type(exc).__name__}: {exc}"

    return evaluate_canary(hits)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AV 反查可用性金丝雀")
    parser.add_argument("--dry-run", action="store_true", help="只探测，不发任何通知")
    parser.add_argument("--no-notify", action="store_true", help="探测但只打日志，不私聊告警")
    parser.add_argument("--refresh-reference", action="store_true", help="忽略本地参考图，重新抓一张")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    ok, detail = await _probe(dry_run=args.dry_run, refresh=args.refresh_reference)
    stamp = "OK  " if ok else "FAIL"
    print(f"[{stamp}] AV 反查金丝雀 | {detail}", flush=True)
    if ok:
        log.info("AV 反查金丝雀通过 | %s", detail)
        return 0

    log.warning("AV 反查金丝雀异常 | %s", detail)
    if args.dry_run or args.no_notify:
        print("  （按参数要求，未发通知）", flush=True)
        return 1
    sent = await _notify(
        "<b>AV 反查金丝雀异常</b>\n"
        f"{detail}\n\n"
        "影响：/av 仍可用（会自动退回「读图上文字 → 演员名候选」），只是画面截图认不出番号了。\n"
        "处理：先看上游是否需要换端点/字段（配置项 av_reverse_endpoint / av_reverse_provider），"
        "再跑一次 <code>python -m bot.tools.av_reverse_canary</code> 复核。"
    )
    print(f"  告警已{'发送' if sent else '未能发送'}", flush=True)
    return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
