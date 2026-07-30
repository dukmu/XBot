from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


EXPECTED_DRIFT = [
    {"extract_date": "2026-04-28", "field_name": "marketing_consent_v2", "drift_type": "new_unapproved_field", "expected": "absent", "observed": "present", "severity": "medium"},
    {"extract_date": "2026-04-28", "field_name": "event_type", "drift_type": "enum_expansion", "expected": "signup|purchase|cancel", "observed": "trial_pause", "severity": "high"},
    {"extract_date": "2026-04-29", "field_name": "event_ts", "drift_type": "date_format_drift", "expected": "timestamp_iso", "observed": "04/29/2026 11:00", "severity": "medium"},
    {"extract_date": "2026-04-29", "field_name": "customer_id", "drift_type": "nullability_violation", "expected": "non-null", "observed": "blank", "severity": "high"},
    {"extract_date": "2026-04-30", "field_name": "customer_id", "drift_type": "missing_required_field", "expected": "present", "observed": "absent", "severity": "high"},
    {"extract_date": "2026-04-30", "field_name": "amount_usd", "drift_type": "type_change", "expected": "decimal", "observed": "zero", "severity": "high"},
]
EXPECTED_REJECTS = [
    {"extract_date": "2026-04-28", "source_row": "2", "record_id": "E004", "reason": "invalid_enum", "notes": "event_type trial_pause"},
    {"extract_date": "2026-04-29", "source_row": "1", "record_id": "E005", "reason": "invalid_timestamp", "notes": "event_ts not ISO"},
    {"extract_date": "2026-04-29", "source_row": "2", "record_id": "E006", "reason": "missing_required", "notes": "customer_id blank"},
    {"extract_date": "2026-04-30", "source_row": "1", "record_id": "E007", "reason": "missing_required", "notes": "customer_id column missing"},
    {"extract_date": "2026-04-30", "source_row": "2", "record_id": "E008", "reason": "invalid_type", "notes": "amount_usd zero"},
]


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), [{k: (v or "").strip() for k, v in row.items()} for row in r]


def _sorted_rows(rows: list[dict[str, str]], keys: list[str]) -> list[dict[str, str]]:
    return sorted(rows, key=lambda row: tuple(row.get(key, "") for key in keys))


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", " ").replace("-", " ")


def _value_matches(actual: str, expected: str) -> bool:
    actual_n = _norm(actual)
    expected_n = _norm(expected)
    aliases = {
        "absent": ["absent", "not present", "notpresent", "not_present", "missing"],
        "blank": ["blank", "empty", "null", "null empty"],
        "present": ["present", "exists", "required"],
        "timestamp iso": ["timestamp iso", "iso", "iso8601", "yyyy mm dd", "yyyy mm ddthh:mm:ssz"],
        "decimal": ["decimal", "numeric", "number"],
        "zero": ["zero", "0"],
    }
    if expected_n == "signup|purchase|cancel":
        return all(token in actual_n for token in ["signup", "purchase", "cancel"])
    if expected_n in aliases:
        return any(alias in actual_n for alias in aliases[expected_n])
    return expected_n in actual_n or actual_n in expected_n


def _note_matches(actual: str, expected: str) -> bool:
    actual_n = _norm(actual)
    expected_n = _norm(expected)
    token_aliases = {
        "blank": ["blank", "empty", "null"],
        "iso": ["iso", "iso8601", "timestamp"],
        "zero": ["zero", "0"],
        "trial": ["trial"],
        "pause": ["pause"],
        "column": ["column", "header"],
        "missing": ["missing", "absent"],
    }
    tokens = [token for token in expected_n.split() if len(token) > 2 and token != "not"]
    if not tokens:
        return bool(actual_n)
    hits = 0
    for token in tokens:
        aliases = token_aliases.get(token, [token])
        if any(alias in actual_n for alias in aliases):
            hits += 1
    return hits >= max(1, len(tokens) - 1)


