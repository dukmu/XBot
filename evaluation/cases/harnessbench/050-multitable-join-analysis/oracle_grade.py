from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_DEFAULT_GT = _TASK_DIR / "ground_truth.json"


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixtures_unchanged(workspace: Path, gt: dict[str, Any]) -> bool:
    for rel, digest in gt.get("fixture_hashes", {}).items():
        candidate = workspace / "in" / rel
        if not candidate.is_file() or _sha256(candidate) != digest:
            return False
    return True


def score_workspace(workspace: str | Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((ground_truth_path or _DEFAULT_GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    _add(checks, "fixtures_unchanged", _fixtures_unchanged(w, gt), 0.06, "one or more input files are missing or modified")

    for name, expected_count in gt["fixture_counts"].items():
        path = w / "in" / f"{name}.csv"
        try:
            _, fixture_rows = _rows(path)
            _add(checks, f"fixture_{name}_unchanged", len(fixture_rows) == expected_count, 0.03, f"{name}.csv row count changed")
        except Exception as exc:
            _add(checks, f"fixture_{name}_unchanged", False, 0.03, str(exc))

    out = w / gt["output"]
    _add(checks, "output_exists", out.is_file(), 0.10, "missing out/customer_metrics.csv")
    if out.is_file():
        try:
            header, rows = _rows(out)
            _add(checks, "exact_header", header == gt["header"], 0.10, f"got {header}")
            by_id = {row.get("canonical_customer_id", ""): row for row in rows}
            _add(checks, "all_canonical_customers_once", set(by_id) == {r["canonical_customer_id"] for r in gt["rows"]} and len(rows) == len(gt["rows"]), 0.12, f"got ids {sorted(by_id)}")
            exact = rows == gt["rows"]
            _add(checks, "exact_metric_rows", exact, 0.26, f"got {rows}")
            _add(checks, "multi_alias_merge", by_id.get("C001", {}).get("order_count") == "4" and by_id.get("C001", {}).get("gross_revenue_usd") == "440.00" and by_id.get("C003", {}).get("order_count") == "2", 0.08, "C001/C003 aliases not fully merged")
            _add(checks, "refund_and_chargeback_deducted", by_id.get("C001", {}).get("refund_amount_usd") == "22.00" and by_id.get("C003", {}).get("chargeback_amount_usd") == "43.00", 0.08, "refund or chargeback not converted/deducted")
            _add(checks, "cancelled_order_excluded", by_id.get("C002", {}).get("order_count") == "0" and by_id.get("C002", {}).get("gross_revenue_usd") == "0.00", 0.06, "cancelled order appears included")
            _add(checks, "same_order_different_amount_additive", by_id.get("C004", {}).get("gross_revenue_usd") == "100.00", 0.04, "same-order different captured amounts must be additive")
            _add(checks, "platinum_boundary", by_id.get("C008", {}).get("segment") == "platinum" and by_id.get("C008", {}).get("net_revenue_usd") == "500.00", 0.04, "net=500 should be platinum")
            money_ok = all(re.fullmatch(r"\d+\.\d{2}", row.get(col, "")) for row in rows for col in ["gross_revenue_usd", "refund_amount_usd", "chargeback_amount_usd", "net_revenue_usd"])
            _add(checks, "money_format", money_ok, 0.05, "money fields must have two decimals")
        except Exception as exc:
            _add(checks, "csv_readable", False, 0.30, str(exc))
    else:
        _add(checks, "exact_header", False, 0.10, "missing out/customer_metrics.csv")
        _add(checks, "all_canonical_customers_once", False, 0.12, "missing out/customer_metrics.csv")
        _add(checks, "exact_metric_rows", False, 0.26, "missing out/customer_metrics.csv")
        _add(checks, "multi_alias_merge", False, 0.08, "missing out/customer_metrics.csv")
        _add(checks, "refund_and_chargeback_deducted", False, 0.08, "missing out/customer_metrics.csv")
        _add(checks, "cancelled_order_excluded", False, 0.06, "missing out/customer_metrics.csv")
        _add(checks, "same_order_different_amount_additive", False, 0.04, "missing out/customer_metrics.csv")
        _add(checks, "platinum_boundary", False, 0.04, "missing out/customer_metrics.csv")
        _add(checks, "money_format", False, 0.05, "missing out/customer_metrics.csv")

    summary_path = w / gt["region_summary"]
    _add(checks, "region_summary_exists", summary_path.is_file(), 0.04, "missing out/region_summary.json")
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary_ok = True
            summary_keys_ok = isinstance(summary, dict) and set(summary) == set(gt["region_summary_expected"])
            for region, exp in gt["region_summary_expected"].items():
                got = summary.get(region, {}) if isinstance(summary, dict) else {}
                summary_ok = summary_ok and int(got.get("canonical_customer_count", -1)) == exp["canonical_customer_count"]
                summary_ok = summary_ok and abs(float(got.get("gross_revenue_usd", -999)) - exp["gross_revenue_usd"]) <= 0.01
                summary_ok = summary_ok and abs(float(got.get("net_revenue_usd", -999)) - exp["net_revenue_usd"]) <= 0.01
            _add(checks, "region_summary_values", summary_ok and summary_keys_ok, 0.08, f"got {summary}")
        except Exception as exc:
            _add(checks, "region_summary_parse", False, 0.08, str(exc))
    else:
        _add(checks, "region_summary_values", False, 0.08, "missing out/region_summary.json")

    audit_path = w / gt.get("audit", "out/reconciliation_audit.json")
    _add(checks, "reconciliation_audit_exists", audit_path.is_file(), 0.08, "missing out/reconciliation_audit.json")
    if audit_path.is_file():
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            expected = gt.get("audit_expected", {})
            _add(checks, "reconciliation_audit_keys", isinstance(audit, dict) and set(audit) == set(expected), 0.04, f"got keys {sorted(audit) if isinstance(audit, dict) else type(audit)}")
            exact_lists = isinstance(audit, dict) and all(audit.get(key) == value for key, value in expected.items())
            _add(checks, "reconciliation_audit_exact", exact_lists, 0.22, f"got {audit}")
            _add(checks, "audit_chargeback_classification", isinstance(audit, dict) and audit.get("included_chargeback_ids") == ["CB7001", "CB7002"] and audit.get("excluded_chargeback_ids") == ["CB7003", "CB7004"], 0.05, "chargeback audit classification wrong")
            _add(checks, "audit_refund_anomaly_classification", isinstance(audit, dict) and audit.get("refund_anomaly_ids") == ["R9002", "R9003", "R9006"], 0.05, "refund anomaly audit classification wrong")
        except Exception as exc:
            _add(checks, "reconciliation_audit_parse", False, 0.22, str(exc))
    else:
        for cid, weight in [
            ("reconciliation_audit_keys", 0.04),
            ("reconciliation_audit_exact", 0.22),
            ("audit_chargeback_classification", 0.05),
            ("audit_refund_anomaly_classification", 0.05),
        ]:
            _add(checks, cid, False, weight, "missing out/reconciliation_audit.json")

    notes_path = w / gt["notes"]
    _add(checks, "notes_exists", notes_path.is_file(), 0.04, "missing out/reconciliation_notes.md")
    if notes_path.is_file():
        text = notes_path.read_text(encoding="utf-8", errors="replace").lower()
        groups = {
            "duplicate_payments": ["p5002-dup", "p5008-dup", "duplicate"],
            "orphan_payments": ["p5007", "p5013", "orphan"],
            "refund_anomalies": ["r9002", "r9003", "r9006", "cancelled", "no captured"],
            "chargebacks": ["cb7001", "cb7002", "cb7003", "cb7004", "chargeback"],
            "canonical_aliases": ["c006", "c007", "c010", "canonical"],
        }
        group_hits = {name: sum(term in text for term in terms) >= min(2, len(terms)) for name, terms in groups.items()}
        _add(checks, "notes_anomalies", all(group_hits.values()), 0.10, f"group hits {group_hits}")
    else:
        _add(checks, "notes_anomalies", False, 0.10, "missing out/reconciliation_notes.md")

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    if any(c["id"] == "exact_metric_rows" and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "050-multitable-join-analysis", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
