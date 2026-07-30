from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _row_matches(row: dict[str, Any], expected_key: str, terms: list[str]) -> bool:
    span = _norm(row.get("evidence_span"))
    details = _norm(row.get("details"))
    fix = _norm(row.get("expected_fix"))
    text = " ".join([span, details, fix])
    return bool(span) and all(_norm(term) in text for term in terms)


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    err_path = w / "out" / "citation_errors.csv"
    bib_path = w / "out" / "corrected_bib.json"
    notes_path = w / "out" / "audit_notes.md"
    graph_path = w / "out" / "citation_graph.json"
    checks: list[dict[str, Any]] = []
    error_score = corrected_score = format_score = 0.0

    try:
        with err_path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        cols_ok = rows and {"error_type", "citation_key", "details", "expected_fix", "evidence_span"}.issubset(rows[0].keys())
        format_score += 0.5 if cols_ok else 0.0
        rows_by_key: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = f"{row.get('error_type', '').strip()}:{row.get('citation_key', '').strip()}"
            rows_by_key.setdefault(key, []).append(row)
        no_merged_keys = all("/" not in row.get("citation_key", "") and "," not in row.get("citation_key", "") for row in rows)
        hits = 0
        evidence_hits = 0
        expected = []
        for etype, keys in gt["errors"].items():
            expected.extend((etype, key) for key in keys)
        consumed_rows: set[tuple[str, int]] = set()
        for item in expected:
            canonical = f"{item[0]}:{item[1]}"
            candidates = [canonical, *gt.get("error_aliases", {}).get(canonical, [])]
            matched: tuple[str, int] | None = None
            for candidate in candidates:
                for idx, _row in enumerate(rows_by_key.get(candidate, [])):
                    row_ref = (candidate, idx)
                    if row_ref not in consumed_rows:
                        matched = row_ref
                        break
                if matched is not None:
                    break
            if matched is not None:
                consumed_rows.add(matched)
                hits += 1
        for key, terms in gt.get("evidence_span_terms", {}).items():
            keys = [key, *gt.get("error_aliases", {}).get(key, [])]
            candidate_rows = [row for k in keys for row in rows_by_key.get(k, [])]
            if any(_row_matches(row, key, terms) for row in candidate_rows):
                evidence_hits += 1
        row_score = hits / len(expected)
        span_score = evidence_hits / max(len(gt.get("evidence_span_terms", {})), 1)
        count_score = 1.0 if len(rows) == len(expected) and no_merged_keys else 0.0
        error_score = 0.65 * row_score + 0.25 * span_score + 0.10 * count_score
        checks.append({"id": "errors_csv", "label": "citation_errors.csv covers expected error rows and evidence spans", "pass": error_score >= 1.0 and cols_ok, "weight": 0.40, "detail": {"hits": hits, "expected": len(expected), "evidence_hits": evidence_hits, "rows": len(rows), "no_merged_keys": no_merged_keys}})
    except Exception as exc:
        checks.append({"id": "errors_parse", "label": "citation_errors.csv parseable", "pass": False, "weight": 0.40, "detail": str(exc)})

    try:
        bib = json.loads(bib_path.read_text(encoding="utf-8"))
        format_score += 0.5 if isinstance(bib, dict) else 0.0
        required = gt["corrected"]["required_keys"]
        forbidden = gt["corrected"]["forbidden_keys"]
        key_score = 0.5 * (sum(1 for key in required if key in bib) / len(required)) + 0.2 * (sum(1 for key in forbidden if key not in bib) / len(forbidden))
        field_hits = 0
        field_total = 0
        for key, exp in gt["corrected"]["field_expectations"].items():
            entry = bib.get(key, {})
            if "year" in exp:
                field_total += 1
                actual_year = str(entry.get("year"))
                expected_year = str(exp["year"])
                if expected_year in {"2024a", "2024b"}:
                    field_hits += int(actual_year in {expected_year, "2024"})
                else:
                    field_hits += int(actual_year == expected_year)
            if "title" in exp:
                field_total += 1
                field_hits += int(_norm(entry.get("title")) == _norm(exp["title"]))
            if "authors_contain" in exp:
                field_total += 1
                authors = json.dumps(entry.get("authors", []), ensure_ascii=False).lower()
                field_hits += int(all(token.lower() in authors for token in exp["authors_contain"]))
        corrected_score = min(1.0, key_score + 0.3 * (field_hits / max(field_total, 1)))
        checks.append({"id": "corrected_bib", "label": "corrected_bib.json has corrected keys and fields", "pass": corrected_score >= 0.85, "weight": 0.40, "detail": {"field_hits": field_hits, "field_total": field_total}})
    except Exception as exc:
        checks.append({"id": "bib_parse", "label": "corrected_bib.json parseable", "pass": False, "weight": 0.40, "detail": str(exc)})

    checks.append({"id": "format", "label": "required output formats are valid CSV and JSON", "pass": format_score >= 1.0, "weight": 0.10, "detail": {"score": format_score}})
    notes_score = 0.0
    if notes_path.is_file():
        notes = notes_path.read_text(encoding="utf-8", errors="replace").lower()
        aliases = gt.get("audit_term_aliases", {})
        hits = 0
        for term in gt.get("audit_terms", []):
            candidates = [term, *aliases.get(term, [])]
            hits += int(any(candidate.lower() in notes for candidate in candidates))
        notes_score = hits / max(len(gt.get("audit_terms", [])), 1)
    checks.append({"id": "audit_notes", "label": "audit_notes.md explains unknowns, suffixes, and no external lookup", "pass": notes_score >= 0.75, "weight": 0.10, "detail": {"score": notes_score}})
    graph_score = 0.0
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        keys_ok = isinstance(graph, dict) and {"in_text_citations", "bibliography_entries", "key_renames"}.issubset(graph)
        text = json.dumps(graph, ensure_ascii=False)
        terms = gt.get("citation_graph_terms", [])
        graph_score = 0.25 * bool(keys_ok) + 0.75 * (sum(term in text for term in terms) / max(len(terms), 1))
        checks.append({"id": "citation_graph", "label": "citation_graph.json maps draft citations to corrected bibliography actions", "pass": graph_score >= 0.85, "weight": 0.10, "detail": {"score": round(graph_score, 4)}})
    except Exception as exc:
        checks.append({"id": "citation_graph_parse", "label": "citation_graph.json parseable", "pass": False, "weight": 0.10, "detail": str(exc)})

    total = 0.45 * error_score + 0.27 * corrected_score + 0.08 * format_score + 0.10 * notes_score + 0.10 * graph_score
    caps = []
    if error_score < 0.95:
        caps.append(0.78)
    if corrected_score < 0.85:
        caps.append(0.74)
    if format_score < 1.0:
        caps.append(0.70)
    if graph_score < 0.60:
        caps.append(0.84)
    if caps:
        total = min(total, min(caps))
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "036-citation-consistency-audit", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
