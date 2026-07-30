      
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _load_json(_GT)
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    state: dict[str, Any] = {}
    try:
        state = _load_json(w / "out" / "seen_state.json")
        add("state_parse", "seen_state.json is valid JSON", True, 0.10)
    except Exception as exc:
        add("state_parse", "seen_state.json is valid JSON", False, 0.10, str(exc))

    if state:
        add("window", "state records exact inclusive time window", state.get("window_start") == gt["window_start"] and state.get("window_end") == gt["window_end"], 0.10)
        
        # Allow the model to include additional state ids while still requiring all in-window updates
        seen_ids = {str(x) for x in state.get("seen_ids", [])}
        required_seen_ids = {str(x) for x in gt["seen_ids"]}
        add("seen_ids", "state includes all unique in-window updates", required_seen_ids.issubset(seen_ids), 0.20, state.get("seen_ids"))
        add("duplicates", "state records duplicate update ids", sorted(state.get("duplicate_ids", [])) == sorted(gt["duplicate_ids"]), 0.15, state.get("duplicate_ids"))
        add("ignored", "state records out-of-window ignored updates", sorted(state.get("ignored_ids", [])) == sorted(gt["ignored_ids"]), 0.15, state.get("ignored_ids"))
        
        # 兼容处理嵌套字典或纯字符串的情况
        model_comps = state.get("component_latest_status", {})
        gt_comps = gt.get("components", {})
        comps_ok = True
        if not isinstance(model_comps, dict) or not model_comps:
            comps_ok = False
        else:
            for comp, expected_status in gt_comps.items():
                actual_val = model_comps.get(comp)
                actual_status = actual_val.get("status") if isinstance(actual_val, dict) else actual_val
                if actual_status != expected_status:
                    comps_ok = False
                    break
        
        add("components", "latest component statuses are correct", comps_ok, 0.20, state.get("component_latest_status"))

    rollup_path = w / "out" / "status_rollup.md"
    if rollup_path.is_file():
        text = rollup_path.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        ok = all(token.lower() in low for token in gt["required_rollup_tokens"])
        add("rollup_content", "rollup covers sections, included ids, and ignored ids", ok, 0.20)
    else:
        add("rollup_content", "status_rollup.md exists", False, 0.20, "missing")

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    return {"task": "061-periodic-status-rollup", "workspace": str(w), "outcome_score": score, "checks": checks}

    