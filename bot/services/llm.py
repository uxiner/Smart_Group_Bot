from __future__ import annotations

import asyncio
import copy as copy_module
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from inspect import isawaitable
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

# Loading LiteLLM otherwise performs a blocking five-second GitHub fetch at
# import time.  Model pricing metadata is not required for this bot; use the
# bundled map so startup and test discovery never depend on external DNS.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

import litellm

from bot.config import ChatEndpointConfig, EmbedConfig, EmbedEndpointConfig, ModelConfig
from bot.services import llm_metrics
from bot.services import model_limits as model_limits_module
from bot.services.model_limits import (
    CONTEXT_MODE_FIXED,
    MIN_OUTPUT_RESERVE_TOKENS,
    ModelLimits,
    conservative_message_tokens,
    estimate_tools_tokens,
)
from bot.services.payload_fit import (
    FittedPayload,
    fit_messages_to_input_budget,
    strip_internal_message_keys,
)
from bot.services import policy_runtime
from bot.services.request_priority import (
    ExecutionPriority,
    ReservedCapacityGate,
    execution_priority_scope,
)
from bot.services.resource_health import register_resource_health_provider

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingBatchResult:
    """Vectors generated in one stable embedding space."""

    vectors: list[list[float]]
    space_id: str
    model: str
    dimensions: int

# Keep slow/upstream-broken providers from consuming every Telegram update
# worker at once.  The semaphore is process-wide because LLMService instances
# are intentionally short lived (one is built for each pending reply batch).
_LLM_REQUEST_CAPACITY = 8
_LLM_REQUEST_SEMAPHORE = asyncio.Semaphore(_LLM_REQUEST_CAPACITY)
_LLM_PRIORITY_GATE = ReservedCapacityGate(
    total_capacity=_LLM_REQUEST_CAPACITY,
    noncritical_capacity=7,
    # Ordinary replies may use at most half of the slots. The rest stays
    # available to HIGH join/raid screening and moderation, and the final
    # slot is reserved for CRITICAL permission controls. 8 total keeps a 3x
    # margin below the gateway's measured concurrency knee (~24 in flight;
    # beyond that it answers 503 no_healthy_account).
    normal_capacity=4,
    # 背景摘要单独至多 2 个，并且**与回复共享** normal=4：回复 + 摘要 ≤ 4，
    # 摘要 ≤ 2 ⇒ 普通回复永远至少保留 2 个名额。总数 8 与审核保留边界不变。
    background_capacity=2,
)
# Moderation is safety critical: a missed audit is worse than a late reply,
# so audits are admitted at HIGH priority (one slot beyond the two NORMAL
# reply slots).  Ordinary replies keep the default NORMAL priority and can
# no longer push an audit past its stage deadline during a burst.
_MODERATION_EXECUTION_PRIORITY = ExecutionPriority.HIGH
_LLM_ORPHAN_TASKS: set[asyncio.Future[Any]] = set()
_LLM_ORPHAN_STARTED: dict[asyncio.Future[Any], float] = {}
_LLM_CLEANUP_TASKS: set[asyncio.Future[Any]] = set()
_LLM_CLEANUP_STARTED: dict[asyncio.Future[Any], float] = {}
_LLM_CLEANUP_TASK_LIMIT = 32
_LLM_ORPHAN_MAX_AGE_SECONDS = 120.0
_LLM_MAX_RETRY_ATTEMPTS = 3
_LLM_MAX_ATTEMPT_TIMEOUT_SECONDS = 60.0
_LLM_MAX_CANDIDATES = 4
_LLM_STAGE_DEADLINES = {
    "decision": 35.0,
    "moderation": 35.0,
    "embed": 60.0,
    "compress": 90.0,
    "vision": 90.0,
    "main": 120.0,
    "skill": 120.0,
    # /av 的可选 AI 题材概述：附加项，宁可拿不到也不能拖住查询。
    "synopsis": 20.0,
    # 后台群摘要：入场后（含 fallback 与重试）整个模型调用最多 15 秒。
    "group_summary": 15.0,
}
_LLM_CIRCUIT_FAILURE_THRESHOLD = 3
_LLM_CIRCUIT_COOLDOWN_SECONDS = 30.0

# A tool-capable stage is useless when the model answers with an apology string
# instead of a tool call: the local web bridge does exactly that whenever it
# receives a `tools` array.  Such a response used to be returned as the final
# answer, which silently disabled every search/website skill and could leak the
# apology into the group.  Recognise the phrasing and keep walking the fallback
# chain so a tool-capable endpoint gets a turn.
_TOOL_UNUSABLE_CONTENT_RE = re.compile(
    r"(?:"
    r"something went wrong"
    r"|encounter(?:ing|ed)?\s+an?\s+(?:unknown\s+)?error"
    r"|having a hard time fulfilling"
    r"|try (?:your request|that) again"
    r"|could you try again"
    r"|服务(?:器)?(?:出现)?异常"
    r"|出错了[，,]?\s*请(?:稍后)?重试"
    r")",
    re.IGNORECASE,
)
# Only short replies are treated as failure notices; a long, substantive answer
# that happens to contain one of these phrases must stay usable.
_TOOL_UNUSABLE_MAX_CHARS = 240
_TOOL_UNUSABLE_TTL_SECONDS = 300.0
# A tool-capable fallback must not be starved by a primary endpoint that hangs:
# cap a non-final candidate's call so the rest of the stage budget survives.
_TOOL_CANDIDATE_CAP_SECONDS = 40.0
# The same bridge degrades *chat* turns too: it answers HTTP 200 whose body is
# Google's own failure notice ("Sorry, something went wrong...") or a
# "I am only a language model" refusal template.  The chat path has no tool
# contract to check, so such a string used to be posted to the group verbatim
# as if it were an answer.  Treat it as an unusable turn and keep walking the
# fallback chain -- the free bridge still gets the first try, a working
# endpoint only answers once it fails.
_CHAT_DEGRADED_CONTENT_RE = re.compile(
    r"(?:"
    # Google web failure notices (mirrors _TOOL_UNUSABLE_CONTENT_RE)
    r"something went wrong"
    r"|encounter(?:ing|ed)?\s+an?\s+(?:unknown\s+)?error"
    r"|having a hard time fulfilling"
    r"|try (?:your request|that) again"
    r"|could you try again"
    r"|服务(?:器)?(?:出现)?异常"
    r"|出错了[，,]?\s*请(?:稍后)?重试"
    # "I am only a text model" refusal templates seen from the bridge
    r"|我只是(?:一个|个)?(?:语言模型|文本\s*AI|AI)"
    r"|(?:身为|作为)(?:一个|个)?(?:语言模型|文本\s*AI|AI)"
    r"|我是一个(?:语言模型|文本\s*AI)"
    r"|我(?:只|仅)会生成(?:文字|文本)"
    r"|爱莫能助"
    r"|超出了我的(?:程序逻辑范畴|能力范围|设计用途|能力|设计)"
    r"|程序代码的局限"
    r"|不具备这方面的信息或能力"
    r"|(?:没法|无法|不能)(?:帮(?:到|上|得了)你|办到|提供这方面的(?:帮助|信息|内容))"
    r")",
    re.IGNORECASE,
)
# Only short replies are treated as degraded; a long, substantive answer that
# happens to quote one of these phrases must stay usable.
_CHAT_DEGRADED_MAX_CHARS = 260
# A degraded bridge stays degraded for hours (an upstream protocol change, not a
# blip), so one wasted attempt every 30s would make every group reply feel slow.
# Trip the circuit for longer than the default cooldown.
_CHAT_DEGRADED_COOLDOWN_SECONDS = 300.0
# Identifies the bridge-shaped endpoints the check above is designed for: the
# degraded notice only ever comes from the free in-house Gemini web bridge,
# which is reached over a docker service name or a loopback address.  A public
# provider answering with the same words is a genuine reply and stays usable.
# (``provider`` holds the wire protocol here, not the configured provider name,
# so the address is the only reliable signal.)
_LLM_TOKENIZER_THREAD_CAPACITY = 2
_LLM_TOKENIZER_THREAD_TIMEOUT_SECONDS = 1.0
_LLM_TOKENIZER_STALE_SECONDS = 30.0
#: 载荷超过这么多**源字符**时不再尝试真实分词器：那一定跑不完单次超时，只会在共享
#: 线程池里占着槽位（让后面的请求全都退化成保守估算）。此时直接用同一个保守上界。
_LLM_TOKENIZER_MAX_SOURCE_CHARACTERS = 4_000_000
_LLM_TOKENIZER_THREAD_SLOTS = threading.BoundedSemaphore(
    _LLM_TOKENIZER_THREAD_CAPACITY
)
_LLM_TOKENIZER_STATS_LOCK = threading.Lock()
_LLM_TOKENIZER_ACTIVE_STARTED: dict[object, float] = {}
_LLM_TOKENIZER_STARTED_TOTAL = 0
_LLM_TOKENIZER_SATURATED_TOTAL = 0
_LLM_TOKENIZER_TIMEOUT_TOTAL = 0
_LLM_TOKENIZER_FAILURE_TOTAL = 0
_LLM_TOKENIZER_WARNING_INTERVAL_SECONDS = 30.0
_LLM_TOKENIZER_LAST_WARNING_AT = 0.0
_LLM_CIRCUITS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[str, str, str, str], tuple[int, float]],
] = weakref.WeakKeyDictionary()


def _tokenizer_stats_snapshot() -> dict[str, int | float]:
    now = time.monotonic()
    with _LLM_TOKENIZER_STATS_LOCK:
        oldest_age = max(
            (now - started for started in _LLM_TOKENIZER_ACTIVE_STARTED.values()),
            default=0.0,
        )
        return {
            "capacity": _LLM_TOKENIZER_THREAD_CAPACITY,
            "active": len(_LLM_TOKENIZER_ACTIVE_STARTED),
            "oldest_active_seconds": round(oldest_age, 3),
            "started_total": _LLM_TOKENIZER_STARTED_TOTAL,
            "saturated_total": _LLM_TOKENIZER_SATURATED_TOTAL,
            "timeout_total": _LLM_TOKENIZER_TIMEOUT_TOTAL,
            "failure_total": _LLM_TOKENIZER_FAILURE_TOTAL,
        }


def _log_tokenizer_warning(message: str, *args: Any) -> None:
    global _LLM_TOKENIZER_LAST_WARNING_AT

    now = time.monotonic()
    with _LLM_TOKENIZER_STATS_LOCK:
        if now - _LLM_TOKENIZER_LAST_WARNING_AT < _LLM_TOKENIZER_WARNING_INTERVAL_SECONDS:
            return
        _LLM_TOKENIZER_LAST_WARNING_AT = now
    log.warning(message, *args)


async def _run_bounded_tokenizer_call(
    call: Callable[[], int],
    *,
    fallback: int,
) -> tuple[int, bool]:
    """Run synchronous tokenizer CPU work without blocking the event loop.

    A timed-out tokenizer thread deliberately keeps its slot until the real
    call exits.  This prevents cancellation-resistant tokenizer work from
    creating an unbounded number of threads while later requests immediately
    fall back to a conservative estimate.
    """

    global _LLM_TOKENIZER_FAILURE_TOTAL
    global _LLM_TOKENIZER_SATURATED_TOTAL
    global _LLM_TOKENIZER_STARTED_TOTAL
    global _LLM_TOKENIZER_TIMEOUT_TOTAL

    if not _LLM_TOKENIZER_THREAD_SLOTS.acquire(blocking=False):
        with _LLM_TOKENIZER_STATS_LOCK:
            _LLM_TOKENIZER_SATURATED_TOTAL += 1
        _log_tokenizer_warning(
            "LLM tokenizer capacity exhausted; using conservative estimate"
        )
        return fallback, False

    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[int] = loop.create_future()
    call_token = object()
    with _LLM_TOKENIZER_STATS_LOCK:
        _LLM_TOKENIZER_STARTED_TOTAL += 1
        _LLM_TOKENIZER_ACTIVE_STARTED[call_token] = time.monotonic()

    def _settle(result: int | None = None, error: BaseException | None = None) -> None:
        if result_future.done():
            return
        if error is not None:
            result_future.set_exception(error)
        else:
            result_future.set_result(max(0, int(result or 0)))

    def _worker() -> None:
        try:
            result = call()
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(_settle, None, exc)
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(_settle, result, None)
            except RuntimeError:
                pass
        finally:
            with _LLM_TOKENIZER_STATS_LOCK:
                _LLM_TOKENIZER_ACTIVE_STARTED.pop(call_token, None)
            _LLM_TOKENIZER_THREAD_SLOTS.release()

    try:
        threading.Thread(
            target=_worker,
            name="llm-tokenizer",
            daemon=True,
        ).start()
    except RuntimeError:
        with _LLM_TOKENIZER_STATS_LOCK:
            _LLM_TOKENIZER_ACTIVE_STARTED.pop(call_token, None)
            _LLM_TOKENIZER_FAILURE_TOTAL += 1
        _LLM_TOKENIZER_THREAD_SLOTS.release()
        log.exception("Unable to start bounded LLM tokenizer thread")
        return fallback, False

    try:
        done, _ = await asyncio.wait(
            {result_future},
            timeout=_LLM_TOKENIZER_THREAD_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        result_future.cancel()
        raise
    if not done:
        result_future.cancel()
        with _LLM_TOKENIZER_STATS_LOCK:
            _LLM_TOKENIZER_TIMEOUT_TOTAL += 1
        _log_tokenizer_warning(
            "LLM tokenizer exceeded %.2fs; using conservative estimate",
            _LLM_TOKENIZER_THREAD_TIMEOUT_SECONDS,
        )
        return fallback, False
    try:
        return result_future.result(), True
    except BaseException as exc:
        with _LLM_TOKENIZER_STATS_LOCK:
            _LLM_TOKENIZER_FAILURE_TOTAL += 1
        _log_tokenizer_warning(
            "LLM tokenizer failed (%s: %s); using conservative estimate",
            type(exc).__name__,
            exc,
        )
        return fallback, False


def _observe_llm_task(task: asyncio.Future[Any]) -> None:
    _LLM_ORPHAN_TASKS.discard(task)
    _LLM_ORPHAN_STARTED.pop(task, None)
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def _track_llm_orphan(task: asyncio.Future[Any]) -> None:
    if task.done():
        _observe_llm_task(task)
        return
    _LLM_ORPHAN_TASKS.add(task)
    _LLM_ORPHAN_STARTED.setdefault(task, time.monotonic())
    task.add_done_callback(_observe_llm_task)


def _observe_llm_cleanup_task(task: asyncio.Future[Any]) -> None:
    _LLM_CLEANUP_TASKS.discard(task)
    _LLM_CLEANUP_STARTED.pop(task, None)
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def _track_llm_cleanup_task(task: asyncio.Future[Any]) -> None:
    """Observe best-effort stream cleanup without counting it as a permit leak.

    Stream ``aclose()`` is scheduled after the provider request has yielded its
    response and the request coroutine can release the real LLM semaphore.
    Treating that cleanup task as a request orphan could therefore mark the
    process fatal even though all request permits were available.
    """

    if task.done():
        _observe_llm_cleanup_task(task)
        return
    _LLM_CLEANUP_TASKS.add(task)
    _LLM_CLEANUP_STARTED.setdefault(task, time.monotonic())
    task.add_done_callback(_observe_llm_cleanup_task)


async def flush_llm_request_tasks(*, timeout_seconds: float = 15.0) -> None:
    """Cancel and join cancellation-resistant provider calls at shutdown."""

    tasks = {
        task
        for task in (*_LLM_ORPHAN_TASKS, *_LLM_CLEANUP_TASKS)
        if not task.done()
    }
    for task in tasks:
        task.cancel()
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=max(0.0, timeout_seconds))
    for task in done:
        if task in _LLM_ORPHAN_TASKS:
            _observe_llm_task(task)
        if task in _LLM_CLEANUP_TASKS:
            _observe_llm_cleanup_task(task)
    if pending:
        log.error("%d LLM request task(s) ignored shutdown cancellation", len(pending))


