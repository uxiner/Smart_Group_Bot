"""Offline equivalence against the exact frozen blob, without Git at test time."""

from __future__ import annotations

import ast
import asyncio
import builtins
import copy
import hashlib
import inspect
import json
import linecache
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict, fields
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import bot
from bot.services import context_gate as gate
from bot.services import context_observability as obs

BASELINE_SHA256 = "86da4376401022b111241e580a39b9b93ff6974c5338fafd3650112b035af687"
ROOT = Path(__file__).resolve().parents[1]
BASELINE_FIXTURE = ROOT / "tests/fixtures/context_gate_0a909fb.py.txt"
CANARY = "SECRET-CANARY-query-user-group-model-key"


def _message(body: str) -> dict:
    return {"role": "user", "content": body, "scope_id": CANARY}


def _inputs(**changes) -> dict:
    return {
        "system": [_message("persona"), _message("guardrail")],
        "current_turn": [_message("current1"), _message("current2")],
        "budget_tokens": 1024,
        **changes,
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network forbidden in context-observability tests")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    obs.reset_context_observations()
    yield
    obs.reset_context_observations()


@pytest.fixture(scope="module")
def baseline():
    source = BASELINE_FIXTURE.read_bytes()
    assert hashlib.sha256(source).hexdigest() == BASELINE_SHA256
    name = "_context_observability_frozen_baseline"
    module = ModuleType(name)
    module.__file__ = str(BASELINE_FIXTURE)
    sys.modules[name] = module  # dataclasses resolves its defining module.
    linecache.cache[module.__file__] = (
        len(source),
        None,
        source.decode().splitlines(keepends=True),
        module.__file__,
    )
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    yield module
    del sys.modules[name]
    del linecache.cache[module.__file__]


def _bytes(assembly) -> bytes:
    # Do not sort: layer and message-field insertion order are part of the oracle.
    return json.dumps(asdict(assembly), ensure_ascii=False).encode()


def _assert_latest(assembly):
    snapshot = obs.snapshot_context_observations()
    assert snapshot.state == "observed"
    sample = snapshot.latest
    assert sample is not None
    assert (sample.used_tokens, sample.budget_tokens, sample.over_budget) == (
        assembly.used_tokens,
        assembly.budget_tokens,
        assembly.over_budget,
    )
    assert sample.sampled_at_unix_ns > 0
    assert tuple(row.layer for row in sample.trims) == gate.TRIM_PRIORITY
    for row in sample.trims:
        actual = [trim for trim in assembly.trims if trim.layer == row.layer]
        assert row.dropped_messages == sum(t.dropped_messages for t in actual)
        assert row.truncated_messages == sum(t.truncated_messages for t in actual)
        assert row.dropped_tokens == sum(t.dropped_tokens for t in actual)
    assert CANARY not in json.dumps(asdict(snapshot))
    return snapshot


CASES = [
    pytest.param({}, id="empty"),
    pytest.param(_inputs(), id="fixed-only"),
    pytest.param(_inputs(history=[_message("short")]), id="normal-no-trim"),
    *[
        pytest.param(
            _inputs(**{layer: [_message("中" * 500), _message("中" * 900)]}),
            id=f"drop-and-truncate-{layer}",
        )
        for layer in gate.TRIM_PRIORITY
    ],
    pytest.param(
        _inputs(**{layer: [_message("中" * 1500)] for layer in gate.TRIM_PRIORITY}),
        id="all-layers-in-order",
    ),
    pytest.param(_inputs(system=[_message("中" * 2000)]), id="fixed-overflow"),
    pytest.param(_inputs(reserve_tokens=1100), id="reserve-overflow"),
    pytest.param(
        _inputs(history=[_message("a" * 3000)], reserve_tokens=200),
        id="explicit-reserve",
    ),
    pytest.param(_inputs(history=[_message("中" * 2000)]), id="surviving-truncate"),
    pytest.param(
        _inputs(history=[_message("中" * 2000)], reserve_tokens=1000),
        id="truncate-to-empty-drop",
    ),
    pytest.param(
        _inputs(history=[_message("")], reserve_tokens=1000), id="overhead-only-drop"
    ),
    pytest.param(
        _inputs(system=[_message("中" * 400)], history=[_message("中" * 400)]),
        id="preserve-double-fixed-allowance",
    ),
    *[
        pytest.param(_inputs(budget_tokens=value), id=f"budget-{name}")
        for name, value in [
            ("tiny", 1),
            ("negative", -1),
            ("zero", 0),
            ("invalid", "bad"),
            ("none", None),
            ("huge", 4_000_000),
        ]
    ],
    *[
        pytest.param(_inputs(reserve_tokens=value), id=f"reserve-{name}")
        for name, value in [("negative", -1), ("invalid", "bad"), ("none", None)]
    ],
    *[
        pytest.param(
            _inputs(
                **{layer: [_message("中" * 1500)] for layer in gate.TRIM_PRIORITY},
                trim_order=order,
            ),
            id=f"custom-order-{name}",
        )
        for name, order in [
            ("reversed", tuple(reversed(gate.TRIM_PRIORITY))),
            ("omitted", ()),
            ("duplicate-unknown", (CANARY, "history", "history")),
            ("fixed", ("system", "current_turn", "history")),
        ]
    ],
    pytest.param(
        _inputs(history=[None, "ignored", _message("short")]), id="non-mapping"
    ),
]


@pytest.mark.parametrize("kwargs", CASES)
def test_actual_assembly_matches_exact_baseline(baseline, kwargs):
    before = copy.deepcopy(kwargs)
    expected = baseline.assemble_context_within_budget(**copy.deepcopy(kwargs))
    actual = gate.assemble_context_within_budget(**kwargs)
    assert _bytes(actual) == _bytes(expected)
    assert kwargs == before
    if "system" not in kwargs.get("trim_order", ()):
        assert actual.layers["system"] == kwargs.get("system", [])
    if "current_turn" not in kwargs.get("trim_order", ()):
        assert actual.layers["current_turn"] == kwargs.get("current_turn", [])
    assert obs.snapshot_context_observations().observation_count == 1
    _assert_latest(actual)


def test_unchanged_algorithm_and_dataclass_api(baseline):
    assert Path(bot.__file__).resolve() == ROOT / "bot/__init__.py"
    assert Path(gate.__file__).resolve() == ROOT / "bot/services/context_gate.py"
    assert (
        Path(obs.__file__).resolve() == ROOT / "bot/services/context_observability.py"
    )
    assert inspect.signature(gate.assemble_context_within_budget) == inspect.signature(
        baseline.assemble_context_within_budget
    )
    for name in ("ContextAssembly", "ContextTrim"):
        # Field instances have identity equality across independently loaded modules.
        # Compare the complete defining AST instead (including every field/default).
        assert ast.dump(ast.parse(inspect.getsource(getattr(gate, name)))) == ast.dump(
            ast.parse(inspect.getsource(getattr(baseline, name)))
        )
    old = ast.parse(inspect.getsource(baseline.assemble_context_within_budget)).body[0]
    new = ast.parse(inspect.getsource(gate.assemble_context_within_budget)).body[0]
    # All pre-return statements are identical, not merely fixture-equivalent.
    assert ast.dump(ast.Module(body=old.body[:-1], type_ignores=[])) == ast.dump(
        ast.Module(body=new.body[:-3], type_ignores=[])
    )
    assert isinstance(old.body[-1], ast.Return)
    assert isinstance(new.body[-3], ast.Assign)
    assert ast.dump(old.body[-1].value) == ast.dump(new.body[-3].value)


def test_missing_is_unknown_and_reset_does_not_fake_a_sample():
    for _ in range(2):
        snap = obs.snapshot_context_observations()
        assert snap.state == "not_observed"
        assert snap.latest is None
        assert snap.observation_count == 0
        assert len(snap.trim_totals) == 3
        assert all(row.dropped_messages == 0 for row in snap.trim_totals)
        obs.reset_context_observations()


def test_real_trim_order_and_dropped_token_semantics():
    actual = gate.assemble_context_within_budget(
        **_inputs(**{layer: [_message("中" * 1500)] for layer in gate.TRIM_PRIORITY})
    )
    assert tuple(trim.layer for trim in actual.trims) == gate.TRIM_PRIORITY
    assert actual.trims[-1].truncated_messages == 1
    assert actual.trims[-1].dropped_tokens == 0  # NOT surviving truncation savings.
    snap = _assert_latest(actual)
    assert snap.trim_totals == snap.latest.trims
    next_result = gate.assemble_context_within_budget()
    updated = _assert_latest(next_result)
    assert all(row == obs.TrimCounts(row.layer) for row in updated.latest.trims)
    assert updated.trim_totals == snap.trim_totals
    assert updated.observation_count == 2
    obs.reset_context_observations()
    assert obs.snapshot_context_observations().latest is None


def test_immutable_snapshot_and_mutated_result_are_independent():
    actual = gate.assemble_context_within_budget(**_inputs(history=[_message(CANARY)]))
    snap = _assert_latest(actual)
    actual.messages.clear()
    actual.layers["history"].clear()
    assert obs.snapshot_context_observations() == snap
    with pytest.raises(FrozenInstanceError):
        snap.latest.used_tokens = 0
    with pytest.raises(FrozenInstanceError):
        snap.trim_totals[0].dropped_tokens = 0


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError,
        ValueError,
        OSError,
        asyncio.CancelledError,
        KeyboardInterrupt,
        SystemExit,
    ],
)
@pytest.mark.parametrize("where", ["hook", "recorder", "import", "clock"])
def test_telemetry_failure_isolation_and_cancellation_propagation(
    baseline, monkeypatch, failure, where
):
    kwargs = _inputs(history=[_message("中" * 3000)])
    expected = baseline.assemble_context_within_budget(**kwargs)
    gate.assemble_context_within_budget()
    before = obs.snapshot_context_observations()
    error = failure("telemetry fault")

    def fail(*args, **kwargs):
        raise error

    if where == "hook":
        monkeypatch.setattr(gate, "_observe_completed_assembly", fail)
    elif where == "recorder":
        monkeypatch.setattr(obs, "record_context_assembly", fail)
    elif where == "clock":
        monkeypatch.setattr(obs, "time_ns", fail)
    else:
        original_import = builtins.__import__

        def fail_import(name, *args, **kwargs):
            if name == "bot.services.context_observability":
                fail()
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail_import)
    if isinstance(error, Exception):
        assert _bytes(gate.assemble_context_within_budget(**kwargs)) == _bytes(expected)
    else:
        with pytest.raises(failure) as caught:
            gate.assemble_context_within_budget(**kwargs)
        assert caught.value is error
    assert obs.snapshot_context_observations() is before


