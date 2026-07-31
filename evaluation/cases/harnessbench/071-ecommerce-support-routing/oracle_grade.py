from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _check(cid: str, label: str, ok: bool, weight: float, detail: str = "") -> dict[str, Any]:
    return {"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": None if ok else detail}


def _first_line(path: Path) -> str:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    return raw[0].strip() if raw else ""


def _template_sections(md: str) -> dict[str, str]:
    rx = re.compile(r"^###\s+Template\s+(T-\d+)\s*$", re.MULTILINE)
    ms = list(rx.finditer(md))
    out: dict[str, str] = {}
    for i, m in enumerate(ms):
        tid = m.group(1)
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(md)
        out[tid] = md[start:end]
    return out


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    decisions_path = w / gt["decisions_path"]
    templates_path = w / gt["templates_path"]
    escalation_path = w / gt["escalation_path"]
    expected: dict[str, Any] = gt["expected"]

    checks: list[dict[str, Any]] = []
    checks.append(_check("decisions_exists", "routing_decisions.json exists", decisions_path.is_file(), 0.05))
    checks.append(_check("templates_exists", "reply_templates.md exists", templates_path.is_file(), 0.05))
    checks.append(_check("escalations_exists", "escalation_notes.csv exists", escalation_path.is_file(), 0.05))

    data: Any = None
    try:
        data = json.loads(decisions_path.read_text(encoding="utf-8")) if decisions_path.exists() else None
    except Exception:
        data = None
    req_keys = {"ticket_id", "action", "policy_basis", "order_id", "priority", "customer_reply_key"}
    schema_ok = (
        isinstance(data, list)
        and len(data) == len(expected)
        and all(isinstance(x, dict) and req_keys.issubset(x) for x in data)
    )
    checks.append(_check("json_schema", "decisions JSON objects include required keys", schema_ok, 0.06))

    by_id = {str(x.get("ticket_id")): x for x in data if isinstance(x, dict)} if isinstance(data, list) else {}
    all_ids = set(by_id) == set(expected)
    checks.append(_check("all_tickets", "one routing row per ticket id", all_ids, 0.06))

    actions_ok = all(by_id.get(tid, {}).get("action") == exp["action"] for tid, exp in expected.items())
    checks.append(_check("actions", "routing actions match ground truth", actions_ok, 0.13))

    basis_ok = all(str(by_id.get(tid, {}).get("policy_basis", "")).find(exp["policy_basis"]) >= 0 for tid, exp in expected.items())
    checks.append(_check("policy_basis", "policy_basis cites expected clause ids", basis_ok, 0.10))

    allowed_ok = bool(by_id) and all(str(x.get("action")) in gt["allowed_actions"] for x in by_id.values())
    checks.append(_check("allowed_actions", "actions stay within taxonomy", allowed_ok, 0.05))

    order_ok = True
    order_detail = ""
    for tid, exp in expected.items():
        got = by_id.get(tid, {}).get("order_id", "")
        if got is None:
            got_s = ""
        else:
            got_s = str(got).strip()
        exp_s = exp.get("order_id", "")
        if exp_s != got_s:
            order_ok = False
            order_detail = f"{tid}: order_id expected {exp_s!r} got {got_s!r}"
            break
    checks.append(_check("order_id_fields", "order_id echoes ticket input", order_ok, 0.08))

    prio_ok = True
    prio_detail = ""
    for tid, exp in expected.items():
        exp_p = str(exp.get("priority", "")).strip().lower()
        got_p = str(by_id.get(tid, {}).get("priority", "")).strip().lower()
        if got_p != exp_p:
            prio_ok = False
            prio_detail = f"{tid}: priority expected {exp_p!r} got {got_p!r}"
            break
    checks.append(_check("priority_map", "priority reflects escalation and vip-info urgency", prio_ok, 0.10))

    keys_ok = True
    keys_detail = ""
    for tid, exp in expected.items():
        exp_k = str(exp.get("customer_reply_key", "")).strip()
        got_k = str(by_id.get(tid, {}).get("customer_reply_key", "")).strip()
        if got_k != exp_k:
            keys_ok = False
            keys_detail = f"{tid}: customer_reply_key expected {exp_k!r} got {got_k!r}"
            break
    checks.append(_check("reply_key_contract", "customer_reply_key matches routing contract", keys_ok, 0.07))

    tmpl_raw = templates_path.read_text(encoding="utf-8", errors="replace") if templates_path.exists() else ""
    sections = _template_sections(tmpl_raw)
    headings_ok = templates_path.is_file() and set(sections.keys()) == set(expected)
    checks.append(_check("template_headings", "reply_templates uses ### Template <ticket_id> sections", headings_ok, 0.07))

    tmpl_lc = tmpl_raw.lower()
    safe_templates = templates_path.is_file() and not any(p.lower() in tmpl_lc for p in gt["forbidden_template_phrases"])
    checks.append(_check("templates_no_overpromise", "templates avoid forbidden promises", safe_templates, 0.05))

    wants_esc = {tid for tid, exp in expected.items() if exp["action"] == "escalate_human"}
    esc_header_ok = escalation_path.is_file() and _first_line(escalation_path) == gt["escalation_header"]

    escalation_ok = False
    esc_detail = ""
    teams_map: dict[str, str] = gt.get("escalation_human_team") or {}
    if escalation_path.exists() and esc_header_ok:
        try:
            with escalation_path.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            by_esc = {str(r.get("ticket_id", "")).strip(): r for r in rows if r.get("ticket_id")}
            ids_ok = set(by_esc.keys()) == wants_esc
            teams_ok = True
            reasons_ok = True
            for tid in wants_esc:
                row = by_esc.get(tid, {})
                pb = str(row.get("policy_basis", "")).strip()
                rs = str(row.get("reason", "")).strip()
                hm = str(row.get("human_team", "")).strip()
                want_team = teams_map.get(tid, "")
                if len(rs) < 8:
                    reasons_ok = False
                    esc_detail = f"{tid}: reason too short"
                    break
                if not pb:
                    reasons_ok = False
                    esc_detail = f"{tid}: missing policy_basis"
                    break
                if hm != want_team:
                    teams_ok = False
                    esc_detail = f"{tid}: human_team expected {want_team!r} got {hm!r}"
                    break
            escalation_ok = ids_ok and teams_ok and reasons_ok and len(rows) == len(wants_esc)
            if not ids_ok and not esc_detail:
                esc_detail = "escalation_notes.csv ticket_id set mismatch"
        except Exception as exc:
            escalation_ok = False
            esc_detail = str(exc)

    checks.append(_check("escalation_csv_header", "escalation_notes.csv header exact", esc_header_ok, 0.03))
    checks.append(_check("escalation_notes_complete", "escalation rows teams reasons policy_basis", escalation_ok, 0.05))

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0

    for c in checks:
        if c["id"] == "order_id_fields" and not c["pass"]:
            c["detail"] = order_detail
        if c["id"] == "priority_map" and not c["pass"]:
            c["detail"] = prio_detail
        if c["id"] == "reply_key_contract" and not c["pass"]:
            c["detail"] = keys_detail
        if c["id"] == "escalation_notes_complete" and not c["pass"]:
            c["detail"] = esc_detail or c.get("detail")

    return {"task": "071-ecommerce-support-routing", "workspace": str(w), "outcome_score": score, "checks": checks}
