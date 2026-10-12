"""按群 / 按用途 / 按真实供应商的 LLM 用量明细累加器。

与 :mod:`bot.services.llm_metrics` 的分工（**两套口径，不是同一份账**）：

* ``llm_metrics`` → ``llm_usage_daily``：历史表，只记**成功调用**，键是
  (日, 阶段)。看板与周报按它读，本轮不改。
* 本模块 → ``llm_cost_daily``：明细表。一次真实发出供应商请求就是一个
  ``attempt``（重试、回退、工具循环的每一步各算各的），并记录
  失败 / 超时 / 空响应 / 用量未知。

两者相加会重复计数，所以任何报表必须声明读的是哪一张。

**金额与用量是两个维度。** ``llm_cost_daily`` 的键里没有价格版本，因为一次
attempt 的价格是"发出那一刻"的事：``LlmCostPricedDaily`` 把同一批 attempt 再按
「发送时冻结的目录版本 + 币种」聚合一遍，金额只在冻结价上算（改价不追改历史）。
两张表的 ``attempts`` 数的是**同一次发出**，所以报表只能相减求 legacy（旧数据
没有价格元数据），**永远不能相加**。

三条不可让步的正确性：

* **usage 缺失就是未知**，绝不填 0 —— 0 会被下游当成"这次免费"，让预算和
  成本报表同时说谎。
* **发送前取消不记 attempt**：根本没发出去的请求不是用量。
* **落盘失败并回内存**：遥测丢一轮不等于账可以复原；下一次 flush 会补上。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from bot.db.models import LlmCostDaily, LlmCostFlushBatch, LlmCostPricedDaily
from bot.services import pricing, usage_scope
from bot.utils.timezone import now_shanghai_naive

log = logging.getLogger(__name__)

#: 与 llm_metrics 同样的节奏：主路径只加内存整数，落盘交给惰性定时器。
FLUSH_INTERVAL_SECONDS = 60.0

COUNTER_FIELDS: tuple[str, ...] = (
    "attempts",
    "successes",
    "failures",
    "timeouts",
    "empty_responses",
    "parse_errors",
    "usage_unknown",
)
TOKEN_FIELDS: tuple[str, ...] = (
    "prompt_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "thinking_tokens",
)

#: 台账键：(日, 归属类型, 群 id, 用途, 阶段, 供应商, 模型)
LedgerKey = tuple[str, str, int, str, str, str, str]

#: 发送时计价聚合的键：在台账键之后再加 (价格版本, 币种)。
PricedKey = tuple[str, str, int, str, str, str, str, int, str]

#: 计价聚合里可累加的字段。``amount_micro`` 也是整数微单位，跟别的计数字段一样
#: 只用整数加（不用浮点）。
PRICED_FIELDS: tuple[str, ...] = (
    "attempts",
    "priced_attempts",
    "unknown_attempts",
    "unknown_usage_attempts",
    "unknown_price_attempts",
    "amount_micro",
)

#: 未归群归属在库里的技术占位。0 永远不代表一个群。
NO_GROUP = 0

#: 在飞尝试的**回收**上限。它不再用于"提前丢弃"，只作为诊断阈值：
#: 真正的回收靠结算；超出这个数说明有调用方忘了结算，值得报警而不是删账。
_MAX_ATTEMPTS_TRACKED = 4096
#: 已结算去重表的保留上限（防无限增长）；它是**元数据**，不是账。
_SETTLED_MAX_ENTRIES = 8192
_RETENTION_DAYS = 3

_INT_LITERAL = re.compile(r"^[0-9]+$")

_lock = threading.RLock()  # 台账内部会重入（prune → 已落盘日期）
_counters: dict[LedgerKey, dict[str, int]] = {}
#: **发送时**计价聚合（键含价格版本与币种）。与 ``_counters`` 走同一份冻结批次、
#: 同一个去重 marker、同一个事务：不允许出现"用量写了、金额没写"的半批。
_priced_counters: dict[PricedKey, dict[str, int]] = {}
#: 仍然在飞（还没出结果）的 attempt：归属快照 + 去重键。
_attempts: dict[str, dict[str, Any]] = {}
#: 已经结算过的 attempt id：迟到/重复的结果只算一次。
_settled: dict[str, float] = {}
#: 已冻结、等待落库的批次。**不可变**对象：冻结时把当时的计数整体拍成一份
#: delta 快照，之后无论活 counter 怎么变、这次落库成功还是失败重试，写的都是
#: 同一份内容。旧批**不并回活 counter** —— 并回去就会和提交窗口里新到的请求
#: 混成一批，那批到底写没写过就再也说不清了。
_PENDING_BATCHES: "deque[_FrozenBatch]" = deque()
#: 真实的单写屏障：序列化「取走 → SQL → 提交 → 返回」整段。
#: ``_flushing`` 只是自动调度的提示，**不能**当互斥 —— 它既不是锁，也不足以
#: 解释"force 看到 counter 空就返回"。
_WRITE_LOCK: "asyncio.Lock | None" = None
_WRITE_LOCK_LOOP: Any = None
_session_factory: Any = None
#: 已经成功写进数据库的日期集合。只有落在它之前的内存桶才允许被清理。
_flushed_days: set[str] = set()
_last_flush = 0.0
_flushing = False
_flush_task: Any = None


@dataclass(frozen=True, slots=True)
class _FrozenBatch:
    """一批已冻结的增量。

    ``batch_id`` 与内容、台账行、模型**都无关**：每次冻结随机分配，所以两次
    内容完全相同的新请求也是两个不同对象，会各自加一次。
    """

    batch_id: str
    rows: tuple[tuple[LedgerKey, dict[str, int]], ...]
    #: 同一批 attempt 的**发送时计价**聚合。空 tuple = 这批没有可写的计价行
    #: （例如计价聚合被 prune 掉的日期），不是"金额为 0"。
    priced_rows: tuple[tuple[PricedKey, dict[str, int]], ...] = ()


def _write_lock() -> "asyncio.Lock":
    """取当前事件循环的单写锁；跨循环重建（测试换 loop / 重启）。"""

    global _WRITE_LOCK, _WRITE_LOCK_LOOP
    loop = asyncio.get_running_loop()
    if _WRITE_LOCK is None or _WRITE_LOCK_LOOP is not loop:
        _WRITE_LOCK = asyncio.Lock()
        _WRITE_LOCK_LOOP = loop
    return _WRITE_LOCK


def _freeze_locked() -> _FrozenBatch:
    """把当前内存计数拍成不可变批次（调用方必须已持有 ``_lock``）。"""

    global _batch_sequence
    _batch_sequence += 1
    rows = tuple(
        (key, dict(values)) for key, values in sorted(_counters.items())
    )
    _counters.clear()
    priced_rows = tuple(
        (key, dict(values)) for key, values in sorted(_priced_counters.items())
    )
    _priced_counters.clear()
    return _FrozenBatch(
        batch_id=f"{int(time.time() * 1e6):x}-{_batch_sequence:x}-{secrets.token_hex(6)}",
        rows=rows,
        priced_rows=priced_rows,
    )


def _batch_row(key: LedgerKey, values: dict[str, int]) -> dict[str, Any]:
    day, scope, group_id, purpose, stage, provider, model = key
    row: dict[str, Any] = {
        "usage_date": day,
        "scope": scope,
        "group_id": int(group_id),
        "purpose": purpose,
        "stage": stage,
        "provider": provider,
        "model": model,
    }
    row.update({field: int(values.get(field, 0)) for field in COUNTER_FIELDS})
    for field in TOKEN_FIELDS:
        value = values.get(field)
        # None 必须原样写 NULL：填 0 等于宣布这次调用零花费。
        row[field] = int(value) if value is not None else None
    return row


def _batch_priced_row(key: PricedKey, values: dict[str, Any]) -> dict[str, Any]:
    """把一条发送时计价聚合拍成落库行。

    ``price_snapshot`` 来自**冻结时**存下的条目文档（同一版本 + 同一
    provider/model 的条目必然相同），所以写库这段不需要、也不允许回头查目录。
    """

    day, scope, group_id, purpose, stage, provider, model, version, currency = key
    row: dict[str, Any] = {
        "usage_date": day,
        "scope": scope,
        "group_id": int(group_id),
        "purpose": purpose,
        "stage": stage,
        "provider": provider,
        "model": model,
        "price_version": int(version),
        "currency": currency,
        "price_snapshot": values.get("price_snapshot"),
    }
    row.update({field: int(values.get(field, 0)) for field in PRICED_FIELDS})
    return row


_batch_sequence = 0
_attempt_counter = 0


def configure(session_factory: Any) -> None:
    global _session_factory
    _session_factory = session_factory


def _today() -> str:
    try:
        return now_shanghai_naive().date().isoformat()
    except Exception:  # pragma: no cover - 时区工具不可用时退回 UTC 日期
        return date.today().isoformat()


def _key_for(
    scope: usage_scope.UsageScope,
    *,
    stage: str,
    provider: str,
    model: str,
) -> LedgerKey:
    return (
        _today(),
        scope.kind,
        int(scope.group_id) if scope.group_id is not None else NO_GROUP,
        usage_scope.effective_purpose(scope, stage or scope.stage),
        str(stage or scope.stage or "")[:32],
        str(provider or "")[:48],
        str(model or "")[:128],
    )


def _token_count(value: Any) -> int | None:
    """一个 token 计数的严格解析。

    只有**非负、有限、真正的整数**才算数。浮点（哪怕 12.0）、``nan``/``inf``、
    负数、bool 一律返回 ``None`` —— 调用方据此记 ``usage_unknown`` 而不是
    伪造一个 0。

    为什么这么严：这些数字会进成本报表和预算。``int(12.5) == 12`` 会让一次
    真实调用凭空少记一半 token；``int(-10) == -10`` 会让累计变成负数，于是
    额度看起来凭空多出来；``int(float('nan'))`` 直接抛异常，把整次尝试的
    计数一起吞掉（attempt 已经从在飞表里 pop 掉了）。
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value) or value < 0:
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or not _INT_LITERAL.match(text):
            return None
        parsed = int(text)
        return parsed if parsed >= 0 else None
    return None


