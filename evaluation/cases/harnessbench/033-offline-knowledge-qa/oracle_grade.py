from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import re

TASK_DIR = Path(__file__).resolve().parent


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# def _norm(value: Any) -> str:
#     return str(value or "").strip().lower()

def _norm(value: Any) -> str:
    s = str(value or "").strip().lower()
    # Normalize time range format: transform "hh:mm-hh:mm utc" or "utc hh:mm-hh:mm" to standard
    s = re.sub(r'(\d{2}:\d{2})-(\d{2}:\d{2})\s*utc', r'\1-\2 utc', s, flags=re.I)
    s = re.sub(r'utc\s*(\d{2}:\d{2})-(\d{2}:\d{2})', r'\1-\2 utc', s, flags=re.I)
    return s



def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = _load_json(TASK_DIR / "ground_truth.json")
    out_path = w / "out" / "answers.json"
    checks: list[dict[str, Any]] = []
    answers_score = 0.0
    source_score = 0.0
    insufficient_score = 0.0
    format_score = 0.0

    if not out_path.is_file():
        return {"task": "033-offline-knowledge-qa", "outcome_score": 0.0, "level": "fail", "checks": [{"id": "answers_missing", "pass": False, "weight": 1.0, "detail": "out/answers.json missing"}]}

    try:
        data = _load_json(out_path)
        rows = data if isinstance(data, list) else data.get("answers", [])
        by_id = {str(row.get("question_id")): row for row in rows if isinstance(row, dict)}
        format_ok = isinstance(rows, list) and all({"question_id", "answer", "source_file", "quote_or_signal"}.issubset(row) for row in rows if isinstance(row, dict))
        format_score = 1.0 if format_ok and len(by_id) == len(gt["answers"]) else 0.0
        checks.append({"id": "format", "label": "answers.json is a complete JSON array with required fields", "pass": bool(format_score), "weight": 0.15, "detail": {"rows": len(by_id)}})

        fact_hits = 0
        source_hits = 0
        quote_hits = 0
        insufficient_hits = 0
        for qid, exp in gt["answers"].items():
            row = by_id.get(qid, {})
            answer = _norm(row.get("answer"))
            source = str(row.get("source_file") or "")
            quote = _norm(row.get("quote_or_signal"))
            if exp.get("insufficient"):
                if answer == "insufficient_evidence":
                    insufficient_hits += 1
                if not source and exp["missing_signal"] in quote:
                    quote_hits += 1
            else:
                if all(_norm(fact) in answer for fact in exp["facts"]):
                    fact_hits += 1
                if source == exp["source_file"]:
                    source_hits += 1
                if all(_norm(token) in quote for token in exp["quote_tokens"]):
                    quote_hits += 1
        answerable_count = sum(1 for exp in gt["answers"].values() if not exp.get("insufficient"))
        answers_score = fact_hits / answerable_count
        source_score = (source_hits + quote_hits) / (answerable_count * 2 + 1)
        insufficient_score = insufficient_hits
        checks.append({"id": "facts", "label": "answer facts match offline documents", "pass": answers_score >= 0.75, "weight": 0.35, "detail": {"fact_hits": fact_hits, "answerable": answerable_count}})
        checks.append({"id": "evidence", "label": "source_file and quote_or_signal support answers", "pass": source_score >= 0.70, "weight": 0.30, "detail": {"source_hits": source_hits, "quote_hits": quote_hits}})
        checks.append({"id": "insufficient", "label": "unanswerable question marked insufficient_evidence", "pass": insufficient_score == 1, "weight": 0.20, "detail": {"hits": insufficient_hits}})
    except Exception as exc:
        checks.append({"id": "parse_error", "label": "answers.json parseable", "pass": False, "weight": 1.0, "detail": str(exc)})

    total = 0.15 * format_score + 0.35 * answers_score + 0.30 * source_score + 0.20 * insufficient_score
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "033-offline-knowledge-qa", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
