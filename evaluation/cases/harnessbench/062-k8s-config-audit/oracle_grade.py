from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _source_unchanged(workspace: Path) -> bool:
    root = _TASK_DIR / "fixtures" / "in"
    src = workspace.resolve()
    if not src.is_dir():
        return True
    for original in root.rglob("*"):
        if not original.is_file():
            continue
        rel = original.relative_to(root)
        candidate = src / "in" / rel
        if candidate.is_file() and candidate.read_bytes() != original.read_bytes():
            return False
    return True


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    gt = _load_json(_GT)
    checks: list[dict[str, Any]] = []
    weights = gt["scoring"]["weights"]

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    csv_score = 0.0
    path = w / "out" / "k8s_audit.csv"
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            columns_ok = set(gt["required_columns"]).issubset(rows[0].keys() if rows else set())
            by_id = {str(row.get("check_id", "")).strip(): row for row in rows}
            per = 1.0 / len(gt["expected_findings"])
            for check_id, exp in gt["expected_findings"].items():
                row = by_id.get(check_id, {})
                text = " ".join(str(row.get(k, "")) for k in ("evidence", "recommendation", "resource"))
                keyword_hits = sum(1 for token in exp["keywords"] if token.lower() in text.lower())
                row_score = (
                    0.35 * (_norm(row.get("status")) == exp["status"])
                    + 0.25 * (_norm(row.get("severity")) == exp["severity"])
                    + 0.25 * min(keyword_hits / max(len(exp["keywords"]), 1), 1)
                    + 0.15 * bool(str(row.get("recommendation", "")).strip())
                )
                csv_score += per * row_score
            csv_score = min(1.0, 0.12 * columns_ok + 0.88 * csv_score)
            add("k8s_audit_csv", "k8s_audit.csv contains required findings", csv_score >= 0.70, weights["csv"], {"score": round(csv_score, 4), "rows": len(rows)})
        except Exception as exc:
            add("k8s_audit_parse", "k8s_audit.csv parseable", False, weights["csv"], str(exc))
    else:
        add("k8s_audit_missing", "k8s_audit.csv exists", False, weights["csv"], "missing")

    unchanged = _source_unchanged(w)
    add("source_unchanged", "fixtures are not modified", unchanged, weights["source_unchanged"])

    total = csv_score * weights["csv"] + float(unchanged) * weights["source_unchanged"]
    thresholds = gt["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "062-k8s-config-audit", "workspace": str(w), "outcome_score": round(total, 4), "level": level, "checks": checks}
