from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def score_workspace(workspace: Path) -> dict[str, Any]:
    result_path = workspace / "out" / "result.json"
    report_path = workspace / "out" / "run_report.md"
    result_ok = False
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result_ok = result == {
            "request_id": "BG-204",
            "status": "completed",
            "records": 3,
        }
    report = (
        report_path.read_text(encoding="utf-8", errors="replace").lower()
        if report_path.is_file()
        else ""
    )
    report_ok = all(term in report for term in ("bg-204", "task-", "completed"))
    score = (0.7 if result_ok else 0.0) + (0.3 if report_ok else 0.0)
    return {
        "task": "xbot-background-shell",
        "outcome_score": score,
        "checks": [
            {"id": "background_result", "pass": result_ok, "weight": 0.7},
            {"id": "completion_report", "pass": report_ok, "weight": 0.3},
        ],
    }
