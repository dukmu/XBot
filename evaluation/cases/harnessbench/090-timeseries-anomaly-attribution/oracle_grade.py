from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any


EXPECTED_ROWS = [
    {"anomaly_id": "A001", "timestamp": "2026-04-20T10:00:00Z", "service": "checkout", "region": "us-east", "metric": "error_rate", "observed": "0.0938", "expected": "0.0100", "z_score": "8.4", "severity": "high", "attributed_cause": "deployment:D-441"},
    {"anomaly_id": "A002", "timestamp": "2026-04-20T10:00:00Z", "service": "payments", "region": "eu-west", "metric": "latency_p95_ms", "observed": "910", "expected": "210", "z_score": "7.0", "severity": "high", "attributed_cause": "third_party:I-19"},
    {"anomaly_id": "A003", "timestamp": "2026-04-20T11:00:00Z", "service": "search", "region": "global", "metric": "requests", "observed": "410", "expected": "1000", "z_score": "-5.9", "severity": "medium", "attributed_cause": "marketing:M-07"},
    {"anomaly_id": "A004", "timestamp": "2026-04-20T11:30:00Z", "service": "checkout", "region": "us-east", "metric": "latency_p95_ms", "observed": "880", "expected": "220", "z_score": "6.6", "severity": "high", "attributed_cause": "deployment:D-443"},
    {"anomaly_id": "A005", "timestamp": "2026-04-20T12:30:00Z", "service": "profile", "region": "global", "metric": "error_rate", "observed": "0.0500", "expected": "0.0100", "z_score": "4.2", "severity": "medium", "attributed_cause": "unattributed"},
    {"anomaly_id": "A006", "timestamp": "2026-04-20T12:30:00Z", "service": "profile", "region": "global", "metric": "latency_p95_ms", "observed": "260", "expected": "210", "z_score": "3.1", "severity": "medium", "attributed_cause": "unattributed"},
]


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), [{k: (v or "").strip() for k, v in row.items()} for row in r]


def _close(got: Any, exp: float, tol: float = 0.01) -> bool:
    try:
        return abs(float(got) - exp) <= tol
    except Exception:
        return False


def _norm_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _norm_cause(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", ":")
    text = re.sub(r":+", ":", text)
    return text


def _cause_category(value: Any) -> str:
    cause = _norm_cause(value)
    if cause.startswith("deployment"):
        return "deployment"
    if cause.startswith("third_party"):
        return "third_party"
    if cause.startswith("marketing"):
        return "marketing"
    return cause


def _row_matches(actual: dict[str, str], expected: dict[str, str]) -> bool:
    if _norm_id(actual.get("anomaly_id")) != _norm_id(expected.get("anomaly_id")):
        return False
    for field in ["timestamp", "service", "region", "metric", "severity"]:
        if actual.get(field) != expected.get(field):
            return False
    if _norm_cause(actual.get("attributed_cause")) != _norm_cause(expected.get("attributed_cause")):
        return False
    return all(_close(actual.get(field), float(expected[field]), 0.01) for field in ["observed", "expected", "z_score"])


def _anomaly_rows_match(rows: list[dict[str, str]]) -> bool:
    actual = {_norm_id(r.get("anomaly_id")): r for r in rows}
    expected = {_norm_id(r.get("anomaly_id")): r for r in EXPECTED_ROWS}
    return set(actual) == set(expected) and all(_row_matches(actual[k], expected[k]) for k in expected)


def _normalized_cause_counts(counts: Any) -> dict[str, int]:
    out = {"deployment": 0, "third_party": 0, "marketing": 0, "unattributed": 0}
    if not isinstance(counts, dict):
        return out
    for key, value in counts.items():
        try:
            n = int(value)
        except Exception:
            continue
        category = _cause_category(key)
        if category in out:
            out[category] += n
    return out


def score_workspace(workspace: str | Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []
    for rel in ["hourly_metrics.csv", "deployments.csv", "incident_calendar.csv", "detection_rules.md"]:
        _add(checks, f"fixture_present_{rel}", (w / "in" / rel).is_file(), 0.015, f"missing {rel}")

    out = w / "out" / "anomalies.csv"
    summary_path = w / "out" / "attribution_summary.json"
    notes_path = w / "out" / "reconciliation_notes.md"
    _add(checks, "anomalies_exists", out.is_file(), 0.06, "missing anomalies.csv")
    if out.is_file():
        try:
            header, rows = _rows(out)
            _add(checks, "anomalies_header", header == ["anomaly_id", "timestamp", "service", "region", "metric", "observed", "expected", "z_score", "severity", "attributed_cause"], 0.06, f"got {header}")
            _add(checks, "anomaly_exact_set", _anomaly_rows_match(rows), 0.36, f"got {rows}")
            ids = {r.get("service") for r in rows}
            _add(checks, "low_volume_suppressed", "recommendations" not in ids, 0.08, "low-volume recommendations row should not be flagged")
            causes = {_norm_cause(r.get("attributed_cause")) for r in rows}
            _add(checks, "attribution_priority", "deployment:d-441" in causes and "deployment:d-443" in causes and "third_party:i-19" in causes, 0.08, "deployment/third-party attribution missing")
        except Exception as exc:
            _add(checks, "anomalies_parseable", False, 0.30, str(exc))
    else:
        for cid, weight in [("anomalies_header", 0.06), ("anomaly_exact_set", 0.36), ("low_volume_suppressed", 0.08), ("attribution_priority", 0.08)]:
            _add(checks, cid, False, weight, "missing anomalies.csv")

    _add(checks, "summary_exists", summary_path.is_file(), 0.06, "missing attribution_summary.json")
    if summary_path.is_file():
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            _add(checks, "summary_ids", [_norm_id(x) for x in data.get("anomaly_ids", [])] == ["A001", "A002", "A003", "A004", "A005", "A006"], 0.08, f"got {data.get('anomaly_ids')}")
            counts = _normalized_cause_counts(data.get("cause_counts"))
            _add(checks, "summary_counts", counts == {"deployment": 2, "third_party": 1, "marketing": 1, "unattributed": 2} and data.get("high_severity_count") == 3, 0.10, f"got {data}")
            _add(checks, "summary_impact", _close(data.get("total_revenue_impact_usd"), 2570.75), 0.08, f"got {data.get('total_revenue_impact_usd')}")
            _add(checks, "summary_unattributed", [_norm_id(x) for x in data.get("unattributed_anomaly_ids", [])] == ["A005", "A006"], 0.04, f"got {data.get('unattributed_anomaly_ids')}")
        except Exception as exc:
            _add(checks, "summary_parseable", False, 0.20, str(exc))
    else:
        for cid, weight in [("summary_ids", 0.08), ("summary_counts", 0.10), ("summary_impact", 0.08), ("summary_unattributed", 0.04)]:
            _add(checks, cid, False, weight, "missing summary")

    _add(checks, "notes_exists", notes_path.is_file(), 0.04, "missing notes")
    if notes_path.is_file():
        text = notes_path.read_text(encoding="utf-8", errors="replace").lower()
        _add(checks, "notes_caveats", all(term in text for term in ["low-volume", "overlap", "correlation", "causation"]), 0.08, "missing required caveats")
    else:
        _add(checks, "notes_caveats", False, 0.08, "missing notes")

    total = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total, 4)
    if any(c["id"] == "anomaly_exact_set" and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "090-timeseries-anomaly-attribution", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