_TOKEN_FIELD_NAMES: dict[str, tuple[str, ...]] = {
    "prompt": ("prompt_tokens", "input_tokens"),
    "completion": ("completion_tokens", "output_tokens"),
    "cached": ("cached_tokens", "cache_read_input_tokens", "cache_read_tokens"),
    "cache_write": ("cache_creation_input_tokens", "cache_write_tokens"),
    "thinking": ("thinking_tokens", "reasoning_tokens"),
}


def _token_fields(usage: Any) -> dict[str, tuple[bool, int | None]]:
    """每个 token 字段 → (上游报没报, 合法值或 None)。

    「报了但非法」（负数 / bool / 浮点 / nan / 非数字串）与「没报」必须分开：
    前者是**脏数据**，必须让这次调用标 unknown；后者只是协议里没有这一段。
    """

    result: dict[str, tuple[bool, int | None]] = {}
    for field, names in _TOKEN_FIELD_NAMES.items():
        present = False
        value: int | None = None
        for name in names:
            raw = getattr(usage, name, None)
            if raw is None and isinstance(usage, dict):
                raw = usage.get(name)
            if raw is None:
                continue
            present = True
            value = _token_count(raw)
            if value is not None:
                break
        result[field] = (present, value)
    return result


def _usage_value(usage: Any, *names: str) -> int | None:
    for name in names:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        if value is not None:
            parsed = _token_count(value)
            if parsed is not None:
                return parsed
    return None


def _usage_presence(usage: Any, *names: str) -> bool:
    """这个字段**上游报没报**（区别于「报了但是 0」）。"""

    if usage is None:
        return False
    for name in names:
        if getattr(usage, name, None) is not None:
            return True
        if isinstance(usage, dict) and usage.get(name) is not None:
            return True
    return False


def _apply(bucket: dict[str, Any], deltas: dict[str, int]) -> None:
    for name, value in deltas.items():
        if value:
            bucket[name] = bucket.get(name, 0) + value


def _add_locked(key: LedgerKey, deltas: dict[str, int]) -> None:
    bucket = _counters.setdefault(key, {})
    _apply(bucket, deltas)
    _prune_locked()


def _add_priced_locked(
    key: PricedKey,
    deltas: dict[str, int],
    *,
    snapshot: dict[str, Any] | None,
) -> None:
    """累加一条**发送时**计价聚合（调用方必须已持有 ``_lock``）。

    价格快照只在第一次遇到这个键时写入：键里已经含 (版本, provider, model,
    币种)，同一组合的条目永远相同，所以后来的 attempt 带等价快照是正常的。
    ``None`` 也要落下来 —— "发送时这个 provider/model 没有价格"是既成事实，
    不能留成"还没填"，否则报表会把缺价画成免费。
    """

    bucket = _priced_counters.setdefault(key, {})
    if "price_snapshot" not in bucket:
        bucket["price_snapshot"] = snapshot
    _apply(bucket, deltas)
    _prune_locked()