async def close_llm_clients() -> None:
    """Close LiteLLM's cached HTTP clients on shutdown/reconfiguration exit."""

    close = getattr(litellm, "close_litellm_async_clients", None)
    if not callable(close):
        return
    result = close()
    if isawaitable(result):
        await result


def llm_resource_health_snapshot() -> dict[str, Any]:
    now = time.monotonic()
    active_orphans = [task for task in _LLM_ORPHAN_TASKS if not task.done()]
    active_cleanup = [task for task in _LLM_CLEANUP_TASKS if not task.done()]
    oldest_age = max(
        (now - _LLM_ORPHAN_STARTED.get(task, now) for task in active_orphans),
        default=0.0,
    )
    semaphore_waiters = getattr(_LLM_REQUEST_SEMAPHORE, "_waiters", None)
    orphan_count = len(active_orphans)
    oldest_cleanup_age = max(
        (now - _LLM_CLEANUP_STARTED.get(task, now) for task in active_cleanup),
        default=0.0,
    )
    tokenizer = _tokenizer_stats_snapshot()
    tokenizer_fatal = bool(
        tokenizer["active"] >= _LLM_TOKENIZER_THREAD_CAPACITY
        and tokenizer["oldest_active_seconds"] >= _LLM_TOKENIZER_STALE_SECONDS
    )
    fatal = bool(
        orphan_count >= _LLM_REQUEST_CAPACITY
        or oldest_age >= _LLM_ORPHAN_MAX_AGE_SECONDS
        or tokenizer_fatal
    )
    return {
        "ok": not fatal,
        "fatal": fatal,
        "capacity": _LLM_REQUEST_CAPACITY,
        "available_permits": int(getattr(_LLM_REQUEST_SEMAPHORE, "_value", 0)),
        "semaphore_waiters": len(semaphore_waiters or ()),
        "orphan_count": orphan_count,
        "oldest_orphan_seconds": round(oldest_age, 3),
        "cleanup_task_count": len(active_cleanup),
        "oldest_cleanup_task_seconds": round(oldest_cleanup_age, 3),
        "tokenizer": tokenizer,
        "priority_gate": _LLM_PRIORITY_GATE.snapshot(),
    }


register_resource_health_provider("llm", llm_resource_health_snapshot)

_PROVIDER_CREDENTIAL_ENV_KEYS = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
    "dashscope": ("DASHSCOPE_API_KEY",),
    "volcengine": ("VOLCENGINE_API_KEY", "ARK_API_KEY"),
    "xai": ("XAI_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "groq": ("GROQ_API_KEY",),
}
_PROVIDER_OFFICIAL_HOSTS = {
    "gemini": {"generativelanguage.googleapis.com"},
    "openai": {"api.openai.com"},
    "anthropic": {"api.anthropic.com"},
    "openrouter": {"openrouter.ai"},
    "minimax": {"api.minimaxi.com", "api.minimax.io", "api.minimaxi.chat"},
    "deepseek": {"api.deepseek.com"},
    "moonshot": {"api.moonshot.cn", "api.moonshot.ai"},
    "dashscope": {"dashscope.aliyuncs.com"},
    "volcengine": {"ark.cn-beijing.volces.com"},
    "xai": {"api.x.ai"},
    "mistral": {"api.mistral.ai"},
    "groq": {"api.groq.com"},
}

# Reasoning models behind OpenAI-compatible gateways (MiniMax M3, GLM, ...)
# can inline `<think>...</think>` instead of using a separate reasoning field.
# Only exact, same-name pairs are reasoning markup: broad prefix matching here
# can silently remove ordinary output such as `<analysis_result>`.
_REASONING_TAG = r"(?:think|analysis|reasoning|scratchpad)"
_REASONING_TAG_RE = re.compile(
    rf"(?is)<\s*(?P<closing>/?)\s*(?P<tag>{_REASONING_TAG})"
    rf"(?=\s|/?>)(?P<attributes>[^>]*)>"
)


class _IncompleteInlineReasoningError(RuntimeError):
    """A provider emitted malformed inline reasoning markup."""

# Disable extra LiteLLM debug logs.
litellm.suppress_debug_info = True
litellm.set_verbose = False


