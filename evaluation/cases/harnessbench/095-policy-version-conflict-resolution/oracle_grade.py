from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains_all(text: str, tokens: list[str]) -> bool:
    t = _norm(text)
    return all(_norm(token) in t for token in tokens)


def _check(cid: str, label: str, score: float, threshold: float, weight: float, detail: Any = None) -> dict[str, Any]:
    return {"id": cid, "label": label, "pass": score >= threshold, "weight": weight, "detail": detail}


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    rulings_path = w / "out" / "policy_rulings.json"
    audit_path = w / "out" / "conflict_audit.csv"
    checks: list[dict[str, Any]] = []

    if not rulings_path.is_file():
        return {"task": "095-policy-version-conflict-resolution", "workspace": str(w), "outcome_score": 0.0, "level": "fail", "checks": [{"id": "missing", "pass": False, "weight": 1.0, "detail": "out/policy_rulings.json missing"}]}

    format_score = coverage_score = decision_score = source_score = evidence_score = conflict_score = scope_score = insuff_score = forbidden_score = audit_score = 0.0
    try:
        data = json.loads(rulings_path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("rulings", [])
        required = {"case_id", "decision", "applicable_policy", "evidence_id", "quote_or_signal", "conflict_resolution", "coverage_scope", "caveat"}
        format_score = 1.0 if isinstance(rows, list) and all(isinstance(r, dict) and required.issubset(r) for r in rows) else 0.0
        by_id = {str(r.get("case_id", "")).strip(): r for r in rows if isinstance(r, dict)}
        expected_ids = set(gt["rulings"])
        coverage_score = len(set(by_id) & expected_ids) / len(expected_ids)
        if set(by_id) == expected_ids:
            coverage_score = 1.0

        decision_hits = source_hits = evidence_hits = conflict_hits = scope_hits = insuff_hits = forbidden_hits = 0
        for cid, exp in gt["rulings"].items():
            row = by_id.get(cid, {})
            decision = _norm(row.get("decision"))
            if decision == exp["decision"]:
                decision_hits += 1
            source = str(row.get("applicable_policy") or "").strip()
            if source == exp["source"]:
                source_hits += 1
            evidence_text = f"{row.get('evidence_id', '')} {row.get('quote_or_signal', '')}"
            if _contains_all(evidence_text, exp["evidence_tokens"]):
                evidence_hits += 1
            cr = row.get("conflict_resolution") if isinstance(row.get("conflict_resolution"), dict) else {}
            superseded = [str(x).strip() for x in cr.get("superseded_sources", [])] if isinstance(cr.get("superseded_sources", []), list) else []
            if all(src in superseded for src in exp["superseded"]):
                conflict_hits += 1
            scope_text = f"{row.get('coverage_scope', '')} {cr.get('reason', '')} {row.get('caveat', '')}"
            if _contains_all(scope_text, exp["scope_tokens"]):
                scope_hits += 1
            if exp["decision"] == "insufficient_evidence":
                if decision == "insufficient_evidence" and not source and _contains_all(scope_text + " " + evidence_text, exp["scope_tokens"]):
                    insuff_hits += 1
            else:
                insuff_hits += 1
            forbidden = gt.get("forbidden_final_tokens", {}).get(cid, [])
            final_text = json.dumps({"decision": row.get("decision"), "applicable_policy": row.get("applicable_policy"), "quote_or_signal": row.get("quote_or_signal")}, ensure_ascii=False).lower()
            if not any(tok.lower() in final_text for tok in forbidden):
                forbidden_hits += 1

        n = len(expected_ids)
        decision_score = decision_hits / n
        source_score = source_hits / n
        evidence_score = evidence_hits / n
        conflict_score = conflict_hits / n
        scope_score = scope_hits / n
        insuff_score = insuff_hits / n
        forbidden_score = forbidden_hits / n
        checks.extend([
            _check("format", "policy_rulings.json has required schema", format_score, 1.0, 0.10, {"rows": len(by_id)}),
            _check("coverage", "all and only expected cases are covered", coverage_score, 1.0, 0.10, sorted(by_id)),
            _check("decisions", "case decisions match policy/version rules", decision_score, 0.88, 0.22, {"hits": decision_hits, "total": n}),
            _check("sources", "applicable_policy cites governing source", source_score, 0.88, 0.14, {"hits": source_hits}),
            _check("evidence", "evidence signals include clause-specific tokens", evidence_score, 0.80, 0.14, {"hits": evidence_hits}),
            _check("conflicts", "superseded conflicting sources are recorded", conflict_score, 0.75, 0.12, {"hits": conflict_hits}),
            _check("scope", "coverage_scope preserves scoped exceptions and expiry", scope_score, 0.75, 0.10, {"hits": scope_hits}),
            _check("insufficient", "insufficient evidence case is not fabricated", insuff_score, 1.0, 0.08, {"hits": insuff_hits}),
            _check("forbidden", "stale or contradicted final outcomes are absent", forbidden_score, 1.0, 0.05, {"hits": forbidden_hits}),
        ])
    except Exception as exc:
        checks.append({"id": "parse", "label": "policy_rulings.json parseable", "pass": False, "weight": 0.95, "detail": str(exc)})

    try:
        with audit_path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        cols_ok = rows and ["case_id", "claim_axis", "winning_source", "losing_sources", "priority_rule", "coverage_scope"] == list(rows[0].keys())
        text = json.dumps(rows, ensure_ascii=False).lower()
        case_hits = sum(cid.lower() in text for cid in gt["conflict_cases"])
        priority_hits = sum(term in text for term in ["newer", "scoped", "effective", "supersede", "expired"])
        audit_score = 0.30 * bool(cols_ok) + 0.45 * (case_hits / len(gt["conflict_cases"])) + 0.25 * min(priority_hits / 4, 1.0)
        checks.append(_check("conflict_audit", "conflict_audit.csv covers conflict cases and priority rules", audit_score, 0.80, 0.05, {"score": round(audit_score, 4), "case_hits": case_hits}))
    except Exception as exc:
        checks.append({"id": "conflict_audit_parse", "label": "conflict_audit.csv parseable", "pass": False, "weight": 0.05, "detail": str(exc)})

    total = (
        0.08 * format_score + 0.08 * coverage_score + 0.22 * decision_score + 0.14 * source_score
        + 0.14 * evidence_score + 0.11 * conflict_score + 0.09 * scope_score
        + 0.07 * insuff_score + 0.04 * forbidden_score + 0.03 * audit_score
    )
    if scope_score < 0.75:
        total = min(total, 0.74)
    if insuff_score < 1.0:
        total = min(total, 0.69)
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "095-policy-version-conflict-resolution", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