def _price_attempt(
    key: LedgerKey,
    price: pricing.PriceFreeze | None,
    *,
    prompt: int | None,
    output: int | None,
    cached: int | None,
    cache_write: int | None,
    usable: bool,
) -> tuple[PricedKey, dict[str, int]]:
    """把一次结算的用量在**发送时冻结的价**上算成金额。

    返回 ``(计价聚合键, 字段增量)``。金额未知时只记 ``unknown_attempts``，
    ``amount_micro`` 保持 0 —— 未知**不会**被写成"0 元"。

    能计价的条件：冻结时有价格条目、用量里有可解析的输入计数、没有坏字段。
    缺 ``output`` / ``cached`` / ``cache_write`` 按 0 算（embedding 只有输入；
    没用到缓存就不需要缓存单价 —— 只有**实际用到**的组件缺价才算未知）。
    但**输入计数缺失或坏掉时整次调用不可计价**：没有输入数就算不出输入成本，
    报一个"只有输出的金额"是假完整。
    """

    if price is None:  # 理论路径：结算时手上没有冻结价，按当前目录兜底
        price = pricing.freeze_price(key[5], key[6])
    priced_key: PricedKey = key + (int(price.price_version), price.currency)
    delta: dict[str, int] = {"attempts": 1}
    if not price.has_entry:
        # 发送时就没有这个 provider/model 的价格条目：金额未知，原因是"缺价"。
        delta["unknown_attempts"] = 1
        delta["unknown_price_attempts"] = 1
        return priced_key, delta
    if not usable or prompt is None:
        # 用量缺失或坏掉：连输入数都没有，任何人都不可能算出这次花的钱。
        delta["unknown_attempts"] = 1
        delta["unknown_usage_attempts"] = 1
        return priced_key, delta
    estimate = price.estimate(
        prompt_tokens=prompt,
        output_tokens=output or 0,
        cached_tokens=cached or 0,
        cache_write_tokens=cache_write or 0,
    )
    if estimate.known and estimate.micro is not None:
        # 0 单价 + 已知用量 = 明确已知的"这次免费"，算已计价。
        delta["priced_attempts"] = 1
        delta["amount_micro"] = int(estimate.micro)
    else:
        # 用量齐全，但**实际用到**的某个组件在冻结条目里没有单价。
        delta["unknown_attempts"] = 1
        delta["unknown_price_attempts"] = 1
    return priced_key, delta


def _prune_locked() -> None:
    """给未落盘的台账**兜底**，但绝不动尚未写进数据库的计数。

    这里删掉的每一个桶都还只是内存里的整数：它一旦被删，就再也不会落盘，
    于是那一天的花费从账上消失。所以判据必须是「这个日期**已经成功落过盘**」，
    而不是「它比今天早 N 天」—— 数据库长时间故障时，后者会把当天和昨天的
    计数一起清掉，等于把一次故障放大成一段静默的数据丢失。
    """

    if len(_counters) <= 4096:
        return
    # 已落盘日期在**锁外**取一次再传进来：早前这里在锁内再取一次，于是
    # 4097 个真实桶就会把 flush 死锁在非重入的 threading.Lock 里
    # （父代理实测 exit124 + traceback 卡在这里）。
    cutoff = min(_flushed_days) if _flushed_days else _today()
    for key in [k for k in _counters if k[0] < cutoff]:
        _counters.pop(key, None)
    # 计价聚合同样只在**已落盘**的日期上清理：删掉就是那天的金额永久消失。
    for key in [k for k in _priced_counters if k[0] < cutoff]:
        _priced_counters.pop(key, None)


def _scoped_override(
    scope: usage_scope.UsageScope,
    *,
    purpose: str | None,
    group_id: int | None,
    stage: str,
) -> usage_scope.UsageScope:
    """在**已有**归属上覆盖 purpose / group_id，其余字段（kind / bucket）保留。

    显式传 group_id 时只有当前上下文本来就是 group 归属才接受 —— 让一次
    system / private 调用凭空变成某个群的账单，正是我们要防的事。
    """

    resolved_group = scope.group_id
    if group_id is not None:
        if scope.kind != usage_scope.SCOPE_GROUP:
            raise group_costs_error("非群归属不能指定 group_id。")
        resolved_group = group_id
    return usage_scope.build_scope(
        kind=scope.kind,
        group_id=resolved_group,
        purpose=purpose,
        stage=stage,
        bucket=scope.bucket if scope.kind == usage_scope.SCOPE_PRIVATE_USER else None,
    )


def group_costs_error(message: str) -> ValueError:
    return ValueError(message)


def _new_attempt_id() -> str:
    global _attempt_counter
    with _lock:
        _attempt_counter += 1
        return f"{time.monotonic_ns():x}-{_attempt_counter:x}"


def record_attempt(
    *,
    stage: str = "",
    provider: str = "",
    model: str = "",
    purpose: str | None = None,
    group_id: int | None = None,
) -> str:
    """记一次**即将真实发出**的供应商请求，返回 attempt id。

    归属在**这一刻**快照：重试与回退各自新建一个 attempt，归属跟着上下文走，
    所以中途换了上下文的新尝试不会被算到旧账上。

    **价格也在这一刻冻结**（`pricing.freeze_price`）：结算时只用这份快照，
    目录后来改版不会追改这次调用的金额。价格目录没装配、或这一版里没有这个
    provider/model 时冻结的是"发送时就不知道价"这个事实，而不是 0。
    """

    try:
        scope = usage_scope.current_usage_scope()
        # 只在**显式**给了 purpose / group_id 时重建归属；仅仅指定 stage 不
        # 该重建 —— 重建会把当前上下文的 kind / group_id 丢掉（``build_scope``
        # 只看显式 group_id），于是群里的调用会被记成 group_id=0。
        if purpose is not None or group_id is not None:
            scope = _scoped_override(scope, purpose=purpose, group_id=group_id, stage=stage)
        elif stage:
            scope = usage_scope.with_stage(scope, stage)
        key = _key_for(scope, stage=stage, provider=provider, model=model)
        frozen_price = pricing.freeze_price(key[5], key[6])
        attempt_id = _new_attempt_id()
        with _lock:
            _attempts[attempt_id] = {
                "key": key,
                "created": time.monotonic(),
                "price": frozen_price,
            }
            # 在飞表**只在结算后回收**。曾经这里有一个
            # ``while len(_attempts) > CAP: pop(最老)``：上限一到，最老的那些
            # 还没结算的尝试就被丢掉，于是它们的 token 花费**永远进不了账**。
            # 真正的内存上界由「每次尝试必定结算一次」保证（异常路径也走
            # finally），而不是靠提前丢弃。
        _maybe_schedule_flush()
        return attempt_id
    except Exception:  # pragma: no cover - 统计绝不冒泡
        log.debug("group_costs.record_attempt failed", exc_info=True)
        return ""


