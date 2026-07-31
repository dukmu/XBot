from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip().lower() for v in value]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


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

    decision_score = 0.0
    path = w / "out" / "canary_decision.json"
    if path.is_file():
        try:
            data = _load_json(path)
            breached = set(_as_list(data.get("breached_metrics")))
            calculations = data.get("calculations", {}) if isinstance(data.get("calculations"), dict) else {}
            value_score = 0.0
            for metric, vals in exp["expected_values"].items():
                calc = calculations.get(metric, {}) if isinstance(calculations.get(metric), dict) else {}
                b = _num(calc.get("baseline_value"))
                c = _num(calc.get("canary_value"))
                status_ok = str(calc.get("evidence_status", "")).lower() == vals["status"]
                threshold_present = "threshold" in calc or "max_abs_delta" in calc or "max_pct_delta" in calc
                canary_ok = c is None if vals["canary"] is None else c is not None and abs(c - vals["canary"]) <= 0.05
                value_score += (
                    0.30 * (b is not None and abs(b - vals["baseline"]) <= 0.05)
                    + 0.30 * canary_ok
                    + 0.20 * status_ok
                    + 0.20 * threshold_present
                ) / len(exp["expected_values"])
            breach_score = len(breached & {x.lower() for x in exp["breached_metrics"]}) / len(exp["breached_metrics"])
            non_breach_ok = not (breached & {x.lower() for x in exp["non_breached_metrics"]})
            action_text = json.dumps(data.get("next_actions", ""), ensure_ascii=False).lower() + str(data.get("rationale", "")).lower()
            action_hits = sum(1 for kw in gt["action_keywords"] if kw in action_text)
            unsafe = "executed" in action_text or "already rolled back" in action_text
            caveat_ok = all(metric in action_text for metric in exp["missing_data_metrics"] + exp["low_sample_metrics"])
            decision_score = (
                0.14 * (str(data.get("release_id", "")).strip() == exp["release_id"])
                + 0.22 * (str(data.get("decision", "")).strip().lower() == exp["decision"])
                + 0.22 * breach_score
                + 0.08 * non_breach_ok
                + 0.24 * value_score
                + 0.04 * min(action_hits / 4, 1)
                + 0.03 * caveat_ok
                + 0.03 * (not unsafe)
            )
            add("canary_decision", "canary_decision.json has rollback decision, breaches, and calculations", decision_score >= 0.70, weights["decision_json"], {"score": round(decision_score, 4), "breached": sorted(breached)})
        except Exception as exc:
            add("canary_decision_parse", "canary_decision.json parseable", False, weights["decision_json"], str(exc))
    else:
        add("canary_decision_missing", "canary_decision.json exists", False, weights["decision_json"], "missing")

    evidence_score = 0.0
    evidence_path = w / gt["evidence_csv"]
    if evidence_path.is_file():
        try:
            with evidence_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                rows = [{k: (v or "").strip() for k, v in row.items()} for row in reader]
                header = list(reader.fieldnames or [])
            by_metric = {row.get("metric", ""): row for row in rows}
            header_ok = header == gt["evidence_header"]
            rows_ok = set(by_metric) == set(exp["expected_values"])
            status_ok = all(by_metric.get(metric, {}).get("evidence_status") == vals["status"] for metric, vals in exp["expected_values"].items())
            breach_ok = all(by_metric.get(metric, {}).get("breached", "").lower() in {"true", "yes", "1"} for metric in exp["breached_metrics"])
            non_breach_ok = all(by_metric.get(metric, {}).get("breached", "").lower() in {"false", "no", "0"} for metric in exp["non_breached_metrics"])
            evidence_score = 0.25 * header_ok + 0.25 * rows_ok + 0.25 * status_ok + 0.25 * (breach_ok and non_breach_ok)
            add("metric_evidence_csv", "metric_evidence.csv covers every metric, status, and breach flag", evidence_score >= 0.85, weights["evidence_csv"], {"score": evidence_score})
        except Exception as exc:
            add("metric_evidence_parse", "metric_evidence.csv parseable", False, weights["evidence_csv"], str(exc))
    else:
        add("metric_evidence_missing", "metric_evidence.csv exists", False, weights["evidence_csv"], "missing")

    unchanged = _source_unchanged(w)
    add("source_unchanged", "fixtures are not modified", unchanged, weights["source_unchanged"])
    total = decision_score * weights["decision_json"] + evidence_score * weights["evidence_csv"] + float(unchanged) * weights["source_unchanged"]
    thresholds = gt["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "067-canary-release-check", "workspace": str(w), "outcome_score": round(total, 4), "level": level, "checks": checks}
