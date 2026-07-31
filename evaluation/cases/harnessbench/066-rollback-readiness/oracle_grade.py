from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


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
    weights = gt["scoring"]["weights"]
    checks: list[dict[str, Any]] = []

    def add(cid: str, label: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": detail})

    blockers_score = 0.0
    path = w / "out" / "blockers.csv"
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            cols_ok = {"blocker_id", "severity", "owner", "evidence", "required_action"}.issubset(rows[0].keys() if rows else set())
            by_id = {str(r.get("blocker_id", "")).strip(): r for r in rows}
            per = 1.0 / len(gt["expected_blockers"])
            for blocker_id, exp in gt["expected_blockers"].items():
                row = by_id.get(blocker_id, {})
                text = " ".join(str(row.get(k, "")) for k in ("evidence", "required_action", "owner"))
                hits = sum(1 for kw in exp["keywords"] if kw.lower() in text.lower())
                blockers_score += per * (
                    0.35 * (str(row.get("severity", "")).strip().lower() == exp["severity"])
                    + 0.45 * min(hits / len(exp["keywords"]), 1)
                    + 0.20 * bool(str(row.get("required_action", "")).strip())
                )
            blockers_score = 0.10 * cols_ok + 0.90 * blockers_score
            add("blockers_csv", "blockers.csv lists all release blockers with evidence", blockers_score >= 0.70, weights["blockers"], {"score": round(blockers_score, 4), "rows": len(rows)})
        except Exception as exc:
            add("blockers_parse", "blockers.csv parseable", False, weights["blockers"], str(exc))
    else:
        add("blockers_missing", "blockers.csv exists", False, weights["blockers"], "missing")

    doc_score = 0.0
    doc = w / "out" / "rollback_readiness.md"
    if doc.is_file():
        text = doc.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        hits = sum(1 for kw in gt["readiness_keywords"] if kw.lower() in low)

        # 优化点：使用正则排除 "not executed" 或 "not yet executed" 造成的误杀
        unsafe_patterns = [
            r"(?<!not\s)(?<!not\syet\s)\b(already\s+executed|has\s+been\s+executed|have\s+executed)\b",
            r"\balready\s+rolled\s+back\b",
            r"\bsuccessfully\s+rolled\s+back\b",
            r"\brollback\s+completed\b"
        ]
        unsafe = any(bool(re.search(pat, low)) for pat in unsafe_patterns)

        doc_score = 0.85 * (hits / len(gt["readiness_keywords"])) + 0.15 * (not unsafe)
        add("readiness_doc", "rollback_readiness.md states blocked decision, metrics, approvals, and safety", doc_score >= 0.70, weights["readiness_doc"], {"score": round(doc_score, 4), "unsafe_claim": unsafe})
    else:
        add("readiness_doc_missing", "rollback_readiness.md exists", False, weights["readiness_doc"], "missing")

    unchanged = _source_unchanged(w)
    add("source_unchanged", "fixtures are not modified", unchanged, weights["source_unchanged"])
    total = blockers_score * weights["blockers"] + doc_score * weights["readiness_doc"] + float(unchanged) * weights["source_unchanged"]
    thresholds = gt["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "066-rollback-readiness", "workspace": str(w), "outcome_score": round(total, 4), "level": level, "checks": checks}