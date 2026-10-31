"""只读自检摘要：从**本进程**已有遥测里取一份白名单快照。

用途（第一批 B）：给最高管理员一条确定性命令，能看到"这个进程现在健康吗"，
而不引入任何新的探测通道。

边界（写死在实现里，不靠调用方自觉）：

* 数据来源为本进程资源快照，以及工具终态/上下文装配的只读有界快照。
  工具累计量是启动/重置以来的终态计数，近期窗口至多 64 条、没有时间范围；
  上下文是全局装配估算，不是完整请求用量或上游账单。没有记录就是未知。
  不探测外部服务、Docker 或另一台机器，不调用任何 producer 的 reset。
* 输出白名单化：只回显本模块里显式列出的数值/布尔/计数。原始 provider 名称、
  ``error`` 文本、文件路径、配置值、任意异常内容一律**不**出现在结果里。
* 采样异常 → ``状态未知/采样失败``，既不抛给聊天链路，也不冒充健康。
  ``asyncio.CancelledError`` 属于取消语义，必须原样传播（``BaseException``
  不在此拦截），否则任务取消会被吞掉。
* 不读 ``.env``、不枚举进程/日志、不起 shell、不访问网络/模型/搜索/TTS、
  不写数据库、不重启服务、不改 watchdog 阈值。
* 重复调用走**单槽有界缓存**（容量恒为 1，按单调时钟 TTL 过期），不累积字典。
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from bot.services.context_observability import (
    snapshot_context_observations as context_snapshot,
)
from bot.services.resource_health import resource_health_snapshot
from bot.services.tool_observability import snapshot as tool_snapshot
from bot.utils.timezone import now_shanghai

log = logging.getLogger(__name__)

#: 缓存寿命（秒）。快照本身是廉价的本进程采样，这里只是为了"连点几下命令"
#: 不去反复读 ``/proc``；不承担任何正确性职责。
CACHE_TTL_SECONDS = 5.0

#: 明确不接入的观测项口径。不得改写成"正常"。
NOT_OBSERVED_REASON = "本轮未接入/暂无观测"

#: 采样失败时的总体口径。不得改写成"健康"。
SAMPLING_FAILED_STATUS = "状态未知/采样失败"

#: 时间与时长的展示口径（本地进程视角，不冒充容器/服务启动时间）。
TIMEZONE_LABEL = "Asia/Shanghai"
UPTIME_BASIS = "自本进程加载自检模块起（进程内单调计时，非容器/服务启动时间）"

#: 每次都必须出现在结果里的免责说明。
DISCLAIMER = "本地进程快照，不代表外部服务实时探测。"

#: 版本标识只从这两个环境变量取，且必须通过 :data:`VERSION_PATTERN`。
VERSION_ENV_KEYS = ("BUILD_REVISION", "APP_VERSION")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$")
VERSION_UNKNOWN = "未标注"

#: ``system`` 段里允许回显的数值字段（白名单；其余一律丢弃）。
SAFE_SYSTEM_FIELDS = (
    "rss_bytes",
    "swap_bytes",
    "effective_swap_bytes",
    "threads",
    "fd_count",
    "fd_soft_limit",
    "cgroup_memory_ratio",
    "cgroup_swap_ratio",
    "cgroup_pids_ratio",
    "event_loop_lag_seconds",
    "max_event_loop_lag_seconds",
)

#: ``system["data_disk"]`` 里允许回显的数值字段（路径不进展示）。
SAFE_DISK_FIELDS = ("free_bytes", "total_bytes", "free_ratio")

_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_MAX_COUNTER = (1 << 63) - 1
_MAX_DURATION = 1e12
_TOOL_PATHWAYS = ("group_skill", "private_search", "private_tts")
_TOOL_OUTCOMES = ("success", "failure", "timeout", "cancelled", "ambiguous", "other")
_TRIM_LAYERS = ("history", "search_records", "memory_recall")
_TRIM_FIELDS = ("dropped_messages", "truncated_messages", "dropped_tokens")
_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$"
)
MAX_RENDERED_CHARACTERS = 3500

#: 本进程加载本模块的单调时刻：运行时长一律以此为起点。
_PROCESS_REFERENCE_MONOTONIC = time.monotonic()

#: 单槽缓存（容量恒为 1；不随调用次数增长）。
_CACHE: dict[str, Any] = {}


def reset_cache() -> None:
    """清空单槽缓存（测试与配置重载用；不触碰任何真实采样状态）。"""

    _CACHE.clear()


def safe_build_revision(environ: Mapping[str, str] | None = None) -> str:
    """白名单化的版本标识：只认环境变量，且必须短、字符集受限。

    不做任何 ``git`` 调用、不读目录、不读文件——每次自检都稳定可预期。
    """

    source = os.environ if environ is None else environ
    try:
        for key in VERSION_ENV_KEYS:
            raw = source.get(key)
            if type(raw) is str and len(raw) <= 40:
                revision = raw.strip()
                if VERSION_PATTERN.fullmatch(revision):
                    return revision
    except Exception:
        return VERSION_UNKNOWN
    return VERSION_UNKNOWN


def _number(value: Any) -> float | None:
    """只接受有限数值；布尔/字符串/NaN/inf 一律当"没取到"。"""

    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _safe_system_metrics(system: Any) -> dict[str, float | int]:
    """从 ``system`` 段挑白名单数值；缺失字段就不出现，不补零。"""

    if not isinstance(system, Mapping):
        return {}
    metrics: dict[str, float | int] = {}
    for field in SAFE_SYSTEM_FIELDS:
        if field in {
            "rss_bytes",
            "swap_bytes",
            "effective_swap_bytes",
            "threads",
            "fd_count",
            "fd_soft_limit",
        }:
            number = _int(system.get(field))
        else:
            number = _number(system.get(field))
        if number is not None:
            metrics[field] = number
    disk = system.get("data_disk")
    if isinstance(disk, Mapping):
        for field in SAFE_DISK_FIELDS:
            number = (
                _int(disk.get(field))
                if field.endswith("_bytes")
                else _number(disk.get(field))
            )
            if number is not None:
                metrics[f"disk_{field}"] = number
    return metrics


def _component_counts(resources: Any) -> tuple[int, int]:
    """返回 ``(已注册组件数, 异常组件数)``；只数个数，不暴露名字。

    判定与 :func:`bot.services.resource_health.resource_health_snapshot` 一致：
    缺 ``ok`` 视为 True；条目不是映射记为异常。
    """

    if not isinstance(resources, Mapping):
        return 0, 0
    registered = 0
    anomalies = 0
    for entry in resources.values():
        registered += 1
        if not isinstance(entry, Mapping):
            anomalies += 1
            continue
        if entry.get("ok", True) is not True:
            anomalies += 1
    return registered, anomalies


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= _MAX_COUNTER else None


def _duration_number(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number <= _MAX_DURATION else None


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _normalize_tools(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise ValueError("invalid tool snapshot")
    aggregates, events = snapshot.get("aggregates"), snapshot.get("events")
    if (
        type(aggregates) not in (list, tuple)
        or type(events) not in (list, tuple)
        or len(aggregates) > 2048
        or len(events) > 64
    ):
        raise ValueError("invalid tool snapshot rows")
    pathways = {
        scope: {
            "state": "not_observed",
            "cumulative_calls": 0,
            "cumulative_duration_seconds": 0.0,
            "cumulative_outcomes": dict.fromkeys(_TOOL_OUTCOMES, 0),
            "recent_calls": 0,
            "recent_duration_seconds": 0.0,
            "recent_outcomes": dict.fromkeys(_TOOL_OUTCOMES, 0),
        }
        for scope in _TOOL_PATHWAYS
    }
    for rows, prefix in ((aggregates, "cumulative"), (events, "recent")):
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid tool row")
            scope = row.get("scope")
            if type(scope) is not str or scope not in _TOOL_PATHWAYS:
                continue
            outcome = row.get("outcome")
            outcome = next(
                (
                    label
                    for label in _TOOL_OUTCOMES
                    if type(outcome) is str and outcome == label
                ),
                "other",
            )
            calls = _count(row.get("calls")) if prefix == "cumulative" else 1
            duration = _duration_number(row.get("duration_seconds"))
            if calls is None or duration is None:
                raise ValueError("invalid tool numeric observation")
            target = pathways[scope]
            target[f"{prefix}_calls"] = min(
                _MAX_COUNTER, target[f"{prefix}_calls"] + calls
            )
            target[f"{prefix}_duration_seconds"] = min(
                _MAX_DURATION, target[f"{prefix}_duration_seconds"] + duration
            )
            counts = target[f"{prefix}_outcomes"]
            counts[outcome] = min(_MAX_COUNTER, counts[outcome] + calls)
            if calls:
                target["state"] = "observed"
    return {
        "sampling_ok": True,
        "state": "observed"
        if any(row["state"] == "observed" for row in pathways.values())
        else "not_observed",
        "recent_limit": 64,
        "pathways": pathways,
    }


def _trim_rows(rows: Any) -> dict[str, dict[str, int | None]]:
    if type(rows) not in (tuple, list) or len(rows) > 3:
        raise ValueError("invalid context trim rows")
    result = {layer: dict.fromkeys(_TRIM_FIELDS) for layer in _TRIM_LAYERS}
    for row in rows:
        layer = _field(row, "layer")
        if type(layer) is str and layer in _TRIM_LAYERS:
            numbers = {field: _count(_field(row, field)) for field in _TRIM_FIELDS}
            if any(number is None for number in numbers.values()):
                raise ValueError("invalid context trim counts")
            result[layer] = numbers
    return result


def _normalize_context(snapshot: Any) -> dict[str, Any]:
    state = _field(snapshot, "state")
    count = _count(_field(snapshot, "observation_count"))
    if (
        type(state) is not str
        or state not in ("observed", "not_observed")
        or count is None
    ):
        raise ValueError("invalid context snapshot")
    latest = None
    latest_trims = {layer: dict.fromkeys(_TRIM_FIELDS) for layer in _TRIM_LAYERS}
    totals = _trim_rows(_field(snapshot, "trim_totals"))
    if state == "observed":
        raw = _field(snapshot, "latest")
        latest = {
            field: _count(_field(raw, field))
            for field in ("used_tokens", "budget_tokens", "sampled_at_unix_ns")
        }
        over_budget = _field(raw, "over_budget")
        if (
            count == 0
            or any(value is None for value in latest.values())
            or type(over_budget) is not bool
        ):
            raise ValueError("invalid latest context sample")
        latest["over_budget"] = over_budget
        latest_trims = _trim_rows(_field(raw, "trims"))
    elif count != 0 or _field(snapshot, "latest") is not None:
        raise ValueError("inconsistent empty context snapshot")
    return {
        "sampling_ok": True,
        "state": "observed" if latest is not None else "not_observed",
        "observation_count": count,
        "latest": latest,
        "trim_rows": {
            layer: {"latest": latest_trims[layer], "cumulative": totals[layer]}
            for layer in _TRIM_LAYERS
        },
    }


def _observe(
    provider: Callable[[], Any], normalize: Callable[[Any], dict[str, Any]]
) -> dict[str, Any]:
    try:
        return normalize(provider())
    except Exception:
        log.warning("self check: observation snapshot failed")
        return {"sampling_ok": False, "state": "sampling_failed"}


def build_report(
    *,
    snapshot_provider: Callable[[], Any] | None = None,
    tool_snapshot_provider: Callable[[], Any] | None = None,
    context_snapshot_provider: Callable[[], Any] | None = None,
    now: Callable[[], Any] | None = None,
    monotonic: Callable[[], float] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """构建一份自检报告（纯函数式：不读缓存、不写缓存）。

    采样失败不会抛出：``sampling_ok=False`` 且 ``overall_ok=None``，其余字段
    退化为"没取到"。唯一的例外是取消：``asyncio.CancelledError`` 不被捕获。
    """

    clock_now = now if now is not None else now_shanghai
    clock_monotonic = monotonic if monotonic is not None else time.monotonic
    provider = (
        snapshot_provider if snapshot_provider is not None else resource_health_snapshot
    )

    try:
        checked_at = clock_now()
    except Exception:
        checked_at = None
    try:
        reading = _number(clock_monotonic())
        uptime_seconds = (
            max(0.0, min(float(_MAX_COUNTER), reading - _PROCESS_REFERENCE_MONOTONIC))
            if reading is not None
            else 0.0
        )
    except Exception:  # pragma: no cover - 单调时钟不可用属极端环境
        uptime_seconds = 0.0

    sampling_ok = True
    overall_ok: bool | None = None
    fatal = False
    registered = anomalies = 0
    metrics: dict[str, float | int] = {}
    try:
        snapshot = provider()
        if not isinstance(snapshot, Mapping):
            raise ValueError("invalid resource snapshot")
        raw_ok = snapshot.get("ok")
        if type(raw_ok) is bool:
            overall_ok = raw_ok
        fatal = snapshot.get("fatal") is True
        registered, anomalies = _component_counts(snapshot.get("resources"))
        metrics = _safe_system_metrics(snapshot.get("system"))
        if anomalies or fatal:
            overall_ok = False
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # 类型名也不是 provider 返回值；正文与 traceback 均不保留。
        log.warning("self check: resource snapshot failed (%s)", type(exc).__name__)
        sampling_ok = False
        overall_ok = None
        fatal = False
        registered = anomalies = 0
        metrics = {}

    tools = _observe(
        tool_snapshot if tool_snapshot_provider is None else tool_snapshot_provider,
        _normalize_tools,
    )
    context = _observe(
        context_snapshot
        if context_snapshot_provider is None
        else context_snapshot_provider,
        _normalize_context,
    )

    return {
        "checked_at": _format_checked_at(checked_at),
        "timezone": TIMEZONE_LABEL,
        "uptime_seconds": round(uptime_seconds, 1),
        "uptime_basis": UPTIME_BASIS,
        "build_revision": safe_build_revision(environ),
        "sampling_ok": sampling_ok,
        "overall_ok": overall_ok,
        "fatal": fatal,
        "registered_components": registered,
        "anomaly_count": anomalies,
        "system_metrics": metrics,
        "tool_observations": tools,
        "context_observations": context,
        "disclaimer": DISCLAIMER,
    }


def self_check_report(
    *,
    snapshot_provider: Callable[[], Any] | None = None,
    tool_snapshot_provider: Callable[[], Any] | None = None,
    context_snapshot_provider: Callable[[], Any] | None = None,
    now: Callable[[], Any] | None = None,
    monotonic: Callable[[], float] | None = None,
    environ: Mapping[str, str] | None = None,
    use_cache: bool = True,
    cache_ttl_seconds: float = CACHE_TTL_SECONDS,
) -> dict[str, Any]:
    """读一份报告，带**单槽**、有界、按单调时钟过期的缓存。

    容量恒为一个槽位，按单调时钟 TTL 过期；三个 provider、两个时钟、环境映射
    的身份或有效版本改变即换槽，避免注入源与默认源串用报告。命中保留原 checked_at，
    返回深拷贝，不累积历史；需要新鲜采样可传 ``use_cache=False``。
    """

    provider = (
        resource_health_snapshot if snapshot_provider is None else snapshot_provider
    )
    tools = tool_snapshot if tool_snapshot_provider is None else tool_snapshot_provider
    context = (
        context_snapshot
        if context_snapshot_provider is None
        else context_snapshot_provider
    )
    clock = time.monotonic if monotonic is None else monotonic
    wall_clock = now_shanghai if now is None else now
    source = os.environ if environ is None else environ
    configuration = (provider, tools, context, clock, wall_clock, source)
    revision = safe_build_revision(source)
    try:
        stamp = _number(clock()) if use_cache else None
    except Exception:
        stamp = None
    ttl = _number(cache_ttl_seconds)

    if use_cache:
        cached_stamp, cached_configuration, cached_revision = _CACHE.get(
            "monotonic", (None, (), None)
        )
        cached_report = _CACHE.get("report")
        if (
            stamp is not None
            and cached_stamp is not None
            and ttl is not None
            and len(cached_configuration) == len(configuration)
            and all(
                old is new
                for old, new in zip(cached_configuration, configuration, strict=True)
            )
            and cached_revision == revision
            and isinstance(cached_report, dict)
            and 0.0 <= stamp - cached_stamp < max(0.0, ttl)
        ):
            return deepcopy(cached_report)

    report = build_report(
        snapshot_provider=provider,
        tool_snapshot_provider=tools,
        context_snapshot_provider=context,
        now=wall_clock,
        monotonic=clock,
        environ=source,
    )
    if use_cache and stamp is not None:
        _CACHE["monotonic"] = (stamp, configuration, revision)
        _CACHE["report"] = deepcopy(report)
    return deepcopy(report)


def _format_checked_at(value: Any) -> str:
    """``YYYY-mm-dd HH:MM:SS``；拿不到可格式化时间就明确说不知道。"""

    try:
        result = value.strftime("%Y-%m-%d %H:%M:%S")
        return _safe_checked_at(result)
    except Exception:
        return "未知"


def _safe_checked_at(value: Any) -> str:
    if type(value) is str and len(value) == 19 and _TIMESTAMP_PATTERN.fullmatch(value):
        return value
    return "未知"


def _display_number(value: Any, places: int = 0) -> str:
    number = _number(value)
    if number is None:
        return "未知"
    if abs(number) >= 1e9:
        return f"{number:.3g}"
    return f"{number:.{places}f}"


def format_duration(seconds: Any) -> str:
    """人类可读的运行时长（不进位、不四舍五入到"看起来更久"）。"""

    total = _number(seconds)
    if total is None:
        return "未知"
    total_seconds = int(min(_MAX_COUNTER, max(0.0, total)))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{_display_number(days)}天{hours}小时{minutes}分"
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def _format_mib(bytes_value: float | int) -> str:
    return f"{_display_number(float(bytes_value) / _MIB)} MiB"


def _format_ratio(ratio: float | int) -> str:
    return f"{_display_number(float(ratio) * 100, 1)}%"


def _render_metrics(metrics: Mapping[str, Any]) -> str:
    """把白名单数值拼成一行；一个都没有就说"未取到"，不编造。"""

    metrics = {
        field: _number(metrics.get(field))
        for field in (*SAFE_SYSTEM_FIELDS, *(f"disk_{key}" for key in SAFE_DISK_FIELDS))
    }
    parts: list[str] = []
    if (value := metrics.get("rss_bytes")) is not None:
        parts.append(f"RSS {_format_mib(value)}")
    if (value := metrics.get("swap_bytes")) is not None:
        parts.append(f"交换 {_format_mib(value)}")
    if (value := metrics.get("threads")) is not None:
        parts.append(f"线程 {_display_number(value)}")
    if (value := metrics.get("fd_count")) is not None:
        limit = metrics.get("fd_soft_limit")
        parts.append(
            f"fd {_display_number(value)}/{_display_number(limit)}"
            if limit is not None
            else f"fd {_display_number(value)}"
        )
    if (value := metrics.get("cgroup_memory_ratio")) is not None:
        parts.append(f"cgroup 内存 {_format_ratio(value)}")
    if (value := metrics.get("cgroup_pids_ratio")) is not None:
        parts.append(f"cgroup PID {_format_ratio(value)}")
    if (value := metrics.get("event_loop_lag_seconds")) is not None:
        parts.append(f"事件循环滞后 {_display_number(value, 2)}s")
    if (value := metrics.get("max_event_loop_lag_seconds")) is not None:
        parts.append(f"最大滞后 {_display_number(value, 2)}s")
    if (value := metrics.get("disk_free_bytes")) is not None:
        free_gib = float(value) / _GIB
        ratio = metrics.get("disk_free_ratio")
        parts.append(
            f"数据盘剩余 {_display_number(free_gib, 1)} GiB（{_format_ratio(ratio)}）"
            if ratio is not None
            else f"数据盘剩余 {_display_number(free_gib, 1)} GiB"
        )
    return " · ".join(parts) if parts else "未取到"


def _render_status(report: Mapping[str, Any]) -> str:
    if not report.get("sampling_ok"):
        return SAMPLING_FAILED_STATUS
    overall = report.get("overall_ok")
    if overall is True:
        return "本地健康"
    if overall is False:
        return "本地异常"
    return "状态未知"


def _render_tool_observations(observations: Any) -> str:
    lines = [
        "工具观测（本进程；累计自启动/重置以来，近期至多64条，无时间范围）",
        "结果顺序 success/failure/timeout/cancelled/ambiguous/other",
        "耗时为逻辑调用累计秒数；无事件时间戳，不表示外部服务实时健康。",
    ]
    if not isinstance(observations, Mapping):
        return "\n".join([*lines, "工具观测：暂无观测/未知"])
    if observations.get("sampling_ok") is not True:
        return "\n".join([*lines, "工具观测：状态未知/采样失败"])
    pathways = observations.get("pathways")
    for scope in _TOOL_PATHWAYS:
        row = pathways.get(scope) if isinstance(pathways, Mapping) else None
        if not isinstance(row, Mapping) or row.get("state") != "observed":
            lines.append(f"{scope}：暂无观测/未知")
            continue
        parts = []
        for prefix, label in (("cumulative", "累计"), ("recent", "近期64")):
            counts = row.get(f"{prefix}_outcomes")
            outcomes = "/".join(
                _display_number(_count(counts.get(outcome)))
                if isinstance(counts, Mapping)
                else "未知"
                for outcome in _TOOL_OUTCOMES
            )
            calls = _display_number(_count(row.get(f"{prefix}_calls")))
            duration = _display_number(
                _duration_number(row.get(f"{prefix}_duration_seconds")), 2
            )
            parts.append(f"{label} {calls}次/{duration}s [{outcomes}]")
        lines.append(f"{scope}：{'；'.join(parts)}")
    return "\n".join(lines)


def _render_context_observations(observations: Any) -> str:
    lines = ["上下文装配（全局，不能归属群/私聊；估算，非计费/非完整请求）"]
    if not isinstance(observations, Mapping):
        return "\n".join([*lines, "上下文：暂无观测/未知"])
    if observations.get("sampling_ok") is not True:
        return "\n".join([*lines, "上下文：状态未知/采样失败"])
    latest = observations.get("latest")
    if observations.get("state") != "observed" or not isinstance(latest, Mapping):
        return "\n".join([*lines, "上下文：暂无观测/未知"])
    used = _display_number(_count(latest.get("used_tokens")))
    budget = _display_number(_count(latest.get("budget_tokens")))
    over = latest.get("over_budget")
    over_label = "是" if over is True else "否" if over is False else "未知"
    sampled_at = _count(latest.get("sampled_at_unix_ns"))
    timestamp = str(sampled_at) if sampled_at is not None else "未知"
    count = _display_number(_count(observations.get("observation_count")))
    lines.extend(
        [
            f"最新 used_tokens={used} / budget_tokens={budget}；"
            f"over_budget={over_label}",
            f"样本 sampled_at_unix_ns={timestamp}；启动/重置以来完成装配 {count}次",
            "裁剪（最新/累计）：丢弃消息/截短消息/丢弃估算token",
        ]
    )
    rows = observations.get("trim_rows")
    for layer in _TRIM_LAYERS:
        row = rows.get(layer) if isinstance(rows, Mapping) else None
        parts = []
        for key, label in (("latest", "最新"), ("cumulative", "累计")):
            counts = row.get(key) if isinstance(row, Mapping) else None
            values = "/".join(
                _display_number(_count(counts.get(field)))
                if isinstance(counts, Mapping)
                else "未知"
                for field in _TRIM_FIELDS
            )
            parts.append(f"{label} {values}")
        lines.append(f"{layer}：{'；'.join(parts)}")
    return "\n".join(lines)


def render_self_check_text(report: Mapping[str, Any]) -> str:
    """固定字段与有界数值渲染；各段异常隔离，不回显任意报告字符串。"""
    lines = ["<b>自检（本地只读快照）</b>"]
    try:
        checked_at = _safe_checked_at(report.get("checked_at"))
        uptime = format_duration(report.get("uptime_seconds"))
        revision = safe_build_revision({"BUILD_REVISION": report.get("build_revision")})
        lines.extend(
            [
                f"时间 {checked_at}（{TIMEZONE_LABEL}）",
                f"运行时长 {uptime}（{UPTIME_BASIS}）",
                f"版本 {revision}",
                f"总体：{_render_status(report)}",
            ]
        )
    except Exception:
        lines.append("时间/版本/本地状态：未知")
    try:
        if report.get("sampling_ok") is True:
            registered = _count(report.get("registered_components"))
            anomalies = _count(report.get("anomaly_count"))
            if registered is not None and anomalies is not None:
                lines.append(
                    f"组件：已注册 {_display_number(registered)} 个 · "
                    f"异常 {_display_number(anomalies)} 个"
                )
            metrics = report.get("system_metrics")
            resource_text = _render_metrics(
                metrics if isinstance(metrics, Mapping) else {}
            )
        else:
            resource_text = "本轮没有取到进程资源快照，数值一律不报告。"
        if len(resource_text) > 600:
            raise ValueError("resource rendering exceeds fixed budget")
        lines.append(
            f"<blockquote>{html.escape(resource_text, quote=False)}</blockquote>"
        )
    except Exception:
        lines.append("进程资源：状态未知/渲染失败")
    try:
        tools_text = _render_tool_observations(report.get("tool_observations"))
        if type(tools_text) is not str or len(tools_text) > 1300:
            raise ValueError("tool rendering exceeds fixed budget")
        lines.append(tools_text)
    except Exception:
        lines.append("工具观测：状态未知/渲染失败")
    try:
        context_text = _render_context_observations(report.get("context_observations"))
        if type(context_text) is not str or len(context_text) > 1100:
            raise ValueError("context rendering exceeds fixed budget")
        lines.append(context_text)
    except Exception:
        lines.append("上下文：状态未知/渲染失败")
    lines.append(f"<i>{DISCLAIMER}</i>")
    # 固定3路径/6结果/3裁剪层、每个数值短格式，正常最坏情况也低于上限。
    # 各段超额时已单独降级；最终兜底只防内部格式常量失误，不截断HTML。
    text = "\n".join(lines)
    if len(text) > MAX_RENDERED_CHARACTERS:
        return "\n".join(
            [
                "<b>自检（本地只读快照）</b>",
                "展示超出有界格式：状态未知/渲染失败。",
                f"<i>{DISCLAIMER}</i>",
            ]
        )
    return text
