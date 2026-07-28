from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"
_FIXTURE = _TASK_DIR / "fixtures" / "in" / "case_queue.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _load_json(_GT)
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    state_path = w / "out" / "state.json"
    final_path = w / "out" / "final_result.json"
    state: dict[str, Any] = {}
    final: dict[str, Any] = {}

    try:
        state = _load_json(state_path)
        add("state_parse", "out/state.json is parseable", True, 0.10)
    except Exception as exc:
        add("state_parse", "out/state.json is parseable", False, 0.10, str(exc))

    try:
        final = _load_json(final_path)
        add("final_parse", "out/final_result.json is parseable", True, 0.10)
    except Exception as exc:
        add("final_parse", "out/final_result.json is parseable", False, 0.10, str(exc))

    if state:
        add("state_complete", "state records final completion and no pending ids", state.get("completed_ids") == gt["completed_final"] and state.get("pending_ids") == [], 0.15)
        results = state.get("per_item_results", {})
        score_ok = isinstance(results, dict) and all(int(results.get(k, {}).get("risk_score", -1)) == v for k, v in gt["risk_scores"].items())
        add("state_scores", "state keeps correct per-item risk scores", score_ok, 0.15)
        log = state.get("processing_log", [])
        round2_ids = [e.get("id") for e in log if e.get("step") == "round2"]
        round2_pending_ok = set(["C-104", "C-105"]).issubset(set(round2_ids))
        redo_ids = [e.get("id") for e in log if e.get("step") in {"round2", "reprocessed"} and e.get("id") in gt["completed_after_round1"]]
        skip_ids = [
            e.get("id")
            for e in log
            if e.get("step") in {"skipped_preexisting", "skip_preexisting", "round1"}
            and str(e.get("status", "")).lower() in {"skipped_preexisting", "skip_preexisting", "reused_preexisting", "reused"}
        ]
        add("resume_log", "log shows pending work processed in round 2 without reprocessing completed items", round2_pending_ok and not redo_ids, 0.15, {"round2_ids": round2_ids, "redo_ids": redo_ids})
        skip_ids_present = set(skip_ids).issuperset(set(gt["completed_after_round1"]))
        add("skip_audit", "preexisting round 1 items are explicitly skipped or reused", skip_ids_present, 0.10, skip_ids)

    if final:
        ok = (
            final.get("run_id") == "resume-drill-74"
            and final.get("resumed_from_state") is True
            and final.get("completed_ids") == gt["completed_final"]
            and final.get("escalations") == gt["escalations"]
            and final.get("monitor_ids") == gt["monitor_ids"]
            and int(final.get("aggregate_risk_score", -1)) == gt["aggregate_risk_score"]
        )
        add("final_content", "final result has correct resume summary and aggregates", ok, 0.20, final)
        audit = final.get("resume_audit", {})
        add("final_audit", "final audit separates skipped and newly processed ids", audit.get("skipped_preexisting_ids") == gt["completed_after_round1"] and audit.get("newly_processed_ids") == ["C-104", "C-105"], 0.10, audit)
        patch_audit = final.get("patch_audit", {})
        add("patch_audit", "final audit separates applied and ignored patches", patch_audit.get("applied_patch_ids") == gt["applied_patch_ids"] and patch_audit.get("ignored_patch_ids") == gt["ignored_patch_ids"], 0.10, patch_audit)

    audit_text = (w / "out" / "resume_audit.md").read_text(encoding="utf-8", errors="replace").lower() if (w / "out" / "resume_audit.md").is_file() else ""
    add("resume_audit_md", "resume_audit.md explains history preservation and patch handling", all(term.lower() in audit_text for term in gt["audit_terms"]), 0.10)

    fixture_ok = _load_json(_FIXTURE) == {
        "run_id": "resume-drill-74",
        "items": _load_json(_FIXTURE)["items"],
    }
    add("fixture_present", "source fixture remains readable", fixture_ok, 0.05)

    total_w = sum(c["weight"] for c in checks)
    total = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    return {"task": "057-interruption-resume", "workspace": str(w), "outcome_score": total, "checks": checks}
