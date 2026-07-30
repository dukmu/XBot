from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).lower()


def _has_all(text: str, tokens: list[str]) -> bool:
    return all(_norm(tok) in text for tok in tokens)


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    out_path = w / "out" / "answers.json"
    checks: list[dict[str, Any]] = []
    if not out_path.is_file():
        return {"task": "096-offline-knowledge-qa-insufficient-evidence", "workspace": str(w), "outcome_score": 0.0, "level": "fail", "checks": [{"id": "missing", "pass": False, "weight": 1.0, "detail": "out/answers.json missing"}]}

    format_score = coverage_score = status_score = fact_score = source_score = evidence_score = missing_score = no_fab_score = 0.0
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
        rows = data.get("answers", data if isinstance(data, list) else [])
        required = {"question_id", "status", "answer", "sources", "missing_evidence"}
        format_score = 1.0 if isinstance(rows, list) and all(isinstance(row, dict) and required.issubset(row) for row in rows) else 0.0
        by_id = {str(row.get("question_id", "")).strip(): row for row in rows if isinstance(row, dict)}
        expected_ids = set(gt["answers"])
        coverage_score = 1.0 if set(by_id) == expected_ids else len(set(by_id) & expected_ids) / len(expected_ids)

        status_hits = fact_hits = source_hits = evidence_hits = missing_hits = no_fab_hits = 0
        for qid, exp in gt["answers"].items():
            row = by_id.get(qid, {})
            row_text = _text(row)
            status = _norm(row.get("status"))
            if status == exp["status"]:
                status_hits += 1
            if exp["facts"]:
                if _has_all(_norm(row.get("answer")) + " " + row_text, exp["facts"]):
                    fact_hits += 1
            else:
                if _norm(row.get("answer")) == "insufficient_evidence" or status == "insufficient_evidence":
                    fact_hits += 1
            sources = row.get("sources", [])
            source_text = _text(sources)
            if all(source.lower() in source_text for source in exp["sources"]):
                source_hits += 1
            if exp["evidence_tokens"]:
                if _has_all(source_text + " " + row_text, exp["evidence_tokens"]):
                    evidence_hits += 1
            else:
                evidence_hits += 1
            missing_text = _text(row.get("missing_evidence", [])) + " " + _norm(row.get("answer")) + " " + _norm(row.get("caveat"))
            if all(_norm(tok) in missing_text for tok in exp["missing"]):
                missing_hits += 1
            forbidden = gt.get("forbidden_answer_tokens", {}).get(qid, [])
            if not any(tok.lower() in row_text for tok in forbidden):
                no_fab_hits += 1

        n = len(expected_ids)
        status_score = status_hits / n
        fact_score = fact_hits / n
        source_score = source_hits / n
        evidence_score = evidence_hits / n
        missing_score = missing_hits / n
        no_fab_score = no_fab_hits / n
        checks.extend([
            {"id": "format", "label": "answers.json has required schema", "pass": format_score == 1.0, "weight": 0.10, "detail": {"rows": len(by_id)}},
            {"id": "coverage", "label": "all and only questions covered", "pass": coverage_score == 1.0, "weight": 0.10, "detail": sorted(by_id)},
            {"id": "status", "label": "answered/partial/insufficient statuses are correct", "pass": status_score >= 0.85, "weight": 0.20, "detail": {"hits": status_hits}},
            {"id": "facts", "label": "answers include required offline facts or insufficient marker", "pass": fact_score >= 0.85, "weight": 0.20, "detail": {"hits": fact_hits}},
            {"id": "sources", "label": "source files match the supporting documents", "pass": source_score >= 0.80, "weight": 0.15, "detail": {"hits": source_hits}},
            {"id": "evidence", "label": "evidence signals point to source text", "pass": evidence_score >= 0.80, "weight": 0.10, "detail": {"hits": evidence_hits}},
            {"id": "missing_evidence", "label": "partial/unanswerable items explain missing evidence", "pass": missing_score >= 0.85, "weight": 0.10, "detail": {"hits": missing_hits}},
            {"id": "no_fabrication", "label": "forbidden fabricated or stale answers are absent", "pass": no_fab_score == 1.0, "weight": 0.05, "detail": {"hits": no_fab_hits}},
        ])
    except Exception as exc:
        checks.append({"id": "parse", "label": "answers.json parseable", "pass": False, "weight": 1.0, "detail": str(exc)})

    total = (
        0.08 * format_score + 0.08 * coverage_score + 0.20 * status_score + 0.20 * fact_score
        + 0.15 * source_score + 0.10 * evidence_score + 0.14 * missing_score + 0.05 * no_fab_score
    )
    if missing_score < 0.85:
        total = min(total, 0.69)
    if no_fab_score < 1.0:
        total = min(total, 0.65)
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "096-offline-knowledge-qa-insufficient-evidence", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
