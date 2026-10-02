"""用图反查番号：调用第三方的**帧级**索引，把「画面截图」直接换成番号。

为什么需要它（2026-10-02 实测，口径同时记在技能
``smart-bot-operations / references/vision-image-input.md``）：

* 视觉模型只能读**图上印着的**编号；印不出来时它会**编**一个像模像样的错编号
  （同一张 SONE-666 封面三次分别读成 ``SSIS-698`` / ``SSIS-498`` / ``SENZAI-012``）；
* 该站点索引的是**视频帧**（实测索引规模 30.5 万部 / 7.08 亿帧，每日新增 1.5 万），
  把它的命中帧原样回喂得到 98.91%，裁掉 30% 仍有 90.25%，模糊 1.5 是 98.55%；
  而番号**封面**丢进去只有 69% —— 所以它补的是「截图 / 画面」这一类，
  封面依旧要走「读文字 + 演员名」的老路。

与用户确认过的四条口径：

1. 阈值 85：实测真命中 ≥90%、假候选 ≤77%，中间是安全的分界；
2. 限流：并发 ≤2、每用户冷却 30 秒、命中 429 后全局退避 30 秒（实测连发 7 次就被 429）；
3. 降级：任何异常（超时 / 非 200 / JSON 形状不对 / 低于阈值 / 被限流）都**只记日志**，
   返回空串，由调用方继续走原有的「提示词读编号 → 演员名兜底」，本模块绝不抛异常，
   也绝不改变群内「先删图、再处理」的顺序；
4. 隐私：图片会离开本机送到该站点 —— 用户已明确同意。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

#: 该站点的检索入口（同源相对路径 ``/search``，表单字段名固定为 ``file``）。
AV_SCAN_ENDPOINT = "https://avscan.cc/search"
AV_SCAN_REFERER = "https://avscan.cc/"
#: 相似度阈值：实测真命中 ≥90%、假候选 ≤77%。
AV_SCAN_MIN_SIMILARITY = 85.0
#: 单次请求超时（比识图的硬超时短：它只是识图之前的一次尝试）。
AV_SCAN_TIMEOUT_SEC = 12.0
#: 同一用户两次反查之间的最小间隔。
AV_SCAN_COOLDOWN_SEC = 30.0
#: 撞上 429 之后全体退避多久。
AV_SCAN_BACKOFF_SEC = 30.0
#: 同时在飞的请求数上限。
AV_SCAN_MAX_CONCURRENCY = 2

_AV_SCAN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class AVScanRateLimited(Exception):
    """被上游限流（HTTP 429）—— 由调用方决定退避多久。"""


@dataclass(frozen=True, slots=True)
class AVScanHit:
    """一条候选：番号 + 相似度 + 命中的帧数。"""

    code: str
    similarity: float
    frames: int = 0


def parse_av_scan_payload(payload: object, *, limit: int = 5) -> list[AVScanHit]:
    """把 ``/search`` 的 JSON 收成候选列表（相似度降序）；形状不对就返回空列表。

    这两种「形状不对」都当空结果处理，绝不抛：上游是第三方，随时可能改字段。
    """

    if not isinstance(payload, dict):
        return []
    rows = payload.get("results")
    if not isinstance(rows, list):
        return []

    hits: list[AVScanHit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("video_code") or "").strip().upper()
        if not code:
            continue
        try:
            similarity = float(row.get("best_similarity") or 0.0)
        except (TypeError, ValueError):
            similarity = 0.0
        frames = row.get("frames")
        hits.append(
            AVScanHit(
                code=code,
                similarity=similarity,
                frames=len(frames) if isinstance(frames, list) else 0,
            )
        )

    hits.sort(key=lambda hit: hit.similarity, reverse=True)
    return hits[: max(1, int(limit))]


def pick_av_scan_code(
    hits: list[AVScanHit], *, min_similarity: float = AV_SCAN_MIN_SIMILARITY
) -> tuple[str, float] | None:
    """取第一个达到阈值的候选（列表已按相似度降序）；没有就返回 ``None``。"""

    for hit in hits:
        if hit.code and hit.similarity >= min_similarity:
            return hit.code, hit.similarity
    return None


def decode_image_data_uri(data_uri: str) -> bytes:
    """把 ``data:image/...;base64,xxx`` 还原成图片字节；不是这种形状就返回空。"""

    raw = str(data_uri or "").strip()
    if not raw.startswith("data:") or "," not in raw:
        return b""
    header, _, encoded = raw.partition(",")
    if "base64" not in header.lower():
        return b""
    try:
        return base64.b64decode(encoded, validate=False)
    except Exception:  # noqa: BLE001 - 第三方/上游给什么都要能扛
        return b""


class AVScanGuard:
    """进程内的限流闸门：并发上限 + 每用户冷却 + 429 后的全局退避。

    这里**不排队**：被冷却或被退避挡下的请求直接放弃、走原有的识图链路，
    因为「识图」本身仍然能把番号读出来，等 30 秒反而让用户白等。
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float = AV_SCAN_COOLDOWN_SEC,
        backoff_seconds: float = AV_SCAN_BACKOFF_SEC,
        max_concurrency: int = AV_SCAN_MAX_CONCURRENCY,
        clock=time.monotonic,
    ) -> None:
        self.cooldown_seconds = float(cooldown_seconds)
        self.backoff_seconds = float(backoff_seconds)
        self.semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._clock = clock
        self._last_request_at: dict[int, float] = {}
        self._blocked_until = 0.0

    def blocked_seconds_left(self) -> float:
        """全局退避还剩几秒（0 = 没在退避）。"""

        return max(0.0, self._blocked_until - self._clock())

    def allow(self, user_id: int) -> bool:
        """能不能现在就发这一次请求；会记录本次请求时刻。"""

        now = self._clock()
        if self.blocked_seconds_left() > 0:
            return False
        last = self._last_request_at.get(int(user_id))
        if last is not None and now - last < self.cooldown_seconds:
            return False
        self._last_request_at[int(user_id)] = now
        return True

    def note_rate_limited(self) -> None:
        """收到 429：全体退避一段时间再试。"""

        self._blocked_until = self._clock() + self.backoff_seconds


