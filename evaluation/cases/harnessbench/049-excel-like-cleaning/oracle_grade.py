from __future__ import annotations

import csv
import json
import hashlib
import re
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_DEFAULT_GT = _TASK_DIR / "ground_truth.json"


def _check(checks: list[dict[str, Any]], cid: str, passed: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(passed), "weight": weight, "detail": None if passed else detail})


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def _fixtures_unchanged(workspace: Path) -> bool:
    fixture_dir = _TASK_DIR / "fixtures" / "in"
    for original in fixture_dir.iterdir():
        candidate = workspace / "in" / original.name
        if not candidate.is_file():
            return False
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != hashlib.sha256(original.read_bytes()).hexdigest():
            return False
    return True


def _has_report_terms(text: str, terms: list[str]) -> bool:
    aliases = {
        "valid row count": ("valid row count", "valid rows"),
        "rejected row count": ("rejected row count", "rejected rows"),
        "duplicate": ("duplicate", "duplicates", "duplicate rows"),
        "invalid_date": ("invalid_date", "invalid date", "invalid dates"),
        "unsupported_currency": ("unsupported_currency", "unsupported currency", "unsupported currencies"),
    }
    for term in terms:
        options = aliases.get(term, (term,))
        if not any(option in text for option in options):
            return False
    return True


def score_workspace(workspace: str | Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((ground_truth_path or _DEFAULT_GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    _check(checks, "fixtures_unchanged", _fixtures_unchanged(w), 0.08, "one or more input files are missing or modified")
    for name, expected_count in gt["input_counts"].items():
        fixture = w / "in" / f"{name}.csv"
        try:
            _, fixture_rows = _read_csv(fixture)
            _check(checks, f"fixture_{name}_row_count", len(fixture_rows) == expected_count, 0.02, f"{name}.csv row count changed")
        except Exception as exc:
            _check(checks, f"fixture_{name}_row_count", False, 0.02, str(exc))

    out_csv = w / gt["outputs"]["cleaned_csv"]
    report = w / gt["outputs"]["report"]
    _check(checks, "cleaned_csv_exists", out_csv.is_file(), 0.08, "missing out/cleaned_sales.csv")
    reject_path = w / gt["outputs"]["reject_ledger"]
    reject_summary_path = w / gt["outputs"].get("reject_summary", "out/reject_summary.json")
    _check(checks, "report_exists", report.is_file(), 0.04, "missing out/cleaning_report.md")
    _check(checks, "reject_ledger_exists", reject_path.is_file(), 0.06, "missing out/reject_ledger.csv")
    _check(checks, "reject_summary_exists", reject_summary_path.is_file(), 0.06, "missing out/reject_summary.json")

    rows: list[dict[str, str]] = []
    if out_csv.is_file():
        try:
            header, rows = _read_csv(out_csv)
            _check(checks, "exact_header", header == gt["cleaned_header"], 0.08, f"got {header}")
            _check(checks, "exact_cleaned_rows", rows == gt["cleaned_rows"], 0.30, f"got {rows}")
            _check(checks, "iso_dates", all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.get("order_date", "")) for r in rows), 0.05, "dates must be YYYY-MM-DD")
            _check(checks, "amount_decimals", all(re.fullmatch(r"-?\d+\.\d{2}", r.get("amount_usd", "")) for r in rows), 0.05, "amounts must have two decimals")
            _check(checks, "locale_fx_effects", any(r.get("amount_usd") == "21.45" for r in rows) and any(r.get("amount_usd") == "-19.99" for r in rows), 0.08, "locale, FX, or refund handling missing")
            by_order = {r.get("order_id", ""): r for r in rows}
            _check(checks, "de_de_decimal_amount", by_order.get("S1020", {}).get("amount_usd") == "1358.02", 0.06, "de_DE decimal/thousands parsing is wrong")
            _check(checks, "us_thousands_amount", by_order.get("S1021", {}).get("amount_usd") == "1234.56", 0.04, "en_US thousands parsing is wrong")
            _check(checks, "refund_sign_once", by_order.get("S1022", {}).get("amount_usd") == "-11.00" and by_order.get("S1023", {}).get("amount_usd") == "-11.00", 0.06, "returned/refunded sign handling is wrong")
            clean_ids = {r.get("order_id") for r in rows}
            _check(checks, "no_duplicate_clean_rows", len(clean_ids) == len(rows), 0.05, "duplicate order_id in cleaned rows")
        except Exception as exc:
            _check(checks, "csv_readable", False, 0.10, str(exc))
    else:
        _check(checks, "de_de_decimal_amount", False, 0.06, "missing out/cleaned_sales.csv")
        _check(checks, "us_thousands_amount", False, 0.04, "missing out/cleaned_sales.csv")
        _check(checks, "refund_sign_once", False, 0.06, "missing out/cleaned_sales.csv")

    if reject_path.is_file():
        try:
            reject_header, reject_rows = _read_csv(reject_path)
            _check(checks, "reject_header", reject_header == gt["reject_header"], 0.06, f"got {reject_header}")
            expected_rejects = sorted(gt["reject_rows"], key=lambda r: int(r["source_row"]))
            got_slim = sorted(
                [{"order_id": r.get("order_id", ""), "reason": r.get("reason", ""), "source_row": r.get("source_row", "")} for r in reject_rows],
                key=lambda r: int(r.get("source_row") or 0),
            )
            notes_ok = all(r.get("notes", "").strip() for r in reject_rows)
            _check(checks, "reject_reasons", got_slim == expected_rejects and notes_ok, 0.14, f"got {got_slim}")
        except Exception as exc:
            _check(checks, "reject_readable", False, 0.10, str(exc))
    else:
        _check(checks, "reject_header", False, 0.06, "missing out/reject_ledger.csv")
        _check(checks, "reject_reasons", False, 0.14, "missing out/reject_ledger.csv")

    if reject_summary_path.is_file():
        try:
            summary = json.loads(reject_summary_path.read_text(encoding="utf-8"))
            expected = gt.get("reject_summary_expected", {})
            summary_ok = isinstance(summary, dict) and set(summary) == set(expected)
            for reason, exp in expected.items():
                got = summary.get(reason, {}) if isinstance(summary, dict) else {}
                summary_ok = summary_ok and int(got.get("count", -1)) == int(exp["count"])
                summary_ok = summary_ok and [int(x) for x in got.get("source_rows", [])] == exp["source_rows"]
                summary_ok = summary_ok and list(got.get("order_ids", [])) == exp["order_ids"]
            _check(checks, "reject_summary_exact", summary_ok, 0.12, f"got {summary}")
        except Exception as exc:
            _check(checks, "reject_summary_exact", False, 0.12, str(exc))
    else:
        _check(checks, "reject_summary_exact", False, 0.12, "missing out/reject_summary.json")

    if report.is_file():
        text = report.read_text(encoding="utf-8", errors="replace").lower()
        reject_categories = {row["reason"] for row in gt["reject_rows"]}
        _check(checks, "report_reject_categories", all(reason.lower() in text for reason in reject_categories), 0.18, "not all reject categories are explained")
        _check(checks, "report_terms", _has_report_terms(text, gt["required_report_terms"]), 0.07, "missing required summary terms")
    else:
        _check(checks, "report_reject_categories", False, 0.18, "missing out/cleaning_report.md")
        _check(checks, "report_terms", False, 0.07, "missing out/cleaning_report.md")

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    if any(c["id"] == "exact_cleaned_rows" and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "049-excel-like-cleaning", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