@pytest.mark.parametrize(
    "failure", [RuntimeError, asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
@pytest.mark.parametrize(
    "where", ["iterator", "content", "budget", "reserve", "estimator", "result"]
)
def test_original_errors_and_cancellation_propagate(
    baseline, monkeypatch, failure, where
):
    error = failure("original assembly fault")

    class Fault:
        def __iter__(self):
            raise error

        def __str__(self):
            raise error

        def __int__(self):
            raise error

    kwargs = {}
    if where == "iterator":
        kwargs["history"] = Fault()
    elif where == "content":
        kwargs["history"] = [_message(Fault())]
    elif where in ("budget", "reserve"):
        kwargs[f"{where}_tokens"] = Fault()
    else:

        def fail(*args, **kwargs):
            raise error

        name = "estimate_text_tokens" if where == "estimator" else "ContextAssembly"
        monkeypatch.setattr(gate, name, fail)
        monkeypatch.setattr(baseline, name, fail)
        kwargs["history"] = [_message("body")]
    before = obs.snapshot_context_observations()
    for module in (baseline, gate):
        with pytest.raises(failure) as caught:
            module.assemble_context_within_budget(**kwargs)
        assert caught.value is error
    assert obs.snapshot_context_observations() is before


def test_no_extra_estimator_pass_or_iterable_consumption(baseline, monkeypatch):
    calls = []
    original = gate.estimate_text_tokens

    def counted(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(gate, "estimate_text_tokens", counted)
    monkeypatch.setattr(baseline, "estimate_text_tokens", counted)
    kwargs = _inputs(history=[_message("中" * 3000)])
    expected = baseline.assemble_context_within_budget(**kwargs)
    original_calls = calls[:]
    calls.clear()
    actual = gate.assemble_context_within_budget(
        **{
            key: iter(value) if isinstance(value, list) else value
            for key, value in kwargs.items()
        }
    )
    assert _bytes(actual) == _bytes(expected)
    assert calls == original_calls


def test_concurrent_callers_share_only_coherent_numeric_aggregates(baseline):
    inputs = [
        _inputs(
            history=[_message(f"{CANARY}-{i}" + "中" * (1400 + i))],
            budget_tokens=1024 + i,
        )
        for i in range(96)
    ]
    expected = [baseline.assemble_context_within_budget(**kw) for kw in inputs]
    valid_samples = {(x.used_tokens, x.budget_tokens, x.over_budget) for x in expected}

    def build(index):
        actual = gate.assemble_context_within_budget(**inputs[index])
        snap = obs.snapshot_context_observations()
        assert snap.latest is not None
        sample = snap.latest
        assert (
            sample.used_tokens,
            sample.budget_tokens,
            sample.over_budget,
        ) in valid_samples
        assert sum(row.truncated_messages for row in snap.trim_totals) == (
            snap.observation_count
        )
        return actual

    with ThreadPoolExecutor(max_workers=8) as pool:
        actual = list(pool.map(build, range(len(inputs))))
    assert [_bytes(x) for x in actual] == [_bytes(x) for x in expected]
    snap = obs.snapshot_context_observations()
    assert snap.observation_count == len(inputs)
    for row in snap.trim_totals:
        trims = [t for x in expected for t in x.trims if t.layer == row.layer]
        assert row.dropped_messages == sum(t.dropped_messages for t in trims)
        assert row.truncated_messages == sum(t.truncated_messages for t in trims)
        assert row.dropped_tokens == sum(t.dropped_tokens for t in trims)
    assert CANARY not in json.dumps(asdict(snap))


def _retained_size(value):
    size = sys.getsizeof(value)
    if hasattr(value, "__dataclass_fields__"):
        return size + sum(_retained_size(getattr(value, f.name)) for f in fields(value))
    if isinstance(value, tuple):
        return size + sum(_retained_size(item) for item in value)
    return size


def test_fixed_retained_memory_and_no_arbitrary_layer_keys():
    gate.assemble_context_within_budget()
    first_size = _retained_size(obs.snapshot_context_observations())
    for i in range(2000):
        actual = gate.assemble_context_within_budget(
            history=[_message(f"{CANARY}-{i}" + "中" * 1100)],
            budget_tokens=1024,
            trim_order=(f"{CANARY}-{i}", "history"),
        )
    snap = _assert_latest(actual)
    assert snap.observation_count == 2001
    assert tuple(row.layer for row in snap.trim_totals) == obs.TRIM_LAYERS
    assert _retained_size(snap) <= first_size + 128
    assert not hasattr(snap, "__dict__")
    assert not hasattr(snap.latest, "__dict__")
    assert len(snap.trim_totals) == len(snap.latest.trims) == 3


def test_counters_saturate_and_sample_numbers_remain_exact(monkeypatch):
    rows = tuple(
        obs.TrimCounts(
            layer, obs.COUNTER_MAX - 1, obs.COUNTER_MAX - 1, obs.COUNTER_MAX - 1
        )
        for layer in obs.TRIM_LAYERS
    )
    monkeypatch.setattr(
        obs,
        "_snapshot",
        obs.ContextObservationSnapshot("observed", None, obs.COUNTER_MAX - 1, rows),
    )
    trim = gate.ContextTrim("history", 2, 2, 2)
    for _ in range(3):
        obs.record_context_assembly(
            used_tokens=1,
            budget_tokens=obs.COUNTER_MAX,
            over_budget=False,
            trims=(trim,),
        )
    snap = obs.snapshot_context_observations()
    assert snap.observation_count == obs.COUNTER_MAX
    assert snap.trim_totals[0] == obs.TrimCounts("history", *([obs.COUNTER_MAX] * 3))
    assert snap.latest.used_tokens == 1
    assert snap.latest.budget_tokens == obs.COUNTER_MAX
    assert snap.latest.trims[0] == obs.TrimCounts("history", 2, 2, 2)


def test_unrepresentable_sample_is_not_silently_clamped(baseline):
    kwargs = _inputs(budget_tokens=obs.COUNTER_MAX + 1)
    actual = gate.assemble_context_within_budget(**kwargs)
    assert _bytes(actual) == _bytes(baseline.assemble_context_within_budget(**kwargs))
    assert obs.snapshot_context_observations().state == "not_observed"


def test_repeated_known_rows_add_and_unknown_rows_are_not_retained():
    obs.record_context_assembly(
        used_tokens=10,
        budget_tokens=1024,
        over_budget=False,
        trims=(
            gate.ContextTrim("history", 1, 2, 3),
            gate.ContextTrim("history", 2, 3, 4),
            SimpleNamespace(layer=CANARY),
        ),
    )
    snap = obs.snapshot_context_observations()
    assert snap.latest.trims[0] == obs.TrimCounts("history", 3, 5, 7)
    assert CANARY not in json.dumps(asdict(snap))
