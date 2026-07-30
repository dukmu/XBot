from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _contains_unnegated(text: str, phrase: str) -> bool:
    low = text.lower()
    target = phrase.lower()
    for match in re.finditer(re.escape(target), low):
        prefix = low[max(0, match.start() - 80) : match.start()]
        if re.search(r"\b(not|no|never|without|avoid|avoiding|not assuming|do not|don't|cannot|can't)\b", prefix):
            continue
        return True
    return False


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or _GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, detail: Any = None) -> None:
        checks.append({"id": cid, "label": cid.replace("_", " "), "pass": bool(ok), "weight": 1.0, "detail": detail})

    data: dict[str, Any] = {}
    p = w / "out" / "thread_summary.json"
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            data = raw if isinstance(raw, dict) else {}
            add("summary_json_parseable", isinstance(raw, dict))
        except Exception as exc:
            add("summary_json_parseable", False, str(exc))
    else:
        add("summary_exists", False, "missing")
    threads = data.get("threads")
    add("thread_count_correct", isinstance(threads, list) and len(threads) == gt["expected_thread_count"], len(threads) if isinstance(threads, list) else None)
    north = None
    if isinstance(threads, list):
        for t in threads:
            blob = json.dumps(t).lower()
            if "northwind" in blob and "onboarding" in blob:
                north = t
                break
    add("northwind_thread_present", isinstance(north, dict), north)
    nblob = json.dumps(north or {}, ensure_ascii=False).lower()
    add("deduped_forwarded_message", nblob.count("m-101") == 1, nblob.count("m-101"))
    add("unique_message_ids_present", all(mid in nblob for mid in gt["northwind_unique_message_ids"]))
    timeline = north.get("timeline") if isinstance(north, dict) else []

    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)   # 将字符串转为列表
        except json.JSONDecodeError:
            timeline = []                     # 解析失败则置空列表

    timeline_text = json.dumps(timeline)
    add("timeline_has_exact_timestamps", all(ts in timeline_text for ts in gt["northwind_timeline"]), timeline_text)
    # add("timeline_sorted", gt["northwind_timeline"] == [ts for ts in gt["northwind_timeline"] if ts in timeline_text])
    # timeline = north.get("timeline") if isinstance(north, dict) else []
    actual_timestamps = [entry.get("timestamp") for entry in timeline] if isinstance(timeline, list) else []
    add("timeline_sorted", actual_timestamps == gt["northwind_timeline"], actual_timestamps)
    todos_text = json.dumps(north.get("final_todos", []) if isinstance(north, dict) else [], ensure_ascii=False).lower()
    add("final_todos_complete", all(todo in todos_text for todo in gt["final_todos"]), todos_text)

    rp = w / "out" / "reply_draft.txt"
    text = rp.read_text(encoding="utf-8", errors="replace") if rp.is_file() else ""
    add("reply_exists", bool(text.strip()))
    missing = [x for x in gt["reply_must_contain"] if x.lower() not in text.lower()]
    add("reply_contains_required_facts", not missing, missing)
    forbidden = [x for x in gt["reply_forbidden"] if _contains_unnegated(text, x)]
    add("reply_avoids_false_approval", not forbidden, forbidden)

    score = sum(c["pass"] for c in checks) / len(checks) if checks else 0.0
    return {"task": "028-email-thread-merge", "workspace": str(w), "outcome_score": round(score, 4), "checks": checks}
