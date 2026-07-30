from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_DEFAULT_GT = _TASK_DIR / "ground_truth.json"


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = []
        for row in reader:
            if None in row:
                raise ValueError("CSV rows with comma-containing fields must quote those fields")
            clean = {k: (v or "").strip() for k, v in row.items()}
            rows.append(clean)
        return fieldnames, rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixtures_unchanged(workspace: Path, gt: dict[str, Any]) -> bool:
    for rel, digest in gt.get("fixture_hashes", {}).items():
        candidate = workspace / "in" / rel
        if not candidate.is_file() or _sha256(candidate) != digest:
            return False
    return True


def score_workspace(workspace: str | Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((ground_truth_path or _DEFAULT_GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    _add(checks, "definitions_present", (w / "in" / "metric_definitions.md").is_file(), 0.05, "missing definitions")
    _add(checks, "dashboard_present", (w / "in" / "dashboard_export.csv").is_file(), 0.05, "missing dashboard export")
    _add(checks, "fixtures_unchanged", _fixtures_unchanged(w, gt), 0.08, "one or more input files are missing or modified")
    out = w / gt["output"]
    equivalence_audit = w / gt.get("equivalence_audit", "out/equivalence_audit.json")
    _add(checks, "output_exists", out.is_file(), 0.10, "missing out/metric_audit.csv")
    _add(checks, "equivalence_audit_exists", equivalence_audit.is_file(), 0.08, "missing out/equivalence_audit.json")
    if out.is_file():
        try:
            header, rows = _read_csv(out)
            rows_sorted = sorted(rows, key=lambda r: r.get("metric_name", ""))
            expected = sorted(gt["rows"], key=lambda r: r["metric_name"])
            names = {r.get("metric_name", "") for r in rows}
            _add(checks, "exact_header", header == gt["header"], 0.10, f"got {header}")
            _add(checks, "only_mismatches", not (names & set(gt["matching_metrics"])) and names == {r["metric_name"] for r in expected}, 0.18, f"got {sorted(names)}")
            _add(checks, "exact_rows", rows_sorted == expected, 0.32, f"got {rows_sorted}")
            severity_expected = {r["metric_name"]: r["severity"] for r in expected}
            severity_got = {r.get("metric_name", ""): r.get("severity", "") for r in rows}
            _add(checks, "severity_mapping", severity_got == severity_expected, 0.08, f"got severities {severity_got}")
            _add(checks, "equivalent_formulas_not_flagged", not (names & set(gt["matching_metrics"])), 0.08, f"equivalent metrics were flagged: {sorted(names & set(gt['matching_metrics']))}")
            low_names = {r["metric_name"] for r in rows if r.get("severity") == "low"}
            _add(checks, "low_severity_present", {"Marketing Opt-in Rate", "Inventory Stockout Count"} <= low_names, 0.04, f"got lows {sorted(low_names)}")
            medium_names = {r["metric_name"] for r in rows if r.get("severity") == "medium"}
            _add(checks, "window_medium_present", "Rolling Active Users" in medium_names, 0.04, f"got mediums {sorted(medium_names)}")
        except Exception as exc:
            _add(checks, "csv_readable", False, 0.40, str(exc))
    else:
        _add(checks, "exact_header", False, 0.10, "missing out/metric_audit.csv")
        _add(checks, "only_mismatches", False, 0.18, "missing out/metric_audit.csv")
        _add(checks, "exact_rows", False, 0.32, "missing out/metric_audit.csv")
        _add(checks, "severity_mapping", False, 0.08, "missing out/metric_audit.csv")
        _add(checks, "equivalent_formulas_not_flagged", False, 0.08, "missing out/metric_audit.csv")
        _add(checks, "low_severity_present", False, 0.04, "missing out/metric_audit.csv")
        _add(checks, "window_medium_present", False, 0.04, "missing out/metric_audit.csv")
    if equivalence_audit.is_file():
        try:
            data = json.loads(equivalence_audit.read_text(encoding="utf-8"))
            expected = gt["equivalence_audit_expected"]
            _add(checks, "equivalence_audit_keys", isinstance(data, dict) and set(data) == set(expected), 0.04, f"got keys {sorted(data) if isinstance(data, dict) else type(data)}")
            _add(checks, "equivalence_audit_mismatches", data.get("mismatched_metrics") == expected["mismatched_metrics"], 0.10, f"got {data.get('mismatched_metrics')}")
            _add(checks, "equivalence_audit_matching_metrics", data.get("semantically_matching_metrics") == expected["semantically_matching_metrics"], 0.10, f"got {data.get('semantically_matching_metrics')}")
            _add(checks, "equivalence_audit_severities", data.get("severity_by_metric") == expected["severity_by_metric"], 0.10, f"got {data.get('severity_by_metric')}")
            _add(checks, "equivalence_audit_reason_classes", data.get("reason_class_by_metric") == expected["reason_class_by_metric"], 0.16, f"got {data.get('reason_class_by_metric')}")
        except Exception as exc:
            _add(checks, "equivalence_audit_parseable", False, 0.20, str(exc))
    else:
        for cid, weight in [
            ("equivalence_audit_keys", 0.04),
            ("equivalence_audit_mismatches", 0.10),
            ("equivalence_audit_matching_metrics", 0.10),
            ("equivalence_audit_severities", 0.10),
            ("equivalence_audit_reason_classes", 0.16),
        ]:
            _add(checks, cid, False, weight, "missing out/equivalence_audit.json")
    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    if any(c["id"].startswith("equivalence_audit_") and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "052-metric-definition-audit", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
