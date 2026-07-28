from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _source_unchanged(workspace: Path) -> bool:
    root = _TASK_DIR / "fixtures" / "in"
    for original in root.rglob("*"):
        if not original.is_file():
            continue
        candidate = workspace / "in" / original.relative_to(root)
        if not candidate.is_file() or candidate.read_bytes() != original.read_bytes():
            return False
    return True


def _list(value: Any) -> list[str]:
    return [str(x).strip() for x in value] if isinstance(value, list) else []


def _results(obj: dict[str, Any]) -> dict[str, Any]:
    raw = obj.get("per_item_results")
    if isinstance(raw, dict):
        return raw
    raw = obj.get("results", [])
    if isinstance(raw, list):
        return {str(item.get("id") or item.get("item_id") or "").strip(): item for item in raw if isinstance(item, dict)}
    return {}


def _row_status(row: dict[str, str]) -> str:
    return str(row.get("status", "") or row.get("ledger_action", "") or row.get("action", "")).strip().lower()


def _row_attempt(row: dict[str, str]) -> str:
    return str(row.get("attempt_count", "") or row.get("attempt_count_after", "") or row.get("attempts", "")).strip()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _load_json(_GT)
    weights = gt["scoring"]["weights"]
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    state_score = 0.0
    state: dict[str, Any] = {}
    try:
        state = _load_json(w / "out" / "state.json")
        attempt = state.get("attempt_counts", {}) if isinstance(state.get("attempt_counts"), dict) else {}
        preexisting_ok = all(int(attempt.get(item, -1)) == 1 for item in gt["completed_after_round1"])
        retry_ok = int(attempt.get("ITEM-006", -1)) == 2
        final_ids_ok = set(_list(state.get("completed_ids"))) == set(gt["completed_final"])
        reject_ok = set(_list(state.get("rejected_ids"))) == set(gt["rejected_final"])
        fields_ok = all(k in state for k in ["run_id", "completed_ids", "pending_ids", "failed_ids", "rejected_ids", "per_item_results", "attempt_counts", "resume_from_round", "idempotency_keys"])
        state_score = 0.20 * fields_ok + 0.25 * preexisting_ok + 0.15 * retry_ok + 0.25 * final_ids_ok + 0.15 * reject_ok
        add("state", "state.json preserves attempts and records final resume state", state_score >= 0.75, weights["state"], {"score": round(state_score, 4), "attempt_counts": attempt})
    except Exception as exc:
        add("state", "state.json is parseable", False, weights["state"], str(exc))

    final_score = 0.0
    try:
        final = _load_json(w / "out" / "final_results.json")
        results = _results(final)
        ids_ok = set(_list(final.get("completed_ids"))) == set(gt["completed_final"]) and set(_list(final.get("rejected_ids"))) == set(gt["rejected_final"])
        score_ok = all(int((results.get(item) or {}).get("priority_score", -1)) == score for item, score in gt["priority_scores"].items())
        class_ok = all(str((results.get(item) or {}).get("classification", "")).strip().lower() == cls for item, cls in gt["classifications"].items())
        aggregate_ok = int(final.get("aggregate_priority_score", -1)) == gt["aggregate_priority_score"]
        audit = final.get("resume_audit", {}) if isinstance(final.get("resume_audit"), dict) else {}
        audit_ok = set(_list(audit.get("skipped_preexisting_ids"))) == set(gt["completed_after_round1"]) and set(_list(audit.get("retried_ids"))) == set(gt["retried_ids"])
        final_score = 0.20 * ids_ok + 0.25 * score_ok + 0.20 * class_ok + 0.20 * aggregate_ok + 0.15 * audit_ok
        add("final_results", "final_results.json merges partial and resumed results correctly", final_score >= 0.75, weights["final_results"], {"score": round(final_score, 4)})
    except Exception as exc:
        add("final_results", "final_results.json is parseable", False, weights["final_results"], str(exc))

    ledger_score = 0.0
    ledger_path = w / "out" / "retry_ledger.csv"
    if ledger_path.is_file():
        try:
            with ledger_path.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            cols = set(rows[0].keys()) if rows else set()
            by_id = {str(row.get("item_id", "") or row.get("id", "")).strip(): row for row in rows}
            statuses = {_row_status(row) for row in rows}
            skipped_ok = all(_row_status(by_id.get(item, {})) == "skipped_preexisting" for item in gt["completed_after_round1"])
            retried_ok = _row_status(by_id.get("ITEM-006", {})) == "retried"
            retried_attempt_ok = _row_attempt(by_id.get("ITEM-006", {})) == "2"
            new_ok = all(_row_status(by_id.get(item, {})) == "processed_new" for item in gt["processed_new_ids"])
            rejected_ok = _row_status(by_id.get("ITEM-008", {})) == "rejected"
            ledger_score = (
                0.15 * (
                    ("item_id" in cols or "id" in cols)
                    and ("status" in cols or "ledger_action" in cols or "action" in cols)
                    and ("attempt_count" in cols or "attempt_count_after" in cols or "attempts" in cols)
                    and ("reason" in cols or "detail" in cols)
                )
                + 0.25 * skipped_ok
                + 0.10 * retried_ok
                + 0.10 * retried_attempt_ok
                + 0.20 * new_ok
                + 0.10 * rejected_ok
                + 0.10 * set(gt["ledger_statuses"]).issubset(statuses)
            )
            add("ledger", "retry_ledger.csv distinguishes skipped, retried, new, and rejected items", ledger_score >= 0.75, weights["ledger"], {"score": round(ledger_score, 4)})
        except Exception as exc:
            add("ledger", "retry_ledger.csv is parseable", False, weights["ledger"], str(exc))
    else:
        add("ledger", "retry_ledger.csv exists", False, weights["ledger"], "missing")

    partial_score = 0.0
    try:
        partial = _load_json(w / "out" / "partial_results.json")
        partial_results = _results(partial)
        completed = set(_list(partial.get("completed_ids")))
        if not completed:
            completed = {
                item_id
                for item_id, result in partial_results.items()
                if isinstance(result, dict) and str(result.get("status", "")).strip().lower() == "completed"
            }
        failed = set(_list(partial.get("failed_ids")))
        pending = set(_list(partial.get("pending_ids")))
        early_ids = {"ITEM-007", "ITEM-008", "ITEM-009", "ITEM-010"}
        stopped_at_failure = (
            "ITEM-006" in failed
            or "ITEM-006" in pending
            or str(partial.get("round", "")).strip() == "1"
        )
        partial_score = (
            0.35 * (completed == set(gt["completed_after_round1"]))
            + 0.25 * stopped_at_failure
            + 0.25 * early_ids.isdisjoint(set(partial_results) | completed)
            + 0.15 * all(item in partial_results for item in gt["completed_after_round1"])
        )
        add("partial_results", "partial_results.json records round 1 stop without processing later items", partial_score >= 0.75, 0.0, {"score": round(partial_score, 4)})
    except Exception as exc:
        add("partial_results", "partial_results.json is parseable", False, 0.0, str(exc))

    report_text = ""
    for rel in ["resume_report.md", "batch_audit.log"]:
        path = w / "out" / rel
        if path.is_file():
            report_text += "\n" + path.read_text(encoding="utf-8", errors="replace").lower()
    report_tokens = ["resume", "state", "skipped", "item-006", "rejected", "item-008"]
    report_ok = all(token in report_text for token in report_tokens)
    source_ok = _source_unchanged(w)
    report_score = 0.45 * report_ok + 0.30 * source_ok + 0.25 * partial_score
    add("report_and_inputs", "resume report/audit explain recovery, partial state is valid, and inputs remain unchanged", report_score >= 0.80, weights["report_and_inputs"], {"source_unchanged": source_ok, "partial_score": round(partial_score, 4)})

    total = state_score * weights["state"] + final_score * weights["final_results"] + ledger_score * weights["ledger"] + report_score * weights["report_and_inputs"]
    if not source_ok:
        total = min(total, 0.70)
    if partial_score < 0.75:
        total = min(total, 0.70)
    return {"task": "105-partial-batch-resume-ledger", "workspace": str(w), "outcome_score": round(total, 4), "checks": checks}
