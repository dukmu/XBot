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

    rp = w / "out" / "revised_draft.md"
    text = rp.read_text(encoding="utf-8", errors="replace") if rp.is_file() else ""
    add("revised_draft_exists", bool(text.strip()))
    missing = [x for x in gt["must_contain"] if x not in text]
    add("required_text_preserved_or_added", not missing, missing)
    forbidden = [x for x in gt["forbidden_terms"] if x.lower() in text.lower()]
    add("banned_hype_removed", not forbidden, forbidden)
    add("markdown_headings_preserved", all(h in text for h in ["## Overview", "## Pilot Scope", "## Data Processing Addendum", "## No Warranty Expansion", "## Call To Action"]))

    rows: list[dict[str, str]] = []
    cp = w / "out" / "change_log.csv"
    if cp.is_file():
        try:
            rows = list(csv.DictReader(cp.open(newline="", encoding="utf-8")))
            add("change_log_parseable", True)
        except Exception as exc:
            add("change_log_parseable", False, str(exc))
    else:
        add("change_log_exists", False, "missing")
    add("change_log_header_exact", bool(rows) and list(rows[0].keys()) == ["section", "change_type", "source_comment", "summary"], list(rows[0].keys()) if rows else None)
    blob = json.dumps(rows).lower()
    add("change_log_has_required_sources", all(src in blob for src in gt["change_log_sources"]), blob)
    add("change_log_maps_legal_comments", all(c.lower() in blob for c in gt["required_comments"]), blob)

    score = sum(c["pass"] for c in checks) / len(checks) if checks else 0.0
    return {"task": "030-word-revision-plan", "workspace": str(w), "outcome_score": round(score, 4), "checks": checks}
