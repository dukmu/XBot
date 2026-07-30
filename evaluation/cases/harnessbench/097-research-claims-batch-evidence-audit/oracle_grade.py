from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _row_text(row: Any) -> str:
    return json.dumps(row, ensure_ascii=False).lower()


def _has_all(text: str, tokens: list[str]) -> bool:
    return all(_norm(tok) in text for tok in tokens)


def _source_matches(actual: Any, expected: str) -> bool:
    got = _norm(actual).removeprefix("in/")
    want = _norm(expected).removeprefix("in/")
    got_base = got.rsplit("/", 1)[-1]
    want_base = want.rsplit("/", 1)[-1]
    return got == want or got.endswith(want) or got_base == want_base


def _source_in_text(text: str, expected: str) -> bool:
    text_n = _norm(text).replace("\\", "/")
    want = _norm(expected).removeprefix("in/").replace("\\", "/")
    want_base = want.rsplit("/", 1)[-1]
    return want in text_n or want_base in text_n


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    csv_path = w / "out" / "claim_audit.csv"
    json_path = w / "out" / "evidence_matrix.json"
    checks: list[dict[str, Any]] = []
    if not csv_path.is_file():
        return {"task": "097-research-claims-batch-evidence-audit", "workspace": str(w), "outcome_score": 0.0, "level": "fail", "checks": [{"id": "missing", "pass": False, "weight": 1.0, "detail": "out/claim_audit.csv missing"}]}

    format_score = coverage_score = status_score = source_score = location_score = evidence_score = secondary_score = repro_score = over_score = matrix_score = 0.0
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        header = ["claim_id", "claim_text", "status", "primary_source", "evidence_location", "evidence_signal", "secondary_sources", "rationale"]
        format_score = 1.0 if rows and list(rows[0].keys()) == header else 0.0
        by_id = {str(row.get("claim_id", "")).strip(): row for row in rows}
        expected_ids = set(gt["claims"])
        coverage_score = 1.0 if set(by_id) == expected_ids else len(set(by_id) & expected_ids) / len(expected_ids)
        status_hits = source_hits = location_hits = evidence_hits = secondary_hits = repro_hits = over_hits = 0
        for cid, exp in gt["claims"].items():
            row = by_id.get(cid, {})
            text = _row_text(row)
            if _norm(row.get("status")) == exp["status"]:
                status_hits += 1
            if _source_matches(row.get("primary_source", ""), exp["primary_source"]):
                source_hits += 1
            if _has_all(_norm(row.get("evidence_location")), exp["location_tokens"]):
                location_hits += 1
            if _has_all(_norm(row.get("evidence_signal")) + " " + _norm(row.get("rationale")), exp["tokens"]):
                evidence_hits += 1
            sec_text = _norm(row.get("secondary_sources")) + " " + text
            if all(_source_in_text(sec_text, source) for source in exp["secondary_sources"]):
                secondary_hits += 1
            if exp["status"] == "not_reproducible":
                if any(tok.lower() in text for tok in gt["repro_gap_tokens"]):
                    repro_hits += 1
            else:
                repro_hits += 1
            if exp["status"] == "overstated":
                if any(tok.lower() in text for tok in gt["overstatement_tokens"]):
                    over_hits += 1
            else:
                over_hits += 1
        n = len(expected_ids)
        status_score = status_hits / n
        source_score = source_hits / n
        location_score = location_hits / n
        evidence_score = evidence_hits / n
        secondary_score = secondary_hits / n
        repro_score = repro_hits / n
        over_score = over_hits / n
        checks.extend([
            {"id": "format", "label": "claim_audit.csv header is exact", "pass": format_score == 1.0, "weight": 0.08, "detail": list(rows[0].keys()) if rows else []},
            {"id": "coverage", "label": "all and only claims covered", "pass": coverage_score == 1.0, "weight": 0.08, "detail": sorted(by_id)},
            {"id": "status", "label": "claim statuses match evidence", "pass": status_score >= 0.85, "weight": 0.24, "detail": {"hits": status_hits}},
            {"id": "sources", "label": "primary sources match decisive evidence", "pass": source_score >= 0.80, "weight": 0.14, "detail": {"hits": source_hits}},
            {"id": "locations", "label": "evidence locations cite sections rows or fields", "pass": location_score >= 0.80, "weight": 0.12, "detail": {"hits": location_hits}},
            {"id": "signals", "label": "evidence signals contain decisive tokens", "pass": evidence_score >= 0.80, "weight": 0.16, "detail": {"hits": evidence_hits}},
            {"id": "secondary", "label": "multi-source claims include required secondary sources", "pass": secondary_score >= 0.85, "weight": 0.08, "detail": {"hits": secondary_hits}},
            {"id": "repro_gaps", "label": "not_reproducible claims cite missing artifacts", "pass": repro_score == 1.0, "weight": 0.05, "detail": {"hits": repro_hits}},
            {"id": "overstatement", "label": "overstated claim preserves scope limitation", "pass": over_score == 1.0, "weight": 0.05, "detail": {"hits": over_hits}},
        ])
    except Exception as exc:
        checks.append({"id": "csv_parse", "label": "claim_audit.csv parseable", "pass": False, "weight": 0.95, "detail": str(exc)})

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        items = data.get("claims", [])
        by_id = {str(item.get("claim_id", "")).strip(): item for item in items if isinstance(item, dict)}
        text = _row_text(data)
        ids_ok = set(by_id) == set(gt["claims"])
        evidence_items_ok = all(isinstance(item.get("evidence"), list) and item.get("evidence") for item in by_id.values())
        status_hits = sum(_norm(by_id.get(cid, {}).get("status")) == exp["status"] for cid, exp in gt["claims"].items())
        gap_hits = sum(tok.lower() in text for tok in gt["repro_gap_tokens"])
        matrix_score = 0.25 * bool(ids_ok) + 0.25 * bool(evidence_items_ok) + 0.35 * (status_hits / len(gt["claims"])) + 0.15 * min(gap_hits / len(gt["repro_gap_tokens"]), 1.0)
        checks.append({"id": "evidence_matrix_json", "label": "evidence_matrix.json mirrors claim statuses with evidence and repro gaps", "pass": matrix_score >= 0.85, "weight": 0.05, "detail": {"score": round(matrix_score, 4), "status_hits": status_hits}})
    except Exception as exc:
        checks.append({"id": "json_parse", "label": "evidence_matrix.json parseable", "pass": False, "weight": 0.05, "detail": str(exc)})

    total = (
        0.07 * format_score + 0.07 * coverage_score + 0.24 * status_score + 0.14 * source_score
        + 0.12 * location_score + 0.16 * evidence_score + 0.08 * secondary_score
        + 0.05 * repro_score + 0.04 * over_score + 0.03 * matrix_score
    )
    if source_score < 0.80:
        total = min(total, 0.68)
    if location_score < 0.80:
        total = min(total, 0.72)
    if repro_score < 1.0 or over_score < 1.0:
        total = min(total, 0.74)
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "097-research-claims-batch-evidence-audit", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
