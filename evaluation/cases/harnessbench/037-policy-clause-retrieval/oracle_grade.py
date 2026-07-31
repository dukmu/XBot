from __future__ import annotations

import json
import csv
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    path = w / "out" / "case_rulings.json"
    summary_path = w / "out" / "ruling_summary.csv"
    checks: list[dict[str, Any]] = []
    format_score = coverage_score = clause_score = decision_score = reason_score = quote_score = secondary_score = blocking_score = missing_score = line_item_score = summary_score = 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("rulings", [])
        by_id = {str(row.get("case_id")): row for row in rows if isinstance(row, dict)}
        required = {"case_id", "applicable_clause", "decision", "reason_code", "quote_or_signal", "secondary_clauses", "blocking_condition", "missing_information"}
        format_score = 1.0 if isinstance(rows, list) and all(required.issubset(row) for row in rows if isinstance(row, dict)) else 0.0
        expected_ids = set(gt["cases"])
        coverage_score = 1.0 if set(by_id) == expected_ids else len(set(by_id) & expected_ids) / len(expected_ids)
        clause_hits = decision_hits = reason_hits = quote_hits = secondary_hits = blocking_hits = missing_hits = 0
        for cid, exp in gt["cases"].items():
            row = by_id.get(cid, {})
            clause_hits += int(str(row.get("applicable_clause", "")).strip() == exp["applicable_clause"])
            decision_hits += int(str(row.get("decision", "")).strip() == exp["decision"])
            reason_code = str(row.get("reason_code", "")).strip()
            accepted_reason_codes = {exp["reason_code"], *exp.get("reason_aliases", [])}
            reason_hits += int(reason_code in accepted_reason_codes)
            quote = _norm(row.get("quote_or_signal"))
            quote_hits += int(all(_norm(token) in quote for token in exp["tokens"]))
            secondary = row.get("secondary_clauses", [])
            if not isinstance(secondary, list):
                secondary = []
            secondary_hits += int(sorted(map(str, secondary)) == sorted(exp.get("secondary_clauses", [])))
            blocking = _norm(row.get("blocking_condition"))
            blocking_hits += int(all(any(_norm(token) in blocking for token in group) for group in exp.get("blocking_token_groups", [])))
            missing = row.get("missing_information", [])
            if not isinstance(missing, list):
                missing = []
            missing_text = _norm(" ".join(map(str, missing)))
            expected_missing = exp.get("missing_terms", [])
            missing_hits += int(bool(missing) == bool(expected_missing) and all(_norm(term) in missing_text for term in expected_missing))
        n = len(expected_ids)
        clause_score = clause_hits / n
        decision_score = decision_hits / n
        reason_score = reason_hits / n
        quote_score = quote_hits / n
        secondary_score = secondary_hits / n
        blocking_score = blocking_hits / n
        missing_score = missing_hits / n
        line_hits = 0
        line_total = len(gt.get("line_items", {}))
        for key, exp in gt.get("line_items", {}).items():
            cid, line_id = key.split(":", 1)
            parent = by_id.get(cid, {})
            line_rows = parent.get("line_item_rulings", [])
            if not isinstance(line_rows, list):
                line_rows = []
            row = next((item for item in line_rows if str(item.get("line_id")) == line_id), {})
            secondary = row.get("secondary_clauses", [])
            if not isinstance(secondary, list):
                secondary = []
            missing = row.get("missing_information", [])
            if not isinstance(missing, list):
                missing = []
            text = json.dumps(row, ensure_ascii=False).lower()
            ok = (
                str(row.get("applicable_clause", "")).strip() == exp["applicable_clause"]
                and str(row.get("decision", "")).strip() == exp["decision"]
                and str(row.get("reason_code", "")).strip() == exp["reason_code"]
                and all(_norm(token) in text for token in exp.get("tokens", []))
                and all(clause in set(map(str, secondary)) for clause in exp.get("secondary_clauses", []))
                and all(_norm(term) in _norm(" ".join(map(str, missing))) for term in exp.get("missing_terms", []))
            )
            line_hits += int(ok)
        line_item_score = line_hits / max(line_total, 1)
        checks.extend([
            {"id": "format", "label": "case_rulings.json has required schema", "pass": bool(format_score), "weight": 0.06, "detail": None},
            {"id": "coverage", "label": "all and only cases covered", "pass": coverage_score == 1.0, "weight": 0.10, "detail": sorted(by_id)},
            {"id": "clauses", "label": "applicable clauses correct", "pass": clause_score >= 0.8, "weight": 0.14, "detail": {"hits": clause_hits}},
            {"id": "decisions", "label": "decisions correct", "pass": decision_score >= 0.8, "weight": 0.24, "detail": {"hits": decision_hits}},
            {"id": "reason_codes", "label": "reason codes correct", "pass": reason_score >= 0.8, "weight": 0.14, "detail": {"hits": reason_hits}},
            {"id": "quotes", "label": "quote_or_signal supports ruling", "pass": quote_score >= 0.8, "weight": 0.08, "detail": {"hits": quote_hits}},
            {"id": "secondary_clauses", "label": "secondary clauses capture exception rules", "pass": secondary_score >= 0.8, "weight": 0.08, "detail": {"hits": secondary_hits}},
            {"id": "blocking_conditions", "label": "blocking conditions explain deny/review decisions", "pass": blocking_score >= 0.8, "weight": 0.10, "detail": {"hits": blocking_hits}},
            {"id": "missing_information", "label": "missing_information captures absent evidence without inventing approvals", "pass": missing_score >= 0.8, "weight": 0.06, "detail": {"hits": missing_hits}},
            {"id": "line_item_rulings", "label": "mixed-packet line item rulings follow amendment and base clauses", "pass": line_item_score >= 0.80, "weight": 0.10, "detail": {"hits": line_hits, "expected": line_total}},
        ])
    except Exception as exc:
        checks.append({"id": "parse_error", "label": "case_rulings.json parseable", "pass": False, "weight": 1.0, "detail": str(exc)})
    try:
        with summary_path.open("r", encoding="utf-8", newline="") as fh:
            summary_rows = list(csv.DictReader(fh))
        cols_ok = summary_rows and {"case_id", "case_decision", "denied_lines", "review_lines", "approved_lines", "controlling_clause", "summary_reason"}.issubset(summary_rows[0].keys())
        text = json.dumps(summary_rows, ensure_ascii=False).lower()
        terms = gt.get("summary_terms", [])
        term_score = sum(term.lower() in text for term in terms) / max(len(terms), 1)
        covers_all = {row.get("case_id", "") for row in summary_rows} == set(gt["cases"])
        summary_score = 0.35 * bool(cols_ok) + 0.35 * term_score + 0.30 * bool(covers_all)
        checks.append({"id": "ruling_summary", "label": "ruling_summary.csv covers all cases and mixed packet aggregation", "pass": summary_score >= 0.85, "weight": 0.08, "detail": {"score": round(summary_score, 4)}})
    except Exception as exc:
        checks.append({"id": "ruling_summary_parse", "label": "ruling_summary.csv parseable", "pass": False, "weight": 0.08, "detail": str(exc)})

    total = 0.05 * format_score + 0.08 * coverage_score + 0.12 * clause_score + 0.20 * decision_score + 0.12 * reason_score + 0.06 * quote_score + 0.07 * secondary_score + 0.08 * blocking_score + 0.04 * missing_score + 0.10 * line_item_score + 0.08 * summary_score
    caps = []
    if coverage_score < 1.0:
        caps.append(0.72)
    if decision_score < 0.9:
        caps.append(0.78)
    if reason_score < 0.8:
        caps.append(0.74)
    if blocking_score < 0.8:
        caps.append(0.78)
    if line_item_score < 0.8:
        caps.append(0.74)
    if summary_score < 0.85:
        caps.append(0.78)
    if caps:
        total = min(total, min(caps))
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "037-policy-clause-retrieval", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