def cancel_before_send(
    *,
    stage: str = "",
    provider: str = "",
    model: str = "",
    purpose: str | None = None,
    group_id: int | None = None,
) -> None:
    """请求**没有真的发出**（发送前取消/熔断/预算拒绝）：不记 attempt。"""

    return None


def record_result(
    *,
    attempt_id: str = "",
    usage: Any = None,
    success: bool | None = None,
    failed: bool = False,
    timed_out: bool = False,
    empty: bool = False,
    parse_error: bool = False,
    stage: str = "",
    provider: str = "",
    model: str = "",
    purpose: str | None = None,
    group_id: int | None = None,
) -> None:
    """结算一次 attempt。迟到/重复的结果只算第一次。"""

    try:
        deltas: dict[str, int] = {}
        tokens: dict[str, int] = {}
        key: LedgerKey | None = None
        #: **发送时**冻结的价格（record_attempt 那一刻取），结算只用它。
        price: pricing.PriceFreeze | None = None
        with _lock:
            entry = _attempts.pop(str(attempt_id or ""), None) if attempt_id else None
            if attempt_id and attempt_id in _settled:
                # 已经结算过（迟到响应 / 取消竞态 / 重复回调）。
                entry = None
                duplicate = True
            else:
                duplicate = False
            if attempt_id and not duplicate:
                _settled[attempt_id] = time.monotonic()
                if len(_settled) > _SETTLED_MAX_ENTRIES:
                    oldest = min(_settled, key=_settled.__getitem__)
                    _settled.pop(oldest, None)
            if entry is not None:
                key = entry["key"]
                price = entry.get("price")
                deltas["attempts"] = 1
        if key is None:
            if attempt_id:
                return
            # 没有 attempt 快照（例如后台补记）：现算一个键，同样**继承**当前
            # 归属而不是从零构造 —— 曾经这里无条件 build_scope，group_id 只看
            # 显式参数，于是群里的补记被记成 group_id=0。
            scope = usage_scope.current_usage_scope()
            if purpose is not None or group_id is not None:
                scope = _scoped_override(
                    scope, purpose=purpose, group_id=group_id, stage=stage
                )
            elif stage:
                scope = usage_scope.with_stage(scope, stage)
            key = _key_for(scope, stage=stage, provider=provider, model=model)
            # 没有 attempt 快照的补记没有"发送那一刻"，就按**现在**这一版冻结。
            price = pricing.freeze_price(key[5], key[6])
            deltas["attempts"] = 1

        if timed_out:
            deltas["timeouts"] = 1
        elif failed:
            deltas["failures"] = 1
        if empty:
            deltas["empty_responses"] = 1
        if parse_error:
            deltas["parse_errors"] = 1

        # 逐字段取数，并区分「上游没报」与「报了但是坏的」。后者必须标 unknown
        # —— 把它当成"没这个字段"会让一次带脏值的调用在报表上看起来完全正常。
        fields = _token_fields(usage)
        prompt = fields["prompt"][1]
        completion = fields["completion"][1]
        cached = fields["cached"][1]
        cache_write = fields["cache_write"][1]
        thinking = fields["thinking"][1]
        any_bad = any(value[0] and value[1] is None for value in fields.values())
        # 「成功」与「上游报没报 usage」是**两个维度**：一次成功但上游漏报
        # usage 的响应仍然是成功，只是金额未知。曾经的 ``has_usage`` 把两者
        # 绑在一起，于是 successes 记成 0 —— 账单看起来像这次调用没发生过。
        if success is None:
            success = not (failed or timed_out)
        if success:
            deltas["successes"] = 1
        # 逐字段覆盖：上游报了就是已知（哪怕是 0），没报就留 NULL。
        tokens: dict[str, int] = {}
        if prompt is not None:
            tokens["prompt_tokens"] = prompt
        if completion is not None:
            tokens["output_tokens"] = completion
        if cached is not None:
            tokens["cached_tokens"] = cached
        if cache_write is not None:
            tokens["cache_write_tokens"] = cache_write
        if thinking is not None:
            tokens["thinking_tokens"] = thinking
        # 「完全没有任何可解析的 usage」才算这次用量未知；有部分字段可用时，
        # 记入可用字段并留下 NULL 的空缺，计价时会因为缺段而拒绝给出完整金额。
        if not tokens or any_bad:
            deltas["usage_unknown"] = 1

        # 金额**逐次**算在发送时冻结的价上，再按 (版本, 币种) 汇总；不先累加
        # token 再用一个 usage_unknown 去估算整批请求。
        #
        # "够不够算钱"和上面"主台账记不记 usage_unknown"是**两把尺子**：主台账的
        # 口径不能动（部分字段可用就照记），但**少报一段用量就等于少算一段钱**。
        # 一次 chat 响应只报 prompt_tokens、输出段缺失时，按 0 输出计价会把这次
        # 调用报成"完整但便宜"，所以这里要求该阶段真正需要的段都在。
        # 嵌入只有输入段，其余阶段输入与输出都必须有。
        needed = {"prompt_tokens"} if key[4] == "embed" else {"prompt_tokens", "output_tokens"}
        priceable = not any_bad and needed <= set(tokens)
        priced_key, priced_delta = _price_attempt(
            key,
            price,
            prompt=prompt,
            output=completion,
            cached=cached,
            cache_write=cache_write,
            usable=priceable,
        )

        with _lock:
            _add_locked(key, deltas)
            if tokens:
                bucket = _counters.setdefault(key, {})
                for name, value in tokens.items():
                    current = bucket.get(name)
                    bucket[name] = (current or 0) + int(value)
            _add_priced_locked(
                priced_key,
                priced_delta,
                snapshot=price.document()["entry"] if price is not None else None,
            )
        _maybe_schedule_flush()
    except Exception:  # pragma: no cover - 统计绝不冒泡
        log.debug("group_costs.record_result failed", exc_info=True)


