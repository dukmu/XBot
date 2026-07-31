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
    grades = w / gt["grades_path"]
    feedback_dir = w / gt["feedback_dir"]
    checks: list[dict[str, Any]] = []
    checks.append(_check("grades_exists", "grades.csv exists", grades.is_file(), 0.10))
    rows: list[dict[str, str]] = []
    if grades.exists():
        with grades.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    by_id = {r.get("submission_id", ""): r for r in rows}
    checks.append(_check("all_submissions", "grades include every submission", set(by_id) == set(gt["expected_scores"]), 0.16))
    exact_scores = True
    totals_ok = True
    for sid, exp in gt["expected_scores"].items():
        row = by_id.get(sid, {})
        for field, val in exp.items():
            exact_scores = exact_scores and row.get(field, "") == str(val)
        try:
            totals_ok = totals_ok and int(row["concept_score"]) + int(row["evidence_score"]) + int(row["clarity_score"]) == int(row["total_score"])
        except Exception:
            totals_ok = False
    checks.append(_check("exact_scores", "scores match rubric expectations", exact_scores, 0.34))
    checks.append(_check("total_math", "total_score equals component sum", totals_ok, 0.14))
    files_ok = all((feedback_dir / f"{sid}.md").is_file() for sid in gt["expected_scores"])
    checks.append(_check("feedback_files", "one feedback file per submission", files_ok, 0.12))
    feedback_ok = True
    for sid in gt["expected_scores"]:
        p = feedback_dir / f"{sid}.md"
        t = p.read_text(encoding="utf-8", errors="replace").lower() if p.exists() else ""
        feedback_ok = feedback_ok and sid in t and all(term in t for term in gt["feedback_required_terms"])
    checks.append(_check("feedback_content", "feedback mentions submission id, strength, and improvement", feedback_ok, 0.14))
    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    return {"task": "074-education-grading-feedback", "workspace": str(w), "outcome_score": score, "checks": checks}
