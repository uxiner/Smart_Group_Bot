"""Bounded, best-effort process-local observations; no business payload retention.

Only terminal logical calls are counted, including admission wait. Calls cancelled
before their coroutine starts have no event. Snapshot/reset are internal APIs,
not authorization boundaries or public/admin endpoints.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable, Coroutine
from functools import wraps
from threading import Lock
from typing import Any

MAX_EVENTS = 64
MAX_COUNTER = 2**63 - 1
MAX_DURATION_SECONDS = 1e12
_GROUP_OPERATIONS = (
    "api_model_query",
    "bilibili_search",
    "conversation_recall",
    "doubao_tts",
    "memory_manage",
    "mihomo_doc",
    "movie_info",
    "music_search",
    "remember",
    "routeros_doc",
    "rule_manage",
    "send_meme",
    "send_sticker",
    "vote_ban",
    "webfetch",
    "websearch",
    "weibo_search",
    "other",
)
_OPERATIONS = {
    "group_skill": _GROUP_OPERATIONS,
    "private_search": ("websearch", "other"),
    "private_tts": ("deliver_private_reply", "other"),
    "other": ("other",),
}
_OUTCOMES = ("success", "failure", "timeout", "cancelled", "ambiguous", "other")
_ERRORS = (
    "none",
    "business",
    "timeout",
    "cancelled",
    "exception",
    "uncertain",
    "other",
)
MAX_AGGREGATES = sum(map(len, _OPERATIONS.values())) * len(_OUTCOMES) * len(_ERRORS)


def _label(value: Any, allowed: Any) -> str:
    # Return the canonical constant, never the caller's string or subclass.
    if type(value) is str:
        for label in allowed:
            if value == label:
                return label
    return "other"


def _duration(value: Any) -> float:
    if type(value) not in (int, float):
        return 0.0
    try:
        number = float(value)
    except OverflowError:
        return MAX_DURATION_SECONDS
    if not math.isfinite(number):
        return 0.0
    return min(MAX_DURATION_SECONDS, max(0.0, number))


class ToolObservability:
    """Fixed label space, 64 scalar-only events, saturating counts/durations.

    A short synchronous lock makes record/reset/snapshot coherent across threads;
    there are no threads created here, external callbacks, persistence or I/O.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._aggregates: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def record(
        self,
        scope: str,
        operation: str,
        outcome: str,
        duration_seconds: float,
        error: str = "none",
    ) -> None:
        scope = _label(scope, _OPERATIONS)
        operation = _label(operation, _OPERATIONS[scope])
        outcome = _label(outcome, _OUTCOMES)
        error = _label(error, _ERRORS)
        duration_seconds = _duration(duration_seconds)
        event = {
            "scope": scope,
            "operation": operation,
            "outcome": outcome,
            "error": error,
            "duration_seconds": duration_seconds,
        }
        key = (scope, operation, outcome, error)
        with self._lock:
            aggregate = self._aggregates.get(key)
            if aggregate is None:
                aggregate = {**event, "calls": 0, "duration_seconds": 0.0}
                self._aggregates[key] = aggregate
            aggregate["calls"] = min(MAX_COUNTER, aggregate["calls"] + 1)
            aggregate["duration_seconds"] = min(
                MAX_DURATION_SECONDS, aggregate["duration_seconds"] + duration_seconds
            )
            self._events.append(event)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return {
                "events": [dict(event) for event in self._events],
                "aggregates": [
                    dict(self._aggregates[key]) for key in sorted(self._aggregates)
                ],
            }

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._aggregates.clear()


observer = ToolObservability()


def snapshot() -> dict[str, list[dict[str, Any]]]:
    return observer.snapshot()


def reset() -> None:
    observer.reset()


def _result_labels(scope: str, result: Any) -> tuple[str, str]:
    if scope == "private_tts":
        if result.delivered is True:
            if result.complete is True and not result.error:
                return "success", "none"
            return "ambiguous", "uncertain"
        return "failure", "business"
    error = result.error
    if error in ("tool_outcome_ambiguous", "partial_send"):
        return "ambiguous", "uncertain"
    if error in ("tool_timeout", "search_timeout", "timeout", "TimeoutError"):
        return "timeout", "timeout"
    if error == "tool_failed":
        return "failure", "exception"
    # Inspect only fixed boolean delivery flags, never copy arbitrary payloads.
    payload = result.payload
    if type(payload) is dict and (
        payload.get("complete") is False or payload.get("voice_complete") is False
    ):
        return "ambiguous", "uncertain"
    if result.ok is True and not error:
        return "success", "none"
    return "failure", "business"


def _finish(scope: str, operation: str, started: float | None, result: Any) -> None:
    try:
        outcome, error = _result_labels(scope, result)
        observer.record(scope, operation, outcome, time.monotonic() - started, error)
    except Exception:
        # Includes classifier, clock and recorder failures. No payload logging.
        pass


def _failed(
    scope: str, operation: str, started: float | None, outcome: str, error: str
) -> None:
    try:
        observer.record(scope, operation, outcome, time.monotonic() - started, error)
    except Exception:
        pass


def observe_tool(
    scope: str, operation: str | None = None
) -> Callable[
    [Callable[..., Coroutine[Any, Any, Any]]], Callable[..., Coroutine[Any, Any, Any]]
]:
    """Wrap a logical async boundary without changing scheduling or results.

    Private TTS counts voice-request delivery orchestration, not plain-text
    replies, synthesis segments or fallback attempts. Group names come only
    from the keyword-only name argument. Unknown labels collapse to other.
    """

    def decorate(function: Callable[..., Coroutine[Any, Any, Any]]) -> Any:
        @wraps(function)
        async def observed(*args: Any, **kwargs: Any) -> Any:
            started = None
            active = False
            name = "other"
            try:
                active = scope != "private_tts" or (
                    type(kwargs.get("delivery")) is str
                    and kwargs["delivery"].strip().lower() == "voice"
                )
                name = operation if operation is not None else kwargs.get("name")
                if active:
                    started = time.monotonic()
            except Exception:
                active = False
            try:
                result = await function(*args, **kwargs)
            except asyncio.CancelledError:
                if active:
                    _failed(scope, name, started, "cancelled", "cancelled")
                raise
            except TimeoutError:
                if active:
                    _failed(scope, name, started, "timeout", "timeout")
                raise
            except Exception:
                if active:
                    _failed(scope, name, started, "failure", "exception")
                raise
            if active:
                _finish(scope, name, started, result)
            return result

        return observed

    return decorate
