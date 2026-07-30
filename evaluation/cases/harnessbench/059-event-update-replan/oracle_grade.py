from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _load_json(_GT)
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    original: dict[str, Any] = {}
    revised: dict[str, Any] = {}
    for name, path, weight in [("original", w / "out" / "original_plan.json", 0.10), ("revised", w / "out" / "revised_plan.json", 0.10)]:
        try:
            data = _load_json(path)
            if name == "original":
                original = data
            else:
                revised = data
            add(f"{name}_parse", f"{name}_plan.json is valid JSON", True, weight)
        except Exception as exc:
            add(f"{name}_parse", f"{name}_plan.json is valid JSON", False, weight, str(exc))

    blocks = {b.get("id"): b for b in revised.get("blocks", []) if isinstance(b, dict)}
    add("required_blocks", "revised plan contains all required blocks", list(blocks) == gt["required_blocks"] or all(b in blocks for b in gt["required_blocks"]), 0.15, list(blocks))
    live = blocks.get("live_support", {})
    add("room_constraint", "live support moved to Room B", live.get("room") == gt["live_support_room"], 0.15, live)
    people = live.get("assigned_people", [])
    add("chen_removed", "Chen is not assigned after 13:00 live support", "Chen" not in people, 0.15, people)
    rehearsal = blocks.get("rehearsal", {})
    try:
        reh_ok = _minutes(str(rehearsal.get("end"))) <= _minutes(gt["rehearsal_latest_end"])
    except Exception:
        reh_ok = False
    add("rehearsal_time", "rehearsal ends before or at 11:00", reh_ok, 0.10, rehearsal)
    acc = blocks.get("accessibility_check", {})
    try:
        acc_ok = _minutes(str(acc.get("end"))) == _minutes(str(live.get("start"))) and _minutes(str(acc.get("end"))) - _minutes(str(acc.get("start"))) == gt["accessibility_duration_minutes"]
    except Exception:
        acc_ok = False
    add("accessibility_check", "15-minute accessibility check immediately precedes live support", acc_ok, 0.15, acc)

    if original and revised:
        unchanged_ok = blocks.get("setup", {}).get("assigned_people") == {b.get("id"): b for b in original.get("blocks", []) if isinstance(b, dict)}.get("setup", {}).get("assigned_people")
        add("preserve_setup", "setup assignment is preserved from original when unaffected", unchanged_ok, 0.05)

    diff_path = w / "out" / "diff.md"
    if diff_path.is_file():
        diff = diff_path.read_text(encoding="utf-8", errors="replace")
        low = diff.lower()
        ok = all(word in low for word in ["added", "changed", "removed"]) and all(token.lower() in low for token in gt["changed_constraints"])
        add("diff_content", "diff.md lists added, removed, changed items and new constraints", ok, 0.15)
    else:
        add("diff_content", "diff.md exists", False, 0.15, "missing")

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    return {"task": "059-event-update-replan", "workspace": str(w), "outcome_score": score, "checks": checks}
