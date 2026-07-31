from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm_set(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {str(v).strip().lower() for v in values}


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
    weights = gt["scoring"]["weights"]
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    incident_score = 0.0
    path = w / "out" / "deduped_incidents.json"
    if path.is_file():
        try:
            data = _load_json(path)
            incidents = data.get("incidents", []) if isinstance(data, dict) else []
            by_root = {str(i.get("root_alert_id", "")).strip().lower(): i for i in incidents if isinstance(i, dict)}
            per = 1.0 / len(gt["expected_incidents"])
            for exp in gt["expected_incidents"]:
                inc = by_root.get(exp["root_alert_id"].lower(), {})
                cluster = _norm_set(inc.get("cluster_alert_ids"))
                impact = _norm_set(inc.get("impact_services"))
                evidence_text = json.dumps(inc.get("evidence", ""), ensure_ascii=False).lower() + str(inc.get("summary", "")).lower()
                incident_score += per * (
                    0.20 * (str(inc.get("root_service", "")).lower() == exp["root_service"])
                    + 0.35 * (len(cluster & {x.lower() for x in exp["cluster_alert_ids"]}) / len(exp["cluster_alert_ids"]))
                    + 0.25 * (len(impact & {x.lower() for x in exp["impact_services"]}) / len(exp["impact_services"]))
                    + 0.20 * bool(re.search(r"topology|depends|downstream|fingerprint|timeout|eviction|connection", evidence_text))
                )
            add("deduped_incidents", "deduped_incidents.json groups alerts by root cause and topology", incident_score >= 0.70, weights["incidents"], {"score": round(incident_score, 4), "incidents": len(incidents)})
        except Exception as exc:
            add("deduped_incidents_parse", "deduped_incidents.json parseable", False, weights["incidents"], str(exc))
    else:
        add("deduped_incidents_missing", "deduped_incidents.json exists", False, weights["incidents"], "missing")

    noise_score = 0.0
    report = w / "out" / "noise_report.md"
    if report.is_file():
        text = report.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        id_hits = sum(1 for aid in gt["noise_alert_ids"] if aid.lower() in low)
        keyword_hits = sum(1 for kw in gt["noise_keywords"] if kw.lower() in low)
        symptom_ok = "duplicate" in low or "downstream" in low or "symptom" in low
        noise_score = 0.45 * (id_hits / len(gt["noise_alert_ids"])) + 0.40 * (keyword_hits / len(gt["noise_keywords"])) + 0.15 * symptom_ok
        add("noise_report", "noise_report.md explains filtered noise and duplicate symptoms", noise_score >= 0.70, weights["noise_report"], {"score": round(noise_score, 4), "id_hits": id_hits})
    else:
        add("noise_report_missing", "noise_report.md exists", False, weights["noise_report"], "missing")

    unchanged = _source_unchanged(w)
    add("source_unchanged", "fixtures are not modified", unchanged, weights["source_unchanged"])
    total = incident_score * weights["incidents"] + noise_score * weights["noise_report"] + float(unchanged) * weights["source_unchanged"]
    thresholds = gt["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "063-alert-dedup-noise", "workspace": str(w), "outcome_score": round(total, 4), "level": level, "checks": checks}
