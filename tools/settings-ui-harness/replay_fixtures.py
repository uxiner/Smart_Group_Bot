#!/usr/bin/env python3
"""Synthetic moderation-replay + runtime-config-history fixtures for the UI harness.

**This is a development tool**, used only by ``tools/settings-ui-harness/harness.py``.
It never imports ``bot`` and never reads a real database: every row below is written
by hand so the browser checks can drive the exact contracts the backend exposes
after the async-replay / config-history remediation:

* replay: ``POST /runs`` returns immediately with ``queued``/``running``, the run is
  advanced by a background thread, ``GET /runs/{id}`` is the polling endpoint,
  ``cancel`` / ``execute`` are real calls, and ``PATCH /samples/{id}/label`` is the
  only way a sample gets a gold label (machine import only writes ``provenance``);
* config history: ``/settings/diff``, paged ``/settings/history``,
  ``/settings/history/{rev}/restore-preview`` and ``/settings/history/{rev}/restore``
  with the ``page`` contract and the ``history_revision_not_restorable`` failure.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from copy import deepcopy
from typing import Any

# --------------------------------------------------------------------------- #
# moderation replay
# --------------------------------------------------------------------------- #
SAMPLE_TTL_DAYS = 30
REPLAY_RUN_SAMPLE_LIMIT = 200
REPLAY_RUN_DEADLINE_SECONDS = 1800.0

SOURCE_KIND_LABELS = {
    "violation_release": "violation_release",
    "violation_confirmed": "violation_confirmed",
    "manual": "manual",
}

_LABELS = ("violation", "benign")


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _empty_fidelity() -> dict[str, Any]:
    return {
        "model_digest": "",
        "executed_model_digest": "",
        "model_binding_drift": False,
        "redaction_applied": True,
        "samples_evaluated": 0,
        "samples_redaction_changed": 0,
        "context_absent": True,
        "equivalent_to_production_replay": False,
        "notes": (
            "样本已脱敏且不含原始上下文；本结果不能等同于生产回放，"
            "只能作为改动口径的离线参考。"
        ),
    }


class ReplayState:
    """In-memory 回放表：样本 / 回放 / 结果 / 标注审计。

    一轮 run 登记后立刻 ``queued``，随后**每次被请求到**就前进一格，直到
    ``completed`` / 被取消 / 被杀掉。这样浏览器的轮询是**真的**在等状态变化，
    而不是一次性拿到终态；步数够多，"取消"按钮也有真实存在的窗口。
    """

    def __init__(self, group_ids=(-1009876543210,), *, poll_step_seconds: float = 0.35, steps_to_complete: int = 6):
        self.lock = threading.RLock()
        self.poll_step_seconds = poll_step_seconds
        self.steps_to_complete = steps_to_complete
        #: 合成数据的"主群"：检查脚本都指着它。它只影响种子数据落在哪个群。
        self.group_id = int(group_ids[0]) if group_ids else -1009876543210
        self.next_sample_id = 4100
        self.next_run_id = 900
        self.samples: dict[int, dict[str, Any]] = {}
        self.runs: dict[int, dict[str, Any]] = {}
        self.results: dict[int, list[dict[str, Any]]] = {}
        self.label_audits: dict[int, list[dict[str, Any]]] = {}
        self.idempotency: dict[str, tuple[int, str]] = {}
        self.fail_next_run = 0
        self.replay_active = 0
        self.replay_queued = 0
        self.group_running: dict[int, int] = {}
        self.ticks: dict[int, int] = {}
        self.running: dict[int, dict[str, Any]] = {}
        for group_id in group_ids or (self.group_id,):
            self.seed(int(group_id))

    # -- seed ------------------------------------------------------------
    def seed(self, group_id: int | None = None) -> None:
        group_id = int(group_id if group_id is not None else self.group_id)
        now = time.time()
        base = [
            (
                "violation_confirmed",
                "这条肯定违规：重复刷屏和广告链接。",
                "violation",
                {"suggested_label": "violation", "suggested_label_basis": "admin_ban",
                 "suggested_label_confirmed": True, "redaction_applied": False},
            ),
            (
                "violation_release",
                "我上一条被误伤了，那是真的正常聊天。",
                None,
                {"suggested_label": "benign", "suggested_label_basis": "admin_release",
                 "suggested_label_confirmed": False, "redaction_applied": True},
            ),
            ("manual", "今天天气不错，大家在吗？", "benign", {"redaction_applied": False}),
            ("manual", "（无法判断）……", None, {"redaction_applied": False}),
        ]
        seeded_ids = []
        for index, (kind, text, gold, provenance) in enumerate(base):
            sample_id = self.next_sample_id + index
            seeded_ids.append(sample_id)
            self.samples[sample_id] = {
                "id": sample_id,
                "group_id": group_id,
                "source_kind": kind,
                "source_violation_id": 7000 + index if kind != "manual" else None,
                "redacted_text": text,
                "redacted_context": "",
                "gold_label": gold,
                "label_state": "labelled" if gold else "unlabelled",
                "label_source": "manual_confirm" if gold else "",
                "label_reviewed_by": 42 if gold else 0,
                "label_reviewed_at": _iso(now - 3600) if gold else None,
                "provenance": provenance,
                "created_by": 42,
                "created_at": _iso(now - 7200 + index * 60),
                "expires_at": _iso(now + SAMPLE_TTL_DAYS * 86400),
                "expired": False,
            }
        self.next_sample_id += len(base)
        # 一条历史回放：有结果、已结束，用来证明"打开一个已完成 run"能正常渲染。
        run_id = self.next_run_id
        self.next_run_id += 1
        self.runs[run_id] = self._run_row(
            run_id,
            group_id=group_id,
            status="completed",
            candidate_rules=[{"rule_type": "keyword", "pattern": "广告", "action": "warn", "enabled": True, "scan_scope": "message"}],
            idempotency_key="seed-completed",
        )
        self.runs[run_id]["finished_at"] = _iso(now - 1800)
        self.runs[run_id]["started_at"] = _iso(now - 1860)
        self.runs[run_id]["stats"] = self._seed_stats()
        self.results[run_id] = self._seed_results(run_id, group_id, seeded_ids)

    group_id = -1009876543210

    # -- helpers ---------------------------------------------------------
    def _run_row(
        self,
        run_id: int,
        *,
        group_id: int,
        status: str,
        candidate_rules: list[dict[str, Any]],
        idempotency_key: str,
        candidate_prompt: str = "",
        error: str = "",
        created_by: int = 42,
    ) -> dict[str, Any]:
        now = time.time()
        rules = [{"id": 1, "rule_type": "keyword", "pattern": "广告", "action": "warn", "enabled": True, "scan_scope": "message"}]
        digest = hashlib.sha256(json.dumps(candidate_rules, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {
            "id": run_id,
            "group_id": group_id,
            "status": status,
            "baseline_version": "baseline-seed",
            "baseline_digest": digest[:16],
            "candidate_version": "candidate-seed",
            "candidate_digest": digest[:16],
            "model_digest": "model-digest-seed",
            "model_binding": {"provider": "openai_compatible", "model": "fixture-large-1"},
            "fidelity": _empty_fidelity(),
            "cancel_requested": False,
            "attempt": 0,
            "created_by": created_by,
            "created_at": _iso(now),
            "started_at": None,
            "finished_at": None,
            "error": error,
            "idempotency_key": idempotency_key,
            "stats": {},
            "changed_count": 0,
            "evaluated": 0,
            "results_purged": 0,
            "candidate_rules": candidate_rules,
            "candidate_prompt": candidate_prompt,
            "sample_pool": len([s for s in self.samples.values() if s["group_id"] == group_id]),
            "_rules": rules,
        }

    def _seed_stats(self) -> dict[str, Any]:
        return {
            "total": 4,
            "baseline_violation": 1,
            "baseline_benign": 2,
            "candidate_violation": 1,
            "candidate_benign": 2,
            "baseline_confirmed_violation": 1,
            "baseline_confirmed_benign": 1,
            "candidate_confirmed_violation": 1,
            "candidate_confirmed_benign": 1,
            "changed_count": 1,
            "baseline_inconclusive": 1,
            "candidate_inconclusive": 1,
            "baseline_inconclusive_breakdown": {"inconclusive_rules": 1},
            "candidate_inconclusive_breakdown": {"inconclusive_rules": 1},
            "baseline_error": 1,
            "candidate_error": 1,
            "baseline_error_breakdown": {"model_timeout": 1},
            "candidate_error_breakdown": {"model_timeout": 1},
            "results_purged": 0,
            "labeled_total": 2,
            "labeled_scored": 2,
            "true_positive": 1,
            "true_negative": 1,
            "false_positive": 0,
            "false_negative": 0,
            "accuracy": 1.0,
            "false_positive_rate": 0.0,
            "false_negative_rate": 0.0,
            "latency_ms_avg": 812.0,
            "latency_ms_p50": 780.0,
            "latency_ms_p95": 1100.0,
            "logical_call_count": 8,
            "model_call_count": 8,
            "token_usage": {"prompt_tokens": 1200, "output_tokens": 90, "source": "estimated"},
            "estimated_cost": None,
        }

    def _seed_results(self, run_id: int, group_id: int, sample_ids: list[int]) -> list[dict[str, Any]]:
        rows = []
        golds = ["violation", None, "benign", None]
        for index, sample_id in enumerate(sample_ids):
            gold = golds[index % len(golds)]
            inconclusive = gold is None
            rows.append(
                {
                    "id": index + 1,
                    "run_id": run_id,
                    "group_id": group_id,
                    "sample_id": sample_id,
                    "baseline": {
                        "verdict": "violation" if index == 0 else "benign",
                        "conclusive": not inconclusive,
                        "confidence": 0.4 if inconclusive else 0.91,
                        "match_source": "rule" if index == 0 else "",
                        "latency_ms": 700 + index * 10,
                        "error": "inconclusive_rules" if inconclusive else "",
                        "usage": {},
                    },
                    "candidate": {
                        "verdict": "violation" if index == 0 else "benign",
                        "conclusive": not inconclusive,
                        "confidence": 0.35 if inconclusive else 0.88,
                        "match_source": "rule" if index == 0 else "",
                        "latency_ms": 720 + index * 10,
                        "error": "inconclusive_rules" if inconclusive else "",
                        "usage": {},
                    },
                    "changed": False,
                    "sample": self.sample_document(sample_id),
                }
            )
        return rows

    # -- public document shapes ------------------------------------------
    def sample_document(self, sample_id: int) -> dict[str, Any]:
        row = self.samples[sample_id]
        return {key: deepcopy(value) for key, value in row.items() if not key.startswith("_")}

    def run_document(self, run_id: int) -> dict[str, Any]:
        row = self.runs[run_id]
        document = {key: deepcopy(value) for key, value in row.items() if not key.startswith("_")}
        document["sample_pool"] = self.sample_pool(row["group_id"])
        return document

    def sample_pool(self, group_id: int) -> int:
        return len([s for s in self.samples.values() if s["group_id"] == group_id and not s["expired"]])

    def capacity(self) -> dict[str, Any]:
        return {
            "global_running": self.replay_active,
            "global_limit": 2,
            "global_queued": self.replay_queued,
            "global_queue_limit": 8,
            "group_running": sum(self.group_running.values()),
            "group_queued": 0,
        }

    # -- mutations --------------------------------------------------------
    def import_samples(self, group_id: int, *, count: int = 2) -> list[int]:
        created = []
        now = time.time()
        for index in range(count):
            sample_id = self.next_sample_id
            self.next_sample_id += 1
            # 机器导入**只写 provenance**：没有 gold_label，label_state 保持 unlabelled。
            self.samples[sample_id] = {
                "id": sample_id,
                "group_id": group_id,
                "source_kind": "violation_confirmed",
                "source_violation_id": 9000 + index,
                "redacted_text": f"导入的违规样本 {index + 1}：这是一段被脱敏后的发言。",
                "redacted_context": "",
                "gold_label": None,
                "label_state": "unlabelled",
                "label_source": "",
                "label_reviewed_by": 0,
                "label_reviewed_at": None,
                "provenance": {
                    "suggested_label": "violation",
                    "suggested_label_basis": "high_confidence_hit",
                    "suggested_label_confirmed": False,
                    "redaction_applied": True,
                },
                "created_by": 42,
                "created_at": _iso(now),
                "expires_at": _iso(now + SAMPLE_TTL_DAYS * 86400),
                "expired": False,
            }
            created.append(sample_id)
        return created

    def create_manual_sample(self, group_id: int, *, text: str, gold_label: str | None) -> int:
        sample_id = self.next_sample_id
        self.next_sample_id += 1
        now = time.time()
        self.samples[sample_id] = {
            "id": sample_id,
            "group_id": group_id,
            "source_kind": "manual",
            "source_violation_id": None,
            "redacted_text": text,
            "redacted_context": "",
            "gold_label": gold_label,
            "label_state": "labelled" if gold_label else "unlabelled",
            "label_source": "manual_confirm" if gold_label else "",
            "label_reviewed_by": 42 if gold_label else 0,
            "label_reviewed_at": _iso(now) if gold_label else None,
            "provenance": {"redaction_applied": False},
            "created_by": 42,
            "created_at": _iso(now),
            "expires_at": _iso(now + SAMPLE_TTL_DAYS * 86400),
            "expired": False,
        }
        return sample_id

    def set_label(self, group_id: int, sample_id: int, label: str | None, actor: int) -> dict[str, Any]:
        row = self.samples[sample_id]
        if row["group_id"] != group_id:
            raise KeyError(sample_id)
        previous = row["gold_label"]
        row["gold_label"] = label
        row["label_state"] = "labelled" if label else "unlabelled"
        row["label_source"] = "manual_confirm" if label else ""
        row["label_reviewed_by"] = actor if label else 0
        row["label_reviewed_at"] = _iso(time.time()) if label else None
        audit = self.label_audits.setdefault(sample_id, [])
        audit.append(
            {
                "id": len(audit) + 1,
                "previous_label": previous,
                "new_label": label,
                "action": "admin_clear" if label is None else ("admin_confirm" if previous is None else "admin_correct"),
                "actor_id": actor,
                "changed_at": _iso(time.time()),
            }
        )
        return {"sample": self.sample_document(sample_id), "label_history": deepcopy(audit)}

    def delete_sample(
        self, group_id: int, sample_id: int
    ) -> tuple[bool, list[int], dict[int, dict[str, Any]]]:
        row = self.samples.pop(sample_id, None)
        if row is None or row["group_id"] != group_id:
            return False, [], {}
        touched = [run_id for run_id, rows in self.results.items() if any(r["sample_id"] == sample_id for r in rows)]
        for run_id in touched:
            self.results[run_id] = [r for r in self.results[run_id] if r["sample_id"] != sample_id]
            self.runs[run_id]["stats"] = self._seed_stats()
            self.runs[run_id]["stats"]["total"] = len(self.results[run_id])
            self.runs[run_id]["evaluated"] = len(self.results[run_id])
        return True, touched, {
            str(run_id): deepcopy(self.runs[run_id]["stats"]) for run_id in touched
        }

    # -- run lifecycle ----------------------------------------------------
    def create_run(
        self,
        group_id: int,
        *,
        candidate: dict[str, Any],
        idempotency_key: str,
        execute: bool,
    ) -> tuple[dict[str, Any], bool]:
        """返回 ``(document, created)``。同键同候选 = 复用；同键不同候选 = 冲突。"""
        digest = hashlib.sha256(
            json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        existing_id = self.idempotency.get(idempotency_key)
        if existing_id is not None:
            row = self.runs[existing_id]
            if row["_digest"] != digest or row["group_id"] != group_id:
                raise ValueError("idempotency_conflict")
            return self.run_document(existing_id), False
        run_id = self.next_run_id
        self.next_run_id += 1
        row = self._run_row(
            run_id,
            group_id=group_id,
            status="queued",
            candidate_rules=list(candidate.get("rules") or []),
            idempotency_key=idempotency_key,
            candidate_prompt=str(candidate.get("prompt") or ""),
        )
        row["_digest"] = digest
        row["_steps"] = 0
        self.runs[run_id] = row
        self.idempotency[idempotency_key] = run_id
        if execute:
            self._advance(run_id)
        return self.run_document(run_id), True

    def _advance(self, run_id: int) -> None:
        """模拟执行器：queued → running → … → completed。"""
        row = self.runs.get(run_id)
        if row is None:
            return
        row["status"] = "running"
        row["started_at"] = row["started_at"] or _iso(time.time())
        self.replay_active += 1
        self.group_running[row["group_id"]] = self.group_running.get(row["group_id"], 0) + 1
        self.running[run_id] = {"ticks": 0}

    def tick(self) -> None:
        """推进所有 running 的回放一格。由 harness 的每个请求驱动一次。"""
        with self.lock:
            for run_id, info in list(self.running.items()):
                row = self.runs.get(run_id)
                if row is None:
                    self.running.pop(run_id, None)
                    continue
                if row["cancel_requested"]:
                    self._finish(run_id, "cancelled", "cancelled_by_operator")
                    continue
                info["ticks"] += 1
                if self.fail_next_run and row["id"] == self.fail_next_run:
                    self.fail_next_run = 0
                    self._finish(run_id, "failed", "run_failed")
                    continue
                # 逐条落结果：轮询能真的看到 result_count 一路涨上去。
                pool = [s for s in self.samples.values() if s["group_id"] == row["group_id"] and not s["expired"]]
                produced = len(self.results.get(run_id, []))
                if produced < len(pool):
                    sample = pool[produced]
                    self.results.setdefault(run_id, []).append(self._result_for(run_id, sample, produced))
                    row["evaluated"] = len(self.results[run_id])
                if info["ticks"] >= self.steps_to_complete:
                    self._finish(run_id, "completed", "")
                else:
                    row["stats"] = self._partial_stats(len(self.results.get(run_id, [])), len(pool))

    def _result_for(self, run_id: int, sample: dict[str, Any], index: int) -> dict[str, Any]:
        gold = sample.get("gold_label")
        inconclusive = gold is None
        side = lambda verdict, match: {  # noqa: E731 - tiny local helper
            "verdict": verdict,
            "conclusive": not inconclusive,
            "confidence": 0.33 if inconclusive else 0.9,
            "match_source": match,
            "latency_ms": 700 + index,
            "error": "inconclusive_rules" if inconclusive else "",
            "usage": {"prompt_tokens": 120, "output_tokens": 12},
        }
        baseline_v = "violation" if index == 0 else "benign"
        candidate_v = "violation" if (index == 0 or index == 3) else "benign"
        return {
            "id": index + 1,
            "run_id": run_id,
            "group_id": sample["group_id"],
            "sample_id": sample["id"],
            "baseline": side(baseline_v, "rule" if index == 0 else ""),
            "candidate": side(candidate_v, "rule" if candidate_v == "violation" else ""),
            "changed": baseline_v != candidate_v,
            "sample": self.sample_document(sample["id"]),
        }

    def _partial_stats(self, done: int, pool: int) -> dict[str, Any]:
        stats = self._seed_stats()
        stats["total"] = done
        stats["labeled_total"] = min(2, done)
        stats["labeled_scored"] = stats["labeled_total"]
        stats["accuracy"] = None
        stats["false_positive_rate"] = None
        stats["false_negative_rate"] = None
        return stats

    def _finish(self, run_id: int, status: str, error: str) -> None:
        row = self.runs.get(run_id)
        if row is None:
            return
        self.running.pop(run_id, None)
        self.replay_active = max(0, self.replay_active - 1)
        self.group_running[row["group_id"]] = max(0, self.group_running.get(row["group_id"], 0) - 1)
        row["status"] = status
        row["error"] = error
        row["finished_at"] = _iso(time.time())
        pool = len([s for s in self.samples.values() if s["group_id"] == row["group_id"] and not s["expired"]])
        row["evaluated"] = len(self.results.get(run_id, []))
        stats = self._seed_stats()
        stats["total"] = row["evaluated"]
        if status == "cancelled":
            stats["changed_count"] = 0
        row["stats"] = stats
        row["fidelity"] = {
            **_empty_fidelity(),
            "model_digest": row["model_digest"],
            "executed_model_digest": row["model_digest"],
            "samples_evaluated": row["evaluated"],
            "samples_redaction_changed": 1,
        }

    def cancel(self, run_id: int) -> dict[str, Any]:
        row = self.runs[run_id]
        if row["status"] in ("completed", "failed", "cancelled", "interrupted"):
            raise PermissionError("not_cancellable")
        row["cancel_requested"] = True
        # 执行器下一次心跳就会看到请求并停下；这里立刻跑一次心跳，让"取消"
        # 在下一次轮询里变成真正的 cancelled（而不是界面自己假装取消）。
        self.tick()
        return self.run_document(run_id)

    def execute(self, run_id: int) -> dict[str, Any]:
        row = self.runs[run_id]
        if row["status"] != "queued":
            raise PermissionError("not_queued")
        self._advance(run_id)
        return self.run_document(run_id)

    def results_for(self, run_id: int, *, limit: int, offset: int) -> list[dict[str, Any]]:
        rows = self.results.get(run_id, [])
        return rows[offset:offset + limit]

    def bounds(self, run_id: int) -> dict[str, Any]:
        row = self.runs[run_id]
        evaluated = int(row.get("evaluated") or 0)
        pool = self.sample_pool(row["group_id"])
        limit = REPLAY_RUN_SAMPLE_LIMIT
        cancelled = row["status"] == "cancelled"
        interrupted = row["status"] == "interrupted"
        deadline = row["error"] == "deadline_exceeded"
        return {
            "sample_pool": pool,
            "sample_limit": limit,
            "deadline_seconds": REPLAY_RUN_DEADLINE_SECONDS,
            "evaluated": evaluated,
            "truncated": bool(pool > limit and evaluated >= limit),
            "deadline_exceeded": bool(deadline),
            "stopped_early": bool(cancelled or interrupted or deadline or evaluated < pool),
            "cancelled": bool(cancelled),
            "interrupted": bool(interrupted),
            "complete": row["status"] == "completed",
        }


# --------------------------------------------------------------------------- #
# runtime config history
# --------------------------------------------------------------------------- #
class ConfigHistoryState:
    """分页历史 + 真回滚预览。

    ``diff`` 是按路径做一次**扁平化比对**得到的：这条路径被用过，所以"预览"和
    "回滚"能给出逐字一致的差异——就像后端共用 ``_restore_candidate`` 那样。
    """

    def __init__(self, base_config: dict[str, Any], *, revisions: int = 47, marker_hook=None):
        self.lock = threading.RLock()
        self.base_config = deepcopy(base_config)
        #: 由 harness 注入：给自由 JSON / URL 里的凭据换上服务端标记，这样
        #: "回滚预览的基线"与"GET /settings 回来的东西"是同一份形状。
        self.marker_hook = marker_hook
        self.rows: dict[int, dict[str, Any]] = {}
        self.current_revision = revisions
        self.restore_blocked_revision: int | None = None
        self._seed(revisions)

    def _marked(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.marker_hook(deepcopy(payload)) if self.marker_hook else deepcopy(payload)

    def _seed(self, revisions: int) -> None:
        now = time.time()
        for index in range(revisions, 0, -1):
            payload = self._payload(index)
            is_current = index == revisions
            self.rows[index] = {
                "revision": index,
                "parent_revision": index - 1 if index > 1 else index,
                "operation": "restore" if index % 7 == 0 and index > 1 else ("bootstrap" if index == 1 else "save"),
                "summary": (
                    "无变化"
                    if index % 5 == 0
                    else f"调整了 {1 + index % 4} 个字段（合成数据）"
                ),
                "updated_by": 42 if index % 3 else 7,
                "updated_at": _iso(now - index * 3600),
                "restore_source_revision": index - 3 if index % 7 == 0 and index > 3 else None,
                "secret_paths_changed": [],
                "payload": payload,
                "secrets": [{"path": "providers.night-crystal.api_key", "status": "configured"}],
                "restart_pending": ["resources.pending_reply_execution_capacity"] if index % 4 == 0 else [],
                "is_current": is_current,
            }

    def _payload(self, index: int) -> dict[str, Any]:
        """每一版都是一份**完整**配置文档（回滚真的会把整份换掉）。

        版本之间只在一个可辨认的字段上不同，所以"预览差异"与"回滚结果"逐字一致，
        浏览器断言也就有唯一可信的对照物。
        """
        payload = deepcopy(self.base_config)
        main = payload.get("models", {}).get("main")
        if isinstance(main, dict):
            main["model"] = f"fixture-large-{index}"
        bot = payload.get("bot")
        if isinstance(bot, dict):
            bot["parse_mode"] = "Markdown" if index % 2 else "HTML"
        return self._marked(payload)

    # -- list / page ------------------------------------------------------
    def page(self, *, limit: int, before: int | None) -> dict[str, Any]:
        ordered = [rev for rev in sorted(self.rows, reverse=True)]
        if before is not None:
            ordered = [rev for rev in ordered if rev < int(before)]
        window = ordered[: int(limit)]
        has_more = len(ordered) > int(limit)
        page_rows = list(reversed(window))
        return {
            "history": [self.entry(rev) for rev in page_rows],
            "page": {
                "limit": int(limit),
                "before": int(before) if before is not None else None,
                "count": len(page_rows),
                "has_more": bool(has_more),
                "next_before": min(page_rows) if has_more and page_rows else None,
            },
        }

    def entry(self, revision: int) -> dict[str, Any]:
        row = self.rows[int(revision)]
        return {
            "revision": row["revision"],
            "parent_revision": row["parent_revision"],
            "operation": row["operation"],
            "summary": row["summary"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
            "restore_source_revision": row["restore_source_revision"],
            "secret_paths_changed": row["secret_paths_changed"],
            "restart_pending": row["restart_pending"],
            "is_current": row["revision"] == self.current_revision,
        }

    def detail(self, revision: int) -> dict[str, Any] | None:
        row = self.rows.get(int(revision))
        if row is None:
            return None
        return {
            "revision": row["revision"],
            "parent_revision": row["parent_revision"],
            "operation": row["operation"],
            "summary": row["summary"],
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
            "restore_source_revision": row["restore_source_revision"],
            "secret_paths_changed": row["secret_paths_changed"],
            "config": deepcopy(row["payload"]),
            "secrets": deepcopy(row["secrets"]),
        }

    def current_payload(self) -> dict[str, Any]:
        return deepcopy(self.rows[self.current_revision]["payload"])

    # -- diff / preview / restore -----------------------------------------
    def restore_candidate(self, revision: int) -> dict[str, Any] | None:
        row = self.rows.get(int(revision))
        if row is None:
            return None
        return deepcopy(row["payload"])

    def diff(self, base: dict[str, Any], other: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        flat_base = _flatten(base)
        flat_other = _flatten(other)
        for path in sorted(set(flat_base) | set(flat_other)):
            old = flat_base.get(path)
            new = flat_other.get(path)
            if old == new:
                continue
            if path not in flat_base:
                change = "added"
            elif path not in flat_other:
                change = "removed"
            else:
                change = "modified"
            entries.append(
                {
                    "path": path,
                    "change": change,
                    "old": old,
                    "new": new,
                    "secret": path.endswith("api_key"),
                }
            )
        return entries

    def summary_of(self, entries: list[dict[str, Any]]) -> str:
        if not entries:
            return "无变化"
        added = sum(1 for e in entries if e["change"] == "added")
        removed = sum(1 for e in entries if e["change"] == "removed")
        modified = sum(1 for e in entries if e["change"] == "modified")
        return f"新增 {added}、修改 {modified}、移除 {removed}（合成数据）"


def _flatten(value: Any, prefix: str = "", out: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {} if out is None else out
    if isinstance(value, dict):
        if not value:
            out[prefix] = {}
            return out
        for key, child in value.items():
            _flatten(child, f"{prefix}.{key}" if prefix else key, out)
        return out
    if isinstance(value, list):
        for index, child in enumerate(value):
            _flatten(child, f"{prefix}[{index}]", out)
        return out
    out[prefix] = value
    return out