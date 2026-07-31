from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _check(cid: str, label: str, ok: bool, weight: float, detail: str = "") -> dict[str, Any]:
    return {"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": None if ok else detail}


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    actions = w / gt["actions_path"]
    messages = w / gt["messages_path"]
    checks: list[dict[str, Any]] = []
    checks.append(_check("actions_exists", "delay_actions.csv exists", actions.is_file(), 0.08))
    checks.append(_check("messages_exists", "customer_messages.md exists", messages.is_file(), 0.08))
    rows: list[dict[str, str]] = []
    if actions.exists():
        with actions.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    by_id = {r.get("shipment_id", ""): r for r in rows}
    checks.append(_check("all_shipments", "one row per shipment", set(by_id) == set(gt["expected"]), 0.14))
    status_ok = all(by_id.get(sid, {}).get("delay_status") == exp["delay_status"] for sid, exp in gt["expected"].items())
    checks.append(_check("delay_status", "delays identified correctly", status_ok, 0.18))
    tier_ok = all(by_id.get(sid, {}).get("customer_tier") == exp["customer_tier"] for sid, exp in gt["expected"].items())
    checks.append(_check("tiers", "customer tiers joined correctly", tier_ok, 0.12))
    action_ok = all(by_id.get(sid, {}).get("action") == exp["action"] for sid, exp in gt["expected"].items())
    checks.append(_check("actions", "actions match delay and tier policy", action_ok, 0.20))
    comp_ok = all(exp["compensation"] in by_id.get(sid, {}).get("compensation", "") for sid, exp in gt["expected"].items())
    checks.append(_check("compensation", "compensation or alternative recorded", comp_ok, 0.12))
    msg = messages.read_text(encoding="utf-8", errors="replace") if messages.exists() else ""
    msg_l = msg.lower()
    delayed_ids = [sid for sid, exp in gt["expected"].items() if exp["delay_status"] == "delayed"]
    msg_ok = all(sid in msg for sid in delayed_ids) and not any(term in msg_l for term in gt["forbidden_message_terms"])
    checks.append(_check("messages", "messages cover delayed shipments and avoid forbidden terms", msg_ok, 0.08))
    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    return {"task": "072-logistics-delay-response", "workspace": str(w), "outcome_score": score, "checks": checks}
