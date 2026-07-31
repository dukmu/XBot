from __future__ import annotations

import json
import re
import csv
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _check(cid: str, label: str, ok: bool, weight: float, detail: str = "") -> dict[str, Any]:
    return {"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": None if ok else detail}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _json(ground_truth_path or TASK_DIR / "ground_truth.json")
    plan_path = w / "out/launch_plan.md"
    pack_path = w / "out/content_pack.json"
    plan = plan_path.read_text(encoding="utf-8", errors="replace") if plan_path.exists() else ""
    plan_l = plan.lower()
    checks: list[dict[str, Any]] = []

    checks.append(_check("plan_exists", "launch_plan.md exists", plan_path.is_file(), 0.08))
    checks.append(_check("pack_exists", "content_pack.json exists", pack_path.is_file(), 0.08))
    checklist_path = w / "out/launch_checklist.csv"
    checks.append(_check("checklist_exists", "launch_checklist.csv exists", checklist_path.is_file(), 0.06))
    checks.append(_check("sections", "plan contains required sections", all(s.lower() in plan_l for s in gt["required_sections"]), 0.12))
    checks.append(_check("audience", "plan mentions all audience segment names", all(s.lower() in plan_l for s in gt["segment_names"]), 0.12))
    checks.append(_check("timeline", "plan includes all required milestone dates", all(d in plan for d in gt["required_dates"]), 0.12))
    checks.append(_check("offline_webinar", "plan includes offline webinar", gt["required_phrase"] in plan_l, 0.08))
    checks.append(_check("no_forbidden_claims", "plan avoids early availability and unavailable feature claims", plan_path.is_file() and not any(p in plan_l for p in gt["forbidden_early_availability"][:4]) and "promise mobile app" not in plan_l, 0.08))

    amounts = [int(x) for x in re.findall(r"\$?\b([0-9]{3,5})\b", plan.replace(",", ""))]
    over_budget = any(x > gt["approved_total_usd"] for x in amounts)
    checks.append(_check("budget_bound", "no single planned spend exceeds approved total", plan_path.is_file() and amounts and not over_budget, 0.06))
    checks.append(_check("dependency_compliance_terms", "plan covers compliance, dependencies, and excluded segment", all(t in plan_l for t in gt["required_plan_terms"]), 0.10))

    pack_ok = False
    pack_detail = "missing or invalid JSON"
    if pack_path.exists():
        try:
            pack = _json(pack_path)
            keys = {"tagline", "email_subjects", "social_posts", "webinar_agenda", "segment_messages"}
            seg_msg = pack.get("segment_messages", {})
            pack_ok = (
                isinstance(pack, dict)
                and keys.issubset(pack)
                and isinstance(pack.get("email_subjects"), list) and len(pack["email_subjects"]) >= 3
                and isinstance(pack.get("social_posts"), list) and len(pack["social_posts"]) >= 3
                and isinstance(pack.get("webinar_agenda"), list) and len(pack["webinar_agenda"]) >= 4
                and isinstance(seg_msg, dict) and all(seg in seg_msg for seg in gt["segments"])
                and all(seg in seg_msg for seg in gt["available_segments"])
                and "agency_partners" in seg_msg
                and any(word in json.dumps(seg_msg.get("agency_partners", ""), ensure_ascii=False).lower() for word in ["exclude", "not target", "unavailable", "not in this release"])
            )
            pack_detail = "required content_pack keys, counts, or segment messages missing"
        except Exception as exc:
            pack_detail = str(exc)
    checks.append(_check("content_pack_schema", "content_pack.json schema and required counts", pack_ok, 0.20, pack_detail))

    checklist_ok = False
    if checklist_path.exists():
        try:
            with checklist_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                header = reader.fieldnames or []
            all_text = json.dumps(rows, ensure_ascii=False).lower()
            checklist_ok = header == ["item", "owner", "due_date", "dependency", "status"] and all(term in all_text for term in gt["required_checklist_terms"])
        except Exception:
            checklist_ok = False
    checks.append(_check("launch_checklist", "launch_checklist.csv captures dependencies", checklist_ok, 0.08))

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    return {"task": "068-product-launch-ops", "workspace": str(w), "outcome_score": score, "checks": checks}
