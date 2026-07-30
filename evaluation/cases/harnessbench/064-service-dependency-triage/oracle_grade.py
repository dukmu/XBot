from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains_all(text: str, tokens: list[str]) -> int:
    low = text.lower()
    return sum(1 for token in tokens if token.lower() in low)


def _source_unchanged(workspace: Path) -> bool:
    root = _TASK_DIR / "fixtures" / "in"
    src = workspace.resolve()
    if not src.is_dir():
        return True
    for original in root.rglob("*"):
        if not original.is_file():
            continue
        rel = original.relative_to(root)
        candidate = src / "in" / rel
        if candidate.is_file() and candidate.read_bytes() != original.read_bytes():
            return False
    return True


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _load_json(_GT)
    exp = gt["expected"]
    weights = gt["scoring"]["weights"]
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    json_score = 0.0
    path = w / "out" / "root_cause.json"
    if path.is_file():
        try:
            data = _load_json(path)
            affected_values = data.get("affected_services", [])
            affected = {str(x).lower() for x in affected_values} if isinstance(affected_values, list) else set()
            evidence_text = json.dumps(data.get("evidence", ""), ensure_ascii=False).lower()
            red_text = json.dumps(data.get("excluded_red_herrings", ""), ensure_ascii=False).lower()
            source_hits = _contains_all(evidence_text, exp["evidence_sources"])
            affected_hits = len(affected & {x.lower() for x in exp["affected_services"]})
            red_hits = _contains_all(red_text, exp["red_herrings"])
            confidence_ok = str(data.get("confidence", "")).lower() in {"high", "medium", "0.8", "0.9", "0.95"} or isinstance(data.get("confidence"), (int, float))
            json_score = (
                0.22 * (_norm(data.get("incident_id")) == gt["incident_id"].lower())
                + 0.24 * (_norm(data.get("root_cause_service")) == exp["root_cause_service"])
                + 0.18 * (_norm(data.get("root_cause_change_id")) == exp["root_cause_change_id"].lower())
                + 0.14 * (affected_hits / len(exp["affected_services"]))
                + 0.12 * min(source_hits / 4, 1)
                + 0.07 * min(red_hits / len(exp["red_herrings"]), 1)
                + 0.03 * confidence_ok
            )
            add("root_cause_json", "root_cause.json identifies root cause, evidence, and red herrings", json_score >= 0.70, weights["root_cause_json"], {"score": round(json_score, 4), "source_hits": source_hits, "red_hits": red_hits})
        except Exception as exc:
            add("root_cause_parse", "root_cause.json parseable", False, weights["root_cause_json"], str(exc))
    else:
        add("root_cause_missing", "root_cause.json exists", False, weights["root_cause_json"], "missing")

    notes_score = 0.0
    notes = w / "out" / "triage_notes.md"
    if notes.is_file():
        text = notes.read_text(encoding="utf-8", errors="replace")
        keyword_hits = _contains_all(text, gt["notes_keywords"])
        file_hits = _contains_all(text, exp["evidence_sources"])
        fact_inference_ok = bool(re.search(r"fact|observed|inference|inferred", text, re.IGNORECASE))
        notes_score = 0.45 * (keyword_hits / len(gt["notes_keywords"])) + 0.40 * min(file_hits / 3, 1) + 0.15 * fact_inference_ok
        add("triage_notes", "triage_notes.md cites sources and separates facts from inference", notes_score >= 0.70, weights["triage_notes"], {"score": round(notes_score, 4)})
    else:
        add("triage_notes_missing", "triage_notes.md exists", False, weights["triage_notes"], "missing")

    unchanged = _source_unchanged(w)
    add("source_unchanged", "fixtures are not modified", unchanged, weights["source_unchanged"])
    total = json_score * weights["root_cause_json"] + notes_score * weights["triage_notes"] + float(unchanged) * weights["source_unchanged"]
    thresholds = gt["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "064-service-dependency-triage", "workspace": str(w), "outcome_score": round(total, 4), "level": level, "checks": checks}
