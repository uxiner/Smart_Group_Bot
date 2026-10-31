"""Process-local numeric observations of completed context assemblies only.

No prompt access, token estimation, I/O, persistence, or scope identifiers. This
internal producer is not a health check, request-usage meter, or public endpoint.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from threading import Lock
from time import time_ns
from typing import Literal, Protocol

# Mirrors context_gate.TRIM_PRIORITY without importing the assembler (no cycle).
TRIM_LAYERS: tuple[str, ...] = ("history", "search_records", "memory_recall")
COUNTER_MAX = (1 << 63) - 1


class _Trim(Protocol):
    layer: str
    dropped_messages: int
    truncated_messages: int
    dropped_tokens: int


@dataclass(frozen=True, slots=True)
class TrimCounts:
    """Exact sample counts, or saturating cumulative counts, for one fixed layer."""

    layer: str
    dropped_messages: int = 0
    truncated_messages: int = 0
    dropped_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ContextObservation:
    """Existing estimator-based assembly numbers, not upstream billed usage.

    used_tokens excludes reserve_tokens; over_budget already accounts for reserve
    using the assembler's unchanged calculation. Nothing is recomputed here.
    """

    used_tokens: int
    budget_tokens: int
    over_budget: bool
    sampled_at_unix_ns: int
    trims: tuple[TrimCounts, ...]


@dataclass(frozen=True, slots=True)
class ContextObservationSnapshot:
    """One global latest sample and three fixed aggregate rows since reset.

    not_observed means unknown, not a healthy zero. No per-scope attribution or
    history exists. Counts saturate at COUNTER_MAX instead of growing bigints.
    """

    state: Literal["not_observed", "observed"]
    latest: ContextObservation | None
    observation_count: int
    trim_totals: tuple[TrimCounts, ...]


def _zero_counts() -> tuple[TrimCounts, ...]:
    return tuple(TrimCounts(layer) for layer in TRIM_LAYERS)


def _number(value: int) -> int:
    # Reject rather than misreport an unrepresentable sample. The assembler's
    # best-effort boundary isolates this failure; no sample text is retained.
    if type(value) is not int or not 0 <= value <= COUNTER_MAX:
        raise ValueError("observation requires a nonnegative signed-64-bit integer")
    return value


_lock = Lock()
_snapshot = ContextObservationSnapshot("not_observed", None, 0, _zero_counts())


def record_context_assembly(
    *,
    used_tokens: int,
    budget_tokens: int,
    over_budget: bool,
    trims: Iterable[_Trim],
) -> None:
    """Publish already-produced numeric fields, never the assembly/messages.

    Only TRIM_LAYERS are counted; legacy custom trim_order names cannot create
    keys. This internal writer may raise; its assembler caller isolates failures.
    Sample preparation happens before publication, so failure leaves state intact.
    """
    global _snapshot

    used = _number(used_tokens)
    budget = _number(budget_tokens)
    if type(over_budget) is not bool:
        raise ValueError("observation requires a boolean over_budget")
    counts = [[0, 0, 0] for _ in TRIM_LAYERS]
    for trim in trims:
        if trim.layer not in TRIM_LAYERS:
            continue
        row = counts[TRIM_LAYERS.index(trim.layer)]
        row[0] = _number(row[0] + _number(trim.dropped_messages))
        row[1] = _number(row[1] + _number(trim.truncated_messages))
        row[2] = _number(row[2] + _number(trim.dropped_tokens))
    sample_counts = tuple(
        TrimCounts(layer, *row) for layer, row in zip(TRIM_LAYERS, counts, strict=True)
    )
    with _lock:
        latest = ContextObservation(
            used, budget, over_budget, _number(time_ns()), sample_counts
        )
        totals = tuple(
            TrimCounts(
                old.layer,
                min(COUNTER_MAX, old.dropped_messages + new.dropped_messages),
                min(COUNTER_MAX, old.truncated_messages + new.truncated_messages),
                min(COUNTER_MAX, old.dropped_tokens + new.dropped_tokens),
            )
            for old, new in zip(_snapshot.trim_totals, sample_counts, strict=True)
        )
        _snapshot = ContextObservationSnapshot(
            "observed",
            latest,
            min(COUNTER_MAX, _snapshot.observation_count + 1),
            totals,
        )


def snapshot_context_observations() -> ContextObservationSnapshot:
    """Read a coherent immutable snapshot without retaining any mutable inputs."""
    with _lock:
        return _snapshot


def reset_context_observations() -> None:
    """Clear process-local observations; internal/test API, not a public command."""
    global _snapshot

    with _lock:
        _snapshot = ContextObservationSnapshot("not_observed", None, 0, _zero_counts())
