from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _add(checks: list[dict[str, Any]], cid: str, label: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "label": label, "pass": ok, "weight": weight, "detail": None if ok else detail})


def _workspace_rel(rel: str) -> Path:
    path = Path(rel)
    if path.parts and path.parts[0] == "fixtures":
        return Path(*path.parts[1:])
    return path


def _normalize_source_row(row: dict[str, str]) -> dict[str, str]:
    out = dict(row)
    source = out.get("source", "")
    if source.startswith("fixtures/"):
        out["source"] = source[len("fixtures/"):]
    return out


def score_workspace(workspace: Path) -> dict[str, Any]:
    truth = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    for rel, expected in truth["source_hashes"].items():
        path = workspace / _workspace_rel(rel)
        actual = _sha256(path) if path.is_file() else ""
        _add(checks, "source_" + Path(rel).name, f"{rel} unchanged", actual == expected, 1.0, f"got {actual!r}")

    normalized = workspace / "out" / "normalized"
    actual_names = sorted(p.name for p in normalized.iterdir()) if normalized.is_dir() else []
    expected_names = sorted(Path(p).name for p in truth["outputs"])
    _add(checks, "target_file_set", "normalized output file set is exact", actual_names == expected_names, 2.0, f"got {actual_names!r}")

    for rel, expected in truth["outputs"].items():
        path = workspace / rel
        if isinstance(expected, str):
            actual = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            _add(checks, "content_" + path.name, f"{rel} exact content", actual == expected, 2.0, f"got {actual!r}")
        else:
            try:
                actual_json = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                actual_json = None
                _add(checks, "json_" + path.name, f"{rel} valid JSON", False, 1.0, str(exc))
            else:
                _add(checks, "json_" + path.name, f"{rel} valid JSON", True, 1.0)
            _add(checks, "content_" + path.name, f"{rel} exact transformed data", actual_json == expected, 2.0, f"got {actual_json!r}")

    log_path = workspace / "out" / "rename_log.csv"
    try:
        with log_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames
    except Exception as exc:
        rows = []
        fieldnames = None
        _add(checks, "log_read", "rename_log.csv can be parsed", False, 1.0, str(exc))
    else:
        _add(checks, "log_read", "rename_log.csv can be parsed", True, 1.0)
    _add(checks, "log_header", "rename_log.csv header is exact", fieldnames == ["source", "target", "action"], 1.0, f"got {fieldnames!r}")
    expected_rows = [_normalize_source_row(row) for row in truth["rename_log_rows"]]
    _add(checks, "log_rows", "rename_log.csv rows are exact and sorted", rows == expected_rows, 3.0, f"got {rows!r}")

    err_path = workspace / "out" / "error_report.csv"
    try:
        with err_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            err_rows = list(reader)
            err_fields = reader.fieldnames
    except Exception as exc:
        err_rows = []
        err_fields = None
        _add(checks, "error_report_read", "error_report.csv can be parsed", False, 1.0, str(exc))
    else:
        _add(checks, "error_report_read", "error_report.csv can be parsed", True, 1.0)
    _add(checks, "error_report_header", "error_report.csv header is exact", err_fields == ["source", "row_or_record", "error_type", "details"], 1.0, f"got {err_fields!r}")
    expected_errors = {(r["source"], r["row_or_record"], r["error_type"]) for r in truth["error_rows"]}
    actual_errors = {(r.get("source", ""), r.get("row_or_record", ""), r.get("error_type", "")) for r in err_rows}
    _add(checks, "error_report_rows", "error_report.csv reports malformed and unsupported inputs", expected_errors <= actual_errors, 3.0, f"got {err_rows!r}")

    score = sum(c["weight"] for c in checks if c["pass"]) / sum(c["weight"] for c in checks)
    return {"task": "021-batch-rename-transform", "workspace": str(workspace), "checks": checks, "outcome_score": score}