def score_workspace(workspace: str | Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []
    _add(checks, "schema_present", (w / "in" / "contracts" / "customer_events_schema.json").is_file(), 0.04, "missing schema")
    out = w / "out" / "schema_drift_report.csv"
    rejects = w / "out" / "rejected_rows.csv"
    summary = w / "out" / "drift_summary.json"
    notes = w / "out" / "audit_notes.md"

    _add(checks, "drift_exists", out.is_file(), 0.06, "missing drift report")
    if out.is_file():
        try:
            header, rows = _rows(out)
            _add(checks, "drift_header", header == ["extract_date", "field_name", "drift_type", "expected", "observed", "severity"], 0.06, f"got {header}")
            by_key = {
                (row.get("extract_date"), row.get("field_name"), row.get("drift_type")): row
                for row in rows
            }
            identity_hits = 0
            detail_hits = 0
            severity_hits = 0
            for exp in EXPECTED_DRIFT:
                key = (exp["extract_date"], exp["field_name"], exp["drift_type"])
                row = by_key.get(key)
                if not row:
                    continue
                identity_hits += 1
                if _value_matches(row.get("expected", ""), exp["expected"]) and _value_matches(row.get("observed", ""), exp["observed"]):
                    detail_hits += 1
                if row.get("severity", "").strip().lower() == exp["severity"]:
                    severity_hits += 1
            drift_identity_score = identity_hits / len(EXPECTED_DRIFT)
            drift_detail_score = detail_hits / len(EXPECTED_DRIFT)
            drift_severity_score = severity_hits / len(EXPECTED_DRIFT)
            _add(checks, "drift_identity", drift_identity_score == 1.0 and len(rows) == len(EXPECTED_DRIFT), 0.18, f"hits {identity_hits}/{len(EXPECTED_DRIFT)} got {rows}")
            _add(checks, "drift_details", drift_detail_score >= 0.85, 0.08, f"hits {detail_hits}/{len(EXPECTED_DRIFT)}")
            _add(checks, "drift_severity", drift_severity_score >= 0.85, 0.08, f"hits {severity_hits}/{len(EXPECTED_DRIFT)}")
            _add(checks, "schema_vs_data_drift_covered", {r["drift_type"] for r in rows} >= {"new_unapproved_field", "missing_required_field", "type_change", "enum_expansion", "nullability_violation", "date_format_drift"}, 0.08, "missing drift type")
        except Exception as exc:
            _add(checks, "drift_parseable", False, 0.30, str(exc))
    else:
        for cid, weight in [("drift_header", 0.06), ("drift_identity", 0.18), ("drift_details", 0.08), ("drift_severity", 0.08), ("schema_vs_data_drift_covered", 0.08)]:
            _add(checks, cid, False, weight, "missing")

    _add(checks, "rejects_exists", rejects.is_file(), 0.05, "missing rejected_rows.csv")
    if rejects.is_file():
        try:
            header, rows = _rows(rejects)
            _add(checks, "rejects_header", header == ["extract_date", "source_row", "record_id", "reason", "notes"], 0.05, f"got {header}")
            by_key = {
                (row.get("extract_date"), row.get("source_row"), row.get("record_id"), row.get("reason")): row
                for row in rows
            }
            identity_hits = 0
            note_hits = 0
            for exp in EXPECTED_REJECTS:
                key = (exp["extract_date"], exp["source_row"], exp["record_id"], exp["reason"])
                row = by_key.get(key)
                if not row:
                    continue
                identity_hits += 1
                if _note_matches(row.get("notes", ""), exp["notes"]):
                    note_hits += 1
            _add(checks, "rejects_identity", identity_hits == len(EXPECTED_REJECTS) and len(rows) == len(EXPECTED_REJECTS), 0.13, f"hits {identity_hits}/{len(EXPECTED_REJECTS)} got {rows}")
            _add(checks, "rejects_notes", note_hits >= len(EXPECTED_REJECTS) - 1, 0.05, f"hits {note_hits}/{len(EXPECTED_REJECTS)}")
        except Exception as exc:
            _add(checks, "rejects_parseable", False, 0.12, str(exc))
    else:
        _add(checks, "rejects_header", False, 0.05, "missing")
        _add(checks, "rejects_identity", False, 0.13, "missing")
        _add(checks, "rejects_notes", False, 0.05, "missing")

    _add(checks, "summary_exists", summary.is_file(), 0.05, "missing summary")
    if summary.is_file():
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
            _add(checks, "summary_dates_counts", data.get("extract_dates") == ["2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30"] and data.get("drift_count_by_date") == {"2026-04-27": 0, "2026-04-28": 2, "2026-04-29": 2, "2026-04-30": 2}, 0.08, f"got {data}")
            _add(checks, "summary_rejects_mismatches", data.get("high_severity_count") == 4 and data.get("rejected_row_count") == 5 and data.get("changelog_mismatches") == ["2026-04-30 customer_id removed despite changelog saying no required fields removed"], 0.08, f"got {data}")
        except Exception as exc:
            _add(checks, "summary_parseable", False, 0.12, str(exc))
    else:
        _add(checks, "summary_dates_counts", False, 0.08, "missing")
        _add(checks, "summary_rejects_mismatches", False, 0.08, "missing")

    _add(checks, "notes_exists", notes.is_file(), 0.03, "missing notes")
    if notes.is_file():
        text = notes.read_text(encoding="utf-8", errors="replace").lower()
        _add(checks, "notes_distinguish", all(term in text for term in ["schema drift", "row-level", "change log"]), 0.05, "notes must distinguish schema drift, row-level bad data, and change log mismatch")
    else:
        _add(checks, "notes_distinguish", False, 0.05, "missing")

    total = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total, 4)
    if any(c["id"] == "drift_identity" and not c["pass"] for c in checks):
        score = min(score, 0.69)
    if any(c["id"] == "drift_severity" and not c["pass"] for c in checks):
        score = min(score, 0.74)
    return {"task": "092-schema-drift-audit", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
