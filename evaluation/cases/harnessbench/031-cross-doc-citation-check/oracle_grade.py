from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or _GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, detail: Any = None) -> None:
        checks.append({"id": cid, "label": cid.replace("_", " "), "pass": bool(ok), "weight": 1.0, "detail": detail})

    rows: list[dict[str, str]] = []
    p = w / "out" / "citation_audit.csv"
    if p.is_file():
        try:
            rows = list(csv.DictReader(p.open(newline="", encoding="utf-8")))
            add("audit_csv_parseable", True)
        except Exception as exc:
            add("audit_csv_parseable", False, str(exc))
    else:
        add("audit_csv_exists", False, "missing")
    add("audit_header_exact", bool(rows) and list(rows[0].keys()) == ["citation_id", "location", "issue", "expected_source", "recommended_fix"], list(rows[0].keys()) if rows else None)
    add("exactly_three_audit_rows", len(rows) == 3, len(rows))
    for exp in gt["expected_audit"]:
        hit = [r for r in rows if r.get("citation_id") == exp["citation_id"] and exp["location"].lower() in r.get("location", "").lower() and exp["issue_contains"].lower() in r.get("issue", "").lower() and r.get("expected_source") == exp["expected_source"]]
        add(f"audit_{exp['location'].replace(' ', '_')}", bool(hit), exp)

    fp = w / "out" / "fixed_references.md"
    text = fp.read_text(encoding="utf-8", errors="replace") if fp.is_file() else ""
    add("fixed_references_exists", bool(text.strip()))
    missing = [r for r in gt["fixed_refs"] if r not in text]
    add("fixed_references_complete", not missing, missing)
    forbidden = [r for r in gt["forbidden_refs"] if r.lower() in text.lower()]
    add("fixed_references_avoid_retired_sources", not forbidden, forbidden)

    score = sum(c["pass"] for c in checks) / len(checks) if checks else 0.0
    return {"task": "031-cross-doc-citation-check", "workspace": str(w), "outcome_score": round(score, 4), "checks": checks}
