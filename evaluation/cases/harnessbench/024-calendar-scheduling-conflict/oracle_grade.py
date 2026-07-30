from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _participant_email_set(val: Any) -> set[str]:
    """Normalize participants to lowercase emails for subset checks.

    Models sometimes emit a list of strings or a list of objects like ``{"email": "..."}``;
    ``set()`` cannot contain dicts (unhashable).
    """
    if not isinstance(val, list):
        return set()
    out: set[str] = set()
    for p in val:
        if isinstance(p, str):
            s = p.strip()
            if s:
                out.add(s.lower())
        elif isinstance(p, dict):
            email = p.get("email") or p.get("Email")
            if isinstance(email, str) and email.strip():
                out.add(email.strip().lower())
    return out


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _json(ground_truth_path or _GT)
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, detail: Any = None, weight: float = 1.0) -> None:
        checks.append({"id": cid, "label": cid.replace("_", " "), "pass": bool(ok), "weight": weight, "detail": detail})

    slots_path = w / "out" / "proposed_slots.json"
    data: dict[str, Any] = {}
    if slots_path.is_file():
        try:
            raw = _json(slots_path)
            data = raw if isinstance(raw, dict) else {}
            add("proposed_slots_parseable", isinstance(raw, dict))
        except Exception as exc:
            add("proposed_slots_parseable", False, str(exc))
    else:
        add("proposed_slots_exists", False, "missing")

    slots = data.get("slots") if isinstance(data, dict) else None
    add("exactly_three_slots", isinstance(slots, list) and len(slots) == 3, slots)
    allowed = {(s["start"], s["end"]) for s in gt["allowed_slots"]}
    got_pairs = []
    participants_ok = True
    timezone_ok = True
    fields_ok = True
    if isinstance(slots, list):
        for slot in slots:
            if not isinstance(slot, dict):
                fields_ok = False
                continue
            got_pairs.append((slot.get("start"), slot.get("end")))
            fields_ok = fields_ok and all(k in slot for k in ["start", "end", "timezone", "participants", "rationale"])
            timezone_ok = timezone_ok and slot.get("timezone") == "America/New_York"
            participants_val = slot.get("participants")
            if isinstance(participants_val, list):
                participants = _participant_email_set(participants_val)
                # 规范要求为邮箱列表；若全是 dict 且无 email 字段，则无法参与子集判断
                if participants_val and not participants and any(isinstance(x, dict) for x in participants_val):
                    fields_ok = False
            else:
                participants = set()
                fields_ok = False
            required_emails = {e.strip().lower() for e in gt["required_participants"]}
            participants_ok = participants_ok and required_emails.issubset(participants)
    add("slot_times_are_valid_nonconflicting_options", set(got_pairs) == allowed, got_pairs)
    add("slot_schema_complete", fields_ok)
    add("timezone_is_new_york", timezone_ok)
    add("all_required_participants_each_slot", participants_ok)

    invite_path = w / "out" / "invite_draft.txt"
    text = invite_path.read_text(encoding="utf-8", errors="replace") if invite_path.is_file() else ""
    add("invite_draft_exists", invite_path.is_file() and bool(text.strip()))
    missing = [token for token in gt["invite_must_contain"] if token.lower() not in text.lower()]
    add("invite_contains_required_facts", not missing, missing)
    forbidden = [term for term in gt["forbidden_terms"] if term.lower() in text.lower()]
    add("invite_avoids_forbidden_promises", not forbidden, forbidden)

    total_w = sum(c["weight"] for c in checks)
    score = sum(c["weight"] for c in checks if c["pass"]) / total_w if total_w else 0.0
    return {"task": "024-calendar-scheduling-conflict", "workspace": str(w), "outcome_score": round(score, 4), "checks": checks}
