from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _source_unchanged(workspace: Path) -> bool:
    root = _TASK_DIR / "fixtures" / "in"
    src = workspace.resolve()
    if not src.is_dir():
        return True
    for original in root.rglob("*"):
        if not original.is_file():
            continue
        rel = original.relative_to(root)
        candidate = src / "in" / rel
        if candidate.is_file() and candidate.read_bytes() != original.read_bytes():
            return False
    return True


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _load_json(_GT)
    exp = gt["expected"]
    weights = gt["scoring"]["weights"]
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    plan_score = 0.0
    path = w / "out" / "capacity_plan.json"
    if path.is_file():
        try:
            data = _load_json(path)
            services = data.get("services", [])
            by_service = {str(item.get("service")): item for item in services if isinstance(item, dict)}
            checkout = by_service.get("checkout-api", data)
            pricing = by_service.get("pricing-worker", {})
            forecast = _num(checkout.get("forecast_peak_rps"))
            required = _num(checkout.get("required_capacity_rps"))
            provided = _num(checkout.get("provided_capacity_rps"))
            cost = _num(checkout.get("hourly_cost_usd"))
            headroom = _num(checkout.get("required_headroom_pct"))
            pricing_exp = exp["pricing_worker"]
            p_forecast = _num(pricing.get("forecast_peak_rps"))
            p_required = _num(pricing.get("required_capacity_rps"))
            p_cost = _num(pricing.get("hourly_cost_usd"))
            total_cost = _num(data.get("total_hourly_cost_usd"))
            plan_score = (
                0.08 * ("checkout-api" in by_service and "pricing-worker" in by_service)
                + 0.10 * (forecast is not None and exp["forecast_peak_rps_min"] <= forecast <= exp["forecast_peak_rps_max"])
                + 0.09 * (required is not None and exp["required_capacity_rps_min"] <= required <= exp["required_capacity_rps_max"])
                + 0.06 * (headroom is not None and abs(headroom - exp["required_headroom_pct"]) <= 0.01)
                + 0.10 * (str(checkout.get("selected_instance_type", "")).lower() == exp["selected_instance_type"])
                + 0.08 * (int(checkout.get("on_demand_count", checkout.get("instance_count", -1))) == exp["instance_count"])
                + 0.05 * (provided is not None and provided >= exp["provided_capacity_rps"])
                + 0.06 * (cost is not None and abs(cost - exp["hourly_cost_usd"]) <= 0.01)
                + 0.08 * (p_forecast is not None and pricing_exp["forecast_peak_rps_min"] <= p_forecast <= pricing_exp["forecast_peak_rps_max"])
                + 0.07 * (p_required is not None and pricing_exp["required_capacity_rps_min"] <= p_required <= pricing_exp["required_capacity_rps_max"])
                + 0.07 * (str(pricing.get("selected_instance_type", "")).lower() == pricing_exp["selected_instance_type"])
                + 0.06 * (int(pricing.get("on_demand_count", -1)) == pricing_exp["on_demand_count"] and int(pricing.get("spot_count", -1)) == pricing_exp["spot_count"])
                + 0.05 * (p_cost is not None and abs(p_cost - pricing_exp["hourly_cost_usd"]) <= 0.01)
                + 0.05 * (total_cost is not None and abs(total_cost - exp["total_hourly_cost_usd"]) <= 0.01)
            )
            add("capacity_plan", "capacity_plan.json has forecast, headroom, count, and cost", plan_score >= 0.70, weights["capacity_plan"], {"score": round(plan_score, 4)})
        except Exception as exc:
            add("capacity_plan_parse", "capacity_plan.json parseable", False, weights["capacity_plan"], str(exc))
    else:
        add("capacity_plan_missing", "capacity_plan.json exists", False, weights["capacity_plan"], "missing")

    notes_score = 0.0
    notes = w / "out" / "cost_notes.md"
    if notes.is_file():
        text = notes.read_text(encoding="utf-8", errors="replace").lower()
        hits = sum(1 for kw in gt["cost_keywords"] if kw.lower() in text)
        notes_score = hits / len(gt["cost_keywords"])
        add("cost_notes", "cost_notes.md explains calculation and alternatives", notes_score >= 0.70, weights["cost_notes"], {"score": round(notes_score, 4), "hits": hits})
    else:
        add("cost_notes_missing", "cost_notes.md exists", False, weights["cost_notes"], "missing")

    unchanged = _source_unchanged(w)
    risk_score = 0.0
    risk_path = w / gt["risk_tradeoffs_path"]
    if risk_path.is_file():
        try:
            with risk_path.open("r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            text = json.dumps(rows, ensure_ascii=False).lower()
            risk_score = sum(term in text for term in gt["risk_terms"]) / len(gt["risk_terms"])
            add("risk_tradeoffs", "risk_tradeoffs.csv covers budget, spot, compatibility, and SLO headroom", risk_score >= 0.75, weights["risk_tradeoffs"], {"score": risk_score})
        except Exception as exc:
            add("risk_tradeoffs_parse", "risk_tradeoffs.csv parseable", False, weights["risk_tradeoffs"], str(exc))
    else:
        add("risk_tradeoffs_missing", "risk_tradeoffs.csv exists", False, weights["risk_tradeoffs"], "missing")
    add("source_unchanged", "fixtures are not modified", unchanged, weights["source_unchanged"])
    total = plan_score * weights["capacity_plan"] + notes_score * weights["cost_notes"] + risk_score * weights["risk_tradeoffs"] + float(unchanged) * weights["source_unchanged"]
    thresholds = gt["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "065-capacity-planning", "workspace": str(w), "outcome_score": round(total, 4), "level": level, "checks": checks}
