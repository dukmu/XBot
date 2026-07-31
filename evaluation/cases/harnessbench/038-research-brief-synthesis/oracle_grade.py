from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _contains(text: str, token: str) -> bool:
    return token.lower() in text.lower()


def _source_matches(actual: str, expected: str) -> bool:
    actual = actual.strip()
    expected = expected.strip()
    return actual == expected or actual == Path(expected).name


def _row_text(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False).lower()


def _rows_text(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, ensure_ascii=False).lower()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    brief_path = w / "out" / "research_brief.md"
    notes_path = w / "out" / "source_notes.csv"
    matrix_path = w / gt["evidence_matrix_path"]
    assumptions_path = w / "out" / "assumptions.json"
    checks: list[dict[str, Any]] = []
    structure_score = metrics_score = notes_score = safety_score = 0.0

    if brief_path.is_file():
        text = brief_path.read_text(encoding="utf-8", errors="replace")
        structure_tokens = gt["brief_required_terms"][:5]
        metric_tokens = gt["brief_required_terms"][5:]
        structure_hits = sum(1 for token in structure_tokens if _contains(text, token))
        metric_hits = sum(1 for token in metric_tokens if _contains(text, token))
        structure_score = structure_hits / len(structure_tokens)
        metrics_score = metric_hits / len(metric_tokens)
        safety_score = 1.0 if not any(_contains(text, token) for token in gt["forbidden_terms"]) else 0.0
        checks.extend([
            {"id": "brief_structure", "label": "research_brief.md has required sections", "pass": structure_score >= 1.0, "weight": 0.20, "detail": {"hits": structure_hits}},
            {"id": "brief_metrics", "label": "brief includes required metrics and caveats", "pass": metrics_score >= 0.8, "weight": 0.40, "detail": {"hits": metric_hits, "total": len(metric_tokens)}},
            {"id": "no_external_fabrication", "label": "brief does not claim external/current data", "pass": bool(safety_score), "weight": 0.15, "detail": None},
        ])
    else:
        checks.append({"id": "brief_missing", "label": "research_brief.md exists", "pass": False, "weight": 0.75, "detail": "missing"})

    try:
        with notes_path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        cols_ok = rows and {"source_file", "used_for", "key_signal"}.issubset(rows[0].keys())
        sources = {row.get("source_file", "").strip() for row in rows}
        coverage = sum(1 for source in gt["source_files"] if any(_source_matches(actual, source) for actual in sources)) / len(gt["source_files"])
        signals_ok = rows and all(str(row.get("key_signal", "")).strip() for row in rows)
        signal_hits = 0
        signal_total = len(gt.get("source_signal_expectations", {}))
        for source, tokens in gt.get("source_signal_expectations", {}).items():
            candidates = [row for row in rows if _source_matches(str(row.get("source_file", "")), source)]
            signal_hits += int(bool(candidates) and all(_contains(_rows_text(candidates), token) for token in tokens))
        signal_score = signal_hits / max(signal_total, 1)
        notes_score = 0.30 * bool(cols_ok) + 0.35 * coverage + 0.10 * bool(signals_ok) + 0.25 * signal_score
        checks.append({"id": "source_notes", "label": "source_notes.csv covers all offline sources with source-specific signals", "pass": notes_score >= 0.9, "weight": 0.25, "detail": {"sources": sorted(sources), "signal_hits": signal_hits}})
    except Exception as exc:
        checks.append({"id": "notes_parse", "label": "source_notes.csv parseable", "pass": False, "weight": 0.25, "detail": str(exc)})

    matrix_score = 0.0
    try:
        with matrix_path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        required_cols = set(gt.get("matrix_required_columns", ["claim", "type", "supporting_sources", "confidence", "caveat"]))
        cols_ok = rows and required_cols.issubset(rows[0].keys())
        all_text = json.dumps(rows, ensure_ascii=False).lower()
        evidence_hits = sum(term in all_text for term in gt["evidence_terms"]) / len(gt["evidence_terms"])
        type_hits = sum(term in all_text for term in gt["type_terms"]) / len(gt["type_terms"])
        calc_hits = sum(term.lower() in all_text for term in gt.get("calculation_terms", [])) / max(len(gt.get("calculation_terms", [])), 1)
        source_rows_ok = any(str(row.get("source_rows", "")).strip() for row in rows)
        claim_hits = 0
        claim_total = len(gt.get("matrix_claim_expectations", []))
        for exp in gt.get("matrix_claim_expectations", []):
            matched = False
            for row in rows:
                text = _row_text(row)
                type_ok = str(row.get("type", "")).strip().lower() == str(exp.get("type", "")).lower()
                token_ok = all(token.lower() in text for token in exp.get("tokens", []))
                source_ok = all(source.lower() in text for source in exp.get("sources", []))
                calc_ok = all(token.lower() in str(row.get("calculation", "")).lower() for token in exp.get("calculation", []))
                if type_ok and token_ok and source_ok and calc_ok and str(row.get("source_rows", "")).strip():
                    matched = True
                    break
            claim_hits += int(matched)
        claim_score = claim_hits / max(claim_total, 1)
        matrix_score = 0.18 * bool(cols_ok) + 0.25 * evidence_hits + 0.10 * type_hits + 0.12 * calc_hits + 0.05 * bool(source_rows_ok) + 0.30 * claim_score
        checks.append({"id": "evidence_matrix", "label": "evidence_matrix.csv aligns claims with types, sources, rows, and calculations", "pass": matrix_score >= 0.85, "weight": 0.20, "detail": {"score": round(matrix_score, 4), "claim_hits": claim_hits}})
    except Exception as exc:
        checks.append({"id": "evidence_matrix_parse", "label": "evidence_matrix.csv parseable", "pass": False, "weight": 0.20, "detail": str(exc)})

    assumptions_score = 0.0
    try:
        assumptions = json.loads(assumptions_path.read_text(encoding="utf-8"))
        text = json.dumps(assumptions, ensure_ascii=False).lower()
        terms = gt.get("assumption_terms", [])
        term_score = sum(term.lower() in text for term in terms) / max(len(terms), 1)
        rows_ok = isinstance(assumptions, list) and len(assumptions) >= 4
        fields_ok = rows_ok and all({"assumption_id", "claim_id", "assumption", "risk_if_wrong", "supporting_sources"}.issubset(row) for row in assumptions if isinstance(row, dict))
        matrix_claims: set[str] = set()
        if matrix_path.is_file():
            with matrix_path.open("r", encoding="utf-8", newline="") as fh:
                matrix_claims = {str(row.get("claim", "")).strip().lower() for row in csv.DictReader(fh)}
        linked = 0
        if isinstance(assumptions, list):
            for row in assumptions:
                claim_id = str(row.get("claim_id", "")).strip().lower() if isinstance(row, dict) else ""
                linked += int(bool(claim_id) and (claim_id in matrix_claims or any(claim_id in claim or claim in claim_id for claim in matrix_claims)))
        link_score = linked / max(len(assumptions), 1) if isinstance(assumptions, list) else 0.0
        assumptions_score = 0.35 * term_score + 0.20 * bool(rows_ok) + 0.25 * bool(fields_ok) + 0.20 * link_score
        checks.append({"id": "assumptions", "label": "assumptions.json ties risks to concrete evidence-matrix claims", "pass": assumptions_score >= 0.85, "weight": 0.10, "detail": {"score": round(assumptions_score, 4), "linked": linked}})
    except Exception as exc:
        checks.append({"id": "assumptions_parse", "label": "assumptions.json parseable", "pass": False, "weight": 0.10, "detail": str(exc)})

    total = 0.14 * structure_score + 0.30 * metrics_score + 0.10 * safety_score + 0.16 * notes_score + 0.20 * matrix_score + 0.10 * assumptions_score
    if assumptions_score < 0.60:
        total = min(total, 0.84)
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "038-research-brief-synthesis", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
