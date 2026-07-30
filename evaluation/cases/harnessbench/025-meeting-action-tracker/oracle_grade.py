from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or _GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, detail: Any = None) -> None:
        checks.append({"id": cid, "label": cid.replace("_", " "), "pass": bool(ok), "weight": 1.0, "detail": detail})

    # 1. 读取 action_items.csv
    p = w / "out" / "action_items.csv"
    rows: list[dict[str, str]] = []
    if p.is_file():
        try:
            rows = _rows(p)
            add("csv_parseable", True)
        except Exception as exc:
            add("csv_parseable", False, str(exc))
    else:
        add("csv_exists", False, "missing")
        rows = []

    # 2. 表头检查
    expected_header = ["action_id", "owner", "task", "deadline", "status", "source"]
    add("csv_header_exact", bool(rows) and list(rows[0].keys()) == expected_header, list(rows[0].keys()) if rows else None)

    # 3. 每个期望的 action 必须出现（action_id, owner, task包含关键词, deadline, status, source包含）
    for exp in gt["expected_actions"]:
        hit = False
        matched_row = None
        for r in rows:
            # 检查 action_id
            if r.get("action_id") != exp["action_id"]:
                continue
            # 检查 owner
            if r.get("owner") != exp["owner"]:
                continue
            # 检查 task 包含关键词
            if exp["task_contains"].lower() not in r.get("task", "").lower():
                continue
            # 检查 deadline
            if r.get("deadline") != exp["deadline"]:
                continue
            # 检查 status（严格匹配）
            if r.get("status", "").lower() != exp["status"].lower():
                continue
            # 检查 source 包含关键词（允许多个合法来源）
            src = r.get("source", "")
            if "source_matches" in exp:
                if not any(m.lower() in src.lower() for m in exp["source_matches"]):
                    continue
            hit = True
            matched_row = r
            break
        add(f"action_{exp['action_id']}", hit, exp if not hit else matched_row)

    # 4. 禁止词检查：已经完成/取消的任务不得出现在 open actions 中
    forbidden_hits = []
    for r in rows:
        task = r.get("task", "")
        for f in gt["forbidden_task_contains"]:
            if f.lower() in task.lower():
                forbidden_hits.append(f"{r.get('action_id')}: {task}")
                break
    add("completed_previous_action_excluded", not forbidden_hits, forbidden_hits)

    # 5. owner_followups.md 检查
    text_path = w / "out" / "owner_followups.md"
    text = text_path.read_text(encoding="utf-8", errors="replace") if text_path.is_file() else ""
    add("followups_exists", bool(text.strip()))
    missing_owners = [o for o in gt["owners"] if o.lower() not in text.lower()]
    missing_dates = [e["deadline"] for e in gt["expected_actions"] if e["deadline"] not in text]
    add("followups_cover_owners", not missing_owners, missing_owners)
    add("followups_cover_deadlines", not missing_dates, missing_dates)

    # 5b. 深度检查：是否提到了 blocked/pending 和 dependency
    text_lower = text.lower()
    has_blocked_note = "blocked" in text_lower or "pending" in text_lower
    add("followups_note_blocked_dependencies", has_blocked_note)

    # 6. merge_rationale.md 检查
    rationale_path = w / "out" / "merge_rationale.md"
    rationale = rationale_path.read_text(encoding="utf-8", errors="replace") if rationale_path.is_file() else ""
    add("rationale_exists", bool(rationale.strip()))

    rationale_lower = rationale.lower()
    # missing_terms = [term for term in gt["rationale_terms"] if term.lower() not in rationale_lower]
    rationale_terms_normalized = [
        "followup_emails" if term == "followup_emails.md" else term
        for term in gt["rationale_terms"]
    ]
    missing_terms = [
        term for term in rationale_terms_normalized
        if term.lower() not in rationale_lower.replace("_", " ").replace(".md", "")
    ]
    add("rationale_covers_merge_decisions", not missing_terms, missing_terms)

    # 6b. 深度检查：是否解释了 dependency-driven changes
    has_dependency_explanation = (
        "dependency" in rationale_lower 
        or "blocked" in rationale_lower 
        or "extend" in rationale_lower
    )
    add("rationale_explains_dependencies", has_dependency_explanation)

    # 6c. 深度检查：是否解释了 bulk update (Jules rolling off)
    has_bulk_explanation = (
        "jules" in rationale_lower 
        and ("bulk" in rationale_lower or "rolling off" in rationale_lower or "cancel" in rationale_lower)
    )
    add("rationale_explains_bulk_update", has_bulk_explanation)

    # 6d. 深度检查：是否解释了 AT-106 pending
    has_pending_explanation = "at-106" in rationale_lower and "pending" in rationale_lower
    add("rationale_explains_pending_status", has_pending_explanation)

    # 计算总分（所有 checks 权重均为 1.0）
    score = sum(c["pass"] for c in checks) / len(checks) if checks else 0.0
    return {
        "task": "025-meeting-action-tracker",
        "workspace": str(w),
        "outcome_score": round(score, 4),
        "checks": checks
    }