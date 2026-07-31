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


def _norm_media(value: Any) -> Any:
    if value == "application/x-yaml":
        return "application/yaml"
    return value


def _norm_manifest_rows(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    out = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        r = dict(row)
        r["media_type"] = _norm_media(r.get("media_type"))
        # The prompt's example names nested archive output with "nested_payload",
        # but it does not explicitly prescribe the generated directory name.
        # Treat the common "payload" choice as equivalent while still checking
        # hashes, sizes, archive chains, and duplicate handling separately.
        logical_path = r.get("logical_path")
        if isinstance(logical_path, str):
            r["logical_path"] = logical_path.replace("incoming_a/payload/", "incoming_a/nested_payload/", 1)
        out.append(r)
    return out


def _safe_key(path: str) -> str:
    return path.replace("incoming_a/payload/", "incoming_a/nested_payload/", 1)


def _norm_chain(value: str) -> str:
    if value.startswith("incoming_a.zip/nested/"):
        return "incoming_a.zip>nested/" + value.split("incoming_a.zip/nested/", 1)[1]
    return value


def _reject_key(row: dict[str, str]) -> tuple[str, str, str]:
    raw_name = row.get("raw_name", "").replace("\n", "\\n")
    return (_norm_chain(row.get("archive_chain", "")), raw_name, row.get("reason", ""))


def score_workspace(workspace: Path) -> dict[str, Any]:
    truth = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    for rel, expected in truth["source_hashes"].items():
        path = workspace / rel
        _add(checks, "source_" + Path(rel).name, f"{rel} unchanged", path.is_file() and _sha256(path) == expected, 1.0)

    try:
        manifest = json.loads((workspace / "out" / "manifest.json").read_text(encoding="utf-8"))
        files = manifest.get("files")
        _add(checks, "manifest_parse", "manifest.json parses as object", isinstance(manifest, dict), 1.0)
    except Exception as exc:
        files = None
        _add(checks, "manifest_parse", "manifest.json parses as object", False, 1.0, str(exc))
    actual_manifest = _norm_manifest_rows(files)
    expected_manifest = _norm_manifest_rows(truth["manifest_files"])
    if isinstance(actual_manifest, list) and isinstance(expected_manifest, list):
        actual_set = {json.dumps(r, sort_keys=True) for r in actual_manifest}
        expected_set = {json.dumps(r, sort_keys=True) for r in expected_manifest}
        required_ok = expected_set <= actual_set
        no_extra_ok = actual_set == expected_set
    else:
        required_ok = no_extra_ok = False
    _add(checks, "manifest_required", "manifest contains all required safe files with hashes, sizes, and chains", required_ok, 6.0, repr(files))
    _add(checks, "manifest_no_extra", "manifest has no extra accepted files", no_extra_ok, 2.0, repr(files))
    sorted_ok = isinstance(files, list) and files == sorted(files, key=lambda r: r.get("logical_path", ""))
    _add(checks, "manifest_sorted", "manifest sorted by logical_path", sorted_ok, 1.0)

    safe_root = workspace / "out" / "extracted_safe"
    actual_safe = {}
    if safe_root.is_dir():
        for p in safe_root.rglob("*"):
            if p.is_file():
                actual_safe[_safe_key(p.relative_to(safe_root).as_posix())] = _sha256(p)
    expected_safe = truth["safe_files"]
    required_safe_ok = all(actual_safe.get(path) == digest for path, digest in expected_safe.items())
    no_extra_safe_ok = actual_safe == expected_safe
    _add(checks, "safe_files_required", "extracted_safe contains all required safe files with hashes", required_safe_ok, 4.0, repr(actual_safe))
    _add(checks, "safe_files_no_extra", "extracted_safe has no extra files", no_extra_safe_ok, 1.0, repr(actual_safe))
    escaped = [workspace.parent / "escape.txt", workspace / "escape.txt", workspace / "out" / "escape.txt"]
    _add(checks, "no_escape_file", "path traversal did not materialize escape.txt", not any(p.exists() for p in escaped), 2.0)

    try:
        with (workspace / "out" / "rejected_entries.csv").open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fields = reader.fieldnames
        _add(checks, "reject_read", "rejected_entries.csv parses", True, 1.0)
    except Exception as exc:
        rows, fields = [], None
        _add(checks, "reject_read", "rejected_entries.csv parses", False, 1.0, str(exc))
    _add(checks, "reject_header", "reject header exact", fields == ["archive_chain", "raw_name", "reason", "normalized_candidate"], 1.0, repr(fields))
    expected = truth["rejected_entries"]
    expected_keys = set(_reject_key(r) for r in expected)
    expected_core_keys = set(_reject_key(r) for r in expected if r.get("reason") != "duplicate_logical_path")
    expected_duplicate_keys = set(_reject_key(r) for r in expected if r.get("reason") == "duplicate_logical_path")
    actual_keys = set(_reject_key(r) for r in rows)
    # normalized_candidate is helpful audit metadata, but the prompt does not define how to fill it
    # for rejected absolute/traversal paths. The security-critical contract is the raw entry,
    # archive chain, and rejection reason, with no extra/missing rows.
    _add(checks, "reject_core_required", "reject rows include every required unsafe/unsupported entry and reason", expected_core_keys <= actual_keys, 4.0, repr(rows))
    _add(checks, "reject_duplicate_required", "reject rows include duplicate logical path entries", expected_duplicate_keys <= actual_keys, 1.0, repr(rows))
    _add(checks, "reject_no_extra", "reject rows have no extra rejected entries", actual_keys <= expected_keys, 1.0, repr(rows))

    summary = (workspace / "out" / "audit_summary.md").read_text(encoding="utf-8", errors="replace").lower() if (workspace / "out" / "audit_summary.md").is_file() else ""
    _add(checks, "summary_terms", "audit summary mentions key rejection classes", all(t in summary for t in truth["required_summary_terms"]), 2.0)
    score = sum(c["weight"] for c in checks if c["pass"]) / sum(c["weight"] for c in checks)
    return {"task": "077-archive-manifest-defense", "workspace": str(workspace), "checks": checks, "outcome_score": score}