class LLMService:
    """Unified LLM interface for main/vision/decision/moderation/compress/embed."""

    def __init__(
        self,
        main: ModelConfig,
        decision: ModelConfig,
        compress: ModelConfig | None = None,
        *,
        moderation: ModelConfig | None = None,
        vision: ModelConfig | None = None,
        skill: ModelConfig | None = None,
        embed: EmbedConfig | None = None,
        max_context_tokens: int | None = None,
        context_window_mode: str | None = None,
        business_context_tokens: int | None = None,
        context_reserve_tokens: int | None = None,
    ) -> None:
        self.main = main
        self.vision_config = vision or main
        self.decision_config = decision
        self.moderation_config = moderation or decision
        self.compress_config = compress or main
        # Tool-calling (skills) endpoints; falls back to ``main``.
        self.skill_config = skill or main
        self.embed_config = embed or EmbedConfig()
        self.max_context_tokens = max(0, int(max_context_tokens or 0))
        # 业务预算（运行时可配置；默认 272Ki 总窗口 / 32Ki 预留）。
        self.business_context_tokens = model_limits_module.configured_business_tokens(
            SimpleNamespace(
                context_budget_tokens=business_context_tokens,
                max_context_tokens=self.max_context_tokens,
            )
        )
        self.context_reserve_tokens = model_limits_module.configured_reserve_tokens(
            SimpleNamespace(
                context_reserve_tokens=context_reserve_tokens,
                context_budget_tokens=self.business_context_tokens,
            )
        )
        # ``auto``（默认）自动发现模型真实窗口；``fixed`` 不查元数据。两者都受
        # 业务预算约束（见 ``input_token_budget``）。
        self.context_window_mode = (
            CONTEXT_MODE_FIXED
            if str(context_window_mode or "").strip().lower() == CONTEXT_MODE_FIXED
            else model_limits_module.CONTEXT_MODE_AUTO
        )
        # Endpoint -> monotonic deadline until which it is known to answer the
        # tool stage with an apology string instead of a tool call.
        self._tool_incapable: dict[tuple[str, str, str], float] = {}
        # Chat endpoints that just answered HTTP 200 with a failure notice, keyed
        # like ``_tool_incapable`` and stamped with a loop deadline.  A degraded
        # bridge is skipped while the stamp lasts so it cannot add its full
        # latency to every single reply.
        self._chat_degraded_until: dict[tuple[str, str, str], float] = {}
        # "这个 endpoint 的窗口未知、已按保守值降级"只提醒一次。
        self._unknown_window_logged: set[tuple[str, str, str]] = set()

    def reconfigure(
        self,
        main: ModelConfig,
        decision: ModelConfig,
        compress: ModelConfig | None = None,
        *,
        moderation: ModelConfig | None = None,
        vision: ModelConfig | None = None,
        skill: ModelConfig | None = None,
        embed: EmbedConfig | None = None,
        max_context_tokens: int | None = None,
        context_window_mode: str | None = None,
        business_context_tokens: int | None = None,
        context_reserve_tokens: int | None = None,
    ) -> None:
        """Replace model endpoints for requests started after this call."""
        self.main = main
        self.vision_config = vision or main
        self.decision_config = decision
        self.moderation_config = moderation or decision
        self.compress_config = compress or main
        self.skill_config = skill or main
        self.embed_config = embed or EmbedConfig()
        self.max_context_tokens = max(0, int(max_context_tokens or 0))
        self.business_context_tokens = model_limits_module.configured_business_tokens(
            SimpleNamespace(
                context_budget_tokens=business_context_tokens,
                max_context_tokens=self.max_context_tokens,
            )
        )
        self.context_reserve_tokens = model_limits_module.configured_reserve_tokens(
            SimpleNamespace(
                context_reserve_tokens=context_reserve_tokens,
                context_budget_tokens=self.business_context_tokens,
            )
        )
        if context_window_mode is not None:
            self.context_window_mode = (
                CONTEXT_MODE_FIXED
                if str(context_window_mode).strip().lower() == CONTEXT_MODE_FIXED
                else model_limits_module.CONTEXT_MODE_AUTO
            )

    @classmethod
    def _resolve_request_api_base(
        cls,
        *,
        provider: str,
        api_base: str | None,
        endpoint_path: str | None,
        model: str,
    ) -> str | None:
        base = str(api_base or "").strip()
        if not base:
            return None

        provider_norm = str(provider or "").strip().lower()
        if not provider_norm:
            provider_norm = str(model or "").partition("/")[0].strip().lower()

        if provider_norm == "anthropic":
            normalized_base = base.rstrip("/")
            lower_base = normalized_base.lower()
            if lower_base.endswith("/messages"):
                return normalized_base
            if lower_base.endswith("/v1"):
                # litellm's anthropic handler appends /v1/messages itself;
                # keeping /v1 here produces /v1/v1/messages.
                return normalized_base[: -len("/v1")]
            return normalized_base

        if provider_norm != "gemini":
            return base

        normalized_base = base.rstrip("/")
        normalized_endpoint = f"/{str(endpoint_path or '/v1beta/models').strip().strip('/')}"
        version_suffix = normalized_endpoint[: -len("/models")] if normalized_endpoint.endswith("/models") else ""
        lower_base = normalized_base.lower()
        lower_endpoint = normalized_endpoint.lower()
        lower_version_suffix = version_suffix.lower()

        if lower_base.endswith(lower_endpoint):
            return normalized_base[: -len("/models")]
        if lower_version_suffix and lower_base.endswith(lower_version_suffix):
            return normalized_base
        if version_suffix:
            return f"{normalized_base}{version_suffix}"
        return normalized_base

    @classmethod
    def _build_chat_kwargs(
        cls,
        cfg: ChatEndpointConfig,
        *,
        stream: bool | None = None,
        exclude_params: frozenset[str] | set[str] = frozenset(),
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "timeout": max(1.0, float(getattr(cfg, "timeout_sec", 12.0) or 12.0)),
        }
        kwargs.update(cls._custom_request_kwargs(cfg))
        if bool(getattr(cfg, "stream", False)) if stream is None else bool(stream):
            kwargs["stream"] = True
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        resolved_api_base = cls._resolve_request_api_base(
            provider=getattr(cfg, "provider", ""),
            api_base=cfg.api_base,
            endpoint_path=getattr(cfg, "endpoint_path", "/chat/completions"),
            model=cfg.model,
        )
        if resolved_api_base:
            kwargs["api_base"] = resolved_api_base
        for name in exclude_params:
            kwargs.pop(name, None)
        return kwargs

    @classmethod
    def _build_responses_kwargs(
        cls,
        cfg: ChatEndpointConfig,
        *,
        stream: bool | None = None,
        exclude_params: frozenset[str] | set[str] = frozenset(),
    ) -> dict[str, Any]:
        model = str(cfg.model or "").strip()
        provider = str(getattr(cfg, "provider", "") or "").strip().lower()
        # Deployment note: this deployment stores its models with an explicit
        # ``openai/`` prefix (see bot/config.py _build_chat_config).  The
        # Responses endpoint needs that same prefix so litellm selects the
        # OpenAI-compatible transport; without it the call is routed as a
        # native Gemini request and fails.
        if provider in {"openai", "openai_compatible"}:
            if not model.lower().startswith("openai/"):
                model = f"openai/{model}"

        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": cfg.temperature,
            "max_output_tokens": cfg.max_tokens,
            "timeout": max(1.0, float(getattr(cfg, "timeout_sec", 12.0) or 12.0)),
        }
        kwargs.update(cls._custom_request_kwargs(cfg))
        if bool(getattr(cfg, "stream", False)) if stream is None else bool(stream):
            kwargs["stream"] = True
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        resolved_api_base = cls._resolve_request_api_base(
            provider=provider,
            api_base=cfg.api_base,
            endpoint_path=getattr(cfg, "endpoint_path", "/chat/completions"),
            model=cfg.model,
        )
        if resolved_api_base:
            kwargs["api_base"] = resolved_api_base
        responses_param_names = {
            "temperature": "temperature",
            "max_tokens": "max_output_tokens",
            "top_p": "top_p",
        }
        for name in exclude_params:
            kwargs.pop(responses_param_names.get(name, name), None)
        return kwargs

    # Parameters that identify/control the request are always derived from
    # the validated model config and call site. Provider-specific JSON may add
    # body fields, including endpoint-native reasoning fields, without being
    # able to redirect credentials or budgets.
    _PROTECTED_REQUEST_PARAMS = frozenset(
        {
            "model",
            "messages",
            "input",
            "provider",
            "api_key",
            "api_base",
            "custom_llm_provider",
            "endpoint_path",
            "chat_endpoint",
            "stream",
            "timeout",
            "temperature",
            "max_tokens",
            "max_output_tokens",
            "max_completion_tokens",
            "base_url",
            "num_retries",
            "tools",
            "tool_choice",
            "drop_params",
            "allowed_openai_params",
        }
    )

    @classmethod
    def _custom_request_params(cls, cfg: ChatEndpointConfig | EmbedEndpointConfig) -> dict[str, Any]:
        raw = getattr(cfg, "request_params", None)
        if not isinstance(raw, Mapping):
            return {}
        params: dict[str, Any] = {}
        for name, value in raw.items():
            clean_name = str(name).strip()
            if clean_name and clean_name.lower() not in cls._PROTECTED_REQUEST_PARAMS:
                params[clean_name] = value
        return params

    @classmethod
    def _uses_openai_compatible_transport(cls, cfg: ChatEndpointConfig | EmbedEndpointConfig) -> bool:
        """Whether LiteLLM will route this endpoint through an OpenAI-style body."""

        provider = str(getattr(cfg, "provider", "") or "").strip().lower()
        model_prefix = str(getattr(cfg, "model", "") or "").partition("/")[0].strip().lower()
        return provider in {"openai", "openai_compatible"} or model_prefix == "openai"

    @classmethod
    def _custom_request_kwargs(cls, cfg: ChatEndpointConfig | EmbedEndpointConfig) -> dict[str, Any]:
        """Build LiteLLM kwargs while preserving custom fields in the HTTP body.

        LiteLLM validates recognized-looking fields such as ``thinking`` and
        ``reasoning_effort`` against each model's capability list. OpenAI-style
        ``extra_body`` bypasses that adapter validation and is merged into the
        final JSON sent by the compatible gateway.
        """

        params = cls._custom_request_params(cfg)
        if not params or not cls._uses_openai_compatible_transport(cfg):
            return params
        return {"extra_body": params}

    @classmethod
    def _build_embed_kwargs(cls, cfg: EmbedEndpointConfig, texts: list[str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "input": texts,
            "timeout": max(1.0, float(getattr(cfg, "timeout_sec", 10.0) or 10.0)),
        }
        kwargs.update(cls._custom_request_kwargs(cfg))
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        resolved_api_base = cls._resolve_request_api_base(
            provider=getattr(cfg, "provider", ""),
            api_base=cfg.api_base,
            endpoint_path=getattr(cfg, "endpoint_path", "/v1beta/models"),
            model=cfg.model,
        )
        if resolved_api_base:
            kwargs["api_base"] = resolved_api_base
        return kwargs

    @staticmethod
    def _chat_candidates(cfg: ModelConfig) -> list[ChatEndpointConfig]:
        candidates = [
            ChatEndpointConfig(
                model=cfg.model,
                provider=getattr(cfg, "provider", ""),
                api_key=cfg.api_key,
                api_base=cfg.api_base,
                stream=cfg.stream,
                chat_endpoint=getattr(cfg, "chat_endpoint", "chat_completions"),
                endpoint_path=getattr(cfg, "endpoint_path", "/chat/completions"),
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                timeout_sec=cfg.timeout_sec,
                retry_attempts=cfg.retry_attempts,
                retry_backoff_sec=cfg.retry_backoff_sec,
                retry_timeout_multiplier=cfg.retry_timeout_multiplier,
                total_deadline_sec=getattr(cfg, "total_deadline_sec", 0.0),
                request_params=dict(getattr(cfg, "request_params", {}) or {}),
            ),
            *cfg.fallbacks,
        ]
        return candidates[:_LLM_MAX_CANDIDATES]

    @staticmethod
    def _embed_candidates(cfg: EmbedConfig) -> list[EmbedEndpointConfig]:
        candidates = [
            EmbedEndpointConfig(
                model=cfg.model,
                provider=getattr(cfg, "provider", ""),
                api_key=cfg.api_key,
                api_base=cfg.api_base,
                endpoint_path=getattr(cfg, "endpoint_path", "/v1beta/models"),
                timeout_sec=cfg.timeout_sec,
                retry_attempts=cfg.retry_attempts,
                retry_backoff_sec=cfg.retry_backoff_sec,
                retry_timeout_multiplier=cfg.retry_timeout_multiplier,
                total_deadline_sec=getattr(cfg, "total_deadline_sec", 0.0),
                request_params=dict(getattr(cfg, "request_params", {}) or {}),
            ),
            *cfg.fallbacks,
        ]
        return candidates[:_LLM_MAX_CANDIDATES]

    @staticmethod
    def chat_configuration_issue(cfg: ChatEndpointConfig) -> str:
        """Return a deterministic credential issue without probing the provider."""
        if str(cfg.api_key or "").strip():
            return ""

        provider = str(getattr(cfg, "provider", "") or "").strip().lower()
        model_prefix = str(cfg.model or "").partition("/")[0].strip().lower()
        if provider != "openai_compatible" and model_prefix in _PROVIDER_CREDENTIAL_ENV_KEYS:
            provider = model_prefix

        ambient_keys = _PROVIDER_CREDENTIAL_ENV_KEYS.get(provider)
        if ambient_keys is None:
            return ""
        if any(str(os.getenv(name, "") or "").strip() for name in ambient_keys):
            return ""

        api_base = str(cfg.api_base or "").strip()
        if api_base:
            hostname = str(urlparse(api_base).hostname or "").strip().lower()
            if hostname not in _PROVIDER_OFFICIAL_HOSTS.get(provider, set()):
                # Custom gateways may intentionally be keyless.
                return ""
        return f"{provider} provider has no API key"

    @staticmethod
    def _strip_inline_reasoning(text: str) -> str:
        """Remove inline reasoning markup that some providers leave in content.

        Only a complete, same-name block is safe to remove.  If an opening tag
        remains after that pass, do not guess which later text is reasoning:
        returning a sliced prefix can send a visibly corrupted answer.  Raise
        so the normal LLM retry/fallback path obtains a fresh response instead.
        """
        if not text or "<" not in text:
            return text

        parts: list[str] = []
        open_tags: list[str] = []
        cursor = 0
        for match in _REASONING_TAG_RE.finditer(text):
            if not open_tags:
                parts.append(text[cursor : match.start()])

            tag = str(match.group("tag") or "").lower()
            closing = bool(match.group("closing"))
            self_closing = str(match.group("attributes") or "").rstrip().endswith("/")
            if self_closing:
                cursor = match.end()
                continue
            if closing:
                if open_tags:
                    if open_tags[-1] != tag:
                        raise _IncompleteInlineReasoningError(
                            "mismatched inline reasoning markup"
                        )
                    open_tags.pop()
            else:
                open_tags.append(tag)
            cursor = match.end()

        if open_tags:
            raise _IncompleteInlineReasoningError("incomplete inline reasoning markup")
        parts.append(text[cursor:])
        return "".join(parts).strip()

    @staticmethod
    def _normalize_content_text(content: Any) -> str:
        """Normalize text across providers that return different structures."""
        if isinstance(content, str):
            return LLMService._strip_inline_reasoning(content)
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type", "") or "").strip().lower()
                if item_type in {"text", "output_text", "input_text"}:
                    txt = item.get("text", "")
                    if isinstance(txt, dict):
                        txt = txt.get("value", "")
                    txt = str(txt or "").strip()
                    if txt:
                        parts.append(txt)
                    continue
                if item_type == "refusal":
                    refusal = str(item.get("refusal", "") or "").strip()
                    if refusal:
                        parts.append(refusal)
                    continue
            return LLMService._strip_inline_reasoning("\n".join(parts))
        return str(content or "")

    @staticmethod
    def _get_value(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _string_value(value: Any) -> str:
        if value is None:
            return ""
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, str):
            return enum_value
        return str(value)

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _append_stream_piece(existing: str, piece: str) -> str:
        """Streaming deltas are raw fragments: concatenate verbatim.

        Never apply prefix/suffix dedup heuristics here — they swallow
        legitimate repeats ("200" + "0" for 2000, repeated URL escapes).
        Authoritative full texts arrive via *.done events and replace the
        slot instead of being merged.
        """
        if not piece:
            return existing
        if not existing:
            return piece
        return existing + piece

    @classmethod
    def _merge_stream_content_slot(
        cls,
        merged: dict[tuple[int, int], list[str]],
        *,
        output_index: Any,
        content_index: Any,
        piece: str,
        replace: bool = False,
    ) -> None:
        if not piece:
            return
        slot = (cls._coerce_int(output_index), cls._coerce_int(content_index))
        if replace:
            merged[slot] = [piece]
            return
        merged.setdefault(slot, []).append(piece)

    @classmethod
    def _replace_stream_output_text(
        cls,
        merged: dict[tuple[int, int], list[str]],
        *,
        output_index: Any,
        text: str,
    ) -> None:
        """output_item.done carries the full joined text of one output item."""
        if not text:
            return
        out_idx = cls._coerce_int(output_index)
        for slot in [s for s in merged if s[0] == out_idx]:
            del merged[slot]
        merged[(out_idx, 0)] = [text]

    @staticmethod
    def _joined_stream_content_slots(
        merged: dict[tuple[int, int], list[str]],
    ) -> str:
        return "".join("".join(parts) for _, parts in sorted(merged.items()))

    @classmethod
    def _stream_delta_text(cls, delta: Any) -> str:
        content = cls._get_value(delta, "content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text_value = item.get("text", "")
                    if isinstance(text_value, dict):
                        text_value = text_value.get("value", "")
                    parts.append(str(text_value or ""))
                    continue
                if "text" in item:
                    parts.append(str(item.get("text", "") or ""))
            return "".join(parts)
        text = cls._get_value(delta, "text")
        if text is None:
            return ""
        return str(text)

    @classmethod
    def _merge_stream_tool_calls(
        cls,
        merged: list[dict[str, Any]],
        delta_tool_calls: Any,
        *,
        replace: bool = False,
    ) -> None:
        """Accumulate streamed tool calls.

        replace=False: fragments append verbatim (Chat Completions deltas,
        function_call_arguments.delta). replace=True: the event carries the
        full authoritative value (output_item.done, arguments.done) and
        overwrites what the fragments built up.
        """
        if not delta_tool_calls:
            return

        for raw_call in delta_tool_calls:
            try:
                index = int(cls._get_value(raw_call, "index", len(merged)) or 0)
            except (TypeError, ValueError):
                index = len(merged)

            while len(merged) <= index:
                merged.append(
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                )

            entry = merged[index]
            call_id = str(cls._get_value(raw_call, "id", "") or "")
            if call_id:
                entry["id"] = call_id

            call_type = cls._string_value(cls._get_value(raw_call, "type", "") or "")
            if call_type:
                entry["type"] = call_type

            raw_function = cls._get_value(raw_call, "function", {}) or {}
            function = entry.setdefault("function", {"name": "", "arguments": ""})

            # Responses-API items carry name/arguments at the top level;
            # Chat-Completions deltas nest them under "function".
            name_piece = str(
                cls._get_value(raw_function, "name", "")
                or cls._get_value(raw_call, "name", "")
                or ""
            )
            if name_piece:
                if replace:
                    function["name"] = name_piece
                else:
                    function["name"] = cls._append_stream_piece(
                        str(function.get("name", "") or ""), name_piece
                    )

            args_piece = cls._get_value(raw_function, "arguments", None)
            if args_piece in (None, ""):
                args_piece = cls._get_value(raw_call, "arguments", "")
            if args_piece is None:
                args_piece = ""
            if isinstance(args_piece, dict):
                args_piece = json.dumps(args_piece, ensure_ascii=False)
            args_piece = str(args_piece)
            if args_piece:
                if replace:
                    function["arguments"] = args_piece
                else:
                    function["arguments"] = cls._append_stream_piece(
                        str(function.get("arguments", "") or ""),
                        args_piece,
                    )

    @classmethod
    def _coerce_usage(cls, usage: Any) -> SimpleNamespace | None:
        if not usage:
            return None
        prompt_tokens = cls._coerce_int(cls._get_value(usage, "prompt_tokens", 0))
        if not prompt_tokens:
            prompt_tokens = cls._coerce_int(cls._get_value(usage, "input_tokens", 0))

        completion_tokens = cls._coerce_int(cls._get_value(usage, "completion_tokens", 0))
        if not completion_tokens:
            completion_tokens = cls._coerce_int(cls._get_value(usage, "output_tokens", 0))

        total_tokens = cls._coerce_int(cls._get_value(usage, "total_tokens", 0))
        if not total_tokens and (prompt_tokens or completion_tokens):
            total_tokens = prompt_tokens + completion_tokens

        if not prompt_tokens and not completion_tokens and not total_tokens:
            return None

        # 缓存与思考 token：网关的字段名有 OpenAI / Anthropic / DeepSeek 三套写法，
        # 逐个兜底取（取到非 0 就用）。成本看板靠这几个字段判断"有没有吃到缓存"
        # 和"关思考到底生效没有"，所以宁可多试几个键名。
        def pick(*names: str) -> int:
            for name in names:
                value = cls._coerce_int(cls._get_value(usage, name, 0))
                if value:
                    return value
            return 0

        nested_cache = cls._coerce_int(
            cls._get_value(cls._get_value(usage, "prompt_tokens_details", {}) or {}, "cached_tokens", 0)
        )
        nested_reasoning = cls._coerce_int(
            cls._get_value(cls._get_value(usage, "completion_tokens_details", {}) or {}, "reasoning_tokens", 0)
        )
        cached_tokens = max(
            pick("cached_tokens", "cache_read_input_tokens", "prompt_cache_hit_tokens"),
            nested_cache,
        )
        cache_write_tokens = pick("cache_creation_input_tokens", "cache_write_tokens")
        thinking_tokens = max(
            pick("completion_thinking_tokens", "thinking_tokens", "reasoning_tokens"),
            nested_reasoning,
        )
        return SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            thinking_tokens=thinking_tokens,
        )

    @classmethod
    def _normalize_tool_calls(cls, raw_tool_calls: Any) -> list[dict[str, Any]]:
        normalized_tool_calls: list[dict[str, Any]] = []
        if not raw_tool_calls:
            return normalized_tool_calls

        for idx, raw_call in enumerate(raw_tool_calls, start=1):
            function = cls._get_value(raw_call, "function", {}) or {}
            name = str(
                cls._get_value(function, "name", "") or cls._get_value(raw_call, "name", "") or ""
            ).strip()
            arguments = cls._get_value(function, "arguments", None)
            if arguments is None:
                arguments = cls._get_value(raw_call, "arguments", "")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            arguments = str(arguments or "")
            call_id = str(
                cls._get_value(raw_call, "id", "") or cls._get_value(raw_call, "call_id", "") or ""
            ).strip() or f"call_{idx}"
            call_type = cls._string_value(cls._get_value(raw_call, "type", "") or "function").strip() or "function"
            if not name and not arguments:
                continue
            normalized_tool_calls.append(
                {
                    "id": call_id,
                    "type": call_type,
                    "function": {
                        "name": name,
                        "arguments": arguments,
                    },
                }
            )
        return normalized_tool_calls

    @classmethod
    def _responses_text_piece(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(cls._responses_text_piece(item) for item in value)

        item_type = cls._string_value(cls._get_value(value, "type", "") or "").strip().lower()
        if item_type in {"output_text", "text", "input_text"}:
            return cls._responses_text_piece(cls._get_value(value, "text", ""))
        if item_type == "refusal":
            return str(cls._get_value(value, "refusal", "") or "")

        text_value = cls._get_value(value, "text", None)
        if text_value not in (None, ""):
            return cls._responses_text_piece(text_value)

        refusal_value = cls._get_value(value, "refusal", None)
        if refusal_value not in (None, ""):
            return str(refusal_value)

        value_text = cls._get_value(value, "value", None)
        if value_text not in (None, ""):
            return str(value_text)

        return ""

    @classmethod
    def _extract_responses_message(cls, response_obj: Any) -> SimpleNamespace | None:
        output_items = cls._get_value(response_obj, "output", None) or []
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        for item in output_items:
            item_type = cls._string_value(cls._get_value(item, "type", "") or "").strip().lower()
            if item_type == "message":
                content_parts.append(cls._responses_text_piece(cls._get_value(item, "content", None)))
                continue
            if item_type == "function_call":
                tool_calls.extend(cls._normalize_tool_calls([item]))
                continue
            if item_type in {"output_text", "text"}:
                text_piece = cls._responses_text_piece(item)
                if text_piece:
                    content_parts.append(text_piece)

        if not content_parts and not tool_calls:
            text_piece = cls._responses_text_piece(cls._get_value(response_obj, "text", None))
            if text_piece:
                content_parts.append(text_piece)

        content = "".join(content_parts)
        if not content.strip() and not tool_calls:
            return None

        return SimpleNamespace(
            content=content,
            tool_calls=tool_calls,
        )

    @classmethod
    def _build_model_response(
        cls,
        *,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        usage: SimpleNamespace | None = None,
    ) -> Any:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=content,
                        tool_calls=tool_calls or [],
                    )
                )
            ],
            usage=usage,
        )

    @classmethod
    def _normalize_response_object(cls, resp: Any) -> Any:
        usage = cls._coerce_usage(cls._get_value(resp, "usage"))
        choices = cls._get_value(resp, "choices", None) or []
        if choices:
            message = cls._get_value(choices[0], "message", None)
            if message is not None:
                return cls._build_model_response(
                    content=cls._normalize_content_text(cls._get_value(message, "content", "")),
                    tool_calls=cls._normalize_tool_calls(cls._get_value(message, "tool_calls", None)),
                    usage=usage,
                )

        message = cls._extract_responses_message(resp)
        if message is not None:
            return cls._build_model_response(
                content=message.content,
                tool_calls=message.tool_calls,
                usage=usage,
            )

        return resp

    @classmethod
    def _completed_stream_response(cls, stream_resp: Any, completed_response: Any) -> Any | None:
        completed = completed_response
        if completed is None:
            completed = cls._get_value(stream_resp, "completed_response", None)

        response_obj = cls._get_value(completed, "response", None)
        if response_obj is None and completed is not None:
            response_obj = completed
        if response_obj is None:
            return None

        normalized = cls._normalize_response_object(response_obj)
        choices = cls._get_value(normalized, "choices", None) or []
        if not choices:
            return None
        return normalized

    @staticmethod
    def _should_stream_upstream(label: str, cfg: ChatEndpointConfig) -> bool:
        return bool(getattr(cfg, "stream", False))

    # Request params a provider may reject; matched against error text so the
    # request can be retried without the offending param instead of failing
    # the whole candidate (e.g. kimi-k3: "invalid temperature: only 1 is
    # allowed for this model").
    _DROPPABLE_PARAMS = ("temperature", "max_tokens", "top_p")

    @classmethod
    def _unsupported_param_from_error(cls, exc: Exception) -> str:
        status_code = getattr(exc, "status_code", None)
        if status_code is not None and int(status_code) != 400:
            return ""
        message = str(exc or "").lower()
        if not any(
            marker in message
            for marker in ("invalid", "not support", "unsupported", "not allowed", "unknown parameter")
        ):
            return ""
        for param in cls._DROPPABLE_PARAMS:
            if param in message:
                return param
        return ""

    async def _consume_chat_stream(self, stream_resp: Any) -> Any:
        content_parts: dict[tuple[int, int], list[str]] = {}
        tool_calls: list[dict[str, Any]] = []
        usage: SimpleNamespace | None = None
        completed_response: Any = None

        try:
            async for chunk in stream_resp:
                if chunk is None:
                    continue

                event_type = self._string_value(self._get_value(chunk, "type", "") or "").strip().lower()
                if event_type.endswith("completed"):
                    completed_response = self._get_value(chunk, "response", None) or completed_response

                chunk_usage = self._coerce_usage(self._get_value(chunk, "usage"))
                if chunk_usage is not None:
                    usage = chunk_usage

                if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "delta", "") or ""),
                    )
                    continue

                if event_type == "response.output_text.done":
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "text", "") or ""),
                        replace=True,
                    )
                    continue

                if event_type == "response.refusal.done":
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "refusal", "") or ""),
                        replace=True,
                    )
                    continue

                if event_type == "response.content_part.done":
                    text_piece = self._responses_text_piece(self._get_value(chunk, "part", None))
                    if text_piece:
                        self._merge_stream_content_slot(
                            content_parts,
                            output_index=self._get_value(chunk, "output_index", 0),
                            content_index=self._get_value(chunk, "content_index", 0),
                            piece=text_piece,
                            replace=True,
                        )
                    continue

                if event_type == "response.output_item.added":
                    output_item = self._get_value(chunk, "item", None)
                    if self._string_value(self._get_value(output_item, "type", "") or "").strip().lower() == "function_call":
                        self._merge_stream_tool_calls(
                            tool_calls,
                            [
                                {
                                    "index": self._get_value(chunk, "output_index", 0),
                                    "id": self._get_value(output_item, "call_id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": self._get_value(output_item, "name", ""),
                                        "arguments": "",
                                    },
                                }
                            ],
                        )
                    continue

                if event_type == "response.output_item.done":
                    output_item = self._get_value(chunk, "item", None)
                    item_type = self._string_value(self._get_value(output_item, "type", "") or "").strip().lower()
                    if item_type == "function_call":
                        self._merge_stream_tool_calls(
                            tool_calls,
                            [
                                {
                                    "index": self._get_value(chunk, "output_index", 0),
                                    "id": self._get_value(output_item, "call_id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": self._get_value(output_item, "name", ""),
                                        "arguments": self._get_value(output_item, "arguments", ""),
                                    },
                                }
                            ],
                            replace=True,
                        )
                    else:
                        text_piece = self._responses_text_piece(self._get_value(output_item, "content", None))
                        if text_piece:
                            self._replace_stream_output_text(
                                content_parts,
                                output_index=self._get_value(chunk, "output_index", 0),
                                text=text_piece,
                            )
                    continue

                if event_type == "response.function_call_arguments.delta":
                    self._merge_stream_tool_calls(
                        tool_calls,
                        [
                            {
                                "index": self._get_value(chunk, "output_index", 0),
                                "type": "function",
                                "function": {
                                    "arguments": self._get_value(chunk, "delta", ""),
                                },
                            }
                        ],
                    )
                    continue

                if event_type == "response.function_call_arguments.done":
                    self._merge_stream_tool_calls(
                        tool_calls,
                        [
                            {
                                "index": self._get_value(chunk, "output_index", 0),
                                "type": "function",
                                "function": {
                                    "arguments": self._get_value(chunk, "arguments", ""),
                                },
                            }
                        ],
                        replace=True,
                    )
                    continue

                choices = self._get_value(chunk, "choices", None) or []
                if not choices:
                    continue

                choice = choices[0]
                delta = self._get_value(choice, "delta", None)
                if delta is None:
                    delta = self._get_value(choice, "message", None)
                if delta is None:
                    delta = choice

                delta_text = self._stream_delta_text(delta)
                if delta_text:
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=0,
                        content_index=0,
                        piece=delta_text,
                    )

                self._merge_stream_tool_calls(tool_calls, self._get_value(delta, "tool_calls"))
        except Exception:
            normalized = self._completed_stream_response(stream_resp, completed_response)
            if normalized is not None:
                return normalized
            raise

        normalized = self._completed_stream_response(stream_resp, completed_response)
        if normalized is not None:
            return normalized

        return self._build_model_response(
            content=self._joined_stream_content_slots(content_parts),
            tool_calls=self._normalize_tool_calls(tool_calls),
            usage=usage,
        )

    def _consume_chat_stream_sync(self, stream_resp: Any) -> Any:
        content_parts: dict[tuple[int, int], list[str]] = {}
        tool_calls: list[dict[str, Any]] = []
        usage: SimpleNamespace | None = None
        completed_response: Any = None

        try:
            for chunk in stream_resp:
                if chunk is None:
                    continue

                event_type = self._string_value(self._get_value(chunk, "type", "") or "").strip().lower()
                if event_type.endswith("completed"):
                    completed_response = self._get_value(chunk, "response", None) or completed_response

                chunk_usage = self._coerce_usage(self._get_value(chunk, "usage"))
                if chunk_usage is not None:
                    usage = chunk_usage

                if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "delta", "") or ""),
                    )
                    continue

                if event_type == "response.output_text.done":
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "text", "") or ""),
                        replace=True,
                    )
                    continue

                if event_type == "response.refusal.done":
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=self._get_value(chunk, "output_index", 0),
                        content_index=self._get_value(chunk, "content_index", 0),
                        piece=str(self._get_value(chunk, "refusal", "") or ""),
                        replace=True,
                    )
                    continue

                if event_type == "response.content_part.done":
                    text_piece = self._responses_text_piece(self._get_value(chunk, "part", None))
                    if text_piece:
                        self._merge_stream_content_slot(
                            content_parts,
                            output_index=self._get_value(chunk, "output_index", 0),
                            content_index=self._get_value(chunk, "content_index", 0),
                            piece=text_piece,
                            replace=True,
                        )
                    continue

                if event_type == "response.output_item.added":
                    output_item = self._get_value(chunk, "item", None)
                    if self._string_value(self._get_value(output_item, "type", "") or "").strip().lower() == "function_call":
                        self._merge_stream_tool_calls(
                            tool_calls,
                            [
                                {
                                    "index": self._get_value(chunk, "output_index", 0),
                                    "id": self._get_value(output_item, "call_id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": self._get_value(output_item, "name", ""),
                                        "arguments": "",
                                    },
                                }
                            ],
                        )
                    continue

                if event_type == "response.output_item.done":
                    output_item = self._get_value(chunk, "item", None)
                    item_type = self._string_value(self._get_value(output_item, "type", "") or "").strip().lower()
                    if item_type == "function_call":
                        self._merge_stream_tool_calls(
                            tool_calls,
                            [
                                {
                                    "index": self._get_value(chunk, "output_index", 0),
                                    "id": self._get_value(output_item, "call_id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": self._get_value(output_item, "name", ""),
                                        "arguments": self._get_value(output_item, "arguments", ""),
                                    },
                                }
                            ],
                            replace=True,
                        )
                    else:
                        text_piece = self._responses_text_piece(self._get_value(output_item, "content", None))
                        if text_piece:
                            self._replace_stream_output_text(
                                content_parts,
                                output_index=self._get_value(chunk, "output_index", 0),
                                text=text_piece,
                            )
                    continue

                if event_type == "response.function_call_arguments.delta":
                    self._merge_stream_tool_calls(
                        tool_calls,
                        [
                            {
                                "index": self._get_value(chunk, "output_index", 0),
                                "type": "function",
                                "function": {
                                    "arguments": self._get_value(chunk, "delta", ""),
                                },
                            }
                        ],
                    )
                    continue

                if event_type == "response.function_call_arguments.done":
                    self._merge_stream_tool_calls(
                        tool_calls,
                        [
                            {
                                "index": self._get_value(chunk, "output_index", 0),
                                "type": "function",
                                "function": {
                                    "arguments": self._get_value(chunk, "arguments", ""),
                                },
                            }
                        ],
                        replace=True,
                    )
                    continue

                choices = self._get_value(chunk, "choices", None) or []
                if not choices:
                    continue

                choice = choices[0]
                delta = self._get_value(choice, "delta", None)
                if delta is None:
                    delta = self._get_value(choice, "message", None)
                if delta is None:
                    delta = choice

                delta_text = self._stream_delta_text(delta)
                if delta_text:
                    self._merge_stream_content_slot(
                        content_parts,
                        output_index=0,
                        content_index=0,
                        piece=delta_text,
                    )

                self._merge_stream_tool_calls(tool_calls, self._get_value(delta, "tool_calls"))
        except Exception:
            normalized = self._completed_stream_response(stream_resp, completed_response)
            if normalized is not None:
                return normalized
            raise

        normalized = self._completed_stream_response(stream_resp, completed_response)
        if normalized is not None:
            return normalized

        return self._build_model_response(
            content=self._joined_stream_content_slots(content_parts),
            tool_calls=self._normalize_tool_calls(tool_calls),
            usage=usage,
        )

    @staticmethod
    def _preview_for_log(text: str, *, limit: int) -> tuple[str, bool]:
        """Render a single-line preview for logs and report whether it was truncated."""
        escaped = (text or "").replace("\r", "\\r").replace("\n", "\\n")
        if len(escaped) <= limit:
            return escaped, False
        return escaped[:limit], True

    @staticmethod
    def model_input_token_limit(cfg: ChatEndpointConfig) -> int:
        try:
            info = litellm.get_model_info(model=cfg.model)
            return max(0, int((info or {}).get("max_input_tokens") or 0))
        except Exception:
            return 0

    def configured_endpoints(self) -> list[ChatEndpointConfig]:
        """本实例上**已配置**的对话 endpoint（含 fallback），供元数据预热用。"""

        endpoints: list[ChatEndpointConfig] = []
        seen: set[tuple[str, str, str]] = set()
        for cfg in (
            self.main,
            self.skill_config,
            self.decision_config,
            self.moderation_config,
            self.vision_config,
            self.compress_config,
        ):
            if cfg is None or not getattr(cfg, "model", ""):
                continue
            for candidate in self._chat_candidates(cfg):
                key = self._tool_endpoint_key(candidate)
                if key in seen:
                    continue
                seen.add(key)
                endpoints.append(candidate)
        return endpoints

    async def refresh_model_limits(self, *, force: bool = False) -> dict[str, Any]:
        """预热/刷新窗口元数据（启动与路由变更时调用）。

        **永远不阻塞主链路**：失败只记日志；拿不到就按保守降级走。
        """

        try:
            return await model_limits_module.MODEL_LIMITS.refresh(
                self.configured_endpoints(),
                force=force,
            )
        except Exception:
            log.exception("model metadata refresh failed (continuing conservatively)")
            return {}

    def endpoint_limits(self, cfg: ChatEndpointConfig | None = None) -> ModelLimits | None:
        """当前生效的**模型侧**窗口结论（业务 272Ki 上限在 :meth:`input_token_budget`）。

        ``None`` = 没有可用结论（调用方按业务预算兜底）。
        """

        target = cfg or self.main
        if self.context_window_mode == CONTEXT_MODE_FIXED:
            # 兼容字段：0/缺项一律按业务预算兜底——绝不"关闭上限"（那就是无限预算版）。
            return model_limits_module.MODEL_LIMITS.legacy_limits(
                target,
                total_window=self.max_context_tokens
                or model_limits_module.BUSINESS_CONTEXT_WINDOW_TOKENS,
            )
        limits = model_limits_module.MODEL_LIMITS.resolve(
            target,
            legacy_total_window=self.max_context_tokens or None,
        )
        if not limits.known:
            self._log_unknown_window_once(target, limits)
        return limits

    def _log_unknown_window_once(
        self,
        cfg: ChatEndpointConfig,
        limits: ModelLimits,
    ) -> None:
        """未知模型的保守降级只提醒一次（每个 endpoint 一条），不刷日志。"""

        key = self._tool_endpoint_key(cfg)
        if key in self._unknown_window_logged:
            return
        self._unknown_window_logged.add(key)
        log.warning(
            "LLM context window unknown, using conservative fallback | model=%s | "
            "provider=%s | host=%s | source=%s | total_window=%s | detail=%s",
            cfg.model,
            str(getattr(cfg, "provider", "") or "-"),
            model_limits_module.redact_base(getattr(cfg, "api_base", "")),
            limits.source,
            limits.total_window or "-",
            limits.detail or "-",
        )

    def input_token_budget(self, cfg: ChatEndpointConfig | None = None) -> int:
        """这个 endpoint 本次请求允许的 prompt 上限（**业务口径**）。

        取 ``min(模型可用输入, 272Ki − 预留)``：正常情况恒为 ``245760``；模型更小就跟着
        更小；本次输出需求更大（``max_tokens > 32Ki``）就更紧。模型真实窗口 1M/4M 不会
        让这一轮填满百万窗口。``0`` 表示没有可用输入空间，调用方必须拒绝
        该候选，绝不能把它解释成关闭预算保护。
        """

        target = cfg or self.main
        limits = self.endpoint_limits(target)
        reserve = max(
            MIN_OUTPUT_RESERVE_TOKENS,
            max(0, int(getattr(target, "max_tokens", 0) or 0)),
        )
        if limits is None:
            return 0
        return limits.business_input_budget(
            output_reserve=reserve,
            business_tokens=self.business_context_tokens,
            reserve_tokens=self.context_reserve_tokens,
        )

    def _context_window_total(self, cfg: ChatEndpointConfig | None = None) -> int:
        return self.input_token_budget(cfg)

    @staticmethod
    def _format_prompt_usage(used_tokens: int, total_tokens: int) -> str:
        if total_tokens > 0:
            return f"{max(0, int(used_tokens))}/{int(total_tokens)}"
        return f"{max(0, int(used_tokens))}/?"

    @staticmethod
    def _conservative_prompt_token_estimate(
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[int, int]:
        """Return ``(conservative_upper_bound, source_characters)``.

        The estimate is the **same** CJK-aware口径 the context assemblers use
        (``bot.utils.tokens.estimate_text_tokens``: 1 token/CJK char, ~3 chars per
        token otherwise) plus a per-message allowance.  It is an upper bound, not
        a model-exact count — callers must not label it ``exact``.
        """

        raw_characters = sum(len(str(msg.get("content", ""))) for msg in messages)
        if tools:
            raw_characters += len(str(tools))
        estimate = 0
        for message in messages:
            estimate += conservative_message_tokens(message)
        estimate += estimate_tools_tokens(tools)
        return max(1, estimate), raw_characters

    def _count_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        cfg: ChatEndpointConfig | None = None,
    ) -> tuple[int, bool]:
        """Return a non-blocking estimate for synchronous callers.

        Exact LiteLLM tokenization is intentionally available only through the
        async method below so no caller can accidentally run tokenizer CPU on
        the asyncio event-loop thread.
        """

        del cfg
        estimate, _raw = self._conservative_prompt_token_estimate(messages, tools)
        return estimate, False

    async def _count_prompt_tokens_async(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        cfg: ChatEndpointConfig | None = None,
    ) -> tuple[int, bool]:
        """``(tokens, exact)``：先试真实分词器（离线程 + 有界超时），失败/超时回退
        到与装配链路**同一个**保守上界，并且**如实标记** ``exact=False``。

        2026-10-04 的事故就是这里：载荷 ≥100K 字符时直接返回"一字符一 token"的估算
        并标成 ``exact=True``，于是最终闸门拿一个比装配口径大 3 倍的数去比 272K，把
        **主模型和备用都 skip 掉**。保守上界不是 exact——这一点必须是硬规矩。
        """

        fallback, raw_characters = self._conservative_prompt_token_estimate(
            messages,
            tools,
        )
        if raw_characters > _LLM_TOKENIZER_MAX_SOURCE_CHARACTERS:
            # 载荷已经大到分词器一定跑不完（而且它只会在共享线程里占着槽位）。
            return fallback, False

        kwargs: dict[str, Any] = {
            "model": (cfg.model if cfg is not None else self.main.model),
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        return await _run_bounded_tokenizer_call(
            lambda: int(litellm.token_counter(**kwargs)),
            fallback=fallback,
        )

    def fit_messages_to_budget(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        budget_tokens: int,
    ) -> FittedPayload:
        """按统一优先级把载荷裁进 ``budget_tokens``（保守口径，可加）。"""

        return fit_messages_to_input_budget(
            messages,
            message_tokens=conservative_message_tokens,
            tools_tokens=estimate_tools_tokens(tools),
            budget_tokens=budget_tokens,
        )

    def count_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        cfg: ChatEndpointConfig | None = None,
    ) -> int:
        return self._count_prompt_tokens(messages, tools=tools, cfg=cfg)[0]

    def prompt_usage_text(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        cfg: ChatEndpointConfig | None = None,
        used_tokens: int | None = None,
    ) -> str:
        used = self.count_prompt_tokens(messages, tools=tools, cfg=cfg) if used_tokens is None else int(used_tokens)
        total = self._context_window_total(cfg)
        return self._format_prompt_usage(used, total)

    @staticmethod
    def _label_cn(label: str) -> str:
        mapping = {
            "main": "main",
            "decision": "decision",
            "moderation": "moderation",
            "compress": "compress",
            "vision": "vision",
            "embed": "embed",
            "skill": "skill",
        }
        return mapping.get(label, label)

    @staticmethod
    def _retry_attempts(cfg: ChatEndpointConfig | EmbedEndpointConfig) -> int:
        return min(
            _LLM_MAX_RETRY_ATTEMPTS,
            max(1, int(getattr(cfg, "retry_attempts", 1) or 1)),
        )

    @staticmethod
    def _retry_backoff_seconds(cfg: ChatEndpointConfig | EmbedEndpointConfig, attempt: int) -> float:
        base = max(0.0, float(getattr(cfg, "retry_backoff_sec", 0.0) or 0.0))
        return base * max(1, attempt)

    @staticmethod
    def _attempt_timeout_seconds(
        cfg: ChatEndpointConfig | EmbedEndpointConfig,
        attempt: int,
    ) -> float:
        base = max(1.0, float(getattr(cfg, "timeout_sec", 12.0) or 12.0))
        multiplier = max(1.0, float(getattr(cfg, "retry_timeout_multiplier", 1.0) or 1.0))
        return min(
            _LLM_MAX_ATTEMPT_TIMEOUT_SECONDS,
            base * (multiplier ** max(0, attempt - 1)),
        )

    @staticmethod
    def _stage_deadline_seconds(
        label: str,
        cfg: ChatEndpointConfig | EmbedEndpointConfig | None = None,
    ) -> float:
        # A per-role total_deadline_sec (settings/Mini App) overrides the
        # built-in stage default; 0/unset keeps the default.
        override = float(getattr(cfg, "total_deadline_sec", 0.0) or 0.0)
        if override > 0:
            return override
        return stage_deadline_seconds(label)


