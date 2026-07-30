from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), [{k: (v or "").strip() for k, v in row.items()} for row in r]


def _norm_float(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except Exception:
        return None


def _close(got: Any, exp: float, tol: float = 0.0001) -> bool:
    try:
        return abs(float(got) - exp) <= tol
    except Exception:
        return False


def _two_prop_stats(a_users: int, a_conv: int, b_users: int, b_conv: int) -> dict[str, float]:
    pa = a_conv / a_users
    pb = b_conv / b_users
    pooled = (a_conv + b_conv) / (a_users + b_users)
    se = math.sqrt(pooled * (1 - pooled) * (1 / a_users + 1 / b_users))
    z = (pb - pa) / se
    # two-sided p-value from the normal CDF, without scipy.
    p = math.erfc(abs(z) / math.sqrt(2))
    return {
        "lift_absolute": round(pb - pa, 4),
        "lift_relative": round((pb - pa) / pa, 4),
        "z_stat": round(z, 4),
        "p_value": round(p, 4),
    }


def _variant_numbers(rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        v = row.get("variant", "")
        try:
            out[v] = {
                "eligible_users": int(row.get("eligible_users", "")),
                "conversions": int(row.get("conversions", "")),
                "conversion_rate": float(row.get("conversion_rate", "")),
                "refund_rate": float(row.get("refund_rate", "")),
                "revenue_per_eligible_user": float(row.get("revenue_per_eligible_user", "")),
            }
        except Exception:
            out[v] = {}
    return out


def _launch_recommendation_ok(value: Any) -> bool:
    text = str(value or "").lower().replace("_", " ")
    return any(term in text for term in ("launch", "ship", "rollout"))


def score_workspace(workspace: str | Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []
    for rel in ["experiment_cells.csv", "exclusions.csv", "analysis_plan.md"]:
        _add(checks, f"fixture_present_{rel}", (w / "in" / rel).is_file(), 0.02, f"missing in/{rel}")

    out_csv = w / "out" / "variant_metrics.csv"
    out_json = w / "out" / "ab_summary.json"
    ledger = w / "out" / "exclusion_ledger.csv"
    rec = w / "out" / "recommendation.md"
    _add(checks, "variant_metrics_exists", out_csv.is_file(), 0.06, "missing variant_metrics.csv")
    if out_csv.is_file():
        try:
            header, rows = _rows(out_csv)
            _add(checks, "variant_header", header == ["variant", "eligible_users", "conversions", "conversion_rate", "refund_rate", "revenue_per_eligible_user"], 0.06, f"got {header}")
            by_v = {r.get("variant", ""): r for r in rows}
            _add(checks, "variants_once", set(by_v) == {"A", "B"} and len(rows) == 2, 0.06, f"got variants {sorted(by_v)}")
            nums = _variant_numbers(rows)
            raw_counts_ok = nums.get("A", {}).get("eligible_users") == 1200 and nums.get("A", {}).get("conversions") == 96 and nums.get("B", {}).get("eligible_users") == 1180 and nums.get("B", {}).get("conversions") == 132
            internally_consistent = True
            refunds = {"A": 6, "B": 10}
            revenue = {"A": 14400.00, "B": 20592.00}
            for v in ("A", "B"):
                n = nums.get(v, {})
                users, conv = n.get("eligible_users"), n.get("conversions")
                internally_consistent = internally_consistent and isinstance(users, int) and isinstance(conv, int) and users > 0 and conv > 0
                internally_consistent = internally_consistent and _close(n.get("conversion_rate"), conv / users)
                internally_consistent = internally_consistent and _close(n.get("refund_rate"), refunds[v] / conv)
                if raw_counts_ok:
                    internally_consistent = internally_consistent and _close(n.get("revenue_per_eligible_user"), revenue[v] / users, 0.01)
            _add(checks, "variant_counts", raw_counts_ok, 0.06, f"got {rows}")
            _add(checks, "variant_calculations", internally_consistent, 0.18, f"got {rows}")
        except Exception as exc:
            _add(checks, "variant_parseable", False, 0.30, str(exc))
    else:
        _add(checks, "variant_header", False, 0.06, "missing")
        _add(checks, "variants_once", False, 0.06, "missing")
        _add(checks, "variant_values", False, 0.24, "missing")

    _add(checks, "summary_exists", out_json.is_file(), 0.06, "missing ab_summary.json")
    if out_json.is_file():
        try:
            data = json.loads(out_json.read_text(encoding="utf-8"))
            _add(checks, "summary_variants_alpha", data.get("control_variant") == "A" and data.get("treatment_variant") == "B" and _close(data.get("alpha"), 0.05), 0.06, f"got {data}")
            variant_rows = []
            if out_csv.is_file():
                _, variant_rows = _rows(out_csv)
            nums = _variant_numbers(variant_rows)
            if {"A", "B"} <= set(nums):
                expected = _two_prop_stats(nums["A"]["eligible_users"], nums["A"]["conversions"], nums["B"]["eligible_users"], nums["B"]["conversions"])
            else:
                expected = _two_prop_stats(1200, 96, 1180, 132)
            nums_ok = all(_close(data.get(k), v, 0.0006) for k, v in expected.items())
            _add(checks, "summary_stats", nums_ok, 0.22, f"got {data}")
            _add(checks, "summary_significant_launch", data.get("significant") is True and _launch_recommendation_ok(data.get("recommendation")), 0.08, f"got {data.get('recommendation')}")
        except Exception as exc:
            _add(checks, "summary_parseable", False, 0.30, str(exc))
    else:
        for cid, weight in [("summary_variants_alpha", 0.06), ("summary_stats", 0.22), ("summary_significant_launch", 0.08)]:
            _add(checks, cid, False, weight, "missing ab_summary.json")

    _add(checks, "ledger_exists", ledger.is_file(), 0.04, "missing exclusion_ledger.csv")
    if ledger.is_file():
        try:
            header, rows = _rows(ledger)
            _, exp_rows = _rows(w / "in" / "exclusions.csv")
            _add(checks, "ledger_header", header == ["user_id", "reason", "variant", "notes"], 0.04, f"got {header}")
            _add(checks, "ledger_exact", rows == exp_rows, 0.14, f"got {rows}")
        except Exception as exc:
            _add(checks, "ledger_parseable", False, 0.12, str(exc))
    else:
        _add(checks, "ledger_header", False, 0.04, "missing")
        _add(checks, "ledger_exact", False, 0.14, "missing")

    _add(checks, "recommendation_exists", rec.is_file(), 0.04, "missing recommendation.md")
    if rec.is_file():
        text = rec.read_text(encoding="utf-8", errors="replace").lower()
        _add(checks, "recommendation_mentions_stats", all(term in text for term in ["significant", "p", "launch"]), 0.05, "must mention significance, p-value, and launch")
        _add(checks, "recommendation_caveats", all(term in text for term in ["mobile", "underpowered", "duplicate"]), 0.05, "must mention mobile underpowered and duplicate cleanup")
    else:
        _add(checks, "recommendation_mentions_stats", False, 0.05, "missing")
        _add(checks, "recommendation_caveats", False, 0.05, "missing")

    total = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total, 4)
    if any(c["id"] in {"variant_calculations", "summary_stats"} and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "089-ab-test-caveat-analysis", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
