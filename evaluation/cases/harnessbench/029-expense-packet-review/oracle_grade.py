from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _money_eq(a: str, b: str) -> bool:
    try:
        return abs(float(str(a).replace("$", "")) - float(b)) < 0.01
    except Exception:
        return False


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or _GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, detail: Any = None) -> None:
        checks.append({"id": cid, "label": cid.replace("_", " "), "pass": bool(ok), "weight": 1.0, "detail": detail})

    rows: list[dict[str, str]] = []
    p = w / "out" / "reimbursement_audit.csv"
    if p.is_file():
        try:
            rows = list(csv.DictReader(p.open(newline="", encoding="utf-8")))
            add("audit_csv_parseable", True)
        except Exception as exc:
            add("audit_csv_parseable", False, str(exc))
    else:
        add("audit_csv_exists", False, "missing")
    add("audit_header_exact", bool(rows) and list(rows[0].keys()) == ["receipt_id", "employee", "amount", "category", "issue", "allowed_amount", "recommended_action"], list(rows[0].keys()) if rows else None)
    add("exactly_five_issue_rows", len(rows) == 5, len(rows))
    for exp in gt["expected_issues"]:
        hit = [r for r in rows if r.get("receipt_id") == exp["receipt_id"] and exp["issue_contains"].lower() in r.get("issue", "").lower() and _money_eq(r.get("allowed_amount", ""), exp["allowed_amount"])]
        add(f"issue_{exp['receipt_id']}", bool(hit), exp)
    add("no_currency_symbols_in_amounts", bool(rows) and all("$" not in (r.get("amount", "") + r.get("allowed_amount", "")) for r in rows))

    mp = w / "out" / "missing_docs.md"
    text = mp.read_text(encoding="utf-8", errors="replace") if mp.is_file() else ""
    add("missing_docs_exists", bool(text.strip()))
    add("missing_docs_lists_required_receipts", all(rid in text for rid in gt["missing_doc_receipts"]), text)
    add("missing_docs_total_claimed", gt["total_claimed"] in text or "$" + gt["total_claimed"] in text)
    add("missing_docs_total_allowed", gt["total_allowed"] in text or "$" + gt["total_allowed"] in text)

    score = sum(c["pass"] for c in checks) / len(checks) if checks else 0.0
    return {"task": "029-expense-packet-review", "workspace": str(w), "outcome_score": round(score, 4), "checks": checks}