def _clear_flush_task(task: Any) -> None:
    global _flush_task
    if _flush_task is task:
        _flush_task = None


def _maybe_schedule_flush() -> None:
    global _flushing, _flush_task
    if _session_factory is None or _flushing:
        return
    if time.monotonic() - _last_flush < FLUSH_INTERVAL_SECONDS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _flushing = True
    try:
        _flush_task = loop.create_task(flush())
        _flush_task.add_done_callback(_clear_flush_task)
    except Exception:  # pragma: no cover
        _flush_task = None
        _flushing = False


async def flush(*, force: bool = False) -> int:
    """把计数落盘。

    三条不可让步的规则：

    1. **一把真实的异步单写锁**序列化「取走 → SQL → 提交 → 返回」整段。
       同步的 ``threading.RLock`` 只保护短内存操作，**绝不**跨 await 持有。
    2. **批次是冻结的不可变对象**。冻结时把当时的计数拍成 delta 快照；旧批
       不并回活 counter，所以提交窗口里新到的请求永远属于**下一批**，
       两批各自加一次。批 ID 与内容无关。
    3. **force 是持久化屏障**：它必须拿到真实锁，把 pending 与期间新到的
       counter 一并落库，才算"这次调用需要的账已经持久"。看到 counter 空就
       返回是不行的 —— 另一把 flush 可能正拿着在飞批次。

    ``_flushing`` 只作自动调度提示，既不充当互斥，也不让 force 短路。
    """

    global _last_flush, _flushing
    written = 0
    if _session_factory is None:
        return 0
    if not force and time.monotonic() - _last_flush < FLUSH_INTERVAL_SECONDS:
        return 0
    async with _write_lock():
        try:
            while True:
                batch: _FrozenBatch | None = None
                with _lock:
                    if not _PENDING_BATCHES and _counters:
                        _PENDING_BATCHES.append(_freeze_locked())
                if not _PENDING_BATCHES:
                    _last_flush = time.monotonic()
                    return written
                batch = _PENDING_BATCHES[0]
                rows = [_batch_row(key, values) for key, values in batch.rows]
                priced_rows = [
                    _batch_priced_row(key, values) for key, values in batch.priced_rows
                ]
                applied = await _write_batch(
                    batch.batch_id, rows, priced_rows=priced_rows
                )
                _PENDING_BATCHES.popleft()
                written += applied
                if not force:
                    break
                with _lock:
                    drained = not _counters
                if drained:
                    break
            _last_flush = time.monotonic()
            return written
        except asyncio.CancelledError:
            # 锁由 async with 可靠释放；被冻结的批次原样留在 _PENDING_BATCHES，
            # 下次按**同一** ID 重试：内容完全一致，已落库就整批跳过。
            raise
        except Exception:
            log.warning("group_costs.flush failed", exc_info=True)
            return written
        finally:
            _flushing = False


async def _write_batch(
    batch_id: str,
    rows: list[dict[str, Any]],
    *,
    priced_rows: list[dict[str, Any]] | None = None,
) -> int:
    """把一个冻结批次写进库（marker 与累加同一事务）。返回实际写入的主行数。

    主台账行与**发送时计价**行在**同一个事务**里写：出现"用量写了、金额没写"
    的半批，报表就会把已发出的调用显示成一文不值。去重 marker 也在这一个事务
    里，所以重放整批两条表一起跳过（不可能只跳一条）。
    """

    async with _session_factory() as session:
        marker = await session.execute(
            sqlite_insert(LlmCostFlushBatch)
            .values(batch_id=batch_id, row_count=len(rows))
            .on_conflict_do_nothing(index_elements=["batch_id"])
        )
        # marker 冲突 = 同一批已经落过库：整批跳过，不重复扣。
        if int(marker.rowcount or 0) != 1:
            await session.rollback()
            return 0
        for row in rows:
            stmt = sqlite_insert(LlmCostDaily).values(**row)
            # token 列：None 与整数要区别对待 —— NULL 不能被累加成 0，
            # 所以只在「本次有值」时才累加。
            assignments: dict[str, Any] = {
                field: getattr(LlmCostDaily, field) + getattr(stmt.excluded, field)
                for field in COUNTER_FIELDS
            }
            for field in TOKEN_FIELDS:
                if row.get(field) is not None:
                    assignments[field] = (
                        func.coalesce(getattr(LlmCostDaily, field), 0)
                        + getattr(stmt.excluded, field)
                    )
                else:
                    assignments[field] = getattr(LlmCostDaily, field)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    "usage_date",
                    "scope",
                    "group_id",
                    "purpose",
                    "stage",
                    "provider",
                    "model",
                ],
                set_=assignments | {"updated_at": func.now()},
            )
            await session.execute(stmt)
        for row in priced_rows or []:
            stmt = sqlite_insert(LlmCostPricedDaily).values(**row)
            assignments = {
                field: getattr(LlmCostPricedDaily, field)
                + getattr(stmt.excluded, field)
                for field in PRICED_FIELDS
            }
            # 同一把键（含版本与币种）的条目必然相同；用 coalesce 只是不让一次
            # NULL 覆盖掉先前已经落库的快照。
            assignments["price_snapshot"] = func.coalesce(
                LlmCostPricedDaily.price_snapshot, stmt.excluded.price_snapshot
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    "usage_date",
                    "scope",
                    "group_id",
                    "purpose",
                    "stage",
                    "provider",
                    "model",
                    "price_version",
                    "currency",
                ],
                set_=assignments | {"updated_at": func.now()},
            )
            await session.execute(stmt)
        await session.commit()
    return len(rows)



def _merge_back(batch: dict[LedgerKey, dict[str, int]]) -> None:
    if not batch:
        return
    try:
        with _lock:
            for key, values in batch.items():
                bucket = _counters.setdefault(key, {})
                _apply(bucket, values)
    except Exception:  # pragma: no cover
        pass


def snapshot() -> dict[LedgerKey, dict[str, int]]:
    """当前内存计数（测试与自检用）。"""

    with _lock:
        return {key: dict(value) for key, value in _counters.items()}


def priced_snapshot() -> dict[PricedKey, dict[str, Any]]:
    """当前内存里**未落盘**的发送时计价聚合（测试与自检用）。"""

    with _lock:
        return {key: dict(value) for key, value in _priced_counters.items()}


def inflight_attempts() -> int:
    with _lock:
        return len(_attempts)


