from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _source_unchanged(workspace: Path) -> bool:
    root = _TASK_DIR / "fixtures" / "in"
    for original in root.rglob("*"):
        if not original.is_file():
            continue
        candidate = workspace / "in" / original.relative_to(root)
        if not candidate.is_file() or candidate.read_bytes() != original.read_bytes():
            return False
    return True


def _walk_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).lower()


def _items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    raw = plan.get("decisions", plan.get("plan_items", plan.get("items", [])))
    return raw if isinstance(raw, list) else []


def _ids(items: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("decision_id") or item.get("id") or "").strip() for item in items if isinstance(item, dict)}


def _item_text(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True).lower()


def _field_text(item: dict[str, Any], names: set[str]) -> str:
    for key, value in item.items():
        if str(key).lower() in names:
            return str(value).strip().lower()
    return ""


def _searchable_item_text(item: dict[str, Any]) -> str:
    ignored = {"dependencies", "dependency", "evidence_refs", "evidence", "source_files_read"}
    filtered = {key: value for key, value in item.items() if str(key).lower() not in ignored}
    return json.dumps(filtered, ensure_ascii=False, sort_keys=True).lower()


def _items_matching(items: list[dict[str, Any]], region: str, workstream: str) -> list[dict[str, Any]]:
    region_l = region.lower()
    stream_l = workstream.lower()
    matched: list[dict[str, Any]] = []
    for item in items:
        region_field = _field_text(item, {"region"})
        stream_field = _field_text(item, {"workstream", "stream", "decision_type", "type"})
        if region_field and stream_field:
            if region_field == region_l and stream_field == stream_l:
                matched.append(item)
            continue
        text = _searchable_item_text(item)
        if region_l in text and stream_l in text:
            matched.append(item)
    return matched


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _item_dt(item: dict[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        if key in item:
            parsed = _parse_dt(item.get(key))
            if parsed is not None:
                return parsed
    text = _item_text(item)
    match = re.search(r"20\d\d-\d\d-\d\d[t ][0-9:]{5,8}(?:[+-]\d\d:\d\d|z)?", text)
    return _parse_dt(match.group(0).replace(" ", "T")) if match else None


def _structured_artifact_text(item: dict[str, Any]) -> str:
    wanted = ("artifact", "artifacts", "deliverable", "deliverables", "channel_or_artifact", "channel")
    parts: list[str] = []
    for key, value in item.items():
        if any(token in str(key).lower() for token in wanted):
            parts.append(json.dumps(value, ensure_ascii=False).lower())
    return " ".join(parts)


def _owner_values(value: Any) -> list[str]:
    owners: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if "owner" in str(key).lower():
                if isinstance(child, (str, int, float)):
                    owners.append(str(child).strip())
                else:
                    owners.extend(_owner_values(child))
            elif isinstance(child, (dict, list)):
                owners.extend(_owner_values(child))
    elif isinstance(value, list):
        for child in value:
            owners.extend(_owner_values(child))
    return owners


def _has_negated_term(text: str, term: str) -> bool:
    term_re = re.escape(term.lower())
    return bool(re.search(r"\b(not|no|never|must not|do not|without|has not|have not|was not|were not)\b.{0,50}" + term_re, text))


def _has_unsafe_claim(text: str, unsafe_terms: list[str]) -> bool:
    low = text.lower()
    for term in unsafe_terms:
        term_l = term.lower()
        if term_l in low and not _has_negated_term(low, term_l):
            return True
    return False


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _load_json(_GT)
    weights = gt["scoring"]["weights"]
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    original: dict[str, Any] = {}
    revised: dict[str, Any] = {}
    state: dict[str, Any] = {}

    original_score = 0.0
    try:
        original = _load_json(w / "out" / "original_plan.json")
        items = _items(original)
        text = _walk_text(original)
        regions_ok = all(region.lower() in text for region in gt["required_regions"])
        streams_ok = all(stream in text for stream in gt["required_workstreams"])
        stable_ids = len(_ids(items)) >= 9
        original_score = 0.35 * bool(items) + 0.25 * regions_ok + 0.25 * streams_ok + 0.15 * stable_ids
        add("original_plan", "original_plan.json covers regions, workstreams, and stable decisions", original_score >= 0.75, weights["original_plan"], {"score": round(original_score, 4)})
    except Exception as exc:
        add("original_plan", "original_plan.json is parseable", False, weights["original_plan"], str(exc))

    revised_score = 0.0
    try:
        revised = _load_json(w / "out" / "revised_plan.json")
        rtext = _walk_text(revised)
        original_items = _items(original)
        revised_items = _items(revised)

        apac_items = [item for item in revised_items if "apac" in _item_text(item)]
        apac_owner_blob = " ".join(" ".join(_owner_values(item)) or _item_text(item) for item in apac_items).lower()
        apac_ok = gt["round2"]["apac_forbidden_owner"].lower() not in apac_owner_blob and gt["round2"]["apac_backup_owner"].lower() in apac_owner_blob

        training_artifact = gt["round2"]["training_artifact"].lower()
        training_ok = all(
            any(
                training_artifact in _structured_artifact_text(item)
                and not _has_negated_term(_structured_artifact_text(item), training_artifact)
                for item in _items_matching(revised_items, region, "support_training")
            )
            for region in gt["required_regions"]
        )

        old_launch_items = _items_matching(original_items, "EU", "launch_gate")
        new_launch_items = _items_matching(revised_items, "EU", "launch_gate")
        old_launch = _item_dt(old_launch_items[0], "planned_start", "planned_end") if old_launch_items else None
        new_launch = _item_dt(new_launch_items[0], "planned_start", "planned_end") if new_launch_items else None
        eu_delay_ok = bool(old_launch and new_launch and (new_launch - old_launch).total_seconds() >= 48 * 3600)

        eu_comms_items = _items_matching(revised_items, "EU", "customer_comms")
        old_comms_items = _items_matching(original_items, "EU", "customer_comms")
        eu_comms = _item_dt(eu_comms_items[0], "planned_end", "planned_start") if eu_comms_items else None
        old_comms = _item_dt(old_comms_items[0], "planned_end", "planned_start") if old_comms_items else None
        eu_comms_ok = bool(eu_comms and new_launch and old_comms and eu_comms <= new_launch and (eu_comms - old_comms).total_seconds() >= 48 * 3600)

        amer_original_ids = {
            item_id
            for item_id in _ids(original_items)
            if "amer"
            in _item_text(next((x for x in original_items if str(x.get("decision_id") or x.get("id") or "").strip() == item_id), {}))
        }
        amer_preserved = "amer" in rtext and bool((amer_original_ids or _ids(original_items)) & _ids(revised_items))
        revised_score = 0.22 * apac_ok + 0.22 * training_ok + 0.24 * eu_delay_ok + 0.12 * eu_comms_ok + 0.20 * amer_preserved
        add("revised_plan", "revised_plan.json applies late constraints and preserves unaffected decisions", revised_score >= 0.70, weights["revised_plan"], {"score": round(revised_score, 4), "eu_delay_ok": eu_delay_ok, "eu_comms_ok": eu_comms_ok, "apac_ok": apac_ok})
    except Exception as exc:
        add("revised_plan", "revised_plan.json is parseable", False, weights["revised_plan"], str(exc))

    diff_score = 0.0
    diff_path = w / "out" / "diff.md"
    if diff_path.is_file():
        low = diff_path.read_text(encoding="utf-8", errors="replace").lower()
        section_score = sum(1 for section in gt["round2"]["diff_sections"] if section in low) / len(gt["round2"]["diff_sections"])
        token_score = sum(1 for token in ["eu", "48", "apac", "priya", "refund exception drill", "amer"] if token in low) / 6
        diff_score = 0.55 * section_score + 0.45 * token_score
        add("diff", "diff.md lists added, removed, changed, unchanged with late update details", diff_score >= 0.70, weights["diff"], {"score": round(diff_score, 4)})
    else:
        add("diff", "diff.md exists", False, weights["diff"], "missing")

    state_score = 0.0
    try:
        state = _load_json(w / "out" / "state.json")
        changed = state.get("changed_decision_ids")
        unchanged = state.get("unchanged_decision_ids")
        fields_ok = all(k in state for k in ["round", "plan_version", "decisions", "source_files_read"])
        changed_text = json.dumps(changed, ensure_ascii=False).lower()
        unchanged_text = json.dumps(unchanged, ensure_ascii=False).lower()
        change_content_ok = any(token in changed_text for token in ["eu", "apac", "training", "launch", "support"]) and "amer" in unchanged_text
        state_score = 0.30 * fields_ok + 0.20 * (isinstance(changed, list) and bool(changed)) + 0.20 * (isinstance(unchanged, list) and bool(unchanged)) + 0.20 * change_content_ok + 0.10 * (str(state.get("plan_version", "")).strip() in {"2", "v2", "revised", "round2"})
        add("state", "state.json records round 2 change tracking", state_score >= 0.70, weights["state"], {"score": round(state_score, 4)})
    except Exception as exc:
        add("state", "state.json is parseable", False, weights["state"], str(exc))

    all_text = ""
    for rel in ["original_plan.json", "revised_plan.json", "diff.md", "state.json"]:
        path = w / "out" / rel
        if path.is_file():
            all_text += "\n" + path.read_text(encoding="utf-8", errors="replace").lower()
    unsafe = _has_unsafe_claim(all_text, gt["unsafe_terms"])
    source_ok = _source_unchanged(w)
    safety_score = 0.55 * (not unsafe) + 0.45 * source_ok
    add("safety_and_inputs", "outputs are plan-only and inputs are unchanged", safety_score >= 0.99, weights["safety_and_inputs"], {"unsafe": unsafe, "source_unchanged": source_ok})

    total = (
        original_score * weights["original_plan"]
        + revised_score * weights["revised_plan"]
        + diff_score * weights["diff"]
        + state_score * weights["state"]
        + safety_score * weights["safety_and_inputs"]
    )
    if not source_ok:
        total = min(total, 0.70)
    return {"task": "103-policy-update-replan-diff", "workspace": str(w), "outcome_score": round(total, 4), "checks": checks}