_AV_SCAN_GUARD = AVScanGuard()


def av_scan_guard() -> AVScanGuard:
    """取进程内的闸门（测试可用 :func:`reset_av_scan_guard` 换新的）。"""

    return _AV_SCAN_GUARD


def reset_av_scan_guard() -> None:
    """测试用：把闸门换成干净的实例。"""

    global _AV_SCAN_GUARD
    _AV_SCAN_GUARD = AVScanGuard()


async def search_av_image(
    image_bytes: bytes,
    *,
    endpoint: str = AV_SCAN_ENDPOINT,
    timeout_seconds: float = AV_SCAN_TIMEOUT_SEC,
    filename: str = "probe.jpg",
    content_type: str = "image/jpeg",
) -> list[AVScanHit]:
    """把图片交给该站点检索，返回候选列表。

    * 429 → 抛 :class:`AVScanRateLimited`（让调用方去设置退避）；
    * 其它非 200 / JSON 解析失败 / 网络异常 → 记日志并返回空列表。
    """

    if not image_bytes:
        return []

    form = aiohttp.FormData()
    form.add_field("file", image_bytes, filename=filename, content_type=content_type)
    headers = {"User-Agent": _AV_SCAN_UA, "Referer": AV_SCAN_REFERER, "Origin": AV_SCAN_REFERER.rstrip("/")}
    timeout = aiohttp.ClientTimeout(total=max(3.0, float(timeout_seconds)))

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(endpoint, data=form, headers=headers) as resp:
            if resp.status == 429:
                raise AVScanRateLimited()
            if resp.status != 200:
                log.warning("【AV 反查】非 200 | status=%s", resp.status)
                return []
            raw = await resp.read()

    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        log.warning("【AV 反查】响应不是 JSON | error=%s", exc)
        return []
    return parse_av_scan_payload(payload)


async def try_reverse_image_lookup(
    data_uri: str,
    settings: object,
    *,
    user_id: int,
) -> str:
    """识图之前的一次尝试：命中返回番号，其余一切情况返回空串。"""

    if not bool(getattr(settings, "av_reverse_enabled", True)):
        return ""

    image_bytes = decode_image_data_uri(data_uri)
    if not image_bytes:
        log.info("【AV 反查】跳过 | user=%s | 原因=图片数据为空", user_id)
        return ""

    guard = av_scan_guard()
    if not guard.allow(user_id):
        log.info(
            "【AV 反查】跳过 | user=%s | 原因=%s",
            user_id,
            "全局退避中" if guard.blocked_seconds_left() > 0 else "用户冷却中",
        )
        return ""

    endpoint = str(getattr(settings, "av_reverse_endpoint", AV_SCAN_ENDPOINT) or AV_SCAN_ENDPOINT)
    try:
        timeout_seconds = float(getattr(settings, "av_reverse_timeout_sec", AV_SCAN_TIMEOUT_SEC))
    except (TypeError, ValueError):
        timeout_seconds = AV_SCAN_TIMEOUT_SEC
    try:
        min_similarity = float(
            getattr(settings, "av_reverse_min_similarity", AV_SCAN_MIN_SIMILARITY)
        )
    except (TypeError, ValueError):
        min_similarity = AV_SCAN_MIN_SIMILARITY

    async with guard.semaphore:
        try:
            hits = await search_av_image(
                image_bytes, endpoint=endpoint, timeout_seconds=timeout_seconds
            )
        except AVScanRateLimited:
            guard.note_rate_limited()
            log.warning(
                "【AV 反查】被限流 429 | user=%s | 退避 %.0fs",
                user_id,
                guard.blocked_seconds_left(),
            )
            return ""
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("【AV 反查】超时 | user=%s | timeout=%.0fs", user_id, timeout_seconds)
            return ""
        except Exception as exc:  # noqa: BLE001 - 第三方不可控，一律降级
            log.warning("【AV 反查】失败 | user=%s | error=%s", user_id, exc)
            return ""

    picked = pick_av_scan_code(hits, min_similarity=min_similarity)
    if picked is None:
        top = hits[0] if hits else None
        log.info(
            "【AV 反查】未达阈值 | user=%s | 阈值=%.0f | 最高=%s",
            user_id,
            min_similarity,
            f"{top.code} {top.similarity:.2f}" if top else "无候选",
        )
        return ""

    code, similarity = picked
    log.info("【AV 反查】命中 | code=%s | 相似度=%.2f | 帧数=%s", code, similarity, hits[0].frames)
    return code
