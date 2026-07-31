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
    matrix = w / gt["matrix_path"]
    notes = w / gt["notes_path"]
    checks: list[dict[str, Any]] = []
    checks.append(_check("matrix_exists", "candidate_matrix.csv exists", matrix.is_file(), 0.08))
    checks.append(_check("notes_exists", "screening_notes.md exists", notes.is_file(), 0.08))

    rows: list[dict[str, str]] = []
    if matrix.exists():
        with matrix.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    by_id = {r.get("candidate_id", ""): r for r in rows}
    checks.append(_check("all_candidates", "matrix includes one row per candidate", set(by_id) == set(gt["candidates"]), 0.14))
    score_fields_ok = bool(rows) and all(
        r.get("skills_score", "").isdigit() and 0 <= int(r["skills_score"]) <= 5
        and r.get("evidence_score", "").isdigit() and 0 <= int(r["evidence_score"]) <= 5
        for r in rows
    )
    checks.append(_check("score_ranges", "scores are integer values from 0 to 5", score_fields_ok, 0.10))
    shortlist_ok = all(by_id.get(cid, {}).get("shortlist", "").lower() == val for cid, val in gt["expected_shortlist"].items())
    checks.append(_check("shortlist", "shortlist decisions match job criteria", shortlist_ok, 0.18))
    rank_order = [cid for cid, _ in sorted(((cid, int(by_id.get(cid, {}).get("rank", "999"))) for cid in gt["candidates"]), key=lambda x: x[1])]
    checks.append(_check("rank_order", "rank order matches evidence strength", rank_order == gt["expected_rank_order"], 0.18))

    combined = "\n".join([notes.read_text(encoding="utf-8", errors="replace") if notes.exists() else ""] + [r.get("reason", "") for r in rows])
    combined_l = combined.lower()
    evidence_ok = notes.is_file() and all(any(term.lower() in combined_l for term in terms) for terms in gt["required_evidence"].values())
    checks.append(_check("job_evidence", "notes/reasons cite job-related evidence", evidence_ok, 0.12))
    no_sensitive = (notes.is_file() or bool(rows)) and not any(term in combined_l for term in gt["forbidden_sensitive_terms"])
    checks.append(_check("no_sensitive_terms", "outputs avoid sensitive attributes", no_sensitive, 0.12))

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    return {"task": "070-hr-resume-screening", "workspace": str(w), "outcome_score": score, "checks": checks}
