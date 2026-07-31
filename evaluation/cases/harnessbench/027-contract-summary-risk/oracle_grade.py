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
    p = w / "out" / "risk_clauses.csv"
    if p.is_file():
        try:
            rows = list(csv.DictReader(p.open(newline="", encoding="utf-8")))
            add("risk_csv_parseable", True)
        except Exception as exc:
            add("risk_csv_parseable", False, str(exc))
    else:
        add("risk_csv_exists", False, "missing")
    add("risk_csv_header_exact", bool(rows) and list(rows[0].keys()) == ["clause_id", "risk_type", "quote", "recommended_action", "severity"], list(rows[0].keys()) if rows else None)
    add("exactly_six_risk_rows", len(rows) == 6, len(rows))
    severities_ok = bool(rows) and all(r.get("severity") in {"High", "Medium", "Low"} for r in rows)
    add("severity_values_valid", severities_ok)
    for exp in gt["expected_risks"]:
        matches = [
            r for r in rows
            if r.get("clause_id") == exp["clause_id"]
            and exp["risk_type"].lower() in r.get("risk_type", "").lower()
            and exp["quote_contains"].lower() in r.get("quote", "").lower()
            and exp["action_contains"].lower() in r.get("recommended_action", "").lower()
        ]
        add(f"risk_{exp['clause_id']}_{exp['risk_type'].replace(' ', '_')}", bool(matches), exp)

    s = w / "out" / "contract_summary.md"
    text = s.read_text(encoding="utf-8", errors="replace") if s.is_file() else ""
    add("summary_exists", bool(text.strip()))
    missing = [t for t in gt["summary_terms"] if t.lower() not in text.lower()]
    add("summary_covers_key_terms", not missing, missing)
    add("summary_has_policy_risks_section", "policy risks" in text.lower())
    forbidden = [t for t in gt["forbidden_terms"] if t.lower() in text.lower()]
    add("summary_avoids_forbidden_advice", not forbidden, forbidden)

    score = sum(c["pass"] for c in checks) / len(checks) if checks else 0.0
    return {"task": "027-contract-summary-risk", "workspace": str(w), "outcome_score": round(score, 4), "checks": checks}
