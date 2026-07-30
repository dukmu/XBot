from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_DEFAULT_GT = _TASK_DIR / "ground_truth.json"


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _num(value: Any) -> float | None:
    try:
        if isinstance(value, str) and not re.fullmatch(r"-?\d+(?:\.\d{1,2})?", value.strip()):
            return None
        if isinstance(value, float) and round(value, 2) != value:
            return None
        return round(float(value), 2)
    except Exception:
        return None


def _int_like(value: Any) -> int | None:
    try:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
            return int(value)
        return None
    except Exception:
        return None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixtures_unchanged(workspace: Path, gt: dict[str, Any]) -> bool:
    for rel, digest in gt.get("fixture_hashes", {}).items():
        candidate = workspace / "in" / rel
        if not candidate.is_file() or _sha256(candidate) != digest:
            return False
    return True


def _normalize_rows(rows: list[dict[str, Any]], key_order: list[str]) -> list[dict[str, Any]]:
    normalized = []
    for row in rows:
        item: dict[str, Any] = {}
        for key in key_order:
            value = row.get(key)
            if key == "revenue":
                item[key] = _num(value)
            elif key in {"order_count", "units_sold"}:
                item[key] = _int_like(value)
            else:
                item[key] = value
        normalized.append(item)
    return normalized


def _expected_from_sql(sql_path: Path) -> dict[str, list[dict[str, Any]]]:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(sql_path.read_text(encoding="utf-8"))
    base = "WHERE o.status='paid' AND o.order_date BETWEEN '2025-01-01' AND '2025-03-31'"
    top = con.execute(
        "SELECT p.product_id, p.product_name, ROUND(SUM(oi.quantity*oi.unit_price),2) AS revenue "
        "FROM orders o JOIN order_items oi ON o.order_id=oi.order_id JOIN products p ON p.product_id=oi.product_id "
        f"{base} GROUP BY p.product_id, p.product_name ORDER BY revenue DESC, p.product_id LIMIT 3"
    ).fetchall()
    regions = con.execute(
        "SELECT c.region, COUNT(DISTINCT o.order_id) AS order_count, ROUND(SUM(oi.quantity*oi.unit_price),2) AS revenue "
        "FROM orders o JOIN customers c ON c.customer_id=o.customer_id JOIN order_items oi ON o.order_id=oi.order_id "
        f"{base} GROUP BY c.region ORDER BY c.region"
    ).fetchall()
    cats = con.execute(
        "SELECT p.category, SUM(oi.quantity) AS units_sold, ROUND(SUM(oi.quantity*oi.unit_price),2) AS revenue "
        "FROM orders o JOIN order_items oi ON o.order_id=oi.order_id JOIN products p ON p.product_id=oi.product_id "
        f"{base} GROUP BY p.category ORDER BY p.category"
    ).fetchall()
    return {
        "top_products_by_revenue": [dict(r) for r in top],
        "revenue_by_region": [dict(r) for r in regions],
        "category_summary": [dict(r) for r in cats],
    }


