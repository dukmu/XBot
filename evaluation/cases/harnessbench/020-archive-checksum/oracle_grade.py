from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _check(checks: list[dict[str, Any]], cid: str, label: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "label": label, "pass": ok, "weight": weight, "detail": None if ok else detail})


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _workspace_rel(rel: str) -> Path:
    path = Path(rel)
    if path.parts and path.parts[0] == "fixtures":
        return Path(*path.parts[1:])
    return path


def score_workspace(workspace: Path) -> dict[str, Any]:
    task_dir = Path(__file__).resolve().parent
    truth = json.loads((task_dir / "ground_truth.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    # 原始归档文件未被修改
    for rel, expected_hash in truth["archive_hashes"].items():
        path = workspace / _workspace_rel(rel)
        actual = _sha256(path) if path.is_file() else ""
        _check(checks, "source_" + Path(rel).name, f"{rel} unchanged", actual == expected_hash, 1.0, f"got {actual!r}")

    # manifest.json 校验
    manifest_path = workspace / "out" / "manifest.json"
    manifest_ok = False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_ok = isinstance(manifest, dict)
        _check(checks, "manifest_parse", "out/manifest.json is valid JSON", manifest_ok, 1.0, None if manifest_ok else "not a dict")
    except Exception as exc:
        manifest = {}
        _check(checks, "manifest_parse", "out/manifest.json is valid JSON", False, 1.0, str(exc))


    if manifest_ok:
        expected_files = sorted(truth["manifest_files"], key=lambda x: (x["archive"], x["path"]))
        actual_files = manifest.get("files") if isinstance(manifest, dict) else None
        files_ok = actual_files == expected_files
        _check(checks, "manifest_files", "manifest lists exact archive/path/size/sha256 rows", files_ok, 5.0, f"got {actual_files!r}")

        sorted_ok = isinstance(actual_files, list) and actual_files == sorted(actual_files, key=lambda x: (x.get("archive", ""), x.get("path", "")))
        _check(checks, "manifest_sorted", "manifest files are sorted by archive then path", sorted_ok, 1.0)

    # mismatches.txt 读取与内容检查
    mismatch_path = workspace / "out" / "mismatches.txt"
    try:
        lines = [line.strip() for line in mismatch_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        _check(checks, "mismatch_read", "out/mismatches.txt can be read", True, 1.0)
    except Exception as exc:
        lines = []
        _check(checks, "mismatch_read", "out/mismatches.txt can be read", False, 1.0, str(exc))


    expected_mismatches = truth["mismatches"]
    mismatch_ok = lines == expected_mismatches
    _check(checks, "mismatch_exact", "mismatches.txt contains only expected failing entries", mismatch_ok, 4.0, f"got {lines!r}")

    # 计算总分
    total_weight = len(truth["archive_hashes"]) * 1.0 + 1.0 + 5.0 + 1.0 + 1.0 + 4.0
    passed_weight = sum(c["weight"] for c in checks if c["pass"])
    score = passed_weight / total_weight if total_weight > 0 else 0.0

    return {
        "task": "020-archive-checksum",
        "workspace": str(workspace),
        "checks": checks,
        "outcome_score": score,
        "grade": "excellent" if score >= 0.9 else "good" if score >= 0.75 else "pass" if score >= 0.6 else "fail",
    }
