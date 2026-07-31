from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    path = w / "out" / "evidence_matrix.csv"
    checks: list[dict[str, Any]] = []
    if not path.is_file():
        return {"task": "034-evidence-matrix-claims", "outcome_score": 0.0, "level": "fail", "checks": [{"id": "missing", "pass": False, "weight": 1.0, "detail": "out/evidence_matrix.csv missing"}]}

    format_score = coverage_score = class_score = source_score = evidence_score = 0.0
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        required = {"claim_id", "claim", "classification", "source_file", "evidence_phrase"}
        format_score = 1.0 if rows and required.issubset(rows[0].keys()) else 0.0
        by_id = {row.get("claim_id", "").strip(): row for row in rows}
        expected_ids = set(gt["claims"])
        coverage_score = 1.0 if set(by_id) == expected_ids else len(set(by_id) & expected_ids) / len(expected_ids)
        class_hits = source_hits = evidence_hits = 0
        for cid, exp in gt["claims"].items():
            row = by_id.get(cid, {})
            if _norm(row.get("classification")) == exp["classification"]:
                class_hits += 1
            if str(row.get("source_file", "")).strip() == exp["source_file"]:
                source_hits += 1
            phrase = _norm(row.get("evidence_phrase"))
            if all(_norm(token) in phrase for token in exp["tokens"]):
                evidence_hits += 1
        n = len(expected_ids)
        class_score = class_hits / n
        source_score = source_hits / n
        evidence_score = evidence_hits / n
        checks.extend([
            {"id": "format", "label": "CSV has required columns", "pass": bool(format_score), "weight": 0.15, "detail": list(rows[0].keys()) if rows else []},
            {"id": "coverage", "label": "all and only expected claims covered", "pass": coverage_score == 1.0, "weight": 0.20, "detail": {"ids": sorted(by_id)}},
            {"id": "classification", "label": "support/contradict/unclear labels are correct", "pass": class_score >= 0.8, "weight": 0.25, "detail": {"hits": class_hits, "total": n}},
            {"id": "sources", "label": "source_file points to correct source", "pass": source_score >= 0.8, "weight": 0.20, "detail": {"hits": source_hits, "total": n}},
            {"id": "evidence", "label": "evidence_phrase includes decisive text signals", "pass": evidence_score >= 0.8, "weight": 0.20, "detail": {"hits": evidence_hits, "total": n}},
        ])
    except Exception as exc:
        checks.append({"id": "parse_error", "label": "evidence_matrix.csv parseable", "pass": False, "weight": 1.0, "detail": str(exc)})

    total = 0.15 * format_score + 0.20 * coverage_score + 0.25 * class_score + 0.20 * source_score + 0.20 * evidence_score
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "034-evidence-matrix-claims", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
