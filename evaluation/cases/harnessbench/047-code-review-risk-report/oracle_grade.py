from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = json.loads((_TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
_HASHES = {
    "diff.patch": "e23f17aa96d248aedcacf48b904ccca9",
    "snippets/api.py": "48ba385d76c2b0dd7c3635b2002032e5",
    "snippets/db.py": "7e41dff3bf0adcae5f35e22acf245873",
    "snippets/logging.py": "6d68a67bfb5e75a33a60a9b8053d6838"
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    shape_score = 0.0
    finding_score = 0.0
    action_score = 0.0
    path = w / "out" / "review_findings.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            findings = data.get("findings", [])
            shape_score = 1.0 if isinstance(findings, list) and len(findings) >= 8 and isinstance(data.get("summary", ""), str) else 0.0
            add("json_shape", shape_score == 1.0, 0.18, {"findings": len(findings) if isinstance(findings, list) else None})
            finding_hits: dict[str, float] = {}
            used_indices: set[int] = set()
            for expected in _GT["expected_findings"]:
                best = 0.0
                best_idx = -1
                for idx, finding in enumerate(findings if isinstance(findings, list) else []):
                    if idx in used_indices:
                        continue
                    if not isinstance(finding, dict):
                        continue
                    text = json.dumps(finding, ensure_ascii=False).lower()
                    file_ok = str(finding.get("file", "")).lower() == expected["file"].lower()
                    term_score = sum(term.lower() in text for term in expected["terms"]) / len(expected["terms"])
                    rec = str(finding.get("recommendation", "")).lower()
                    rec_score = min(sum(term.lower() in rec for term in expected["recommendation_terms"]) / 2, 1.0)
                    severity_ok = str(finding.get("severity", "")).lower() in {"critical", "high", "medium"}
                    score = 0.35 * file_ok + 0.35 * term_score + 0.20 * rec_score + 0.10 * severity_ok
                    if score > best:
                        best = score
                        best_idx = idx
                if best_idx >= 0 and best >= 0.75:
                    used_indices.add(best_idx)
                finding_hits[expected["id"]] = round(best, 3)
            all_text = json.dumps(findings, ensure_ascii=False).lower()
            false_positive_ok = not any(term in all_text for term in _GT.get("false_positive_terms", []))
            finding_score = 0.90 * (sum(finding_hits.values()) / len(finding_hits)) + 0.10 * false_positive_ok
            add("finding_coverage", finding_score >= 0.75, 0.56, {"finding_hits": finding_hits})
            severity_hits = 0
            test_hits = 0
            for expected in _GT["expected_findings"]:
                for finding in findings if isinstance(findings, list) else []:
                    if not isinstance(finding, dict):
                        continue
                    text = json.dumps(finding, ensure_ascii=False).lower()
                    if str(finding.get("file", "")).lower() != expected["file"].lower():
                        continue
                    if sum(term.lower() in text for term in expected["terms"]) < 2:
                        continue
                    if str(finding.get("severity", "")).lower() == expected.get("severity", "").lower():
                        severity_hits += 1
                    test_text = str(finding.get("test", "")).lower()
                    if test_text and sum(term.lower() in test_text for term in expected.get("test_terms", [])) >= 2:
                        test_hits += 1
                    break
            expected_count = len(_GT["expected_findings"])
            add("severity_exactness", severity_hits >= expected_count, 0.10, {"severity_hits": severity_hits, "expected": expected_count})
            add("test_coverage", test_hits >= expected_count, 0.18, {"test_hits": test_hits, "expected": expected_count})
            rec_hits = sum(
                bool(str(item.get("recommendation", "")).strip())
                and bool(str(item.get("evidence", "")).strip())
                and str(item.get("severity", "")).lower() in {"critical", "high", "medium", "low"}
                for item in findings
                if isinstance(item, dict)
            )
            action_score = min(rec_hits / 8, 1.0)
            add("actionability", action_score >= 0.75, 0.16, {"recommendation_evidence_hits": rec_hits})
        except Exception as exc:
            add("json_shape", False, 0.18, str(exc))
    else:
        add("json_shape", False, 0.18, "missing out/review_findings.json")

    base = w / "in" / "review"
    intact = [(_md5(base / rel) == digest) for rel, digest in _HASHES.items()]
    integrity = sum(intact) / len(intact)
    add("fixture_integrity", integrity == 1.0, 0.10, {"score": integrity})
    severity_score = 1.0 if any(c["id"] == "severity_exactness" and c["pass"] for c in checks) else 0.0
    test_score = 1.0 if any(c["id"] == "test_coverage" and c["pass"] for c in checks) else 0.0
    total = shape_score * 0.10 + finding_score * 0.35 + action_score * 0.10 + integrity * 0.10 + severity_score * 0.10 + test_score * 0.25
    strong_findings = sum(1 for score in locals().get("finding_hits", {}).values() if score >= 0.75)
    if strong_findings < 6:
        total = min(total, 0.59)
    elif strong_findings < 8:
        total = min(total, 0.74)
    thresholds = _GT["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "047-code-review-risk-report", "outcome_score": round(total, 4), "level": level, "checks": checks}
