from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _all_in(text: str, tokens: list[str]) -> bool:
    t = _norm(text)
    return all(_norm(tok) in t for tok in tokens)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).lower()


def _source_matches(actual: Any, expected: str) -> bool:
    got = _norm(actual).removeprefix("in/")
    want = _norm(expected).removeprefix("in/")
    got_base = got.split("#", 1)[0]
    want_base = want.split("#", 1)[0]
    return got == want or got_base == want_base or got.endswith(want) or got_base.endswith(want_base)


def _find_decision(by_key: dict[str, Any], final: list[Any], key: str, exp: dict[str, Any]) -> dict[str, Any]:
    exact = by_key.get(key, {})
    if exact:
        return exact
    for item in final:
        if not isinstance(item, dict):
            continue
        if _all_in(item.get("value", ""), exp["value_tokens"]):
            return item
    return {}


def _has_unnegated_forbidden_value(row: dict[str, Any], forbidden_terms: list[str]) -> bool:
    value = _norm(row.get("value"))
    for term in forbidden_terms:
        term_l = _norm(term)
        if term_l and term_l in value:
            return True
    return False


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    record_path = w / "out" / "decision_record.json"
    conflicts_path = w / "out" / "source_conflicts.csv"
    actions_path = w / "out" / "action_items.csv"
    checks: list[dict[str, Any]] = []
    if not record_path.is_file():
        return {"task": "098-three-source-decision-record-synthesis", "workspace": str(w), "outcome_score": 0.0, "level": "fail", "checks": [{"id": "missing", "pass": False, "weight": 1.0, "detail": "out/decision_record.json missing"}]}

    format_score = decision_score = source_score = evidence_score = rejected_score = open_score = forbidden_score = conflict_score = action_score = 0.0
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
        required_top = {"decision_id", "final_decisions", "rejected_options", "open_questions"}
        format_score = 1.0 if required_top.issubset(data) and isinstance(data.get("final_decisions"), list) else 0.0
        final = data.get("final_decisions", [])
        by_key = {str(item.get("decision_key", "")).strip(): item for item in final if isinstance(item, dict)}
        decision_hits = source_hits = evidence_hits = 0
        for key, exp in gt["decisions"].items():
            row = _find_decision(by_key, final, key, exp)
            if _all_in(str(row.get("value", "")), exp["value_tokens"]):
                decision_hits += 1
            if _source_matches(row.get("winning_source", ""), exp["source"]):
                source_hits += 1
            if _all_in(str(row.get("evidence_signal", "")), exp["evidence_tokens"]):
                evidence_hits += 1
        n = len(gt["decisions"])
        decision_score = decision_hits / n
        source_score = source_hits / n
        evidence_score = evidence_hits / n

        rejected_text = _json_text(data.get("rejected_options", []))
        rejected_hits = 0
        for exp in gt["rejected_options"].values():
            if _all_in(rejected_text, exp["tokens"]) and exp["source"].lower() in rejected_text:
                rejected_hits += 1
        rejected_score = rejected_hits / len(gt["rejected_options"])
        open_text = _json_text(data.get("open_questions", []))
        open_score = sum(term.lower() in open_text for term in gt["open_questions"]) / len(gt["open_questions"])
        forbidden_score = 1.0 if not any(
            isinstance(row, dict) and _has_unnegated_forbidden_value(row, gt["forbidden_final_tokens"])
            for row in final
        ) else 0.0
        checks.extend([
            {"id": "format", "label": "decision_record.json has required top-level structure", "pass": format_score == 1.0, "weight": 0.08, "detail": None},
            {"id": "decisions", "label": "final decisions contain expected resolved values", "pass": decision_score >= 0.80, "weight": 0.24, "detail": {"hits": decision_hits}},
            {"id": "winning_sources", "label": "winning sources match governing evidence", "pass": source_score >= 0.80, "weight": 0.14, "detail": {"hits": source_hits}},
            {"id": "evidence", "label": "decision evidence signals include decisive text", "pass": evidence_score >= 0.80, "weight": 0.12, "detail": {"hits": evidence_hits}},
            {"id": "rejected", "label": "rejected options include blocked or superseded proposals", "pass": rejected_score >= 1.0, "weight": 0.12, "detail": {"hits": rejected_hits}},
            {"id": "open_questions", "label": "unresolved support staffing question is preserved", "pass": open_score >= 1.0, "weight": 0.08, "detail": {"score": open_score}},
            {"id": "forbidden", "label": "superseded or blocked proposals are not final decisions", "pass": forbidden_score == 1.0, "weight": 0.07, "detail": None},
        ])
    except Exception as exc:
        checks.append({"id": "record_parse", "label": "decision_record.json parseable", "pass": False, "weight": 0.85, "detail": str(exc)})

    try:
        with conflicts_path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        header = ["decision_key", "winning_source", "superseded_sources", "resolution_reason", "priority_rule"]
        cols_ok = rows and list(rows[0].keys()) == header
        text = _json_text(rows)
        term_hits = sum(term.lower() in text for term in gt["conflict_terms"])
        conflict_score = 0.30 * bool(cols_ok) + 0.70 * (term_hits / len(gt["conflict_terms"]))
        checks.append({"id": "source_conflicts", "label": "source_conflicts.csv captures source precedence conflicts", "pass": conflict_score >= 0.80, "weight": 0.08, "detail": {"score": round(conflict_score, 4), "term_hits": term_hits}})
    except Exception as exc:
        checks.append({"id": "source_conflicts_parse", "label": "source_conflicts.csv parseable", "pass": False, "weight": 0.08, "detail": str(exc)})

    try:
        with actions_path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        header = ["action_id", "owner", "due_date", "action", "status", "source_file", "evidence_signal"]
        cols_ok = rows and list(rows[0].keys()) == header
        text = _json_text(rows)
        action_hits = 0
        for exp in gt["actions"].values():
            if exp["owner"].lower() in text and exp["due"].lower() in text and exp["source"].lower() in text and all(tok.lower() in text for tok in exp["tokens"]):
                action_hits += 1
        action_score = 0.25 * bool(cols_ok) + 0.75 * (action_hits / len(gt["actions"]))
        checks.append({"id": "actions", "label": "action_items.csv contains current owners due dates and source evidence", "pass": action_score >= 0.85, "weight": 0.07, "detail": {"score": round(action_score, 4), "hits": action_hits}})
    except Exception as exc:
        checks.append({"id": "actions_parse", "label": "action_items.csv parseable", "pass": False, "weight": 0.07, "detail": str(exc)})

    total = (
        0.07 * format_score + 0.24 * decision_score + 0.14 * source_score + 0.12 * evidence_score
        + 0.12 * rejected_score + 0.08 * open_score + 0.06 * forbidden_score
        + 0.09 * conflict_score + 0.08 * action_score
    )
    if forbidden_score < 1.0 or open_score < 1.0:
        total = min(total, 0.84)
    if decision_score < 0.80 or source_score < 0.80:
        total = min(total, 0.74)
    th = gt["scoring"]["thresholds"]
    level = "excellent" if total >= th["excellent"] else "good" if total >= th["good"] else "pass" if total >= th["pass"] else "fail"
    return {"task": "098-three-source-decision-record-synthesis", "workspace": str(w), "outcome_score": round(float(total), 4), "level": level, "checks": checks}
