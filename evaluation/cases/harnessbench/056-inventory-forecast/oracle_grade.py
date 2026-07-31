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
    _add(checks, "fixtures_present", (w / "in" / "sales_history.csv").is_file() and (w / "in" / "stock.json").is_file(), 0.08, "missing fixture")
    _add(checks, "fixtures_unchanged", _fixtures_unchanged(w, gt), 0.08, "one or more input files are missing or modified")
    out = w / gt["outputs"]["csv"]
    exceptions = w / gt["outputs"].get("exceptions", "out/inventory_exceptions.json")
    notes = w / gt["outputs"]["notes"]
    _add(checks, "csv_exists", out.is_file(), 0.08, "missing reorder_plan.csv")
    _add(checks, "exceptions_exists", exceptions.is_file(), 0.08, "missing inventory_exceptions.json")
    _add(checks, "notes_exists", notes.is_file(), 0.06, "missing forecast_notes.md")
    if out.is_file():
        try:
            header, rows = _read(out)
            rows_sorted = sorted(rows, key=lambda r: r.get("sku", ""))
            _add(checks, "exact_header", header == gt["header"], 0.08, f"got {header}")
            by_sku = {r.get("sku", ""): r for r in rows}
            _add(checks, "exact_rows", rows_sorted == gt["rows"], 0.30, f"got {rows_sorted}")
            decimals_ok = all(re.fullmatch(r"\d+\.\d{2}", r.get(col, "")) for r in rows for col in ["avg_weekly_sales", "forecast_14d"])
            ints_ok = all(re.fullmatch(r"\d+", r.get(col, "")) for r in rows for col in ["current_stock", "safety_stock", "reorder_qty"])
            _add(checks, "number_format", decimals_ok and ints_ok, 0.08, "incorrect numeric formatting")
            high = {r["sku"] for r in rows if r.get("risk_level") == "high"}
            all_risks = {r["sku"]: r.get("risk_level") for r in gt["rows"]}
            got_risks = {sku: by_sku.get(sku, {}).get("risk_level") for sku in all_risks}
            _add(checks, "risk_classification", got_risks == all_risks, 0.08, f"got risks {got_risks}")
            _add(checks, "includes_missing_history_sku", by_sku.get("SKU-E", {}).get("avg_weekly_sales") == "0.00" and by_sku.get("SKU-E", {}).get("forecast_14d") == "0.00", 0.04, "SKU-E no-history case missing")
            _add(checks, "boundary_equal_target_medium", by_sku.get("SKU-E", {}).get("risk_level") == "medium", 0.03, "equal target should be medium")
            _add(checks, "boundary_one_pack_above_medium", by_sku.get("SKU-F", {}).get("risk_level") == "medium", 0.03, "one pack above target should be medium")
            _add(checks, "boundary_more_than_one_pack_low", by_sku.get("SKU-G", {}).get("risk_level") == "low", 0.03, "more than one pack above target should be low")
            rounding_ok = all(by_sku.get(sku, {}).get("reorder_qty") == qty for sku, qty in gt["pack_rounding_cases"].items())
            _add(checks, "pack_rounding_exact", rounding_ok, 0.05, "pack-size rounding cases wrong")
            _add(checks, "fractional_average_format", by_sku.get("SKU-H", {}).get("avg_weekly_sales") == "13.50", 0.03, "fractional average should keep two decimals")
        except Exception as exc:
            _add(checks, "csv_readable", False, 0.30, str(exc))
    else:
        for cid, weight in [
            ("exact_header", 0.08),
            ("exact_rows", 0.30),
            ("number_format", 0.08),
            ("risk_classification", 0.08),
            ("includes_missing_history_sku", 0.04),
            ("boundary_equal_target_medium", 0.03),
            ("boundary_one_pack_above_medium", 0.03),
            ("boundary_more_than_one_pack_low", 0.03),
            ("pack_rounding_exact", 0.05),
            ("fractional_average_format", 0.03),
        ]:
            _add(checks, cid, False, weight, "missing reorder_plan.csv")
    if exceptions.is_file():
        try:
            data = json.loads(exceptions.read_text(encoding="utf-8"))
            keys = {"high_risk_skus", "missing_history_skus", "skipped_history_skus", "medium_boundary_skus", "more_than_one_pack_low_skus", "pack_rounding_cases"}
            _add(checks, "exceptions_keys", isinstance(data, dict) and set(data) == keys, 0.06, f"got keys {sorted(data) if isinstance(data, dict) else type(data)}")
            _add(checks, "exceptions_high_risk", data.get("high_risk_skus") == sorted(gt["high_risk_skus"]), 0.08, f"got {data.get('high_risk_skus')}")
            _add(checks, "exceptions_missing_and_skipped", data.get("missing_history_skus") == sorted(gt["missing_history_skus"]) and data.get("skipped_history_skus") == sorted(gt["skipped_history_skus"]), 0.08, "missing/skipped history audit mismatch")
            _add(checks, "exceptions_boundary_skus", data.get("medium_boundary_skus") == sorted(gt["medium_boundary_skus"]) and data.get("more_than_one_pack_low_skus") == sorted(gt["more_than_one_pack_low_skus"]), 0.08, "boundary SKU audit mismatch")
            _add(checks, "exceptions_pack_rounding", data.get("pack_rounding_cases") == gt["pack_rounding_cases"], 0.08, f"got {data.get('pack_rounding_cases')}")
        except Exception as exc:
            _add(checks, "exceptions_readable", False, 0.20, str(exc))
    else:
        for cid, weight in [
            ("exceptions_keys", 0.06),
            ("exceptions_high_risk", 0.08),
            ("exceptions_missing_and_skipped", 0.08),
            ("exceptions_boundary_skus", 0.08),
            ("exceptions_pack_rounding", 0.08),
        ]:
            _add(checks, cid, False, weight, "missing inventory_exceptions.json")
    if notes.is_file():
        text = notes.read_text(encoding="utf-8", errors="replace").lower()
        _add(checks, "notes_window", "14" in text or "two-week" in text or "two week" in text, 0.03, "forecast window missing")
        _add(checks, "notes_pack_rounding", "pack" in text and ("round" in text or "multiple" in text), 0.03, "pack-size rounding missing")
        _add(checks, "notes_high_risk_skus", all(sku.lower() in text for sku in gt["high_risk_skus"]), 0.06, "forecast notes should list all high-risk SKUs")
        _add(checks, "notes_missing_history", "sku-e" in text or "no sales history" in text or "missing history" in text, 0.03, "missing-history SKU should be noted")
        _add(checks, "notes_skipped_unknown_history", "sku-z" in text or "skipped" in text or "unknown sku" in text, 0.03, "unknown sales_history SKU should be noted as skipped")
    else:
        for cid, weight in [
            ("notes_window", 0.03),
            ("notes_pack_rounding", 0.03),
            ("notes_high_risk_skus", 0.06),
            ("notes_missing_history", 0.03),
            ("notes_skipped_unknown_history", 0.03),
        ]:
            _add(checks, cid, False, weight, "missing forecast_notes.md")
    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    if any(c["id"].startswith("exceptions_") and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "056-inventory-forecast", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
