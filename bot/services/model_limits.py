"""模型上下文窗口：从**已配置的**网关 ``/models`` 取真实元数据并安全缓存。

为什么单独一个模块（2026-10-04 生产事故的直接修复）：

* 事故现场（group history=2003）里，网关自报主模型窗口 ``1,000,000``，但全项目用
  ``max_context_tokens = 278528``（272K）当**全局硬上限**：真装配出来的 prompt 被这
  个固定值判成"超限"，主模型与备用**都没发 HTTP** 就被 skip，最后由 ``force_reply``
  的固定话术顶上。这一期的口径是「**上限按实际模型自动匹配**」，272K 只作为**没有
  任何元数据时的保守降级**，不再压住已知模型。
* 取数只走**已经配置好、且已经带认证信息**的 endpoint（本仓部署是本地/内网桥：
  ``home_work2api``、``pipio``）。没有 ``api_base`` 的官方 provider 一律不探测——
  不在启动时给第三方打真实请求。
* 缓存键是 ``(provider, api_base, model)``；成功 TTL 6 小时，失败/未知短 TTL 5 分钟。
  回复/审核主链路**永远不查网络**：:meth:`ModelLimitRegistry.resolve` 是纯同步的
  缓存读 + 本地 ``litellm`` 注册表查询，查不到就保守降级并明确日志。

日志纪律：只打印 provider 名、api_base 的**主机名**、model 原样 id、来源与数值。
绝不打印 api_key / Authorization 头（``_redact_base``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from bot.utils import budget as _budget
from bot.utils.tokens import estimate_text_tokens

log = logging.getLogger(__name__)

#: 成功元数据的 TTL（6 小时）。模型窗口是部署期事实，不需要每条消息都问网关。
MODEL_METADATA_SUCCESS_TTL_SECONDS = 6 * 3600.0
#: 失败/未命中的短 TTL（5 分钟）：网关重启或路由变更后最多 5 分钟就会重试，
#: 同时又不会退化成"每条消息一次网络查询"。
MODEL_METADATA_FAILURE_TTL_SECONDS = 300.0
#: 单次元数据查询的硬超时（秒）。refresh 是后台任务，主链路不等它。
MODEL_METADATA_TIMEOUT_SECONDS = 4.0
#: 一次 refresh 最多同时打几个网关。
MODEL_METADATA_MAX_CONCURRENCY = 3
#: 周期刷新间隔（秒）。**缓存 fresh 时这一轮零网络**；失败的负缓存（短 TTL）到点即重试。
#: 5 分钟只查"没有/过期"的 endpoint，绝不进回复/审核热路径，也不变成每条消息一次查询。
MODEL_METADATA_REFRESH_INTERVAL_SECONDS = 300.0

#: 窗口来源（日志/资源健康里会原样出现，便于事后核对）。
LIMIT_SOURCE_GATEWAY = "gateway_metadata"
LIMIT_SOURCE_REGISTRY = "model_registry"
LIMIT_SOURCE_LEGACY = "legacy_configured"
LIMIT_SOURCE_FIXED = "fixed_configured"
LIMIT_SOURCE_UNKNOWN = "conservative_default"

#: 未知模型（网关没元数据、litellm 也不认识这个 id）时的保守降级：**不是无限**。
#: 它恰好等于业务预算 272Ki，所以未知模型走的就是"业务预算本身"（不会更宽松）。
DEFAULT_UNKNOWN_TOTAL_WINDOW = 278_528
#: **兼容字段/保守降级**的夹取范围（与 runtime_config 的字段约束一致）。
#: 注意：网关元数据里解析出来的模型真实窗口**不做任何截断**（1M/4M 都原样记录、原样
#: 出现在日志与快照里）；真正限制"每一轮能塞多少"的是下面的**业务预算**。
CONTEXT_WINDOW_MIN = 1024
CONTEXT_WINDOW_MAX = 2_000_000
#: 保守降级最少保留的输出余量（token）。
MIN_OUTPUT_RESERVE_TOKENS = 1024

# ---------------------------------------------------------------------------
# 业务预算（用户口径，2026-10-04 最终确认）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 业务预算（运行时可配置；常量只是默认值，见 bot/utils/budget.py）
# ---------------------------------------------------------------------------
BUSINESS_CONTEXT_WINDOW_TOKENS = _budget.BUSINESS_CONTEXT_WINDOW_TOKENS
BUSINESS_OUTPUT_RESERVE_TOKENS = _budget.BUSINESS_OUTPUT_RESERVE_TOKENS
BUSINESS_INPUT_BUDGET_TOKENS = _budget.BUSINESS_INPUT_BUDGET_TOKENS
BUSINESS_CONTEXT_TOKENS_MIN = _budget.BUSINESS_CONTEXT_TOKENS_MIN
BUSINESS_CONTEXT_TOKENS_MAX = _budget.BUSINESS_CONTEXT_TOKENS_MAX
CONTEXT_RESERVE_TOKENS_MIN = _budget.CONTEXT_RESERVE_TOKENS_MIN
CONTEXT_RESERVE_TOKENS_MAX = _budget.CONTEXT_RESERVE_TOKENS_MAX
DEFAULT_GROUP_HISTORY_MAX_MESSAGES = _budget.DEFAULT_GROUP_HISTORY_MAX_MESSAGES
GROUP_HISTORY_MAX_MESSAGES_MIN = _budget.GROUP_HISTORY_MAX_MESSAGES_MIN
GROUP_HISTORY_MAX_MESSAGES_MAX = _budget.GROUP_HISTORY_MAX_MESSAGES_MAX
MIN_INPUT_ALLOWANCE_TOKENS = _budget.MIN_INPUT_ALLOWANCE_TOKENS

#: 上下文模式：``auto`` = 自动**发现**模型真实窗口（默认；发现值只用于比业务预算更紧，
#: 不会放松 272Ki）；``fixed`` = 不查元数据，``max_context_tokens`` 即模型侧上限
#: （仍受 272Ki 业务预算约束）。
CONTEXT_MODE_AUTO = "auto"
CONTEXT_MODE_FIXED = "fixed"

#: 网关 ``/models`` 每条 entry 里可能出现的字段名。**故意不认 ``max_tokens``**：
#: 各家网关对它的定义不一致（有的指输出上限），猜错会把总窗口当成输出上限。
_TOTAL_WINDOW_KEYS = (
    "context_length",
    "context_window",
    "contextWindow",
    "context_length_tokens",
    "max_context_tokens",
    "max_context_length",
    "total_token_limit",
)
_INPUT_LIMIT_KEYS = (
    "max_input_tokens",
    "maxInputTokens",
    "input_token_limit",
    "inputTokenLimit",
)
_OUTPUT_LIMIT_KEYS = (
    "max_output_tokens",
    "maxOutputTokens",
    "output_token_limit",
    "outputTokenLimit",
)


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: json.loads 默认接受 Infinity/NaN，int(float("inf")) 抛的是它。
        number = int(default)
    return min(int(high), max(int(low), number))


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _first_positive_int(raw: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        if key in raw:
            found = _positive_int(raw.get(key))
            if found is not None:
                return found
    return None


def redact_base(api_base: str | None) -> str:
    """把 api_base 变成可以安全写日志的形式（只留 scheme + host）。

    网关地址可能内嵌凭据（``http://user:pass@host``）；日志里只允许出现主机名。
    """

    raw = str(api_base or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "//" in raw else f"//{raw}")
    except ValueError:
        return ""
    host = parts.hostname or ""
    if not host:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme or 'http'}://{host}{port}"


@dataclass(frozen=True, slots=True)
class ModelLimits:
    """一个 endpoint 的窗口结论。"""

    model: str
    source: str
    #: 网关自报的**总**窗口（输入 + 输出）。与下面的显式输入上限是两件事。
    total_window: int | None = None
    #: 显式输入上限：有值时**直接用**，不再重复减输出预留。
    max_input_tokens: int | None = None
    #: 网关自报的输出上限（仅用于说明，不参与裁剪）。
    max_output_tokens: int | None = None
    #: 命中的元数据 id（原样 id 或去掉 provider 前缀后的同 id）。
    matched_id: str = ""
    #: 供日志/健康检查的补充说明（例如 ``expired_cache``）。
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.source != LIMIT_SOURCE_UNKNOWN and bool(
            self.total_window or self.max_input_tokens
        )

    @property
    def context_total_tokens(self) -> int | None:
        """用于"总窗口"展示/装配预算的数值（输入 + 输出）。"""

        if self.total_window:
            return int(self.total_window)
        if self.max_input_tokens:
            return int(self.max_input_tokens) + int(self.max_output_tokens or 0)
        return None

    def input_budget_tokens(self, *, output_reserve: int) -> int:
        """**模型侧**允许的 prompt 上限（不含业务预算）。

        口径（"输出预留不可重复减"）：

        * 元数据给了**显式输入上限**（``max_input_tokens`` / ``inputTokenLimit``）→
          直接用，不再减输出；
        * 只给了**总窗口** → 总窗口 − 本次请求的输出预留，减一次；
        * 什么都没有 → 由调用方的保守降级窗口兜底，同样只减一次。

        ``fixed`` 模式下配置值就是硬上限，**不再**被 1024 的下限抬高（旧的
        ``max_context_tokens=100`` 语义必须原样保留，否则逃生舱就失效了）。

        实际发请求用的是 :meth:`business_input_budget`（再叠 272Ki 业务预算）。
        """

        if self.max_input_tokens:
            return max(1, int(self.max_input_tokens))
        total = int(self.total_window or 0)
        if total <= 0:
            return 0
        reserve = max(0, int(output_reserve))
        if reserve >= total:
            # 输出预留比窗口还大只能是配置错了：至少给输入留 1 token 的余地，
            # 让"装不下"由最终裁剪如实报出来，而不是悄悄把门禁关掉。
            reserve = max(1, total // 8)
        return max(1, total - reserve)

    def business_input_budget(
        self,
        *,
        output_reserve: int,
        business_tokens: Any = None,
        reserve_tokens: Any = None,
    ) -> int:
        """**业务**输入上限（最终闸门真正用的那个数）。

        ``min(模型可用输入, 配置业务预算 − 预留)``，预留 = ``max(配置预留, 本次输出需求)``：

        * 默认配置：``278528 − 32768 = 245760``；
        * 运维把业务预算配小/配大 → 跟着变（配置是权威，不被隐藏常量截断）；
        * 模型更小（例如 128K 窗口）→ 与模型窗口取小，跟着更小；
        * 本次实际输出需求更大（``max_tokens > 配置预留``）→ 预留取它，进一步收紧；
        * 元数据给了显式输入上限 → 那条上限本身已经排除了输出，与业务输入上限取小即可；
        * 预留**只在这里扣一次**（调用方不得再扣），非法配置收紧而不是关掉门禁。
        """

        business = _bounded_int(
            business_tokens,
            default=BUSINESS_CONTEXT_WINDOW_TOKENS,
            low=BUSINESS_CONTEXT_TOKENS_MIN,
            high=BUSINESS_CONTEXT_TOKENS_MAX,
        )
        reserve = max(
            _bounded_int(
                reserve_tokens,
                default=BUSINESS_OUTPUT_RESERVE_TOKENS,
                low=CONTEXT_RESERVE_TOKENS_MIN,
                high=CONTEXT_RESERVE_TOKENS_MAX,
            ),
            max(0, int(output_reserve or 0)),
        )
        total = int(self.total_window or 0)
        effective_total = min(total, business) if total > 0 else business
        # No usable input is an honest failure, not permission to shrink the
        # declared output reservation or disable final request protection.
        if reserve >= effective_total:
            return 0
        ceiling = effective_total - reserve
        if self.max_input_tokens:
            # An explicit input cap is an additional constraint. If a total
            # window is also advertised, input + reserved output must fit it.
            return max(0, min(int(self.max_input_tokens), ceiling))
        return ceiling

    def describe(self) -> str:
        return (
            f"model={self.model} source={self.source} "
            f"total_window={self.total_window or '-'} "
            f"max_input={self.max_input_tokens or '-'} "
            f"max_output={self.max_output_tokens or '-'} "
            f"matched_id={self.matched_id or '-'}"
            + (f" detail={self.detail}" if self.detail else "")
        )


@dataclass(slots=True)
class _CacheEntry:
    #: ``None`` = **负缓存**：这段时间内不重问网关，但结论按"未知"往下走。
    limits: ModelLimits | None
    expires_at: float


def parse_models_payload(payload: Any) -> dict[str, dict[str, Any]]:
    """把网关 ``/models`` 响应解析成 ``{原样 id: {total_window, max_input, max_output}}``。

    同时兼容 OpenAI 风格（``{"data": [{"id": ...}]}``）与 Gemini 原生风格
    （``{"models": [{"name": "models/xxx", "inputTokenLimit": ...}]}``）。解析失败/形状
    不认识一律返回空字典——调用方按"没元数据"处理，绝不猜。
    """

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    if not isinstance(payload, Mapping):
        return {}

    entries: Any = payload.get("data")
    if not isinstance(entries, list):
        entries = payload.get("models")
    if not isinstance(entries, list):
        return {}

    parsed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        raw_id = entry.get("id") or entry.get("name") or entry.get("model")
        model_id = str(raw_id or "").strip()
        if not model_id:
            continue
        if model_id.startswith("models/"):
            model_id = model_id[len("models/") :]
        info = {
            "total_window": _first_positive_int(entry, _TOTAL_WINDOW_KEYS),
            "max_input_tokens": _first_positive_int(entry, _INPUT_LIMIT_KEYS),
            "max_output_tokens": _first_positive_int(entry, _OUTPUT_LIMIT_KEYS),
        }
        if not any(value for value in info.values()):
            continue
        parsed[model_id] = info
    return parsed


def candidate_model_ids(model: str) -> list[str]:
    """原样 id 优先；再退一步只去掉 litellm 的 ``provider/`` 前缀。

    **绝不猜别名**：不会把 ``openai/some-alias`` 认成 ``gpt-4o`` 的窗口，只做
    "同一个 id 的两种写法"匹配。
    """

    raw = str(model or "").strip()
    if not raw:
        return []
    ids = [raw]
    if "/" in raw:
        stripped = raw.split("/", 1)[1].strip()
        if stripped and stripped not in ids:
            ids.append(stripped)
    return ids


def _endpoint_key(cfg: Any) -> tuple[str, str, str]:
    provider = str(getattr(cfg, "provider", "") or "").strip().lower()
    model = str(getattr(cfg, "model", "") or "").strip()
    if not provider and "/" in model:
        provider = model.split("/", 1)[0].strip().lower()
    api_base = str(getattr(cfg, "api_base", "") or "").strip()
    return (provider, api_base, model)


def metadata_url(api_base: str | None) -> str:
    """把配置里的 api_base 归一成 ``/models`` 查询地址。

    只做尾部归一（去掉 ``/chat/completions`` 之类的请求路径），不改变 host/前缀，
    也不做任何探测式拼接：拿不准就返回空串，宁可不查。
    """

    raw = str(api_base or "").strip()
    if not raw:
        return ""
    base = raw.rstrip("/")
    lowered = base.lower()
    for suffix in (
        "/chat/completions",
        "/completions",
        "/responses",
        "/messages",
    ):
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            lowered = base.lower()
            break
    if not base:
        return ""
    if lowered.endswith("/models"):
        return base
    return f"{base}/models"


def _auth_headers(cfg: Any) -> dict[str, str]:
    """从**已配置**的 endpoint 上取认证头。没有 key 就返回空 → 不查。"""

    api_key = str(getattr(cfg, "api_key", "") or "").strip()
    if not api_key:
        return {}
    provider = str(getattr(cfg, "provider", "") or "").strip().lower()
    model = str(getattr(cfg, "model", "") or "")
    if not provider and "/" in model:
        provider = model.split("/", 1)[0].strip().lower()
    headers = {"Accept": "application/json"}
    if provider == "gemini":
        # Gemini 原生 API 用 ``x-goog-api-key``；官方 host 上再带一个 Bearer 会让
        # Google 把 key 当 OAuth token 校验而 401，所以官方 host 只发原生头。
        headers["x-goog-api-key"] = api_key
        host = (urlsplit(str(getattr(cfg, "api_base", "") or "")).hostname or "").lower()
        if host.endswith("googleapis.com"):
            return headers
    headers["Authorization"] = f"Bearer {api_key}"
    return headers


class ModelLimitRegistry:
    """窗口元数据的进程级缓存（同步读 + 异步预热）。"""

    def __init__(
        self,
        *,
        success_ttl_seconds: float = MODEL_METADATA_SUCCESS_TTL_SECONDS,
        failure_ttl_seconds: float = MODEL_METADATA_FAILURE_TTL_SECONDS,
        timeout_seconds: float = MODEL_METADATA_TIMEOUT_SECONDS,
        unknown_total_window: int = DEFAULT_UNKNOWN_TOTAL_WINDOW,
        transport: Any | None = None,
    ) -> None:
        # TTL 允许 0（"每次都算过期"）：生产用的是模块常量（6h / 5min），单测要能压到
        # 亚秒级验证"过期就重取"。refresh 的调用点只有启动/配置变更/周期循环，
        # 不会因此变成"每条消息一次网络查询"。
        self.success_ttl_seconds = max(0.0, float(success_ttl_seconds))
        self.failure_ttl_seconds = max(0.0, float(failure_ttl_seconds))
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.unknown_total_window = _bounded_int(
            unknown_total_window,
            default=DEFAULT_UNKNOWN_TOTAL_WINDOW,
            low=CONTEXT_WINDOW_MIN,
            high=CONTEXT_WINDOW_MAX,
        )
        self._transport = transport
        self._entries: dict[tuple[str, str, str], _CacheEntry] = {}
        # (provider, base) -> {模型 id: 元数据}
        self._catalog: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._last_refresh_report: dict[str, Any] = {}

    # -- 同步读（主链路唯一入口，绝不碰网络） -------------------------------
    def resolve(self, cfg: Any, *, legacy_total_window: Any = None) -> ModelLimits:
        """解析一个 endpoint 的窗口。顺序：有效缓存 → 网关元数据 → litellm 注册表
        → 保守降级。"""

        key = _endpoint_key(cfg)
        model = key[2]
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            if entry.limits is not None:
                return entry.limits
            # 负缓存：这段时间内不重问，但结论按"未知"继续往下解析。
        elif entry is not None and entry.limits is not None:
            # 过期缓存仍然比"未知"更接近事实（网关只是这几天没问到），照用并标注。
            return ModelLimits(
                model=entry.limits.model,
                source=entry.limits.source,
                total_window=entry.limits.total_window,
                max_input_tokens=entry.limits.max_input_tokens,
                max_output_tokens=entry.limits.max_output_tokens,
                matched_id=entry.limits.matched_id,
                detail="expired_cache",
            )

        registry_limits = self._registry_limits(model, api_base=key[1])
        if registry_limits is not None:
            return registry_limits

        return ModelLimits(
            model=model,
            source=LIMIT_SOURCE_UNKNOWN,
            total_window=_positive_int(legacy_total_window) or self.unknown_total_window,
            detail="no_gateway_metadata_no_registry_entry",
        )

    def record_negative(self, cfg: Any, *, ttl_seconds: float | None = None) -> None:
        """负缓存：这次没问出结论，``ttl`` 之内不重复问（失败 TTL 通常很短）。"""

        ttl = self.failure_ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
        self._entries[_endpoint_key(cfg)] = _CacheEntry(
            limits=None,
            expires_at=time.monotonic() + ttl,
        )

    def legacy_limits(self, cfg: Any, *, total_window: Any) -> ModelLimits:
        """``fixed`` 模式：用配置值当硬上限（迁移前的语义）。

        这里**故意**不套 1024 的下限：``fixed`` 是逃生舱，配置成多少就是多少
        （旧语义里 ``max_context_tokens=100`` 会让所有请求都装不下而跳过）。
        """

        try:
            raw = int(total_window)
        except (TypeError, ValueError):
            raw = 0
        window = raw if raw > 0 else self.unknown_total_window
        window = min(CONTEXT_WINDOW_MAX, max(1, window))
        return ModelLimits(
            model=str(getattr(cfg, "model", "") or ""),
            source=LIMIT_SOURCE_FIXED,
            total_window=window,
            detail="context_window_mode=fixed",
        )

    @staticmethod
    def _registry_limits(model: str, *, api_base: str = "") -> ModelLimits | None:
        """litellm 已知模型注册表（本地数据，不发请求）。

        **走网关/桥的 endpoint 不看注册表**：``api_base`` 指向自建网关时，
        ``litellm.get_model_info("openai/gpt-4.1")`` 描述的是 OpenAI 官方那个模型，
        而不是这条链路上的实际窗口——拿它当上限就是"凭别名冒认"。只有直连厂商
        （没有 ``api_base``）时才采信。
        """

        if not model:
            return None
        if str(api_base or "").strip():
            return None
        try:
            import litellm

            info = litellm.get_model_info(model=model)
        except Exception:
            return None
        if not isinstance(info, Mapping):
            return None
        max_input = _positive_int(info.get("max_input_tokens"))
        max_output = _positive_int(
            info.get("max_output_tokens") or info.get("max_tokens")
        )
        if not max_input and not max_output:
            return None
        return ModelLimits(
            model=model,
            source=LIMIT_SOURCE_REGISTRY,
            total_window=(max_input + (max_output or 0)) if max_input else None,
            max_input_tokens=max_input,
            max_output_tokens=max_output,
            matched_id=model,
        )

    # -- 写入（供 refresh / 单测 / 外部注入） --------------------------------
    def record(
        self,
        cfg: Any,
        *,
        total_window: Any = None,
        max_input_tokens: Any = None,
        max_output_tokens: Any = None,
        source: str = LIMIT_SOURCE_GATEWAY,
        matched_id: str = "",
        detail: str = "",
        ttl_seconds: float | None = None,
    ) -> ModelLimits:
        """写入/覆盖一个 endpoint 的窗口结论。"""

        limits = ModelLimits(
            model=str(getattr(cfg, "model", "") or ""),
            source=source,
            total_window=_positive_int(total_window),
            max_input_tokens=_positive_int(max_input_tokens),
            max_output_tokens=_positive_int(max_output_tokens),
            matched_id=str(matched_id or ""),
            detail=str(detail or ""),
        )
        ttl = self.success_ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
        self._entries[_endpoint_key(cfg)] = _CacheEntry(
            limits=limits,
            expires_at=time.monotonic() + ttl,
        )
        return limits

    def expire(self, cfg: Any) -> None:
        key = _endpoint_key(cfg)
        entry = self._entries.get(key)
        if entry is not None:
            self._entries[key] = _CacheEntry(
                limits=entry.limits,
                expires_at=time.monotonic() - 1.0,
            )

    def clear(self) -> None:
        self._entries.clear()
        self._catalog.clear()
        self._last_refresh_report = {}

    def cached_model_ids(self, cfg: Any) -> list[str]:
        provider, api_base, _model = _endpoint_key(cfg)
        catalog = self._catalog.get((provider, api_base))
        return sorted(catalog) if catalog else []

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        entries = {}
        for key, entry in self._entries.items():
            limits = entry.limits
            entries[f"{key[0]}|{redact_base(key[1])}|{key[2]}"] = {
                "source": limits.source if limits is not None else "negative_cache",
                "total_window": limits.total_window if limits is not None else None,
                "max_input_tokens": limits.max_input_tokens if limits is not None else None,
                "max_output_tokens": limits.max_output_tokens if limits is not None else None,
                "matched_id": limits.matched_id if limits is not None else "",
                "fresh": entry.expires_at > now,
                "detail": limits.detail if limits is not None else "gateway_unavailable",
            }
        return {"entries": entries, "last_refresh": dict(self._last_refresh_report)}

    # -- 异步预热（启动 / 路由变更时调用） ----------------------------------
    @staticmethod
    def _fetchable(cfg: Any) -> bool:
        """只有"已配置 + 带 api_base + 带 key"的 endpoint 才探测。"""

        if not str(getattr(cfg, "api_base", "") or "").strip():
            return False
        if not _auth_headers(cfg):
            return False
        return bool(metadata_url(getattr(cfg, "api_base", "")))

    async def _fetch_catalog(
        self,
        cfg: Any,
        *,
        headers: Mapping[str, str],
        base: str,
    ) -> dict[str, dict[str, Any]] | None:
        url = metadata_url(base)
        if not url:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(url, headers=dict(headers))
            if int(getattr(response, "status_code", 0) or 0) != 200:
                log.warning(
                    "model metadata: gateway /models returned non-200 | provider=%s | host=%s | status=%s",
                    str(getattr(cfg, "provider", "") or ""),
                    redact_base(base),
                    getattr(response, "status_code", "?"),
                )
                return None
            parsed = parse_models_payload(response.text)
        except Exception as exc:
            log.warning(
                "model metadata: gateway /models failed | provider=%s | host=%s | error=%s",
                str(getattr(cfg, "provider", "") or ""),
                redact_base(base),
                type(exc).__name__,
            )
            return None
        return parsed

    def _degrade_preserving_cache(self, cfg: Any, *, report: dict[str, Any], label: str, reason: str) -> None:
        """刷新没拿到结论时的收尾：**已有成功记录绝不能被负缓存覆盖**。

        否则一次网关抖动（或者 /models 暂时只返回了部分条目）就会把主模型从实测
        1,000,000 无条件退化回保守的 272K——那正是要修的毛病。有成功记录 → 保留值
        （``resolve`` 会照旧用，只标注 ``expired_cache``），同时把有效期提前，让下一轮
        刷新继续重试；没有成功记录 → 短 TTL 负缓存。
        """

        key = _endpoint_key(cfg)
        entry = self._entries.get(key)
        if entry is not None and entry.limits is not None:
            self._entries[key] = _CacheEntry(
                limits=entry.limits,
                expires_at=time.monotonic() - 1.0,
            )
            report[label] = f"{reason}_keeping_cached"
            log.warning(
                "model metadata: refresh inconclusive, keeping the cached window | "
                "reason=%s | model=%s | cached_source=%s | cached_total_window=%s",
                reason,
                str(getattr(cfg, "model", "") or ""),
                entry.limits.source,
                entry.limits.total_window or "-",
            )
            return
        self.record_negative(cfg)
        report[label] = reason

    async def refresh(
        self,
        endpoints: Iterable[Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """为这批 endpoint 预热/刷新窗口元数据。任何失败都只记日志，不抛。

        返回一份报告（``{"<provider>|<host>|<model>": "<source>"}``），供启动日志与
        ``/health`` 之类的诊断读取。**缓存 fresh 的 endpoint 零网络**。
        """

        candidates = [cfg for cfg in (endpoints or []) if getattr(cfg, "model", None)]
        report: dict[str, Any] = {}
        groups: dict[tuple[str, str], list[Any]] = {}
        now = time.monotonic()
        for cfg in candidates:
            key = _endpoint_key(cfg)
            label = f"{key[0]}|{redact_base(key[1])}|{key[2]}"
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > now and not force:
                report[label] = (
                    entry.limits.source
                    if entry.limits is not None
                    else "gateway_unavailable"
                )
                continue
            if not self._fetchable(cfg):
                report[label] = "skipped_no_credentials_or_api_base"
                continue
            groups.setdefault((key[0], key[1]), []).append(cfg)

        if not groups:
            self._last_refresh_report = report
            return report

        semaphore = asyncio.Semaphore(MODEL_METADATA_MAX_CONCURRENCY)

        async def _refresh_group(group_key: tuple[str, str], cfgs: list[Any]) -> None:
            provider, base = group_key
            lock = self._locks.setdefault(group_key, asyncio.Lock())
            if lock.locked():
                return
            async with lock:
                async with semaphore:
                    catalog = await self._fetch_catalog(
                        cfgs[0],
                        headers=_auth_headers(cfgs[0]),
                        base=base,
                    )
                if catalog is None:
                    for cfg in cfgs:
                        label = (
                            f"{provider}|{redact_base(base)}|"
                            f"{str(getattr(cfg, 'model', '') or '')}"
                        )
                        # 已有成功记录 → 保留（只标注过期、下轮重试）；没有才负缓存。
                        self._degrade_preserving_cache(
                            cfg,
                            report=report,
                            label=label,
                            reason="gateway_unavailable",
                        )
                    return
                self._catalog[group_key] = catalog
                for cfg in cfgs:
                    model = str(getattr(cfg, "model", "") or "")
                    label = f"{provider}|{redact_base(base)}|{model}"
                    matched_id, info = self._match_model(catalog, model)
                    if info is None:
                        # 网关这次没列这个 id（可能只是返回了部分列表）：绝不拿它把
                        # 已有的成功记录覆盖成"未知"。
                        self._degrade_preserving_cache(
                            cfg,
                            report=report,
                            label=label,
                            reason="model_id_missing",
                        )
                        log.info(
                            "model metadata: 原样 id 不在网关 /models 里 | "
                            "provider=%s | host=%s | model=%s | ids=%d | result=%s",
                            provider,
                            redact_base(base),
                            model,
                            len(catalog),
                            report.get(label, "-"),
                        )
                        continue
                    limits = self.record(
                        cfg,
                        total_window=info.get("total_window"),
                        max_input_tokens=info.get("max_input_tokens"),
                        max_output_tokens=info.get("max_output_tokens"),
                        source=LIMIT_SOURCE_GATEWAY,
                        matched_id=matched_id,
                    )
                    report[label] = LIMIT_SOURCE_GATEWAY
                    log.info(
                        "model metadata: 采用网关自报窗口 | provider=%s | host=%s | "
                        "model=%s | matched_id=%s | total_window=%s | max_input=%s | max_output=%s",
                        provider,
                        redact_base(base),
                        model,
                        matched_id,
                        limits.total_window or "-",
                        limits.max_input_tokens or "-",
                        limits.max_output_tokens or "-",
                    )

        await asyncio.gather(
            *(_refresh_group(key, cfgs) for key, cfgs in groups.items()),
            return_exceptions=True,
        )
        self._last_refresh_report = report
        return report

    @staticmethod
    def _match_model(
        catalog: Mapping[str, Mapping[str, Any]],
        model: str,
    ) -> tuple[str, Mapping[str, Any] | None]:
        for candidate in candidate_model_ids(model):
            info = catalog.get(candidate)
            if info is not None:
                return candidate, info
        return "", None


#: 进程级单例：装配预算（context_gate / group_context / private_chat / memory）与
#: 最终请求闸门（LLMService）读的是**同一份**结论，避免"前面认为装得下、最终拒绝"。
MODEL_LIMITS = ModelLimitRegistry()


def reset_model_limits_for_tests() -> ModelLimitRegistry:
    """单测用：换一个干净的进程级注册表并返回它。"""

    global MODEL_LIMITS
    MODEL_LIMITS = ModelLimitRegistry()
    return MODEL_LIMITS


# ---------------------------------------------------------------------------
# 配置读取（宽容口径：缺字段/删 settings 都不能炸）
# ---------------------------------------------------------------------------


def _config_view(settings: Any) -> Any:
    """``Settings`` 取 ``.bot``；已经是 ``BotConfig`` 就直接用。"""

    bot = getattr(settings, "bot", None)
    return bot if bot is not None else settings


def context_window_mode(settings: Any) -> str:
    """当前上下文模式：``auto``（默认）或 ``fixed``。

    老配置里没有这个字段 → **auto**：2026-10-04 事故的修复就是"按实际模型上限自动
    匹配"，所以"没写"必须等价于自动，而不是悄悄退回 272K 硬上限。
    """

    view = _config_view(settings)
    mode = str(getattr(view, "context_window_mode", "") or "").strip().lower()
    if mode == CONTEXT_MODE_FIXED:
        return CONTEXT_MODE_FIXED
    return CONTEXT_MODE_AUTO


def configured_context_tokens(settings: Any) -> int:
    """兼容字段 ``max_context_tokens`` 的**原样**值（``0`` = 未设置）。"""

    view = _config_view(settings)
    try:
        number = int(getattr(view, "max_context_tokens", None) or 0)
    except (TypeError, ValueError):
        number = 0
    return min(CONTEXT_WINDOW_MAX, max(0, number))


def legacy_context_tokens(settings: Any) -> int:
    """兼容字段的**生效值**：也是未知模型/未知路由的保守降级窗口。

    ``0``/缺项一律按 272K 处理——"没配上限"不等于"上限是 1K"，更不等于"无限"。
    """

    return configured_context_tokens(settings) or DEFAULT_UNKNOWN_TOTAL_WINDOW


def main_endpoint(settings: Any) -> Any | None:
    """主链路（main → skill → fallbacks）的第一个 endpoint。"""

    view = _config_view(settings)
    main_model = getattr(view, "main_model", None)
    if main_model is not None and getattr(main_model, "model", ""):
        return main_model
    skill_model = getattr(view, "skill_model", None)
    if skill_model is not None and getattr(skill_model, "model", ""):
        return skill_model
    return None


def configured_endpoints(settings: Any) -> list[Any]:
    """所有"已配置"的对话 endpoint（含 fallback），供启动/路由变更预热。"""

    view = _config_view(settings)
    seen: set[tuple[str, str, str]] = set()
    endpoints: list[Any] = []
    for name in (
        "main_model",
        "skill_model",
        "decision_model",
        "moderation_model",
        "vision_model",
        "compress_model",
    ):
        model_cfg = getattr(view, name, None)
        if model_cfg is None or not getattr(model_cfg, "model", ""):
            continue
        for cfg in (model_cfg, *list(getattr(model_cfg, "fallbacks", []) or [])):
            if not getattr(cfg, "model", ""):
                continue
            key = _endpoint_key(cfg)
            if key in seen:
                continue
            seen.add(key)
            endpoints.append(cfg)
    return endpoints


def resolved_main_window(settings: Any) -> int | None:
    """主链路实际生效的**总窗口**；没有任何可信元数据时返回 ``None``。"""

    cfg = main_endpoint(settings)
    if cfg is None:
        return None
    limits = MODEL_LIMITS.resolve(cfg, legacy_total_window=legacy_context_tokens(settings))
    return limits.context_total_tokens


def loose_budget_tokens(value: Any, *, default: int, low: int) -> int:
    """宽容预算：拿不到数字就用默认值、只做**下限**保护。

    用于"调用方已经算好的装配预算"：**业务上限由
    :func:`business_total_window` / :data:`BUSINESS_CONTEXT_WINDOW_TOKENS` 决定**，
    这里只是不给装配入口再藏一道 2M 截断（:data:`CONTEXT_WINDOW_MAX` 只约束兼容配置
    字段的取值范围）。
    """

    try:
        number = int(value)
    except (TypeError, ValueError):
        number = int(default)
    return max(int(low), number)


validate_business_budget = _budget.validate_business_budget
validate_group_history_max_messages = _budget.validate_group_history_max_messages


def configured_business_tokens(settings: Any) -> int:
    """运行时可读写的业务总预算（``bot.context_budget_tokens``，默认 272Ki）。

    显式配置**不被隐藏常量截断**：只做正数与下界的合法性保护；上界交给配置校验
    （``BUSINESS_CONTEXT_TOKENS_MAX``）与模型窗口取小。
    """

    view = _config_view(settings)
    value = _positive_int(getattr(view, "context_budget_tokens", None))
    if value is None:
        # 只有**正的**模型侧兼容值才参与兜底；0/缺项一律用推荐默认值
        # （绝不能让"没配业务预算"变成 1024 或 0-关闭门禁）。
        value = _positive_int(getattr(view, "max_context_tokens", None))
    return _bounded_int(
        value,
        default=BUSINESS_CONTEXT_WINDOW_TOKENS,
        low=BUSINESS_CONTEXT_TOKENS_MIN,
        high=BUSINESS_CONTEXT_TOKENS_MAX,
    )


def configured_reserve_tokens(settings: Any) -> int:
    """运行时可读写的输出/工具预留（``bot.context_reserve_tokens``，默认 32Ki）。

    非法（≥ 总预算）时**收紧**到 ``总预算 − 1024``：宁可少留余量，也不能把业务门禁关掉。
    兼容旧字段 ``group_history_reserve_tokens``（旧库只配了它时以它为准）。
    """

    view = _config_view(settings)
    value = getattr(view, "context_reserve_tokens", None)
    legacy = getattr(view, "group_history_reserve_tokens", None)
    fields_set = getattr(view, "model_fields_set", None)
    if isinstance(fields_set, (set, frozenset)):
        # pydantic：按**显式设置过哪个字段**决定，绝不按"值看起来等于默认"猜。
        # 新字段被显式设置（哪怕正好等于默认 32768）→ 以新字段为准；
        # 只有"旧字段被显式设置、新字段没设"时才用旧字段（旧库/旧代码兼容）。
        if "context_reserve_tokens" in fields_set:
            value = value
        elif "group_history_reserve_tokens" in fields_set:
            value = legacy
    elif value is None:
        # 非 pydantic 替身：新字段缺项才回退旧字段。
        value = legacy
    reserve = _bounded_int(
        value,
        default=BUSINESS_OUTPUT_RESERVE_TOKENS,
        low=CONTEXT_RESERVE_TOKENS_MIN,
        high=CONTEXT_RESERVE_TOKENS_MAX,
    )
    budget = configured_business_tokens(settings)
    if reserve >= budget:
        # 配置非法（正常会被 pydantic 拒绝）：只收紧**配置值**本身，绝不缩小
        # "本次实际输出需求"——`business_input_budget` 始终取
        # ``max(配置预留, 实际 max_tokens)``，所以不会制造虚假的正数输入空间。
        reserve = max(1, budget - MIN_INPUT_ALLOWANCE_TOKENS)
    return reserve


def configured_group_history_max_messages(settings: Any) -> int:
    """群历史单次读取安全条数（``bot.group_history_max_messages``，默认 1000）。"""

    view = _config_view(settings)
    return _bounded_int(
        getattr(view, "group_history_max_messages", None),
        default=DEFAULT_GROUP_HISTORY_MAX_MESSAGES,
        low=GROUP_HISTORY_MAX_MESSAGES_MIN,
        high=GROUP_HISTORY_MAX_MESSAGES_MAX,
    )


def business_total_window(
    model_total_window: Any,
    *,
    business_tokens: Any = None,
) -> int:
    """每轮**业务总窗口** = min(模型有效窗口, 272Ki)。

    用户最终口径（2026-10-04 确认，覆盖之前"不设上限"的说法）：

    * 模型真实窗口 1M/4M → 业务窗口仍是 272Ki（**不把百万窗口每轮填满**）；
    * 模型真实窗口比 272Ki 小 → 保持模型那个更小的值；
    * 模型未知 → 保守降级值参与同一个 min（未知 ≠ 无限）。
    """

    ceiling = (
        BUSINESS_CONTEXT_WINDOW_TOKENS
        if business_tokens is None
        else _bounded_int(
            business_tokens,
            default=BUSINESS_CONTEXT_WINDOW_TOKENS,
            low=BUSINESS_CONTEXT_TOKENS_MIN,
            high=BUSINESS_CONTEXT_TOKENS_MAX,
        )
    )
    model = _positive_int(model_total_window)
    if model is None:
        return ceiling
    return max(CONTEXT_WINDOW_MIN, min(int(model), ceiling))


def effective_context_window(settings: Any) -> int:
    """装配链路统一使用的**业务总窗口**（含 272Ki 上限）。

    * ``auto`` + 可信元数据 → ``min(模型真实窗口, 272Ki)``；
    * ``auto`` + 查不到 → 兼容字段的保守降级值（同一个 min）；
    * ``fixed`` → 配置值（再叠同一个 272Ki 业务上限，只允许比 272Ki 更小）。
    """

    business = configured_business_tokens(settings)
    legacy = legacy_context_tokens(settings)
    if context_window_mode(settings) == CONTEXT_MODE_FIXED:
        return business_total_window(legacy, business_tokens=business)
    resolved = resolved_main_window(settings)
    if not resolved:
        return business_total_window(legacy, business_tokens=business)
    return business_total_window(resolved, business_tokens=business)


def auto_mode_enabled(settings: Any) -> bool:
    return context_window_mode(settings) == CONTEXT_MODE_AUTO


def config_limits(settings: Any) -> ModelLimits | None:
    """按配置里的主链路 endpoint 查窗口（只看缓存/注册表，不发请求）。"""

    cfg = main_endpoint(settings)
    if cfg is None:
        return None
    return MODEL_LIMITS.resolve(cfg, legacy_total_window=legacy_context_tokens(settings))


def auto_window_for(settings: Any, *, llm: Any = None) -> int | None:
    """``auto`` 模式下**可信**的总窗口；没有可信来源（要走保守降级）时返回 ``None``。

    这样调用方可以区分两件事：

    * "拿到了真实窗口" → 按 ``窗口 − 自身余量`` 装配（不再被 272K 压住）；
    * "只有保守降级值" → 保持迁移前的既有预算口径，不因为修复而顺手改小深度。
    """

    if not auto_mode_enabled(settings):
        return None
    resolver = getattr(llm, "endpoint_limits", None) if llm is not None else None
    if callable(resolver):
        try:
            limits = resolver(getattr(llm, "main", None))
        except Exception:
            limits = None
        if limits is None:
            return None
        return limits.context_total_tokens if limits.known else None
    limits = config_limits(settings)
    if limits is None or not limits.known:
        return None
    return limits.context_total_tokens


def estimate_messages_tokens(
    messages: Iterable[Mapping[str, Any]] | None,
    tools: Any = None,
) -> int:
    """一致的**保守上界**：与装配链路同一个 :func:`estimate_text_tokens`。

    这是 2026-10-04 事故里"计量不一致"的修复点：装配用 CJK 感知口径（CJK 1 token/字，
    其它 ~3 字符/token），最终闸门却对 ≥100K 字符的载荷按"一字符一 token"再估一遍，
    于是同一份载荷在装配里"装得下"、在闸门里"超限"。现在两边共用**这一个**函数。
    """

    total = 0
    for message in messages or []:
        if not isinstance(message, Mapping):
            continue
        total += conservative_message_tokens(message)
    if tools:
        total += estimate_text_tokens(json.dumps(tools, ensure_ascii=False, default=str))
    return total


#: 每条消息的固定开销（角色/分隔等），与 context_gate 的 +12 同口径。
MESSAGE_TOKEN_OVERHEAD = 12


def conservative_message_tokens(message: Mapping[str, Any]) -> int:
    """单条消息的保守 token 上界（可加：总和 = :func:`estimate_messages_tokens`）。"""

    total = MESSAGE_TOKEN_OVERHEAD
    total += estimate_text_tokens(str(message.get("content") or ""))
    tool_calls = message.get("tool_calls")
    if tool_calls:
        total += estimate_text_tokens(
            json.dumps(tool_calls, ensure_ascii=False, default=str)
        )
    tool_call_id = message.get("tool_call_id")
    if tool_call_id:
        total += estimate_text_tokens(str(tool_call_id))
    name = message.get("name")
    if name:
        total += estimate_text_tokens(str(name))
    return total


def estimate_tools_tokens(tools: Any) -> int:
    if not tools:
        return 0
    return estimate_text_tokens(json.dumps(tools, ensure_ascii=False, default=str))


class PeriodicModelMetadataRefresh:
    """窗口元数据的后台周期刷新（缓存 fresh 时**零网络**）。

    为什么必须有它：启动预取有 6 秒上限、配置变更只是"顺手重取一次"，光靠这两处
    并不能兑现"成功 TTL 6 小时 / 失败短 TTL 5 分钟"的语义——成功记录过期后没人重取，
    负缓存过期后也没有重取入口。这里按固定节奏（默认 5 分钟）跑一轮：

    * 缓存 fresh 的 endpoint 一个网络请求都不发（``refresh`` 自己跳过）；
    * 成功记录过期 → 重取；这样 6 小时 TTL 到期后窗口会自己刷新；
    * 负缓存过期 → 重取；所以"网关刚起来的那几分钟"最多 5 分钟就能拿到真实窗口；
    * 刷新拿到结论后回调 ``on_refreshed``（可同步可异步），用于把新窗口套到
      MemoryService 的预算上——**不重启也要生效**。

    只做元数据，**绝不进回复/审核热路径**；单轮失败只记日志、循环绝不退出
    （``bot/__main__.py`` 会把后台任务的意外退出当致命错误）。
    """

    def __init__(
        self,
        refresh: Callable[[], Awaitable[dict[str, Any]]],
        *,
        interval_seconds: float = MODEL_METADATA_REFRESH_INTERVAL_SECONDS,
        on_refreshed: Callable[[dict[str, Any]], Any] | None = None,
        name: str = "model-metadata-refresh",
    ) -> None:
        self._refresh = refresh
        self._on_refreshed = on_refreshed
        # 最小 10ms：防止调用方传 0 把循环打成热转。
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.name = name
        self.ticks = 0
        self.last_report: dict[str, Any] = {}

    async def refresh_once(self) -> dict[str, Any]:
        """跑一轮。缓存 fresh 时这一轮不发任何请求（返回缓存来源的报告）。"""

        self.ticks += 1
        self.last_report = dict(await self._refresh() or {})
        return self.last_report

    async def run(self) -> None:
        while True:
            try:
                report = await self.refresh_once()
                callback = self._on_refreshed
                if callback is not None:
                    outcome = callback(report)
                    if isawaitable(outcome):
                        await outcome
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "periodic model metadata refresh failed; keeping current windows"
                )
            await asyncio.sleep(self.interval_seconds)

    def start(self) -> asyncio.Task[Any]:
        """起一个后台任务（加入应用的 background_tasks 关闭流程）。"""

        loop = asyncio.get_running_loop()
        return loop.create_task(self.run(), name=self.name)