def score_workspace(workspace: str | Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((ground_truth_path or _DEFAULT_GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    sql_path = w / "in" / "schema_data.sql"
    expected_sql_path = _TASK_DIR / "fixtures" / "in" / "schema_data.sql"
    expected = _expected_from_sql(expected_sql_path) if expected_sql_path.is_file() else gt["query_results"]
    _add(checks, "fixture_sql_present", sql_path.is_file(), 0.06, "missing schema_data.sql")
    _add(checks, "fixtures_unchanged", _fixtures_unchanged(w, gt), 0.08, "schema_data.sql is missing or modified")

    out_json = w / gt["outputs"]["json"]
    audit_json = w / gt["outputs"].get("audit", "out/query_audit.json")
    analysis = w / gt["outputs"]["analysis"]
    _add(checks, "json_exists", out_json.is_file(), 0.08, "missing out/query_results.json")
    _add(checks, "query_audit_exists", audit_json.is_file(), 0.08, "missing out/query_audit.json")
    _add(checks, "analysis_exists", analysis.is_file(), 0.06, "missing out/analysis.md")

    if out_json.is_file():
        try:
            data = json.loads(out_json.read_text(encoding="utf-8"))
            _add(checks, "top_level_keys", set(data) == set(expected), 0.08, f"got {sorted(data)}")
            top = _normalize_rows(data.get("top_products_by_revenue", []), ["product_id", "product_name", "revenue"])
            regions = _normalize_rows(data.get("revenue_by_region", []), ["region", "order_count", "revenue"])
            cats = _normalize_rows(data.get("category_summary", []), ["category", "units_sold", "revenue"])
            exp_top = _normalize_rows(expected["top_products_by_revenue"], ["product_id", "product_name", "revenue"])
            exp_regions = _normalize_rows(expected["revenue_by_region"], ["region", "order_count", "revenue"])
            exp_cats = _normalize_rows(expected["category_summary"], ["category", "units_sold", "revenue"])
            _add(checks, "top_products_exact", top == exp_top, 0.22, f"got {top}")
            _add(checks, "regions_exact", regions == exp_regions, 0.16, f"got {regions}")
            _add(checks, "categories_exact", cats == exp_cats, 0.14, f"got {cats}")
            _add(checks, "tie_break_top_products", len(top) >= 2 and top[0].get("product_id") == "P1" and top[1].get("product_id") == "P2" and top[0].get("revenue") == top[1].get("revenue") == 1140.0, 0.06, "P1/P2 tie must be ordered by product_id")
            _add(checks, "date_boundaries_included", any(r.get("region") == "East" and r.get("revenue") == 900.0 for r in regions) and any(r.get("region") == "North" and r.get("revenue") == 1590.0 for r in regions), 0.05, "inclusive boundary orders missing")
            _add(checks, "returned_and_outside_excluded", all(r.get("revenue", 0) < 4000 for r in top) and any(r.get("category") == "Hardware" and r.get("revenue") == 2880.0 for r in cats), 0.05, "returned or out-of-window lure rows appear included")
        except Exception as exc:
            _add(checks, "json_parseable", False, 0.30, str(exc))
    else:
        _add(checks, "top_level_keys", False, 0.08, "missing out/query_results.json")
        _add(checks, "top_products_exact", False, 0.22, "missing out/query_results.json")
        _add(checks, "regions_exact", False, 0.16, "missing out/query_results.json")
        _add(checks, "categories_exact", False, 0.14, "missing out/query_results.json")
        _add(checks, "tie_break_top_products", False, 0.06, "missing out/query_results.json")
        _add(checks, "date_boundaries_included", False, 0.05, "missing out/query_results.json")
        _add(checks, "returned_and_outside_excluded", False, 0.05, "missing out/query_results.json")

    if analysis.is_file():
        text = analysis.read_text(encoding="utf-8", errors="replace")
        text_l = text.lower()
        _add(checks, "analysis_top_tie", ("atlas laptop" in text_l or "p1" in text_l) and ("nova monitor" in text_l or "p2" in text_l) and "tie" in text_l, 0.06, "analysis should describe the P1/P2 tie")
        _add(checks, "analysis_highest_region", "north" in text_l, 0.03, "analysis should identify North as highest region")
        exclusion_terms = ["paid", "returned", "cancelled"]
        _add(checks, "analysis_exclusion_rule", all(term in text_l for term in exclusion_terms) and ("out-of-window" in text_l or "outside" in text_l), 0.06, "analysis should explain paid-only, returned/cancelled, and out-of-window exclusions")
        _add(checks, "analysis_inclusive_boundaries", "2025-01-01" in text_l and "2025-03-31" in text_l, 0.03, "analysis should mention inclusive date boundaries")
    else:
        _add(checks, "analysis_top_tie", False, 0.06, "missing out/analysis.md")
        _add(checks, "analysis_highest_region", False, 0.03, "missing out/analysis.md")
        _add(checks, "analysis_exclusion_rule", False, 0.06, "missing out/analysis.md")
        _add(checks, "analysis_inclusive_boundaries", False, 0.03, "missing out/analysis.md")

    if audit_json.is_file():
        try:
            audit = json.loads(audit_json.read_text(encoding="utf-8"))
            expected_audit = gt["query_audit"]
            _add(checks, "query_audit_keys", isinstance(audit, dict) and set(audit) == set(expected_audit), 0.04, f"got keys {sorted(audit) if isinstance(audit, dict) else type(audit)}")
            _add(checks, "included_order_audit", audit.get("included_order_ids") == expected_audit["included_order_ids"], 0.08, f"got {audit.get('included_order_ids')}")
            _add(checks, "excluded_status_audit", audit.get("excluded_status_order_ids") == expected_audit["excluded_status_order_ids"], 0.06, f"got {audit.get('excluded_status_order_ids')}")
            _add(checks, "excluded_window_audit", audit.get("excluded_out_of_window_order_ids") == expected_audit["excluded_out_of_window_order_ids"], 0.06, f"got {audit.get('excluded_out_of_window_order_ids')}")
            _add(checks, "boundary_and_tie_audit", audit.get("boundary_included_order_ids") == expected_audit["boundary_included_order_ids"] and audit.get("top_product_tie_order") == expected_audit["top_product_tie_order"], 0.08, f"got {audit}")
        except Exception as exc:
            _add(checks, "query_audit_parseable", False, 0.20, str(exc))
    else:
        for cid, weight in [
            ("query_audit_keys", 0.04),
            ("included_order_audit", 0.08),
            ("excluded_status_audit", 0.06),
            ("excluded_window_audit", 0.06),
            ("boundary_and_tie_audit", 0.08),
        ]:
            _add(checks, cid, False, weight, "missing out/query_audit.json")

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    if any(c["id"] in {"top_products_exact", "regions_exact", "categories_exact"} and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "051-sql-query-report", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
