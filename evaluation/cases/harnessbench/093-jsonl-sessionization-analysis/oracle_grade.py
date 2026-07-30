from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


EXPECTED_SESSIONS = [
    {"session_id": "U100-1", "user_key": "U100", "session_start": "2026-04-10T10:00:00Z", "session_end": "2026-04-10T10:25:00Z", "event_count": "4", "landing_page": "/home", "last_page": "/checkout", "converted": "true", "campaign_source": "google"},
    {"session_id": "U100-2", "user_key": "U100", "session_start": "2026-04-10T11:00:00Z", "session_end": "2026-04-10T11:00:00Z", "event_count": "1", "landing_page": "/account", "last_page": "/account", "converted": "false", "campaign_source": "direct"},
    {"session_id": "U200-1", "user_key": "U200", "session_start": "2026-04-10T09:00:00Z", "session_end": "2026-04-10T09:30:00Z", "event_count": "2", "landing_page": "/home", "last_page": "/features", "converted": "false", "campaign_source": "email"},
    {"session_id": "U200-2", "user_key": "U200", "session_start": "2026-04-10T10:00:01Z", "session_end": "2026-04-10T10:00:01Z", "event_count": "1", "landing_page": "/checkout", "last_page": "/checkout", "converted": "true", "campaign_source": "email"},
]
EXPECTED_REJECTS = [
    {"line_number": "7", "event_id": "E104", "reason": "duplicate_event_id", "notes": "duplicate after first valid E104"},
    {"line_number": "8", "event_id": "E300", "reason": "bot_user", "notes": "SyntheticBot"},
    {"line_number": "9", "event_id": "", "reason": "malformed_json", "notes": "could not parse JSON"},
    {"line_number": "10", "event_id": "E400", "reason": "missing_timestamp", "notes": "timestamp blank"},
    {"line_number": "11", "event_id": "E401", "reason": "unknown_event_type", "notes": "scroll"},
]


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), [{k: (v or "").strip() for k, v in row.items()} for row in r]


def _sorted_rows(rows: list[dict[str, str]], keys: list[str]) -> list[dict[str, str]]:
    return sorted(rows, key=lambda row: tuple(row.get(key, "") for key in keys))


def _reason_matches(actual: str, expected: str) -> bool:
    actual_n = actual.strip().lower()
    expected_n = expected.strip().lower()
    aliases = {
        "bot_user": {"bot_user", "excluded_bot_user", "bot", "excluded_bot"},
        "duplicate_event_id": {"duplicate_event_id", "duplicate"},
        "malformed_json": {"malformed_json", "invalid_json", "parse_error"},
        "missing_timestamp": {"missing_timestamp", "blank_timestamp"},
        "unknown_event_type": {"unknown_event_type", "invalid_event_type"},
    }
    return actual_n == expected_n or actual_n in aliases.get(expected_n, set())


def _note_is_informative(actual: str, expected: str) -> bool:
    actual_n = actual.strip().lower()
    expected_n = expected.strip().lower()
    if not actual_n:
        return False
    key_terms = {
        "duplicate_event_id": ["duplicate", "line", "first", "kept"],
        "bot_user": ["bot", "synthetic", "crawler"],
        "malformed_json": ["json", "parse", "property", "malformed"],
        "missing_timestamp": ["timestamp", "blank", "empty"],
        "unknown_event_type": ["scroll", "event"],
    }
    for reason, terms in key_terms.items():
        if reason in expected_n:
            return any(term in actual_n for term in terms)
    return True


