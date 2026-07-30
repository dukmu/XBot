from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _add(checks: list[dict[str, Any]], cid: str, label: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "label": label, "pass": ok, "weight": weight, "detail": None if ok else detail})


def _load_yaml(path: Path) -> Any:
    import yaml  # type: ignore
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def score_workspace(workspace: Path) -> dict[str, Any]:
    truth = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    try:
        records = json.loads((workspace / "out" / "canonical_records.json").read_text(encoding="utf-8"))
    except Exception as exc:
        records = None
        _add(checks, "json_parse", "canonical JSON parses", False, 1.0, str(exc))
    else:
        _add(checks, "json_parse", "canonical JSON parses", isinstance(records, list), 1.0)
    _add(checks, "json_exact", "canonical JSON exact", records == truth["canonical_records"], 6.0, repr(records))
    if isinstance(records, list):
        type_ok = isinstance(records[1].get("record_id"), str) and records[1].get("record_id") == "B-007" and records[1].get("note") is None and isinstance(records[0].get("tags"), list)
    else:
        type_ok = False
    _add(checks, "type_preservation", "string ids, nulls, arrays preserved", type_ok, 2.0)

    try:
        y = _load_yaml(workspace / "out" / "canonical_records.yaml")
    except Exception as exc:
        y = None
        _add(checks, "yaml_parse", "canonical YAML parses", False, 1.0, str(exc))
    else:
        _add(checks, "yaml_parse", "canonical YAML parses", True, 1.0)
    if isinstance(y, dict) and "records" in y:
        y_records = y["records"]
    else:
        y_records = y
    _add(checks, "yaml_semantics", "canonical YAML semantics match JSON", y_records == truth["canonical_records"], 4.0, repr(y_records))

    csv_path = workspace / "out" / "canonical_records.csv"
    actual_csv = csv_path.read_text(encoding="utf-8") if csv_path.is_file() else ""
    _add(checks, "csv_exact", "canonical CSV exact", actual_csv == truth["canonical_csv"], 4.0, repr(actual_csv))
    try:
        report = json.loads((workspace / "out" / "conversion_report.json").read_text(encoding="utf-8"))
    except Exception as exc:
        report = {}
        _add(checks, "report_parse", "conversion_report parses", False, 1.0, str(exc))
    else:
        _add(checks, "report_parse", "conversion_report parses", isinstance(report, dict), 1.0)
    dumped = json.dumps(report, ensure_ascii=False).lower()
    _add(checks, "report_terms", "report mentions all sources and conflicts", all(t in dumped for t in truth["report_terms"]), 2.0)
    actual_conflicts = {
        (c.get("record_id"), c.get("field"), c.get("chosen_source") or c.get("resolved_from") or c.get("resolved_by"))
        for c in report.get("conflicts", [])
        if isinstance(c, dict)
    }
    expected_conflicts = {(c["record_id"], c["field"], c["chosen_source"]) for c in truth["conflicts"]}
    _add(checks, "conflicts", "expected conflicts reported", expected_conflicts <= actual_conflicts, 2.0, repr(report.get("conflicts")))
    score = sum(c["weight"] for c in checks if c["pass"]) / sum(c["weight"] for c in checks)
    return {"task": "080-schema-roundtrip-conversion", "workspace": str(workspace), "checks": checks, "outcome_score": score}