def stage_deadline_seconds(label: str) -> float:
    """某个阶段的默认总预算（秒）。

    单一来源 = ``runtime_config.resources.llm_stage_deadlines``（默认表与改造前
    逐字相同：decision/moderation 35、embed 60、compress/vision 90、main/skill 120、
    synopsis 20、group_summary 15）。每次调用现取，所以改表立刻对下一次调用生效。
    未登记的阶段沿用 ``main`` 的量级（120s），不会凭空多出第三份默认值。
    """

    configured = policy_runtime.resources_policy().llm_stage_deadlines
    value = configured.get(str(label))
    if value is None:
        value = configured.get("main", 120.0)
    try:
        seconds = float(value)
    except (TypeError, ValueError):  # pragma: no cover - schema 已挡掉
        return 120.0
    return seconds if seconds > 0 else 120.0

    @staticmethod
    def _circuit_key(
        cfg: ChatEndpointConfig | EmbedEndpointConfig,
        *,
        stage: str,
    ) -> tuple[str, str, str, str]:
        return (
            "embed" if stage == "embed" else "chat",
            str(getattr(cfg, "provider", "") or ""),
            str(getattr(cfg, "model", "") or ""),
            str(getattr(cfg, "api_base", "") or ""),
        )

    @classmethod
    def _circuit_is_open(
        cls,
        cfg: ChatEndpointConfig | EmbedEndpointConfig,
        *,
        stage: str,
    ) -> bool:
        loop = asyncio.get_running_loop()
        states = _LLM_CIRCUITS.setdefault(loop, {})
        key = cls._circuit_key(cfg, stage=stage)
        failures, open_until = states.get(key, (0, 0.0))
        now = loop.time()
        if open_until > now:
            return True
        if open_until:
            states[key] = (0, 0.0)
        return False

    @classmethod
    def _record_circuit_success(
        cls,
        cfg: ChatEndpointConfig | EmbedEndpointConfig,
        *,
        stage: str,
    ) -> None:
        loop = asyncio.get_running_loop()
        _LLM_CIRCUITS.setdefault(loop, {}).pop(
            cls._circuit_key(cfg, stage=stage),
            None,
        )

    @classmethod
    def _record_circuit_failure(
        cls,
        cfg: ChatEndpointConfig | EmbedEndpointConfig,
        *,
        stage: str,
    ) -> None:
        loop = asyncio.get_running_loop()
        states = _LLM_CIRCUITS.setdefault(loop, {})
        key = cls._circuit_key(cfg, stage=stage)
        failures, _ = states.get(key, (0, 0.0))
        failures += 1
        open_until = 0.0
        if failures >= _LLM_CIRCUIT_FAILURE_THRESHOLD:
            open_until = loop.time() + _LLM_CIRCUIT_COOLDOWN_SECONDS
            log.error(
                "LLM circuit opened | stage=%s model=%s cooldown=%.0fs",
                stage,
                getattr(cfg, "model", ""),
                _LLM_CIRCUIT_COOLDOWN_SECONDS,
            )
        states[key] = (failures, open_until)

    @staticmethod
    async def _await_with_timeout(awaitable: Any, *, timeout_sec: float) -> Any:
        """Wait for an operation without letting cancellation delay the deadline.

        ``asyncio.wait_for`` waits until a cancelled child acknowledges
        cancellation.  Several HTTP streaming implementations can delay or
        swallow that cancellation, turning a nominal five second timeout into
        an unbounded wait.  This helper returns at the deadline, while still
        cancelling and observing the child in the background.
        """

        timeout = max(0.01, float(timeout_sec))
        task = asyncio.ensure_future(awaitable)

        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            _track_llm_orphan(task)
            raise
        if task in done:
            return task.result()

        task.cancel()
        _track_llm_orphan(task)
        raise asyncio.TimeoutError

    @staticmethod
    def _close_stream_best_effort(stream_resp: Any) -> None:
        """Close an upstream stream without extending the request deadline."""

        close = getattr(stream_resp, "aclose", None)
        if callable(close):
            try:
                result = close()
            except Exception:
                log.debug("LLM stream aclose failed", exc_info=True)
                return
            if asyncio.iscoroutine(result):
                active_cleanup = sum(
                    not task.done() for task in _LLM_CLEANUP_TASKS
                )
                if active_cleanup >= _LLM_CLEANUP_TASK_LIMIT:
                    # Cleanup does not own a request permit and must never be
                    # allowed to grow into an unbounded secondary task leak.
                    result.close()
                    log.warning(
                        "LLM stream cleanup task limit reached; dropping aclose"
                    )
                    return
                try:
                    task = asyncio.create_task(result, name="llm-stream-close")
                except RuntimeError:
                    result.close()
                    return

                def _observe_close(done_task: asyncio.Task[Any]) -> None:
                    try:
                        done_task.result()
                    except (asyncio.CancelledError, Exception):
                        log.debug("LLM stream async close failed", exc_info=True)

                _track_llm_cleanup_task(task)
                task.add_done_callback(_observe_close)
            return

        close = getattr(stream_resp, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                log.debug("LLM stream close failed", exc_info=True)

    @staticmethod
    def _uses_responses_api(cfg: ChatEndpointConfig) -> bool:
        return str(getattr(cfg, "chat_endpoint", "chat_completions") or "chat_completions") == "responses"

    @classmethod
    def _convert_responses_content(cls, content: Any) -> Any:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")

        parts: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append({"type": "input_text", "text": item})
                continue
            if not isinstance(item, dict):
                text_value = str(item or "")
                if text_value:
                    parts.append({"type": "input_text", "text": text_value})
                continue

            item_type = str(item.get("type", "") or "").strip().lower()
            if item_type in {"text", "input_text", "output_text"}:
                text_value = item.get("text", "")
                if isinstance(text_value, dict):
                    text_value = text_value.get("value", "")
                text_value = str(text_value or "")
                if text_value:
                    parts.append({"type": "input_text", "text": text_value})
                continue

            if item_type in {"image_url", "input_image"}:
                image_payload = item.get("image_url", item)
                if isinstance(image_payload, dict):
                    image_url = str(image_payload.get("url", "") or image_payload.get("image_url", "") or "").strip()
                    detail = str(image_payload.get("detail", "auto") or "auto").strip()
                else:
                    image_url = str(image_payload or "").strip()
                    detail = "auto"
                if image_url:
                    parts.append(
                        {
                            "type": "input_image",
                            "image_url": image_url,
                            "detail": detail or "auto",
                        }
                    )
                continue

        if not parts:
            return cls._normalize_content_text(content)
        return parts

    @classmethod
    def _messages_to_responses_input(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue

            role = str(message.get("role", "") or "").strip().lower()
            content = message.get("content", "")

            if role in {"system", "developer", "user"}:
                items.append(
                    {
                        "role": role,
                        "content": cls._convert_responses_content(content),
                    }
                )
                continue

            if role == "assistant":
                normalized_content = cls._convert_responses_content(content)
                if normalized_content:
                    items.append({"role": "assistant", "content": normalized_content})

                raw_tool_calls = message.get("tool_calls") or []
                for tool_call in raw_tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    function = tool_call.get("function", {}) or {}
                    name = str(function.get("name", "") or "").strip()
                    arguments = function.get("arguments", "")
                    if isinstance(arguments, dict):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    arguments = str(arguments or "")
                    call_id = str(tool_call.get("id", "") or "").strip()
                    if name and call_id:
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": name,
                                "arguments": arguments,
                            }
                        )
                continue

            if role == "tool":
                call_id = str(message.get("tool_call_id", "") or "").strip()
                if not call_id:
                    continue
                output = content if isinstance(content, str) else cls._normalize_content_text(content)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": str(output or ""),
                    }
                )

        return items

    @classmethod
    def _tools_to_responses_format(cls, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Flatten Chat-Completions tool definitions for the Responses API.

        The Responses API expects name/description/parameters at the top level
        of each function tool; the nested {"function": {...}} shape yields
        "Missing required parameter: 'tools[0].name'".
        """
        converted: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                converted.append(tool)
                continue
            function = tool.get("function")
            if str(tool.get("type", "") or "").strip().lower() == "function" and isinstance(function, dict):
                flat: dict[str, Any] = {
                    "type": "function",
                    "name": str(function.get("name", "") or ""),
                }
                description = function.get("description")
                if description:
                    flat["description"] = description
                parameters = function.get("parameters")
                if parameters is not None:
                    flat["parameters"] = parameters
                if function.get("strict") is not None:
                    flat["strict"] = function["strict"]
                converted.append(flat)
                continue
            converted.append(tool)
        return converted

    def _responses_sync_request(
        self,
        *,
        messages: list[dict[str, Any]],
        cfg: ChatEndpointConfig,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        exclude_params: frozenset[str] | set[str] = frozenset(),
    ) -> Any:
        responses_fn = getattr(litellm, "responses", None)
        if responses_fn is None:
            raise RuntimeError("installed litellm does not support responses(); please upgrade litellm")

        kwargs = self._build_responses_kwargs(cfg, stream=stream, exclude_params=exclude_params)
        if tools:
            kwargs["tools"] = self._tools_to_responses_format(tools)
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        input_items = self._messages_to_responses_input(messages)
        try:
            resp = responses_fn(input=input_items, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument 'input'" not in str(exc):
                raise
            resp = responses_fn(messages=messages, **kwargs)
        if stream:
            return self._consume_chat_stream_sync(resp)
        return self._normalize_response_object(resp)

    async def _responses_async_request(
        self,
        *,
        messages: list[dict[str, Any]],
        cfg: ChatEndpointConfig,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        exclude_params: frozenset[str] | set[str] = frozenset(),
        timeout_sec: float,
    ) -> Any:
        """Create a Responses API request using the cancellable async client."""

        responses_fn = getattr(litellm, "aresponses", None)
        if responses_fn is None:
            raise RuntimeError(
                "installed litellm does not support aresponses(); please upgrade litellm"
            )

        kwargs = self._build_responses_kwargs(
            cfg,
            stream=stream,
            exclude_params=exclude_params,
        )
        kwargs["timeout"] = max(0.01, float(timeout_sec))
        if tools:
            kwargs["tools"] = self._tools_to_responses_format(tools)
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        input_items = self._messages_to_responses_input(messages)
        try:
            return await responses_fn(input=input_items, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument 'input'" not in str(exc):
                raise
            return await responses_fn(messages=messages, **kwargs)

    async def _chat_completion_response_with_retries(
        self,
        *,
        messages: list[dict[str, Any]],
        cfg: ChatEndpointConfig,
        label: str,
        preview_limit: int,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        permit: Any | None = None,
    ) -> Any | None:
        label_cn = self._label_cn(label)
        total_attempts = self._retry_attempts(cfg)
        limits = self.endpoint_limits(cfg)
        input_budget = self.input_token_budget(cfg)
        if input_budget <= 0:
            log.error(
                "LLM output reservation leaves no input capacity | stage=%s | model=%s | skipping_model",
                label_cn,
                cfg.model,
            )
            return None
        prompt_tokens, token_count_exact = await self._count_prompt_tokens_async(
            messages,
            tools=tools,
            cfg=cfg,
        )
        # 裁剪用**可加的保守上界**（与装配链路同口径），门禁看两者中更大的那个：
        # 真实分词器说超了就必须裁，而且还要把"真实比保守多出来的那部分"也算进预算，
        # 否则保守口径会先把载荷判成"装得下"而把真实超限放过去。
        conservative_tokens, _raw_characters = self._conservative_prompt_token_estimate(
            messages,
            tools,
        )
        effective_tokens = max(prompt_tokens, conservative_tokens)
        if input_budget > 0 and effective_tokens > input_budget:
            overshoot_gap = max(0, prompt_tokens - conservative_tokens)
            fitted = self.fit_messages_to_budget(
                messages,
                tools=tools,
                budget_tokens=max(1, input_budget - overshoot_gap),
            )
            log.warning(
                "LLM prompt exceeds endpoint budget, fitting before send | stage=%s | model=%s | "
                "source=%s | prompt_tokens=%d (conservative=%d) /%d | exact=%s | dropped=%d | "
                "truncated=%d | layers=%s",
                label_cn,
                cfg.model,
                getattr(limits, "source", "-"),
                prompt_tokens,
                conservative_tokens,
                input_budget,
                token_count_exact,
                fitted.dropped_messages,
                fitted.truncated_messages,
                ",".join(fitted.layers_dropped) or "-",
            )
            if fitted.over_budget:
                log.error(
                    "LLM request cannot fit the model window even after trimming | stage=%s | "
                    "model=%s | source=%s | prompt_tokens=%d/%d | exact=%s | skipping_model",
                    label_cn,
                    cfg.model,
                    getattr(limits, "source", "-"),
                    fitted.prompt_tokens,
                    input_budget,
                    token_count_exact,
                )
                return None
            messages = fitted.messages
            prompt_tokens = max(
                fitted.prompt_tokens,
                prompt_tokens - (conservative_tokens - fitted.prompt_tokens),
            )
        # 清洁：层标记（``_ctx_layer`` 之类）只用于进程内决定裁剪顺序，绝不进请求体。
        messages = strip_internal_message_keys(messages)
        configuration_issue = self.chat_configuration_issue(cfg)
        if configuration_issue:
            log.error(
                "LLM unavailable | stage=%s | model=%s | reason=%s | skipping_model",
                label_cn,
                cfg.model,
                configuration_issue,
            )
            return None
        if self._circuit_is_open(cfg, stage=label):
            log.warning(
                "LLM circuit open | stage=%s model=%s | skipping_model",
                label_cn,
                cfg.model,
            )
            return None

        excluded_params: set[str] = set()
        attempt = 0
        while attempt < total_attempts:
            attempt += 1
            stream = self._should_stream_upstream(label, cfg)
            kwargs = self._build_chat_kwargs(cfg, stream=stream, exclude_params=excluded_params)
            timeout_sec = self._attempt_timeout_seconds(cfg, attempt)
            # Keep the SDK/socket timeout aligned with the current retry attempt.
            # Previously only the outer waiter used the multiplied timeout.
            kwargs["timeout"] = timeout_sec
            prompt_usage = self.prompt_usage_text(
                messages,
                tools=tools,
                cfg=cfg,
                used_tokens=prompt_tokens,
            )
            log.info(
                "LLM request | stage=%s | model=%s | endpoint=%s | attempt=%d/%d | prompt_tokens=%s | messages=%d | tools=%d | timeout=%.1fs | stream=%s",
                label_cn,
                cfg.model,
                getattr(cfg, "endpoint_path", getattr(cfg, "chat_endpoint", "chat_completions")),
                attempt,
                total_attempts,
                prompt_usage,
                len(messages),
                len(tools or []),
                timeout_sec,
                stream,
            )
            try:
                async def _perform_attempt() -> Any:
                    # Keep the permit owned by the actual request task. If an
                    # upstream coroutine ignores cancellation, the permit is
                    # released only when that orphan really exits; nominal
                    # timeouts therefore cannot create unbounded real network
                    # concurrency behind a semaphore that was released early.
                    # A caller that already waited for admission (background
                    # summary) hands its permit over here instead of acquiring
                    # again -- never a second acquire, never a bypass.
                    owned_permit = permit.consume() if permit is not None else None
                    gate_scope = (
                        nullcontext()
                        if owned_permit is not None
                        else _LLM_PRIORITY_GATE.slot(timeout=timeout_sec)
                    )
                    try:
                        async with gate_scope:
                            async with _LLM_REQUEST_SEMAPHORE:
                                raw_resp: Any
                                request: Any
                                if self._uses_responses_api(cfg):
                                    request = self._responses_async_request(
                                        messages=messages,
                                        cfg=cfg,
                                        stream=stream,
                                        tools=tools,
                                        tool_choice=tool_choice,
                                        exclude_params=set(excluded_params),
                                        timeout_sec=timeout_sec,
                                    )
                                else:
                                    if tools:
                                        tool_kwargs: dict[str, Any] = {
                                            "tools": tools,
                                            "tool_choice": tool_choice,
                                        }
                                        if not any(
                                            isinstance(tool, dict)
                                            and str(tool.get("type", "")).strip().lower() == "mcp"
                                            for tool in tools
                                        ):
                                            # LiteLLM 1.92 imports its optional Proxy/MCP stack
                                            # before it checks that these are ordinary function tools.
                                            tool_kwargs["_skip_mcp_handler"] = True
                                        request = litellm.acompletion(
                                            messages=messages,
                                            **tool_kwargs,
                                            **kwargs,
                                        )
                                    else:
                                        request = litellm.acompletion(messages=messages, **kwargs)

                                raw_resp = await request
                                if stream:
                                    try:
                                        return await self._consume_chat_stream(raw_resp)
                                    finally:
                                        self._close_stream_best_effort(raw_resp)
                                return self._normalize_response_object(raw_resp)
                    finally:
                        if owned_permit is not None:
                            # 许可归**实际请求任务**所有；不合作的取消也不会提前释放。
                            owned_permit.release()

                resp = await self._await_with_timeout(
                    _perform_attempt(),
                    timeout_sec=timeout_sec,
                )
                resp = self._normalize_response_object(resp)
                message = resp.choices[0].message
                content = self._normalize_content_text(message.content)
                tool_calls = getattr(message, "tool_calls", None)
                if not content.strip() and not tool_calls:
                    if attempt < total_attempts:
                        llm_metrics.record(label, empty_responses=1)
                        log.warning(
                            "LLM empty response | stage=%s | model=%s | attempt=%d/%d | retrying_same_model",
                            label_cn,
                            cfg.model,
                            attempt,
                            total_attempts,
                        )
                        backoff = self._retry_backoff_seconds(cfg, attempt)
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        continue
                    llm_metrics.record(label, empty_responses=1)
                    self._record_circuit_failure(cfg, stage=label)
                    return None

                usage = getattr(resp, "usage", None)
                tokens_in = getattr(usage, "prompt_tokens", 0)
                tokens_out = getattr(usage, "completion_tokens", 0)
                if not tokens_in:
                    # Reuse the admission-time count instead of running the
                    # synchronous tokenizer a second time on the event loop.
                    tokens_in = prompt_tokens
                preview, truncated = self._preview_for_log(content, limit=preview_limit)
                log.info(
                    "LLM response | stage=%s | model=%s | attempt=%d/%d | len=%d | tool_calls=%d | prompt_tokens=%s | output_tokens=%d | preview_truncated=%s | preview=%s",
                    label_cn,
                    cfg.model,
                    attempt,
                    total_attempts,
                    len(content),
                    len(tool_calls or []),
                    self.prompt_usage_text(messages, tools=tools, cfg=cfg, used_tokens=tokens_in),
                    tokens_out,
                    truncated,
                    preview,
                )
                llm_metrics.record_usage(
                    label, usage, prompt_tokens=tokens_in, output_tokens=tokens_out
                )
                self._record_circuit_success(cfg, stage=label)
                return resp
            except asyncio.TimeoutError:
                if attempt < total_attempts:
                    llm_metrics.record(label, timeouts=1)
                    log.warning(
                        "LLM timeout | stage=%s | model=%s | attempt=%d/%d | timeout=%.1fs | retrying_same_model",
                        label_cn,
                        cfg.model,
                        attempt,
                        total_attempts,
                        timeout_sec,
                    )
                    backoff = self._retry_backoff_seconds(cfg, attempt)
                    if backoff > 0:
                        await asyncio.sleep(backoff)
                    continue
                llm_metrics.record(label, timeouts=1)
                log.warning(
                    "LLM timeout | stage=%s | model=%s | attempt=%d/%d | timeout=%.1fs | fallback_next",
                    label_cn,
                    cfg.model,
                    attempt,
                    total_attempts,
                    timeout_sec,
                )
            except Exception as exc:
                dropped_param = self._unsupported_param_from_error(exc)
                if dropped_param and dropped_param not in excluded_params:
                    excluded_params.add(dropped_param)
                    attempt -= 1
                    log.warning(
                        "LLM unsupported param | stage=%s | model=%s | param=%s | retrying_without_param",
                        label_cn,
                        cfg.model,
                        dropped_param,
                    )
                    continue
                if attempt < total_attempts:
                    llm_metrics.record(label, failures=1)
                    log.warning(
                        "LLM failure | stage=%s | model=%s | attempt=%d/%d | error=%s | retrying_same_model",
                        label_cn,
                        cfg.model,
                        attempt,
                        total_attempts,
                        exc,
                    )
                    backoff = self._retry_backoff_seconds(cfg, attempt)
                    if backoff > 0:
                        await asyncio.sleep(backoff)
                    continue
                llm_metrics.record(label, failures=1)
                log.warning(
                    "LLM failure | stage=%s | model=%s | attempt=%d/%d | error=%s | fallback_next",
                    label_cn,
                    cfg.model,
                    attempt,
                    total_attempts,
                    exc,
                )
        self._record_circuit_failure(cfg, stage=label)
        return None

    async def _chat_with_fallbacks(
        self,
        *,
        messages: list[dict[str, Any]],
        candidates: list[ChatEndpointConfig],
        label: str,
        preview_limit: int,
        permit: Any | None = None,
    ) -> str:
        total = len(candidates)
        loop = asyncio.get_running_loop()
        # Skip endpoints that just answered with a failure notice, as long as a
        # usable candidate remains: with a persistently degraded bridge every
        # reply would otherwise pay the bridge's full timeout before the working
        # endpoint gets its turn.
        now = loop.time()
        fresh = [
            c
            for c in candidates
            if self._chat_degraded_until.get(self._tool_endpoint_key(c), 0.0) <= now
        ]
        if fresh:
            candidates = fresh
            total = len(candidates)
        deadline_sec = self._stage_deadline_seconds(
            label,
            candidates[0] if candidates else None,
        )
        deadline = loop.time() + deadline_sec
        for idx, cfg in enumerate(candidates, start=1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.error("LLM total deadline exhausted | stage=%s", self._label_cn(label))
                return ""
            try:
                async with asyncio.timeout(remaining):
                    resp = await self._chat_completion_response_with_retries(
                        messages=messages,
                        cfg=cfg,
                        label=label,
                        preview_limit=preview_limit,
                        permit=permit,
                    )
            except TimeoutError:
                log.error(
                    "LLM total deadline exceeded | stage=%s deadline=%.1fs",
                    self._label_cn(label),
                    deadline_sec,
                )
                return ""
            if resp is not None:
                text = self._normalize_content_text(resp.choices[0].message.content)
                if not self._chat_response_degraded(text, cfg):
                    return text
                # The endpoint answered HTTP 200 with a failure notice: mark it so
                # a persistently degraded bridge is skipped for a while instead
                # of adding its latency to every single reply.
                self._chat_degraded_until[self._tool_endpoint_key(cfg)] = (
                    loop.time() + _CHAT_DEGRADED_COOLDOWN_SECONDS
                )
                if idx < total:
                    log.warning(
                        "LLM degraded reply, walking fallback | stage=%s | "
                        "model=%s | next=%s | text=%r",
                        self._label_cn(label),
                        cfg.model,
                        candidates[idx].model,
                        text[:120],
                    )
                    continue
                log.error(
                    "LLM degraded reply on last candidate, suppressed | "
                    "stage=%s | model=%s | text=%r",
                    self._label_cn(label),
                    cfg.model,
                    text[:120],
                )
                return ""
            if idx < total:
                continue
            log.error(
                "LLM exhausted | stage=%s | model=%s | no_fallback_left",
                self._label_cn(label),
                cfg.model,
            )
            return ""
        return ""

    @staticmethod
    def _chat_response_degraded(text: str, cfg: ChatEndpointConfig) -> bool:
        """True when a chat turn returned a failure notice instead of an answer.

        The free Gemini web bridge answers HTTP 200 whose body is Google's own
        error notice or a refusal template whenever its upstream session is
        throttled; the chat path has no tool contract to inspect, so such a
        string used to reach the group verbatim.  Only bridge-shaped endpoints
        are judged, and only short replies count, so a long substantive answer
        quoting one of these phrases stays usable.
        """
        stripped = " ".join((text or "").split())
        if not stripped or len(stripped) > _CHAT_DEGRADED_MAX_CHARS:
            return False
        if not LLMService._is_local_bridge_endpoint(cfg):
            return False
        return _CHAT_DEGRADED_CONTENT_RE.search(stripped) is not None

    @staticmethod
    def _is_local_bridge_endpoint(cfg: ChatEndpointConfig) -> bool:
        """True when the endpoint is a self-hosted bridge, not a public provider.

        Only the in-house Gemini web bridge can return a degraded notice, and it
        is always reached over a docker service name or a loopback address; a
        public gateway saying the same words is answering for real and must be
        left alone.
        """
        api_base = str(getattr(cfg, "api_base", "") or "").strip().lower()
        if not api_base:
            return False
        host = api_base.split("//", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1]
        host = host.split(":", 1)[0].strip("[]")
        if not host:
            return False
        if host in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}:
            return True
        # A bare hostname (no dot) is a container/service name on a private
        # network, not something reachable on the public internet.
        if "." not in host or host.endswith(".local"):
            return True
        # A private-range address is a self-hosted service too (NAS/LAN bridge).
        return bool(re.match(r"^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)", host))

    @staticmethod
    def _tool_endpoint_key(cfg: ChatEndpointConfig) -> tuple[str, str, str]:
        return (
            str(getattr(cfg, "provider", "") or ""),
            str(getattr(cfg, "model", "") or ""),
            str(getattr(cfg, "api_base", "") or ""),
        )

    @staticmethod
    def _tool_response_usable(resp: Any) -> bool:
        """False when a tool turn produced neither a tool call nor usable text.

        Covers the two failure shapes seen from the Gemini web bridge: an
        apology/error string returned instead of a tool call, and an empty
        body.  A text-encoded tool call (recovered later by
        ``_salvage_text_tool_calls``) stays usable because it is ordinary
        content that does not match the failure phrasing.
        """
        if resp is None:
            return False
        message: Any = resp
        choices = getattr(resp, "choices", None)
        if not choices and isinstance(resp, dict):
            choices = resp.get("choices")
        if choices:
            first = choices[0]
            message = getattr(first, "message", None)
            if message is None and isinstance(first, dict):
                message = first.get("message")
            if message is None:
                message = first
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls is None and isinstance(message, dict):
            tool_calls = message.get("tool_calls")
        if tool_calls:
            return True
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = " ".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            ).strip()
        else:
            text = ""
        if not text:
            return False
        if len(text) <= _TOOL_UNUSABLE_MAX_CHARS:
            if _TOOL_UNUSABLE_CONTENT_RE.search(text):
                return False
        return True

    async def complete_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        label: str = "skill",
        cfg: ModelConfig | None = None,
        preview_limit: int = 80,
    ) -> Any | None:
        candidates = self._chat_candidates(cfg or self.skill_config)
        loop = asyncio.get_running_loop()
        now = loop.time()
        # 2026-09-29: the CONFIGURED primary always keeps slot #1.  The skill
        # route's contract is "home gateway first, free bridge only as a
        # fallback"; the previous shape dropped the primary whenever it had just
        # failed the tool contract, which promoted the *fallback* above it - so
        # a single gateway hiccup made the following 5 minutes of skill calls
        # wait on the degraded free bridge and then escalate back.  The mark now
        # only skips fallbacks that just failed the same contract; if every
        # fallback is marked the marks are cleared, so the chain still gets a
        # chance instead of answering from a known-bad primary alone.
        head, tail = candidates[:1], candidates[1:]
        fresh_tail = [
            c
            for c in tail
            if self._tool_incapable.get(self._tool_endpoint_key(c), 0.0) <= now
        ]
        if tail and not fresh_tail:
            self._tool_incapable.clear()
            fresh_tail = tail
        candidates = head + fresh_tail
        total = len(candidates)
        deadline_sec = self._stage_deadline_seconds(
            label,
            candidates[0] if candidates else None,
        )
        deadline = loop.time() + deadline_sec
        for idx, candidate in enumerate(candidates, start=1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            call_timeout = remaining
            if idx < total:
                # Leave the rest of the stage budget for a tool-capable fallback.
                call_timeout = min(remaining, _TOOL_CANDIDATE_CAP_SECONDS)
            try:
                async with asyncio.timeout(call_timeout):
                    resp = await self._chat_completion_response_with_retries(
                        messages=messages,
                        cfg=candidate,
                        label=label,
                        preview_limit=preview_limit,
                        tools=tools,
                        tool_choice="auto",
                    )
            except TimeoutError:
                self._tool_incapable[self._tool_endpoint_key(candidate)] = (
                    loop.time() + _TOOL_UNUSABLE_TTL_SECONDS
                )
                if idx < total:
                    log.warning(
                        "LLM tool candidate timed out | stage=%s | model=%s | "
                        "attempt=%d/%d | waited=%.1fs | escalating_to_fallback",
                        self._label_cn(label),
                        candidate.model,
                        idx,
                        total,
                        call_timeout,
                    )
                    continue
                log.error(
                    "LLM tool total deadline exceeded | stage=%s deadline=%.1fs",
                    self._label_cn(label),
                    deadline_sec,
                )
                return None
            if resp is not None:
                if self._tool_response_usable(resp) or idx >= total:
                    return resp
                self._tool_incapable[self._tool_endpoint_key(candidate)] = (
                    loop.time() + _TOOL_UNUSABLE_TTL_SECONDS
                )
                log.warning(
                    "LLM tool response unusable | stage=%s | model=%s | "
                    "attempt=%d/%d | escalating_to_fallback",
                    self._label_cn(label),
                    candidate.model,
                    idx,
                    total,
                )
                continue
            if idx < total:
                continue
            log.error(
                "LLM exhausted | stage=%s | model=%s | no_fallback_left",
                self._label_cn(label),
                candidate.model,
            )
        return None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        use_decision: bool = False,
        use_moderation: bool = False,
        stage: str | None = None,
    ) -> str:
        """Send chat completion request and return assistant text.

        ``stage`` only relabels an ordinary ``main`` call for usage metrics,
        log lines, and the stage deadline (e.g. ``synopsis`` for the optional
        /av AI overview).  It never changes which model configuration is used,
        so no new provider or endpoint is introduced.
        """
        if use_moderation:
            cfg = self.moderation_config
            label = "moderation"
        elif use_decision:
            cfg = self.decision_config
            label = "decision"
        else:
            cfg = self.main
            label = "main"
        if stage:
            label = str(stage).strip() or label
            preview_limit = 80
        else:
            preview_limit = 120 if label == "moderation" else 80

        scope = (
            execution_priority_scope(_MODERATION_EXECUTION_PRIORITY)
            if use_moderation
            else nullcontext()
        )
        with scope:
            return await self._chat_with_fallbacks(
                messages=messages,
                candidates=self._chat_candidates(cfg),
                label=label,
                preview_limit=preview_limit,
            )

    async def background_summary_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        cfg: ModelConfig | None = None,
        label: str = "group_summary",
        preview_limit: int = 80,
        max_tokens: int | None = None,
        permit: Any | None = None,
    ) -> str:
        """后台摘要 / 维护专用的一次性对话调用。

        与普通回复的差别只有**调度类**：整个调用（含 fallback 与重试）跑在
        ``BACKGROUND`` 优先级下，因此走 :data:`_LLM_PRIORITY_GATE` 的背景容量
        （≤2、与回复共享 normal=4、不碰 HIGH/CRITICAL 的保留名额）。**整体硬超时
        由调用方（摘要调度器）用 ``asyncio.timeout`` 施加**，排队时间不计入模型时限。
        """

        target = cfg or self.compress_config
        if max_tokens is not None and int(max_tokens) > 0:
            # 摘要输出上限走**API 的 max_tokens**（不是只靠事后截断）。
            override = int(max_tokens)
            try:
                target = target.model_copy(update={"max_tokens": override})
            except Exception:  # 非 pydantic 替身（测试）时退化为浅拷贝
                copy = copy_module.copy(target)
                try:
                    copy.max_tokens = override
                except Exception:
                    pass
                target = copy
        candidates = self._chat_candidates(target)
        if label == "group_summary":
            # Do not mutate the shared compress/reply/audit route. This request-only
            # override also follows retries/fallbacks within the summary deadline.
            candidates = [
                candidate.model_copy(
                    update={
                        "request_params": {
                            **{
                                key: value
                                for key, value in (candidate.request_params or {}).items()
                                if key != "reasoning_effort"
                            },
                            "thinking": {"type": "disabled"},
                        }
                    }
                )
                for candidate in candidates
            ]
        with execution_priority_scope(ExecutionPriority.BACKGROUND):
            return await self._chat_with_fallbacks(
                messages=messages,
                candidates=candidates,
                label=label,
                preview_limit=preview_limit,
                permit=permit,
            )

    async def decision(self, system: str, user_text: str) -> str:
        """Fast decision call (uses decision model)."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return await self.chat(messages, use_decision=True)

    async def moderation(self, system: str, user_text: str) -> str:
        """Moderation call (uses moderation model)."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return await self.chat(messages, use_moderation=True)

    async def generate(self, system: str, user_text: str) -> str:
        """General generation call (uses main model)."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        return await self.chat(messages)

    async def compress(self, system: str, user_text: str) -> str:
        """Compress conversation history (uses compress model)."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        text = await self._chat_with_fallbacks(
            messages=messages,
            candidates=self._chat_candidates(self.compress_config),
            label="compress",
            preview_limit=100,
        )
        if text:
            log.info(
                "LLM compress summary | input_chars=%d | output_chars=%d",
                len(user_text),
                len(text),
            )
        return text

    async def vision_describe(self, image_url: str, prompt: str) -> str:
        """Image understanding with the dedicated vision model."""
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ]
        return await self._chat_with_fallbacks(
            messages=messages,
            candidates=self._chat_candidates(self.vision_config),
            label="vision",
            preview_limit=80,
        )

    @classmethod
    def embedding_space_id(cls, cfg: EmbedEndpointConfig) -> str:
        """Return a secret-free identity for one exact embedding endpoint."""

        provider = str(getattr(cfg, "provider", "") or "").strip().lower()
        model = str(getattr(cfg, "model", "") or "").strip()
        if not provider:
            provider = model.partition("/")[0].strip().lower()
        api_base = cls._resolve_request_api_base(
            provider=provider,
            api_base=getattr(cfg, "api_base", None),
            endpoint_path=getattr(cfg, "endpoint_path", "/v1beta/models"),
            model=model,
        )
        payload = "\x00".join(
            (
                "embedding-space-v1",
                provider,
                model,
                str(api_base or "").rstrip("/"),
                str(getattr(cfg, "endpoint_path", "") or ""),
            )
        )
        return "emb-v1:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:48]

    def primary_embedding_space_id(self) -> str:
        """Return the configured primary embedding-space identity."""

        candidates = self._embed_candidates(self.embed_config)
        return self.embedding_space_id(candidates[0]) if candidates else ""

    async def _embed_from_candidates(
        self,
        texts: list[str],
        *,
        candidates: list[EmbedEndpointConfig],
        total_deadline_sec: float | None = None,
    ) -> tuple[list[list[float]], EmbedEndpointConfig] | None:
        # Deployment note: embeddings are served locally by the smart_embed
        # container (llama.cpp + bge-m3 at http://smart_embed:8788/v1 on this
        # host's bot docker network).  pipio exposes no embedding model and the
        # local sub2api gateway serves chat models only, so this stage used to
        # fail and re-queue forever (group_message_archive_embeddings:
        # 1888/1888 status='failed', retried up to 172 times) and was
        # short-circuited with `return None`.  Do not reintroduce that
        # short-circuit: with models.embed pointed at the local endpoint the
        # archive queue drains and memory recall works again.
        """Generate embeddings and report the endpoint that produced them."""

        total = len(candidates)
        if not total:
            return None
        loop = asyncio.get_running_loop()
        configured_deadline = self._stage_deadline_seconds("embed", candidates[0])
        deadline_seconds = (
            configured_deadline
            if total_deadline_sec is None
            else max(0.05, float(total_deadline_sec))
        )
        deadline = loop.time() + deadline_seconds
        for idx, cfg in enumerate(candidates, start=1):
            if self._circuit_is_open(cfg, stage="embed"):
                log.warning(
                    "LLM circuit open | stage=embed model=%s | skipping_model",
                    cfg.model,
                )
                continue
            total_attempts = self._retry_attempts(cfg)
            for attempt in range(1, total_attempts + 1):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    log.error("LLM total deadline exhausted | stage=embed")
                    return None
                kwargs = self._build_embed_kwargs(cfg, texts)
                timeout_sec = min(
                    self._attempt_timeout_seconds(cfg, attempt),
                    remaining,
                )
                kwargs["timeout"] = timeout_sec
                log.info(
                    "LLM request | stage=embed | model=%s | attempt=%d/%d | texts=%d | timeout=%.1fs",
                    cfg.model,
                    attempt,
                    total_attempts,
                    len(texts),
                    timeout_sec,
                )
                try:
                    async def _perform_embedding() -> Any:
                        async with _LLM_PRIORITY_GATE.slot(timeout=timeout_sec):
                            async with _LLM_REQUEST_SEMAPHORE:
                                return await litellm.aembedding(**kwargs)

                    resp = await self._await_with_timeout(
                        _perform_embedding(),
                        timeout_sec=timeout_sec,
                    )
                    embeddings = [item["embedding"] for item in resp.data]
                    if not embeddings:
                        if attempt < total_attempts:
                            log.warning(
                                "LLM empty response | stage=embed | model=%s | attempt=%d/%d | retrying_same_model",
                                cfg.model,
                                attempt,
                                total_attempts,
                            )
                            backoff = self._retry_backoff_seconds(cfg, attempt)
                            if backoff > 0:
                                await asyncio.sleep(backoff)
                            continue
                        break
                    log.info(
                        "LLM response | stage=embed | model=%s | attempt=%d/%d | dims=%d",
                        cfg.model,
                        attempt,
                        total_attempts,
                        len(embeddings[0]),
                    )
                    self._record_circuit_success(cfg, stage="embed")
                    return embeddings, cfg
                except asyncio.TimeoutError:
                    if attempt < total_attempts:
                        log.warning(
                            "LLM timeout | stage=embed | model=%s | attempt=%d/%d | timeout=%.1fs | retrying_same_model",
                            cfg.model,
                            attempt,
                            total_attempts,
                            timeout_sec,
                        )
                        backoff = self._retry_backoff_seconds(cfg, attempt)
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        continue
                    log.warning(
                        "LLM timeout | stage=embed | model=%s | attempt=%d/%d | timeout=%.1fs | fallback_next",
                        cfg.model,
                        attempt,
                        total_attempts,
                        timeout_sec,
                    )
                    break
                except Exception as exc:
                    if attempt < total_attempts:
                        log.warning(
                            "LLM failure | stage=embed | model=%s | attempt=%d/%d | error=%s | retrying_same_model",
                            cfg.model,
                            attempt,
                            total_attempts,
                            exc,
                        )
                        backoff = self._retry_backoff_seconds(cfg, attempt)
                        if backoff > 0:
                            await asyncio.sleep(backoff)
                        continue
                    log.warning(
                        "LLM failure | stage=embed | model=%s | attempt=%d/%d | error=%s | fallback_next",
                        cfg.model,
                        attempt,
                        total_attempts,
                        exc,
                    )
                    break
            self._record_circuit_failure(cfg, stage="embed")
            if idx < total:
                continue
            log.error(
                "LLM exhausted | stage=embed | model=%s | no_fallback_left",
                cfg.model,
            )
            return None
        return None

    async def embed_primary_with_space(
        self,
        texts: list[str],
        *,
        total_deadline_sec: float | None = None,
    ) -> EmbeddingBatchResult | None:
        """Embed using only the configured primary endpoint.

        A fallback vector belongs to a different mathematical space, so this
        method intentionally refuses fallback models for persistent indexes.
        """

        candidates = self._embed_candidates(self.embed_config)
        if not candidates or not texts:
            return None
        result = await self._embed_from_candidates(
            texts,
            candidates=candidates[:1],
            total_deadline_sec=total_deadline_sec,
        )
        if result is None:
            return None
        vectors, cfg = result
        if len(vectors) != len(texts):
            log.error(
                "LLM embedding count mismatch | model=%s expected=%d actual=%d",
                cfg.model,
                len(texts),
                len(vectors),
            )
            return None
        dimensions = len(vectors[0]) if vectors else 0
        if dimensions <= 0 or any(len(vector) != dimensions for vector in vectors):
            log.error(
                "LLM embedding dimension mismatch | model=%s texts=%d",
                cfg.model,
                len(texts),
            )
            return None
        return EmbeddingBatchResult(
            vectors=vectors,
            space_id=self.embedding_space_id(cfg),
            model=str(cfg.model or ""),
            dimensions=dimensions,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings, retaining legacy model-fallback behavior."""

        result = await self._embed_from_candidates(
            texts,
            candidates=self._embed_candidates(self.embed_config),
        )
        return result[0] if result is not None else []