def score_workspace(workspace: str | Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []
    for rel in ["clickstream.jsonl", "session_rules.md", "campaign_map.csv"]:
        _add(checks, f"fixture_present_{rel}", (w / "in" / rel).is_file(), 0.015, f"missing {rel}")

    sessions = w / "out" / "sessions.csv"
    rejects = w / "out" / "reject_ledger.csv"
    summary = w / "out" / "session_summary.json"
    notes = w / "out" / "sessionization_notes.md"
    _add(checks, "sessions_exists", sessions.is_file(), 0.06, "missing sessions.csv")
    if sessions.is_file():
        try:
            header, rows = _rows(sessions)
            _add(checks, "sessions_header", header == ["session_id", "user_key", "session_start", "session_end", "event_count", "landing_page", "last_page", "converted", "campaign_source"], 0.06, f"got {header}")
            _add(checks, "sessions_exact", rows == EXPECTED_SESSIONS, 0.38, f"got {rows}")
            _add(checks, "identity_stitching", rows[:2] == EXPECTED_SESSIONS[:2], 0.08, "anonymous a1 must stitch to U100")
            _add(checks, "thirty_minute_boundary", any(r.get("session_id") == "U200-1" and r.get("event_count") == "2" for r in rows) and any(r.get("session_id") == "U200-2" for r in rows), 0.08, "30-minute boundary wrong")
        except Exception as exc:
            _add(checks, "sessions_parseable", False, 0.30, str(exc))
    else:
        for cid, weight in [("sessions_header", 0.06), ("sessions_exact", 0.38), ("identity_stitching", 0.08), ("thirty_minute_boundary", 0.08)]:
            _add(checks, cid, False, weight, "missing")

    _add(checks, "rejects_exists", rejects.is_file(), 0.05, "missing reject_ledger.csv")
    if rejects.is_file():
        try:
            header, rows = _rows(rejects)
            _add(checks, "rejects_header", header == ["line_number", "event_id", "reason", "notes"], 0.05, f"got {header}")
            identity_hits = 0
            note_hits = 0
            for exp in EXPECTED_REJECTS:
                row = next(
                    (
                        got
                        for got in rows
                        if got.get("line_number") == exp["line_number"]
                        and got.get("event_id") == exp["event_id"]
                        and _reason_matches(got.get("reason", ""), exp["reason"])
                    ),
                    None,
                )
                if not row:
                    continue
                identity_hits += 1
                if _note_is_informative(row.get("notes", ""), exp["reason"]):
                    note_hits += 1
            _add(checks, "rejects_identity", identity_hits == len(EXPECTED_REJECTS) and len(rows) == len(EXPECTED_REJECTS), 0.12, f"hits {identity_hits}/{len(EXPECTED_REJECTS)} got {rows}")
            _add(checks, "reject_notes", note_hits >= len(EXPECTED_REJECTS) - 1, 0.04, f"hits {note_hits}/{len(EXPECTED_REJECTS)}")
        except Exception as exc:
            _add(checks, "rejects_parseable", False, 0.10, str(exc))
    else:
        _add(checks, "rejects_header", False, 0.05, "missing")
        _add(checks, "rejects_identity", False, 0.12, "missing")
        _add(checks, "reject_notes", False, 0.04, "missing")

    _add(checks, "summary_exists", summary.is_file(), 0.05, "missing summary")
    if summary.is_file():
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
            expected = {
                "total_sessions": 4,
                "converted_sessions": 2,
                "excluded_bot_users": ["U300"],
                "deduped_event_ids": ["E104"],
                "malformed_line_numbers": [9],
                "missing_timestamp_event_ids": ["E400"],
                "unknown_event_type_ids": ["E401"],
            }
            _add(checks, "summary_exact", data == expected, 0.12, f"got {data}")
        except Exception as exc:
            _add(checks, "summary_parseable", False, 0.10, str(exc))
    else:
        _add(checks, "summary_exact", False, 0.12, "missing")

    _add(checks, "notes_exists", notes.is_file(), 0.03, "missing notes")
    if notes.is_file():
        text = notes.read_text(encoding="utf-8", errors="replace").lower()
        _add(checks, "notes_required", all(term in text for term in ["identity", "30-minute", "malformed", "duplicate"]), 0.06, "missing required caveats")
    else:
        _add(checks, "notes_required", False, 0.06, "missing")

    total = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total, 4)
    if any(c["id"] == "sessions_exact" and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "093-jsonl-sessionization-analysis", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
