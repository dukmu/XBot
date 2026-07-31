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


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
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
    _add(checks, "fixtures_unchanged", _fixtures_unchanged(w, gt), 0.08, "one or more input files are missing or modified")
    out = w / gt["outputs"]["csv"]
    rollup = w / gt["outputs"].get("rollup", "out/department_rollup.csv")
    review_reasons = w / gt["outputs"].get("review_reasons", "out/review_reasons.json")
    summary = w / gt["outputs"]["summary"]
    _add(checks, "fixtures_present", (w / "in" / "budget.csv").is_file() and (w / "in" / "actuals.csv").is_file(), 0.08, "missing fixture")
    _add(checks, "csv_exists", out.is_file(), 0.08, "missing variance_report.csv")
    _add(checks, "rollup_exists", rollup.is_file(), 0.08, "missing department_rollup.csv")
    _add(checks, "review_reasons_exists", review_reasons.is_file(), 0.08, "missing review_reasons.json")
    _add(checks, "summary_exists", summary.is_file(), 0.06, "missing summary.md")
    if out.is_file():
        try:
            header, rows = _read(out)
            rows_sorted = sorted(rows, key=lambda r: (r.get("department", ""), r.get("category", "")))
            _add(checks, "exact_header", header == gt["header"], 0.08, f"got {header}")
            by_key = {r.get("department", "") + " " + r.get("category", ""): r for r in rows}
            _add(checks, "exact_rows", rows_sorted == gt["rows"], 0.34, f"got {rows_sorted}")
            money_ok = all(re.fullmatch(r"-?\d+\.\d{2}", r.get(col, "")) for r in rows for col in ["budget_amount", "actual_amount", "variance_amount"])
            pct_ok = all((r.get("variance_pct") == "N/A") or re.fullmatch(r"-?\d+\.\d{2}", r.get("variance_pct", "")) for r in rows)
            _add(checks, "number_format", money_ok, 0.08, "amounts and percentages need two decimals")
            _add(checks, "review_flags", {r["department"] + " " + r["category"] for r in rows if r.get("flag") == "review"} == set(gt["review_items"]), 0.10, "incorrect review flags")
            _add(checks, "pct_format_or_na", pct_ok, 0.04, "variance_pct must be two decimals or N/A")
            _add(checks, "full_outer_join_rows", {"Product Research", "Security Tools"} <= set(by_key), 0.04, "unplanned or missing-actual rows absent")
            _add(checks, "zero_budget_pct", by_key.get("Product Research", {}).get("variance_pct") == "N/A" and by_key.get("Support Escalations", {}).get("variance_pct") == "N/A", 0.04, "zero-budget rows should use N/A")
            _add(checks, "missing_actual_defaults_zero", by_key.get("Security Tools", {}).get("actual_amount") == "0.00", 0.04, "missing actual should default to 0.00")
            _add(checks, "strict_threshold", by_key.get("Sales Travel", {}).get("variance_pct") == "10.00" and by_key.get("Sales Travel", {}).get("flag") == "ok", 0.04, "exact 10.00 variance should be ok")
        except Exception as exc:
            _add(checks, "csv_readable", False, 0.30, str(exc))
    else:
        for cid, weight in [
            ("exact_header", 0.08),
            ("exact_rows", 0.34),
            ("number_format", 0.08),
            ("review_flags", 0.10),
            ("pct_format_or_na", 0.04),
            ("full_outer_join_rows", 0.04),
            ("zero_budget_pct", 0.04),
            ("missing_actual_defaults_zero", 0.04),
            ("strict_threshold", 0.04),
        ]:
            _add(checks, cid, False, weight, "missing variance_report.csv")
    if rollup.is_file():
        try:
            header, rows = _read(rollup)
            rows_sorted = sorted(rows, key=lambda r: r.get("department", ""))
            _add(checks, "rollup_header", header == gt["rollup_header"], 0.06, f"got {header}")
            _add(checks, "rollup_rows", rows_sorted == gt["rollup_rows"], 0.22, f"got {rows_sorted}")
            by_department = {r.get("department", ""): r for r in rows}
            _add(checks, "rollup_review_counts", by_department.get("Marketing", {}).get("review_item_count") == "2" and by_department.get("Sales", {}).get("review_item_count") == "0", 0.04, "department review counts are wrong")
            _add(checks, "rollup_zero_budget_na", by_department.get("Product", {}).get("variance_pct") == "N/A" and by_department.get("Support", {}).get("variance_pct") == "N/A", 0.04, "zero-budget department rollups should use N/A")
            _add(checks, "rollup_flag_from_review_items", by_department.get("Marketing", {}).get("rollup_flag") == "review" and by_department.get("Sales", {}).get("rollup_flag") == "ok", 0.04, "rollup_flag should follow review_item_count")
        except Exception as exc:
            _add(checks, "rollup_readable", False, 0.22, str(exc))
    else:
        for cid, weight in [
            ("rollup_header", 0.06),
            ("rollup_rows", 0.22),
            ("rollup_review_counts", 0.04),
            ("rollup_zero_budget_na", 0.04),
            ("rollup_flag_from_review_items", 0.04),
        ]:
            _add(checks, cid, False, weight, "missing department_rollup.csv")
    if review_reasons.is_file():
        try:
            data = json.loads(review_reasons.read_text(encoding="utf-8"))
            expected = gt["review_reasons"]
            keys_ok = isinstance(data, dict) and set(data) == set(expected)
            types_ok = keys_ok and all(isinstance(data.get(item), dict) and data[item].get("reason_type") == exp["reason_type"] for item, exp in expected.items())
            drivers_ok = keys_ok and all(str(data.get(item, {}).get("primary_driver", "")).strip() for item in expected)
            _add(checks, "review_reasons_keys", keys_ok, 0.06, f"got keys {sorted(data) if isinstance(data, dict) else type(data)}")
            _add(checks, "review_reason_types", types_ok, 0.18, f"got {data}")
            _add(checks, "review_reason_primary_drivers", drivers_ok, 0.06, "primary_driver missing")
        except Exception as exc:
            _add(checks, "review_reasons_parseable", False, 0.18, str(exc))
    else:
        for cid, weight in [
            ("review_reasons_keys", 0.06),
            ("review_reason_types", 0.18),
            ("review_reason_primary_drivers", 0.06),
        ]:
            _add(checks, cid, False, weight, "missing review_reasons.json")
    if summary.is_file():
        text = summary.read_text(encoding="utf-8", errors="replace")
        text_l = text.lower()
        for item in gt["review_items"]:
            parts = item.lower().split()
            if item == "R&D Tools":
                ok = ("r&d" in text_l or "research and development" in text_l or "r and d" in text_l) and "tools" in text_l
            else:
                ok = all(part in text_l for part in parts)
            _add(checks, f"summary_mentions_{item}", ok, 0.015, f"missing {item}")
        _add(checks, "summary_largest_overrun", all(part in text_l for part in gt["largest_overrun"].lower().split()), 0.04, "largest overrun not identified")
        _add(checks, "summary_mentions_unplanned", "unplanned" in text_l and "product" in text_l and "research" in text_l, 0.015, "summary should mention Product Research unplanned actual")
        _add(checks, "summary_mentions_zero_budget", ("zero budget" in text_l or "zero-budget" in text_l) and ("support" in text_l or "product" in text_l), 0.015, "summary should mention zero-budget rows")
        _add(checks, "summary_mentions_missing_actual", "missing actual" in text_l and "security" in text_l and "tools" in text_l, 0.015, "summary should mention Security Tools missing actual")
    else:
        for item in gt["review_items"]:
            _add(checks, f"summary_mentions_{item}", False, 0.015, "missing summary.md")
        for cid, weight in [
            ("summary_largest_overrun", 0.04),
            ("summary_mentions_unplanned", 0.015),
            ("summary_mentions_zero_budget", 0.015),
            ("summary_mentions_missing_actual", 0.015),
        ]:
            _add(checks, cid, False, weight, "missing summary.md")
    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    if any(c["id"].startswith("review_reason") and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "054-budget-variance-analysis", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
