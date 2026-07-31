from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


EXPECTED_DIFF = [
    {"metric_name": "activation_rate", "old_value": "0.4000", "new_value": "0.4500", "absolute_diff": "0.0500", "relative_diff": "0.1250", "expected_direction": "increase", "classification": "expected_definition_change"},
    {"metric_name": "arr", "old_value": "100000.0000", "new_value": "92000.0000", "absolute_diff": "-8000.0000", "relative_diff": "-0.0800", "expected_direction": "decrease", "classification": "expected_definition_change"},
    {"metric_name": "gross_margin", "old_value": "0.5500", "new_value": "", "absolute_diff": "", "relative_diff": "", "expected_direction": "unknown", "classification": "requires_review"},
    {"metric_name": "retention_rate", "old_value": "0.7000", "new_value": "0.6900", "absolute_diff": "-0.0100", "relative_diff": "-0.0143", "expected_direction": "stable", "classification": "unexpected_regression"},
    {"metric_name": "support_sla", "old_value": "0.9500", "new_value": "0.9510", "absolute_diff": "0.0010", "relative_diff": "0.0011", "expected_direction": "stable", "classification": "no_material_change"},
]
EXPECTED_REGRESSIONS = [
    {"metric_name": "retention_rate", "bad_field": "old_cohort_users", "policy_clause": "retention must use new_cohort_users after migration", "severity": "high"},
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


def _direction_matches(actual: str, expected: str) -> bool:
    actual_n = _norm(actual)
    expected_n = _norm(expected)
    aliases = {
        "increase": ["increase", "increase possible", "up"],
        "decrease": ["decrease", "decrease or flat", "down"],
        "stable": ["stable", "flat", "unchanged", "unchanged definition", "no material change"],
        "unknown": ["unknown", "requires review", "requires comparison", "missing", "blank"],
    }
    return actual_n == expected_n or any(alias in actual_n for alias in aliases.get(expected_n, []))


def _values_match(row: dict[str, str], exp: dict[str, str]) -> bool:
    for key in ["old_value", "new_value", "absolute_diff", "relative_diff"]:
        if str(row.get(key, "")).strip() != exp[key]:
            return False
    return True


def score_workspace(workspace: str | Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []
    for rel in ["old_metric_definitions.md", "new_metric_definitions.md", "dashboard_before.csv", "dashboard_after.csv", "migration_policy.md"]:
        _add(checks, f"fixture_present_{rel}", (w / "in" / rel).is_file(), 0.01, f"missing {rel}")

    diff = w / "out" / "metric_migration_diff.csv"
    reg = w / "out" / "regression_ledger.csv"
    summary = w / "out" / "migration_summary.json"
    caveats = w / "out" / "caveats.md"
    _add(checks, "diff_exists", diff.is_file(), 0.06, "missing diff")
    if diff.is_file():
        try:
            header, rows = _rows(diff)
            _add(checks, "diff_header", header == ["metric_name", "old_value", "new_value", "absolute_diff", "relative_diff", "expected_direction", "classification"], 0.06, f"got {header}")
            by_metric = {row.get("metric_name", ""): row for row in rows}
            expected_metrics = {row["metric_name"] for row in EXPECTED_DIFF}
            coverage_ok = set(by_metric) == expected_metrics
            value_hits = 0
            class_hits = 0
            direction_hits = 0
            for exp in EXPECTED_DIFF:
                row = by_metric.get(exp["metric_name"], {})
                if _values_match(row, exp):
                    value_hits += 1
                if row.get("classification", "").strip() == exp["classification"]:
                    class_hits += 1
                if _direction_matches(row.get("expected_direction", ""), exp["expected_direction"]):
                    direction_hits += 1
            _add(checks, "diff_coverage", coverage_ok, 0.08, f"got {sorted(by_metric)}")
            _add(checks, "diff_values", value_hits == len(EXPECTED_DIFF), 0.16, f"hits {value_hits}/{len(EXPECTED_DIFF)} got {rows}")
            _add(checks, "diff_classifications", class_hits == len(EXPECTED_DIFF), 0.12, f"hits {class_hits}/{len(EXPECTED_DIFF)}")
            _add(checks, "diff_directions", direction_hits >= len(EXPECTED_DIFF) - 1, 0.04, f"hits {direction_hits}/{len(EXPECTED_DIFF)}")
            classes = {r.get("metric_name"): r.get("classification") for r in rows}
            _add(checks, "classification_distinguishes_expected_vs_regression", classes.get("arr") == "expected_definition_change" and classes.get("retention_rate") == "unexpected_regression", 0.08, f"got {classes}")
        except Exception as exc:
            _add(checks, "diff_parseable", False, 0.30, str(exc))
    else:
        for cid, weight in [("diff_header", 0.06), ("diff_coverage", 0.08), ("diff_values", 0.16), ("diff_classifications", 0.12), ("diff_directions", 0.04), ("classification_distinguishes_expected_vs_regression", 0.08)]:
            _add(checks, cid, False, weight, "missing")

    _add(checks, "regression_exists", reg.is_file(), 0.05, "missing regression ledger")
    if reg.is_file():
        try:
            header, rows = _rows(reg)
            _add(checks, "regression_header", header == ["metric_name", "bad_field", "policy_clause", "severity"], 0.05, f"got {header}")
            retention_rows = [row for row in rows if row.get("metric_name") == "retention_rate"]
            required_ok = any(
                "old_cohort_users" in row.get("bad_field", "")
                and row.get("severity", "").strip().lower() == "high"
                and "new_cohort_users" in row.get("policy_clause", "")
                for row in retention_rows
            )
            no_extra = len(rows) == len(retention_rows) == 1
            _add(checks, "regression_required", required_ok, 0.10, f"got {rows}")
            _add(checks, "regression_no_extra", no_extra, 0.04, f"got {rows}")
        except Exception as exc:
            _add(checks, "regression_parseable", False, 0.10, str(exc))
    else:
        _add(checks, "regression_header", False, 0.05, "missing")
        _add(checks, "regression_required", False, 0.10, "missing")
        _add(checks, "regression_no_extra", False, 0.04, "missing")

    _add(checks, "summary_exists", summary.is_file(), 0.05, "missing summary")
    if summary.is_file():
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
            expected = {
                "total_metrics": 5,
                "expected_definition_change_count": 2,
                "unexpected_regression_count": 1,
                "no_material_change_count": 1,
                "requires_review_count": 1,
                "largest_relative_diff_metric": "activation_rate",
            }
            _add(checks, "summary_exact", data == expected, 0.12, f"got {data}")
        except Exception as exc:
            _add(checks, "summary_parseable", False, 0.10, str(exc))
    else:
        _add(checks, "summary_exact", False, 0.12, "missing")

    _add(checks, "caveats_exists", caveats.is_file(), 0.03, "missing caveats")
    if caveats.is_file():
        text = caveats.read_text(encoding="utf-8", errors="replace").lower()
        _add(checks, "caveats_required", all(term in text for term in ["arr", "activation", "non-comparable", "retention", "unexpected regression"]), 0.06, "missing migration caveats")
    else:
        _add(checks, "caveats_required", False, 0.06, "missing")

    total = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total, 4)
    if any(c["id"] == "diff_coverage" and not c["pass"] for c in checks):
        score = min(score, 0.69)
    if any(c["id"] == "regression_required" and not c["pass"] for c in checks):
        score = min(score, 0.74)
    if any(c["id"] == "regression_no_extra" and not c["pass"] for c in checks):
        score = min(score, 0.78)
    return {"task": "094-metric-definition-migration-diff", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
