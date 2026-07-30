from __future__ import annotations

import json
import csv
import re
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _contains_signal(text: str, token: str) -> bool:
    text_norm = _norm(text)
    token_norm = _norm(token)
    if token_norm in text_norm:
        return True
    text_compact = re.sub(r"[^a-z0-9]+", "", text_norm)
    token_compact = re.sub(r"[^a-z0-9]+", "", token_norm)
    return bool(token_compact) and token_compact in text_compact


def _source_text(workspace: Path, rel: str) -> str:
    try:
        return (workspace / "in" / rel).read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        return ""


def _meaningful_words(text: str) -> set[str]:
    stop = {
        "the", "and", "for", "that", "with", "from", "this", "only", "into",
        "are", "not", "was", "were", "has", "have", "will", "must", "than",
    }
    return {word for word in re.findall(r"[a-z0-9]+", _norm(text)) if len(word) >= 4 and word not in stop}


def _source_grounded_quote(quote: str, source_text: str) -> bool:
    quote_compact = re.sub(r"[^a-z0-9]+", "", _norm(quote))
    source_compact = re.sub(r"[^a-z0-9]+", "", _norm(source_text))
    if quote_compact and quote_compact in source_compact:
        return True
    quote_words = _meaningful_words(quote)
    if len(quote_words) < 3:
        return False
    source_words = _meaningful_words(source_text)
    overlap = len(quote_words & source_words) / max(len(quote_words), 1)
    return overlap >= 0.65


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    facts_path = w / "out" / "resolved_facts.json"
    unc_path = w / "out" / "uncertainties.md"
    matrix_path = w / "out" / "conflict_matrix.csv"
    reliability_path = w / "out" / "source_reliability.json"
    decision_log_path = w / "out" / "decision_log.md"
    checks: list[dict[str, Any]] = []
    format_score = facts_score = evidence_score = quote_score = quote_grounding_score = priority_reason_score = scoped_field_score = uncertainty_score = rejection_score = matrix_score = reliability_score = decision_log_score = 0.0

    try:
        data = json.loads(facts_path.read_text(encoding="utf-8"))
        required = {"project_status", "approved_budget_musd", "launch_date", "customer_count", "primary_vendor", "service_scope", "scope_exception", "evidence"}
        format_score = 1.0 if required.issubset(data) and isinstance(data.get("evidence"), list) else 0.0
        fact_hits = 0
        for key, exp in gt["resolved"].items():
            actual = data.get(key)
            if "value" in exp:
                if _num(actual) == float(exp["value"]):
                    fact_hits += 1
            elif all(tok.lower() in _norm(actual) for tok in exp["value_tokens"]):
                fact_hits += 1
        facts_score = fact_hits / len(gt["resolved"])
        ev = data.get("evidence", [])
        ev_hits = 0
        quote_hits = 0
        grounded_hits = 0
        priority_hits = 0
        for key, exp in gt["resolved"].items():
            for item in ev:
                if item.get("fact_key") == key and item.get("source_file") == exp["source_file"] and str(item.get("quote_or_signal", "")).strip() and str(item.get("priority_reason", "")).strip():
                    ev_hits += 1
                    quote = _norm(item.get("quote_or_signal"))
                    priority_reason = _norm(item.get("priority_reason"))
                    source_text = _source_text(w, exp["source_file"])
                    terms = gt.get("evidence_quote_terms", {}).get(key, [])
                    terms_ok = all(_contains_signal(quote, term) for term in terms)
                    source_ok = any(_contains_signal(source_text, term) for term in terms)
                    quote_hits += int(terms_ok and source_ok)
                    grounded_hits += int(_source_grounded_quote(quote, source_text))
                    priority_hits += int(
                        ("rank" in priority_reason or "priority" in priority_reason)
                        and any(term in priority_reason for term in ("override", "supersede", "contradict", "conflict", "not ", "reject"))
                    )
                    break
        evidence_score = ev_hits / len(gt["resolved"])
        quote_score = quote_hits / len(gt["resolved"])
        quote_grounding_score = grounded_hits / len(gt["resolved"])
        priority_reason_score = priority_hits / len(gt["resolved"])
        scoped_hits = 0
        for key, rule in gt.get("scoped_field_rules", {}).items():
            actual = _norm(data.get(key))
            required_ok = all(_norm(term) in actual for term in rule.get("required", []))
            forbidden_ok = not any(_norm(term) in actual for term in rule.get("forbidden", []))
            scoped_hits += int(required_ok and forbidden_ok)
        scoped_field_score = scoped_hits / max(len(gt.get("scoped_field_rules", {})), 1)
        final_facts = {
            key: data.get(key)
            for key in ("project_status", "approved_budget_musd", "launch_date", "customer_count", "primary_vendor", "service_scope")
        }
        text = json.dumps(final_facts, ensure_ascii=False).lower()
        rejection_score = 1.0 if not any(sig.lower() in text for sig in gt["rejected_signals"]) else 0.0
        checks.extend([
            {"id": "format", "label": "resolved_facts.json has required schema", "pass": bool(format_score), "weight": 0.12, "detail": None},
            {"id": "facts", "label": "resolved facts follow priority and local-coverage rules", "pass": facts_score >= 0.8, "weight": 0.32, "detail": {"hits": fact_hits}},
            {"id": "evidence", "label": "evidence cites winning sources and priority reasons", "pass": evidence_score >= 0.8, "weight": 0.18, "detail": {"hits": ev_hits}},
            {"id": "evidence_quotes", "label": "evidence quotes contain fact-specific signals from the cited winning source", "pass": quote_score >= 0.8, "weight": 0.08, "detail": {"hits": quote_hits}},
            {"id": "evidence_grounding", "label": "evidence quotes are grounded in the cited source text", "pass": quote_grounding_score >= 0.8, "weight": 0.04, "detail": {"hits": grounded_hits}},
            {"id": "priority_reasoning", "label": "priority reasons explicitly explain override or rejection logic", "pass": priority_reason_score >= 0.7, "weight": 0.04, "detail": {"hits": priority_hits}},
            {"id": "scoped_fields", "label": "service scope and exception stay separated", "pass": scoped_field_score >= 1.0, "weight": 0.06, "detail": {"hits": scoped_hits}},
            {"id": "reject_low_priority", "label": "low-priority contradicted claims are not adopted", "pass": bool(rejection_score), "weight": 0.08, "detail": None},
        ])
    except Exception as exc:
        checks.append({"id": "facts_parse", "label": "resolved_facts.json parseable", "pass": False, "weight": 0.80, "detail": str(exc)})

    if unc_path.is_file():
        text = unc_path.read_text(encoding="utf-8", errors="replace").lower()
        hits = sum(1 for token in gt["uncertainties"] if token in text)
        uncertainty_score = hits / len(gt["uncertainties"])
        checks.append({"id": "uncertainties", "label": "uncertainties.md lists unconfirmed items", "pass": uncertainty_score >= 1.0, "weight": 0.15, "detail": {"hits": hits}})
    else:
        checks.append({"id": "uncertainties_missing", "label": "uncertainties.md exists", "pass": False, "weight": 0.15, "detail": "missing"})

    try:
        with matrix_path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        cols_ok = rows and set(gt["conflict_matrix"]["required_columns"]).issubset(rows[0].keys())
        text = json.dumps(rows, ensure_ascii=False).lower()
        term_hits = sum(term.lower() in text for term in gt["conflict_matrix"]["required_terms"])
        row_count_ok = len(rows) >= 5
        matrix_score = 0.35 * bool(cols_ok) + 0.45 * (term_hits / len(gt["conflict_matrix"]["required_terms"])) + 0.20 * bool(row_count_ok)
        checks.append({"id": "conflict_matrix", "label": "conflict_matrix.csv explains winners, losers, and local coverage", "pass": matrix_score >= 0.85, "weight": 0.15, "detail": {"score": round(matrix_score, 4), "rows": len(rows), "term_hits": term_hits}})
    except Exception as exc:
        checks.append({"id": "conflict_matrix_parse", "label": "conflict_matrix.csv parseable", "pass": False, "weight": 0.15, "detail": str(exc)})

    try:
        reliability = json.loads(reliability_path.read_text(encoding="utf-8"))
        text = json.dumps(reliability, ensure_ascii=False).lower()
        terms = gt.get("source_reliability_terms", [])
        term_score = sum(term.lower() in text for term in terms) / max(len(terms), 1)
        expected_sources = sorted(str(path.relative_to(w / "in")) for path in (w / "in" / "briefs").glob("*.md"))
        actual_sources = {str(item.get("source_file", "")).strip() for item in reliability if isinstance(item, dict)} if isinstance(reliability, list) else set()
        coverage_score = sum(source in actual_sources for source in expected_sources) / max(len(expected_sources), 1)
        rows_ok = isinstance(reliability, list) and len(reliability) >= len(expected_sources)
        reliability_score = 0.55 * term_score + 0.25 * coverage_score + 0.20 * bool(rows_ok)
        checks.append({"id": "source_reliability", "label": "source_reliability.json ranks and scopes every source", "pass": reliability_score >= 0.85, "weight": 0.08, "detail": {"score": round(reliability_score, 4), "coverage": round(coverage_score, 4)}})
    except Exception as exc:
        checks.append({"id": "source_reliability_parse", "label": "source_reliability.json parseable", "pass": False, "weight": 0.08, "detail": str(exc)})

    if decision_log_path.is_file():
        text = decision_log_path.read_text(encoding="utf-8", errors="replace").lower()
        terms = gt.get("decision_log_terms", [])
        decision_log_score = sum(term.lower() in text for term in terms) / max(len(terms), 1)
        checks.append({"id": "decision_log", "label": "decision_log.md explains scoped resolution and rejected rumors", "pass": decision_log_score >= 0.85, "weight": 0.07, "detail": {"score": round(decision_log_score, 4)}})
    else:
        checks.append({"id": "decision_log_missing", "label": "decision_log.md exists", "pass": False, "weight": 0.07, "detail": "missing"})

    total = 0.08 * format_score + 0.23 * facts_score + 0.11 * evidence_score + 0.07 * quote_score + 0.04 * quote_grounding_score + 0.04 * priority_reason_score + 0.06 * scoped_field_score + 0.06 * rejection_score + 0.12 * uncertainty_score + 0.10 * matrix_score + 0.06 * reliability_score + 0.03 * decision_log_score
    if reliability_score < 0.70 or decision_log_score < 0.70 or quote_score < 0.60 or quote_grounding_score < 0.60 or priority_reason_score < 0.60 or scoped_field_score < 1.0:
        total = min(total, 0.84)
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "035-conflicting-source-resolution", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
