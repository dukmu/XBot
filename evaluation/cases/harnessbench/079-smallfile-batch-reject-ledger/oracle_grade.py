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


def _read_csv(path: Path) -> tuple[list[str] | None, list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def _record_matches(actual: Any, expected: dict[str, Any]) -> bool:
    if not isinstance(actual, dict):
        return False
    required = dict(expected)
    # The prompt defines order inputs without a status field. The benchmark's
    # canonical normalized order includes "active" for convenience, but omitting
    # that synthesized status should not invalidate otherwise correct order
    # normalization.
    if required.get("record_type") == "order" and required.get("status") == "active" and "status" not in actual:
        required.pop("status")
    for key, expected_value in required.items():
        actual_value = actual.get(key)
        if isinstance(expected_value, float) and isinstance(actual_value, str):
            try:
                actual_value = float(actual_value)
            except ValueError:
                pass
        if actual_value != expected_value:
            return False
    allowed_extra = {"source_path"}
    return set(actual) <= (set(expected) | allowed_extra)


def _norm_error_type(value: str) -> str:
    return {
        "unknown_extension": "unsupported_extension",
        "missing_required_fields": "missing_required_field",
    }.get(value, value)


def _summary_value(summary: dict[str, Any], key: str) -> Any:
    aliases = {
        "success_count": ("success_count", "records_created", "normalized_count", "normalized_records", "valid_records"),
        "reject_count": ("reject_count", "rejected_count", "rejected_records", "rejections", "invalid_files", "files_rejected"),
        "by_type": ("by_type", "records_by_type", "type_counts"),
    }
    for candidate in aliases.get(key, (key,)):
        if candidate in summary:
            return summary[candidate]
    return None


def score_workspace(workspace: Path) -> dict[str, Any]:
    truth = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    for rel, expected in truth["source_hashes"].items():
        path = workspace / rel
        _add(checks, "source_" + rel.replace("/", "_"), f"{rel} unchanged", path.is_file() and _sha256(path) == expected, 0.02)

    out_root = workspace / "out" / "normalized"
    actual_names = sorted(p.name for p in out_root.glob("*.json")) if out_root.is_dir() else []
    _add(checks, "file_set", "normalized file set exact", actual_names == sorted(truth["outputs"]), 3.0, repr(actual_names))
    for name, expected in truth["outputs"].items():
        try:
            actual = json.loads((out_root / name).read_text(encoding="utf-8"))
            _add(checks, "json_" + name, f"{name} parses", True, 0.5)
        except Exception as exc:
            actual = None
            _add(checks, "json_" + name, f"{name} parses", False, 0.5, str(exc))
        _add(checks, "content_" + name, f"{name} content matches required normalized fields", _record_matches(actual, expected), 2.0, repr(actual))

    try:
        fields, rows = _read_csv(workspace / "out" / "index.csv")
        _add(checks, "index_read", "index.csv parses", True, 1.0)
    except Exception as exc:
        fields, rows = None, []
        _add(checks, "index_read", "index.csv parses", False, 1.0, str(exc))
    _add(checks, "index_header", "index header exact", fields == ["source_path", "target_path", "record_type", "record_id", "status"], 1.0, repr(fields))
    expected_index_keys = [{k: r[k] for k in ("source_path", "target_path", "record_type", "record_id")} for r in truth["index_rows"]]
    actual_index_keys = [{k: r.get(k, "") for k in ("source_path", "target_path", "record_type", "record_id")} for r in rows]
    index_status_ok = all(r.get("status", "") != "" for r in rows) if len(rows) == len(truth["index_rows"]) else False
    _add(checks, "index_rows", "index rows identify the exact successful records in deterministic order", actual_index_keys == expected_index_keys and index_status_ok, 4.0, repr(rows))

    try:
        rfields, rrows = _read_csv(workspace / "out" / "reject_ledger.csv")
        _add(checks, "reject_read", "reject_ledger.csv parses", True, 1.0)
    except Exception as exc:
        rfields, rrows = None, []
        _add(checks, "reject_read", "reject_ledger.csv parses", False, 1.0, str(exc))
    _add(checks, "reject_header", "reject header exact", rfields == ["source_path", "error_type", "details"], 1.0, repr(rfields))
    expected_rejects = {(r["source_path"], _norm_error_type(r["error_type"])) for r in truth["reject_rows"]}
    actual_rejects = {(r.get("source_path", ""), _norm_error_type(r.get("error_type", ""))) for r in rrows}
    _add(checks, "reject_rows", "reject ledger includes every expected source/error", expected_rejects <= actual_rejects and len(rrows) == len(truth["reject_rows"]), 4.0, repr(rrows))

    try:
        summary = json.loads((workspace / "out" / "batch_summary.json").read_text(encoding="utf-8"))
    except Exception as exc:
        summary = {}
        _add(checks, "summary_parse", "batch_summary parses", False, 1.0, str(exc))
    else:
        _add(checks, "summary_parse", "batch_summary parses", isinstance(summary, dict), 1.0)
    for key, expected in truth["summary"].items():
        actual = _summary_value(summary, key)
        if key == "by_type" and actual is None and _summary_value(summary, "success_count") == sum(expected.values()):
            actual = expected
        _add(checks, "summary_" + key, f"summary {key} exact", actual == expected, 1.0, repr(actual))
    score = sum(c["weight"] for c in checks if c["pass"]) / sum(c["weight"] for c in checks)
    deliverables = [
        out_root.is_dir() and any(out_root.glob("*.json")),
        (workspace / "out" / "index.csv").is_file(),
        (workspace / "out" / "reject_ledger.csv").is_file(),
        (workspace / "out" / "batch_summary.json").is_file(),
    ]
    if not any(deliverables):
        score = min(score, 0.02)
    return {"task": "079-smallfile-batch-reject-ledger", "workspace": str(workspace), "checks": checks, "outcome_score": score}
