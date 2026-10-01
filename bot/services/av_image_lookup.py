"""私聊 ``/av`` 识图反查番号：挑图、抽编号、限流（纯逻辑，不碰 Telegram 路由）。

为什么单独一个模块：

- ``bot/handlers/commands.py`` 已经很长，而路由装饰器必须**紧贴**它的处理器
  （见 ``tests/test_router_route_integrity.py``），不适合再往中间插大段 helper；
- 挑图规则与群内视觉**故意不同**：``bot/handlers/group.py`` 固定取
  ``photo[len-2]``，实测大图（211KB → base64 275KB）会撞上视觉阶段的上下文预算，
  日志是 ``LLM request exceeds configured context budget | stage=vision``。
  识图自己按「最大且 ≤150KB，否则最小」挑，避免这个静默失败；首选档位没读出
  编号时，再按 :data:`AV_PHOTO_RETRY_MAX_BYTES`（190KB，实测安全带的上限）
  挑一次「更大的一档」重试一次（见 ``bot/handlers/commands.py`` 的共享步骤）。

这一层只做三件与 I/O 无关的事 + 一次下载：

1. 从 ``message`` 里挑一档图片（``photo`` 多档尺寸 / ``document`` 里的 ``image/*``）；
2. 下载并以 base64 data URI 的形式交给 ``LLMService.vision_describe``（绝不把远端
   URL 交给模型网关——实测让网关自己抓图会卡死超时）；
3. 从模型输出里抽编号/演员名，并给私聊查询做内存限流。
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import re
import time
from collections import deque
from typing import Any, Callable, Iterable, Sequence

from bot.services.av_search import normalize_av_code, normalize_fc2_code

log = logging.getLogger(__name__)

#: 视觉提示词：只要编号与演员名，不要画面描述（模型实测不会拒绝读封面）。
AV_VISION_PROMPT = (
    "只读出图中可见的作品编号/番号（形如 ABC-123、SSIS-001、FC2-PPV-1234567）"
    "与演员名，不要描述画面内容、不要评论。"
    "看不到编号就回复 NO_CODE；无法识别图片就回复 NO_VALID_IMAGE_CONTENT。"
    "如果只看到演员名、看不到编号，只回复一行：ACTOR: 演员名"
)

#: 与群内视觉一致的上限/超时（``bot/handlers/group.py``）。
AV_VISION_MAX_IMAGE_BYTES = 5 * 1024 * 1024
AV_VISION_DOWNLOAD_TIMEOUT_SEC = 20.0
AV_VISION_TIMEOUT_SEC = 20.0

#: ``message.photo`` 每一档的优先上限：超过它 base64 后容易撞视觉上下文预算。
#:
#: 实测（网关按 base64 字符数近似计 token，视觉阶段预算 256k）：211KB 封面 →
#: base64 ≈275KB → 281,898 tokens → 整次调用被跳过（比降分辨率更糟）；137/166/
#: 173/184KB 都成功、206KB 失败 → 真实天花板约 190KB。首选取 150KB，留出余量。
AV_PHOTO_PREFERRED_MAX_BYTES = 150 * 1024

#: 首选档位**没读出编号**时，升级重试用的上限：实测安全带的上限（约 190KB）。
#:
#: 注意：升级不等于「任何图都能识别」——天花板来自 token 预算，比这更大的图
#: 仍可能被整体跳过，那种情况只能提示用户换小图或直接发番号。
AV_PHOTO_RETRY_MAX_BYTES = 190 * 1024

#: 模型约定的两个「没有结果」标记。
AV_NO_CODE_MARKER = "NO_CODE"
AV_NO_IMAGE_MARKER = "NO_VALID_IMAGE_CONTENT"

_CODE_CANDIDATE_RE = re.compile(r"\b([A-Za-z]{2,10})[-_ ]?(\d{2,5})\b")
_ACTOR_LINE_RE = re.compile(
    r"^\s*(?:ACTOR|演员|女优|女優)\s*[:：]\s*(?P<name>.+?)\s*$",
    re.IGNORECASE,
)
_SENTENCE_PUNCTUATION = "。！？!?；;，,、\n\r"


class _LimitedBytesIO(io.BytesIO):
    """超过上限就直接拒绝写入，避免被超大图片拖爆内存（同群内视觉）。"""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = max(1, int(limit))

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if self.tell() + len(data) > self._limit:
            raise ValueError("av_image_too_large")
        return super().write(data)


def av_image_size_kb(size_bytes: int) -> int:
    """把声明大小换算成日志里用的 KB（向上取整；0 保持 0 = 未知）。"""

    size = max(0, int(size_bytes or 0))
    return (size + 1023) // 1024


def pick_av_photo_size(
    photo_sizes: Sequence[Any] | None,
    *,
    preferred_max_bytes: int = AV_PHOTO_PREFERRED_MAX_BYTES,
) -> Any | None:
    """从 ``message.photo`` 多档尺寸里挑一档。

    规则（实测定死，**不是**群内那套固定的 ``len(photo)-2``）：

    1. 优先「最大且 ``file_size`` ≤ ``preferred_max_bytes``」的那一档
       （默认 :data:`AV_PHOTO_PREFERRED_MAX_BYTES` = 150KB）；
    2. 若每一档都 > ``preferred_max_bytes``，选**最小**的那一档；
    3. ``file_size`` 缺失（0/None）按 0 处理，即视为没超限。

    上限可传参：升级重试时传 :data:`AV_PHOTO_RETRY_MAX_BYTES`（190KB）再挑一次，
    就能拿到「更大的一档」；两档挑出来一样（``file_id`` 相同）说明本来就没有更大
    的档可选，调用方据此决定不重试。
    """

    limit = max(0, int(preferred_max_bytes))
    candidates = [
        size for size in (photo_sizes or []) if getattr(size, "file_id", None)
    ]
    if not candidates:
        return None

    sized = [
        (max(0, int(getattr(size, "file_size", 0) or 0)), size) for size in candidates
    ]
    within_budget = [
        (declared, size) for declared, size in sized if declared <= limit
    ]
    if within_budget:
        return max(within_budget, key=lambda item: item[0])[1]
    return min(sized, key=lambda item: item[0])[1]


def select_av_image_file(
    message: Any,
    *,
    preferred_max_bytes: int = AV_PHOTO_PREFERRED_MAX_BYTES,
) -> tuple[str, str, int] | None:
    """返回 ``(file_id, mime, 声明大小)``；不是可用图片时返回 ``None``。

    - ``message.photo``：按 :func:`pick_av_photo_size` 挑档（上限可传参），
      mime 固定 ``image/jpeg``；
    - ``message.document``：只要 ``image/*``（按需求放开到任意图片子类型）；
      文档没有多档尺寸，``preferred_max_bytes`` 对它没有意义。
    """

    photo = getattr(message, "photo", None)
    if photo:
        picked = pick_av_photo_size(photo, preferred_max_bytes=preferred_max_bytes)
        if picked is None:
            return None
        return (
            str(getattr(picked, "file_id", "") or ""),
            "image/jpeg",
            int(getattr(picked, "file_size", 0) or 0),
        )

    document = getattr(message, "document", None)
    if document is not None:
        mime = str(getattr(document, "mime_type", "") or "").split(";", 1)[0].strip().lower()
        if not mime.startswith("image/"):
            return None
        return (
            str(getattr(document, "file_id", "") or ""),
            mime,
            int(getattr(document, "file_size", 0) or 0),
        )

    return None


async def build_av_image_data_uri(
    message: Any,
    *,
    preferred_max_bytes: int = AV_PHOTO_PREFERRED_MAX_BYTES,
) -> str:
    """按偏好上限挑一档图片，并下载成 base64 data URI；任何失败都返回空串。

    这里**不**把远端 URL 交给模型：网关自己抓 javbus 封面会卡死超时。
    上限可传参（升级重试传 :data:`AV_PHOTO_RETRY_MAX_BYTES`）；要按**已知**的
    ``file_id`` 下载（重试用「另一档」）用 :func:`build_av_image_data_uri_for`。
    """

    info = select_av_image_file(message, preferred_max_bytes=preferred_max_bytes)
    if info is None:
        return ""
    file_id, mime, declared_size = info
    return await build_av_image_data_uri_for(
        message, file_id, mime, declared_size=declared_size
    )


async def build_av_image_data_uri_for(
    message: Any,
    file_id: str,
    mime: str = "image/jpeg",
    *,
    declared_size: int = 0,
) -> str:
    """把**指定的一档**（``file_id`` + ``mime``）下载成 base64 data URI。

    挑图与下载分开：挑图交给 :func:`select_av_image_file` /
    :func:`pick_av_photo_size`，这里只负责把选定的一档取回来编码，所以升级重试
    可以换「另一档」再调一次。任何失败都返回空串（只记日志），绝不抛。
    """

    file_id = str(file_id or "")
    mime = str(mime or "image/jpeg") or "image/jpeg"
    declared_size = max(0, int(declared_size or 0))
    if not file_id:
        return ""
    if declared_size > AV_VISION_MAX_IMAGE_BYTES:
        log.warning("【识图】跳过超限图片 | 声明大小=%dB | 类型=%s", declared_size, mime)
        return ""

    bot = getattr(message, "bot", None)
    if bot is None:
        return ""

    try:
        async with asyncio.timeout(AV_VISION_DOWNLOAD_TIMEOUT_SEC):
            tg_file = await bot.get_file(file_id)
            remote_size = int(getattr(tg_file, "file_size", 0) or 0)
            if remote_size > AV_VISION_MAX_IMAGE_BYTES:
                log.warning(
                    "【识图】跳过超限图片 | 远端大小=%dB | 类型=%s", remote_size, mime
                )
                return ""
            file_path = getattr(tg_file, "file_path", "") or ""
            if not file_path:
                return ""
            buf = _LimitedBytesIO(AV_VISION_MAX_IMAGE_BYTES)
            await bot.download_file(file_path, destination=buf)
    except TimeoutError:
        log.warning("【识图】图片下载超时")
        return ""
    except Exception as exc:
        log.warning("【识图】图片下载失败 | error=%s", exc)
        return ""

    raw = buf.getbuffer()
    if not raw:
        return ""
    log.info("【识图】图片下载完成 | 大小=%dB | 类型=%s", len(raw), mime)
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _consume_task_result(task: "asyncio.Task[Any]") -> None:
    """收尾被取消/超时的子任务，避免 "exception was never retrieved"。"""

    if task.cancelled():
        return
    try:
        task.exception()
    except asyncio.CancelledError:  # pragma: no cover - 竞态收尾
        return


async def run_with_hard_deadline(awaitable: Any, *, timeout_seconds: float) -> Any:
    """在墙钟到点后一定返回，即使子任务拖着不响应取消（同群内视觉的做法）。

    直接用 ``asyncio.timeout`` 时，如果 ``vision_describe`` 卡在不可取消的等待里，
    ``__aexit__`` 会一直等下去——那正是「不要卡住」要避免的。这里改成
    ``asyncio.wait``：到点就取消子任务并立刻抛 ``TimeoutError``，子任务的异常由
    done-callback 消费掉。
    """

    task: "asyncio.Task[Any]" = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait(
            {task}, timeout=max(0.01, float(timeout_seconds))
        )
    except asyncio.CancelledError:
        # 外层被取消（比如进程退出）：别留下一个没人管的子任务。
        task.cancel()
        raise
    if task in done:
        return task.result()
    task.cancel()
    task.add_done_callback(_consume_task_result)
    raise TimeoutError("av_image_vision_deadline_exceeded")


def extract_av_code(vision_text: str) -> str:
    """从模型输出里抽番号：先 FC2，再普通编号，抽不到返回空串。"""

    raw = (vision_text or "").strip()
    if not raw:
        return ""

    fc2_code = normalize_fc2_code(raw)
    if fc2_code:
        return fc2_code

    for matched in _CODE_CANDIDATE_RE.finditer(raw):
        normalized = normalize_av_code(matched.group(0))
        if normalized:
            return normalized
    return ""


def _clean_actor_name(raw: str) -> str:
    name = (raw or "").strip().strip("：:，,、。.　 ")
    name = re.sub(r"\s+", " ", name)
    if not name or len(name) > 30:
        return ""
    if any(char in name for char in _SENTENCE_PUNCTUATION):
        return ""
    if len(name.split(" ")) > 4:
        return ""
    return name


def extract_av_actor(vision_text: str) -> str:
    """从模型输出里抽演员名（最好-effort）。

    优先认 ``ACTOR: 名字`` 这一行（提示词里约定的格式）；抽不到时退化成
    「去掉番号与标记后剩下的短文本」，长句/描述一律不当成人名。
    """

    raw = (vision_text or "").strip()
    if not raw or AV_NO_IMAGE_MARKER in raw:
        return ""

    for line in raw.splitlines():
        matched = _ACTOR_LINE_RE.match(line)
        if matched:
            name = _clean_actor_name(matched.group("name"))
            if name:
                return name

    remainder = raw.replace(AV_NO_CODE_MARKER, " ").replace(AV_NO_IMAGE_MARKER, " ")
    remainder = _CODE_CANDIDATE_RE.sub(" ", remainder)
    remainder = re.sub(r"(?i)\bFC2[-_ ]?(?:PPV[-_ ]?)?\d{5,9}\b", " ", remainder)
    return _clean_actor_name(remainder)


class AVPrivateRateLimiter:
    """内存滑窗限流：每个用户每小时最多 ``limit`` 次私聊 AV 识图/查询。

    取舍：进程重启清零（可接受，见提交说明）。只用单调时钟，不依赖系统时间；
    全部操作都是同步的，单事件循环里不会出现 await 断层。
    """

    def __init__(
        self,
        *,
        limit: int = 10,
        window_seconds: float = 3600.0,
        max_users: int = 4096,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_users = max(16, int(max_users))
        self._clock = clock or time.monotonic
        self._hits: dict[int, deque[float]] = {}

    def _prune(self, user_id: int, now: float) -> deque[float]:
        hits = self._hits.get(user_id)
        if hits is None:
            self._drop_idle_users()
            hits = deque()
            self._hits[user_id] = hits
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def _drop_idle_users(self) -> None:
        """字典只增不减会随用户数慢慢涨；满了先清掉已经过期的空队列。"""

        if len(self._hits) < self.max_users:
            return
        cutoff = self._clock() - self.window_seconds
        stale = [
            uid
            for uid, hits in self._hits.items()
            if not hits or hits[-1] <= cutoff
        ]
        for uid in stale:
            self._hits.pop(uid, None)

    def _retry_after(self, hits: deque[float], now: float) -> int:
        if not hits:
            return 0
        remaining = self.window_seconds - (now - hits[0])
        return max(1, int(math.ceil(remaining)))

    def blocked(self, user_id: int) -> tuple[bool, int]:
        """只查询、不计数（给「点按钮触发外部抓取」这类路径用）。"""

        uid = int(user_id or 0)
        if uid <= 0:
            return False, 0
        now = self._clock()
        hits = self._prune(uid, now)
        if len(hits) < self.limit:
            return False, 0
        return True, self._retry_after(hits, now)

    def allow(self, user_id: int) -> tuple[bool, int]:
        """查询并在放行时计一次数；被拒时返回建议等待秒数。"""

        uid = int(user_id or 0)
        if uid <= 0:
            return True, 0
        now = self._clock()
        hits = self._prune(uid, now)
        if len(hits) >= self.limit:
            return False, self._retry_after(hits, now)
        hits.append(now)
        return True, 0


def rate_limit_minutes(retry_after_seconds: int) -> int:
    """把剩余秒数换算成回复里的分钟数（至少 1 分钟）。"""

    return max(1, int(math.ceil(max(0, int(retry_after_seconds)) / 60.0)))


def iter_code_candidates(vision_text: str) -> Iterable[str]:
    """调试/测试用：列出模型输出里所有看起来像番号的候选。"""

    for matched in _CODE_CANDIDATE_RE.finditer(vision_text or ""):
        normalized = normalize_av_code(matched.group(0))
        if normalized:
            yield normalized