def reset() -> None:
    global _session_factory, _last_flush, _flushing, _attempt_counter
    global _WRITE_LOCK, _WRITE_LOCK_LOOP, _batch_sequence
    with _lock:
        _counters.clear()
        _priced_counters.clear()
        # 冻结批次属于上一个测试/上一次运行的账：留着它们会在下一个 fixture
        # 的库里写入别人的数字。清掉是**清测试残留**，不是"清掉仍需重试的账" ——
        # 真实运行中 reset 之后，pending 批会被重新冻结自新 counter。
        _PENDING_BATCHES.clear()
        _batch_sequence = 0
    with _lock:
        _attempts.clear()
        _settled.clear()
        _flushed_days.clear()
    _WRITE_LOCK = None
    _WRITE_LOCK_LOOP = None
    _session_factory = None
    _last_flush = 0.0
    _flushing = False
    _attempt_counter = 0

async def query_group_costs(
    session: Any,
    *,
    group_id: int,
    days: int = 7,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """按群读明细台账（**不含**金额：计价不在这里做）。

    刻意只返回 token 与计数：把金额塞进这里会让这张表同时承担"用量"和"价格"
    两件事，而价格是版本化的、可能被改 —— 混在一起读出来的数字没法解释。
    金额由 :mod:`bot.services.pricing` 按价格版本在读侧算。

    ``today`` 是**时间窗冻结值**的入口：报表 getter 把一次读到的"今天"同时传给
    主表过滤、计价表过滤和元数据，所以跨午夜的两次读不会各用一个日期（见
    :func:`query_group_costs_report`）。留空 = 各调用点自己取当前时间，兼容旧行为。
    """

    from sqlalchemy import select

    start = window_start(days, today=today)
    result = await session.execute(
        select(LlmCostDaily)
        .where(
            LlmCostDaily.scope == "group",
            LlmCostDaily.group_id == int(group_id),
            LlmCostDaily.usage_date >= start,
        )
        .order_by(LlmCostDaily.usage_date.asc(), LlmCostDaily.id.asc())
    )
    rows: list[dict[str, Any]] = []
    for row in result.scalars().all():
        rows.append(
            {
                "usage_date": str(row.usage_date),
                "purpose": str(row.purpose),
                "stage": str(row.stage),
                "provider": str(row.provider),
                "model": str(row.model),
                "attempts": int(row.attempts or 0),
                "successes": int(row.successes or 0),
                "failures": int(row.failures or 0),
                "timeouts": int(row.timeouts or 0),
                "empty_responses": int(row.empty_responses or 0),
                "usage_unknown": int(row.usage_unknown or 0),
                # None = 未知，绝不用 0 顶替（0 会被读成"这次免费"）。
                "prompt_tokens": row.prompt_tokens,
                "output_tokens": row.output_tokens,
                "cached_tokens": row.cached_tokens,
                "cache_write_tokens": row.cache_write_tokens,
                "thinking_tokens": row.thinking_tokens,
            }
        )
    return rows


async def query_group_costs_priced(
    session: Any,
    *,
    group_id: int,
    days: int = 7,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """按群读**发送时计价**聚合（与 :func:`query_group_costs` 同一时间窗）。

    这是 ``llm_cost_daily`` 的**伴随维度**：同一批 attempt，按发出时冻结的
    (价格版本, 币种) 再聚合一遍。两张表的 ``attempts`` 数的是同一次发出，
    所以报表只能相减求 legacy，**永远不能相加**。

    ``today`` 与 :func:`query_group_costs` 同义：时间窗冻结值入口。
    """

    from sqlalchemy import select

    start = window_start(days, today=today)
    result = await session.execute(
        select(LlmCostPricedDaily)
        .where(
            LlmCostPricedDaily.scope == "group",
            LlmCostPricedDaily.group_id == int(group_id),
            LlmCostPricedDaily.usage_date >= start,
        )
        .order_by(
            LlmCostPricedDaily.usage_date.asc(),
            LlmCostPricedDaily.id.asc(),
        )
    )
    rows: list[dict[str, Any]] = []
    for row in result.scalars().all():
        rows.append(
            {
                "usage_date": str(row.usage_date),
                "purpose": str(row.purpose),
                "stage": str(row.stage),
                "provider": str(row.provider),
                "model": str(row.model),
                "price_version": int(row.price_version or 0),
                "currency": str(row.currency or ""),
                "price_snapshot": row.price_snapshot,
                "attempts": int(row.attempts or 0),
                "priced_attempts": int(row.priced_attempts or 0),
                "unknown_attempts": int(row.unknown_attempts or 0),
                "unknown_usage_attempts": int(row.unknown_usage_attempts or 0),
                "unknown_price_attempts": int(row.unknown_price_attempts or 0),
                "amount_micro": int(row.amount_micro or 0),
            }
        )
    return rows


#: ``build_cost_report`` 的每个币种桶里都会出现的字段（金额一律整数微单位）。
_CURRENCY_FIELDS: tuple[str, ...] = (
    "amount_micro",
    "attempts",
    "priced_attempts",
    "unknown_attempts",
    "unknown_usage_attempts",
    "unknown_price_attempts",
)


def _empty_currency_bucket(currency: str) -> dict[str, Any]:
    bucket: dict[str, Any] = {"currency": currency, "legacy_attempts": 0, "price_versions": []}
    bucket.update({field: 0 for field in _CURRENCY_FIELDS})
    return bucket


def _ledger_identity(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """一张明细行 / 一条计价行在"同一批 attempt"意义上的身份。"""

    return (
        str(row.get("usage_date") or ""),
        str(row.get("purpose") or ""),
        str(row.get("stage") or ""),
        str(row.get("provider") or ""),
        str(row.get("model") or ""),
    )


def build_cost_report(
    rows: list[dict[str, Any]],
    priced_rows: list[dict[str, Any]],
    *,
    days: int,
    today: date | None = None,
) -> dict[str, Any]:
    """把用量明细与发送时计价聚合成**按币种**的金额报表（纯函数）。

    三条不许含糊的地方：

    * **未知不会被已知吞掉**。``amount_micro`` 只是"已计价部分"，出现未知
      （用量缺失 / 缺价 / 旧数据没有元数据）时 ``complete`` 为 False，
      报表必须这么显示，而不是把它当总成本。
    * **不跨币种相加**。USD 与 CNY 各自一行，``cross_currency_total`` 恒为
      ``None``；没有价格元数据的 attempt 落在 ``currency == ""`` 这个**独立**
      未知桶里，绝不折算成 USD。
    * **两张表不许相加**。legacy（主表有、计价表没有的 attempt）用相减得出，
      并且只作为"未计价覆盖"计数，不猜金额。
    """

    days = _clamp_report_days(days)
    main_attempts = 0
    usage_unknown_attempts = 0
    main_by_identity: dict[tuple[str, str, str, str, str], int] = {}
    priced_by_identity: dict[tuple[str, str, str, str, str], int] = {}
    for row in priced_rows:
        identity = _ledger_identity(row)
        priced_by_identity[identity] = priced_by_identity.get(identity, 0) + int(
            row.get("attempts") or 0
        )

    buckets: dict[str, dict[str, Any]] = {}
    amounts_by_identity: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for row in priced_rows:
        currency = str(row.get("currency") or "")
        bucket = buckets.setdefault(currency, _empty_currency_bucket(currency))
        version = int(row.get("price_version") or 0)
        version_entry = None
        for item in bucket["price_versions"]:
            if item["price_version"] == version:
                version_entry = item
                break
        if version_entry is None:
            version_entry = {
                "price_version": version,
                "currency": currency,
                "amount_micro": 0,
                "attempts": 0,
                "priced_attempts": 0,
                "unknown_attempts": 0,
                # 快照为 NULL = 发送时这一版里没有这个 provider/model 的条目。
                "has_price_entry": row.get("price_snapshot") is not None,
            }
            bucket["price_versions"].append(version_entry)
        for field in _CURRENCY_FIELDS:
            value = int(row.get(field) or 0)
            bucket[field] += value
            if field in version_entry:
                version_entry[field] += value
        amounts_by_identity.setdefault(_ledger_identity(row), []).append(
            {
                "currency": currency,
                "price_version": version,
                "amount_micro": int(row.get("amount_micro") or 0),
                "attempts": int(row.get("attempts") or 0),
                "priced_attempts": int(row.get("priced_attempts") or 0),
                "unknown_attempts": int(row.get("unknown_attempts") or 0),
                "unknown_usage_attempts": int(row.get("unknown_usage_attempts") or 0),
                "unknown_price_attempts": int(row.get("unknown_price_attempts") or 0),
                # 快照为 NULL = 发送时这一版目录里**没有**这个 provider/model：
                # 界面据此区分"缺价"与"用量缺失"，两者都不许显示成 0 元。
                "has_price_entry": row.get("price_snapshot") is not None,
            }
        )

    row_reports: list[dict[str, Any]] = []
    for row in rows:
        identity = _ledger_identity(row)
        attempts = int(row.get("attempts") or 0)
        main_attempts += attempts
        main_by_identity[identity] = main_by_identity.get(identity, 0) + attempts
        usage_unknown_attempts += int(row.get("usage_unknown") or 0)
        item = dict(row)
        amounts = amounts_by_identity.get(identity, [])
        item["amounts"] = amounts
        # 主台账记了、计价表没记 = 这一行里有多少次是"价格元数据出现之前"的旧
        # 调用；界面必须把它当**未计价覆盖**显示，而不是当作 0 元。
        priced_here = sum(int(entry.get("attempts") or 0) for entry in amounts)
        item["unpriced_attempts"] = max(0, attempts - priced_here)
        row_reports.append(item)

    # legacy：主台账记了这次 attempt，计价附表里却没有对应行 —— 那是**旧数据**
    # 在计价表出现之前留下的用量，金额按"未知"处理，不回填、不猜价。
    legacy_attempts = 0
    unmatched_priced_attempts = 0
    # 对账必须遍历**两侧身份的全集**。只遍历主表会漏掉"计价表有、主表没有"的孤儿
    # 行：父在 31bda38 上实测到同一个本来空的合成群只写进一条计价行后，
    # ``attempts=0 / priced_attempts=1 / amount_by_currency.USD=43000`` 却仍然
    # ``amount_complete=True`` —— 声称完整的同时给出互相矛盾的次数与金额。
    # 孤儿**不是** legacy：legacy 的定义是"主台账记了、计价表没记"，方向正好相反。
    for identity, attempts in main_by_identity.items():
        gap = attempts - priced_by_identity.get(identity, 0)
        if gap > 0:
            legacy_attempts += gap
    for identity, attempts in priced_by_identity.items():
        overflow = attempts - main_by_identity.get(identity, 0)
        if overflow > 0:
            unmatched_priced_attempts += overflow
    if legacy_attempts:
        bucket = buckets.setdefault("", _empty_currency_bucket(""))
        bucket["legacy_attempts"] += legacy_attempts
        bucket["attempts"] += legacy_attempts

    currencies: list[dict[str, Any]] = []
    for currency in sorted(buckets):
        bucket = buckets[currency]
        bucket["price_versions"].sort(key=lambda item: int(item["price_version"]))
        bucket["amount_is_partial"] = bool(
            bucket["unknown_attempts"] or bucket["legacy_attempts"]
        )
        bucket["complete"] = not bucket["amount_is_partial"]
        if currency == "":
            bucket["note"] = (
                "币种未知：发送时没有价格条目，或该行是价格元数据出现之前的旧数据。"
                "这里没有金额，也**不**折算成任何货币。"
            )
        else:
            bucket["note"] = (
                f"{currency} 的已计价部分；出现未知尝试时这不是完整成本。"
            )
        currencies.append(bucket)

    totals: dict[str, Any] = {
        "attempts": main_attempts,
        "usage_unknown_attempts": usage_unknown_attempts,
        "legacy_attempts": legacy_attempts,
        "priced_attempts": sum(int(b["priced_attempts"]) for b in currencies),
        "unknown_attempts": sum(int(b["unknown_attempts"]) for b in currencies),
        "unknown_usage_attempts": sum(
            int(b["unknown_usage_attempts"]) for b in currencies
        ),
        "unknown_price_attempts": sum(
            int(b["unknown_price_attempts"]) for b in currencies
        ),
        "amount_by_currency": {
            str(b["currency"]): int(b["amount_micro"])
            for b in currencies
            if str(b["currency"])
        },
    }
    return {
        "days": days,
        "window_start": window_start(days, today=today),
        # 用量与金额是**两张表**的两个维度：这里明说，免得下游把 attempts 相加。
        "source_table": "llm_cost_daily",
        "amount_source_table": "llm_cost_priced_daily",
        "estimated": True,
        # 完整 = 每一个币种桶里都没有未知（缺价 / 缺用量 / 旧数据），且没有
        # "计价表有、主表没有"这种对不上的行。出现任何一种都不许报成完整成本。
        "amount_complete": (
            unmatched_priced_attempts == 0
            and all(bool(bucket["complete"]) for bucket in currencies)
        ),
        "cross_currency_total": None,
        "note": (
            "明细口径：一次真实发出供应商请求即一条 attempt，重试与回退各记各的。"
            "金额按**发出时冻结**的价格版本计价，改价不追改历史；"
            "这是估算，**不是**供应商结算账单。与 /cost 看板的 llm_usage_daily"
            "（只记成功调用）口径不同，不要相加。"
        ),
        "totals": totals,
        "currencies": currencies,
        "reconciliation": {
            "main_attempts": main_attempts,
            "priced_table_attempts": sum(priced_by_identity.values()),
            "legacy_attempts": legacy_attempts,
            "unmatched_priced_attempts": unmatched_priced_attempts,
        },
        "rows": row_reports,
    }


class ReportSnapshotUnavailable(RuntimeError):
    """报表**无法建立或无法确定**一致的读快照。

    这不是"可以退回旧读法"的可选路径：退回就等于两条 SELECT 各拿一个快照，
    报表可能把上一代的用量和下一代的金额拼在一起，还报 ``amount_complete=True``。
    宁可明确失败（API 失败路径 → 前端显示"状态未知"），也不返回自相矛盾的数字。
    """


async def _open_report_read_snapshot(session: Any) -> bool:
    """给报表的两次 SELECT 开一个**真实**的读事务（只在需要且安全时）。

    为什么不能只写 ``async with session.begin()``：pysqlite/aiosqlite 默认
    **不在 SELECT 前真正发 BEGIN**，SQLAlchemy 那一层的 ``begin()`` 对它是 no-op
    —— 两条 SELECT 各拿一个快照，中间提交的新行可能只出现在第二张表里，报表就
    混了两代（31bda38 的实测：main=5 / priced=6 而 ``unmatched=0``）。
    SQLite 的读事务在**第一条读语句**处取快照并保持到 COMMIT/ROLLBACK，
    所以这里显式发 ``BEGIN``。

    只在连接**本来不在事务里**时发：否则 SQLite 会报
    ``cannot start a transaction within a transaction``；而已经有事务时，两次读
    本来就在同一个快照里，什么都不用做（调用方的事务也不许被我们碰）。
    非 SQLite 交给 SQLAlchemy 自己 BEGIN。

    快照状态**建立不了或确定不了**（取不到连接、拿不到底层驱动连接、读不出
    ``in_transaction``）→ 抛 :class:`ReportSnapshotUnavailable`，**不**静默返回
    False 装作"安全跳过"：那正好会退回不一致读。

    返回 True 表示"这个读事务是我们开的，读完要 COMMIT 掉"。
    """

    try:
        connection = await session.connection()
    except Exception as exc:
        raise ReportSnapshotUnavailable(
            f"无法取得数据库连接来建立一致读快照：{type(exc).__name__}: {exc}"
        ) from exc
    if str(getattr(getattr(connection, "dialect", None), "name", "")) != "sqlite":
        # 非 SQLite（或拿不到方言名）交给 SQLAlchemy 自己的事务，无需也不该插 BEGIN。
        return False
    try:
        raw = await connection.get_raw_connection()
    except Exception as exc:
        raise ReportSnapshotUnavailable(
            f"无法取得底层驱动连接来确认事务状态：{type(exc).__name__}: {exc}"
        ) from exc
    driver = getattr(raw, "driver_connection", None)
    if driver is None:
        raise ReportSnapshotUnavailable(
            "无法确认底层连接是否已在事务里：驱动连接没有 in_transaction 状态"
        )
    try:
        in_transaction = bool(driver.in_transaction)
    except AttributeError as exc:
        raise ReportSnapshotUnavailable(
            "无法确认底层连接是否已在事务里：驱动连接没有 in_transaction 状态"
        ) from exc
    except Exception as exc:
        raise ReportSnapshotUnavailable(
            f"读取底层连接事务状态失败：{type(exc).__name__}: {exc}"
        ) from exc
    if in_transaction:
        return False
    await session.execute(text("BEGIN"))
    return True


async def query_group_costs_report(
    session: Any,
    *,
    group_id: int,
    days: int = 7,
    today: date | None = None,
) -> dict[str, Any]:
    """明细用量 + 发送时计价 → 按币种的报表（API 直接用这个）。

    两张表必须在**同一个数据库读快照**里读：账本虽然同事务写入，但两条 SELECT
    之间只要有别的会话提交，报表就会把上一代的用量和下一代的金额拼在一起。
    一致性由 :func:`_open_report_read_snapshot` 保证（显式 BEGIN，不长期占锁）。

    **时间窗只冻结一次**：入口读一次"今天"、夹紧一次 ``days``，主表过滤、计价表
    过滤和 :func:`build_cost_report` 的 ``window_start`` 都用同一个值。否则跨午夜
    时会出现"主表读昨天、计价表读今天、元数据写今天"这种自相矛盾的报表：完整的
    同事务计价行反而被算成 legacy。``today`` 可显式传入（测试用），留空则取一次
    当前上海时间 —— 这是**一次调用内的冻结**，不是跨报表的全局缓存。
    """

    report_today = today if today is not None else now_shanghai_naive().date()
    report_days = _clamp_report_days(days)
    opened = await _open_report_read_snapshot(session)
    try:
        rows = await query_group_costs(
            session, group_id=group_id, days=report_days, today=report_today
        )
        priced_rows = await query_group_costs_priced(
            session, group_id=group_id, days=report_days, today=report_today
        )
    except BaseException:
        # 读失败时也要放掉我们开的只读事务，但**不能**让放锁失败盖掉真正的读取异常。
        if opened:
            with contextlib.suppress(Exception):
                await session.execute(text("COMMIT"))
        raise
    if opened:
        # 只读事务读到这里就够了，立刻放掉：不留写锁，也不占住供应商发送。
        await session.execute(text("COMMIT"))
    return build_cost_report(rows, priced_rows, days=report_days, today=report_today)


def _clamp_report_days(days: int) -> int:
    """报表窗口天数：1..90（唯一实现；冻结时间窗时也用它夹紧）。"""

    return max(1, min(90, int(days)))


def window_start(days: int, *, today: date | None = None) -> str:
    """窗口起点（含当天）。和 llm_metrics.window_start 同一口径。"""

    from datetime import timedelta

    days = _clamp_report_days(days)
    base = today or now_shanghai_naive().date()
    return (base - timedelta(days=days - 1)).isoformat()
