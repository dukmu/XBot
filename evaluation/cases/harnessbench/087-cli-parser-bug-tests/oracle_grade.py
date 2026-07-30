from __future__ import annotations

import csv
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


EXISTING_TEST_HASH = "40701e35fab5168e6d88ef65cd6428ae"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    project = Path(workspace).resolve() / "in" / "csvtool"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    env = os.environ.copy()
    env["PYTHONPATH"] = str(project)
    result = subprocess.run([sys.executable, "-m", "pytest", "tests"], cwd=project, env=env, capture_output=True, text=True, timeout=20)
    pytest_score = 1.0 if result.returncode == 0 else 0.0
    add("pytest", result.returncode == 0, 0.25, result.stdout[-1000:] + result.stderr[-1000:])

    hidden_score = 0.0
    try:
        cmd = [sys.executable, "-m", "csvtool.cli", "samples/orders.csv", "--where", "status=paid", "--select", "id,customer,total", "--sort", "-created_at"]
        proc = subprocess.run(cmd, cwd=project, env=env, capture_output=True, text=True, timeout=10)
        rows = list(csv.DictReader(proc.stdout.splitlines())) if proc.stdout.strip() else []
        desc_ok = rows == [
            {"id": "o1", "customer": "Ava, Inc", "total": "1200"},
            {"id": "o3", "customer": "Core, Labs", "total": "750"},
        ]
        empty = subprocess.run([sys.executable, "-m", "csvtool.cli", "samples/orders.csv", "--where", "status=missing", "--select", "id,total"], cwd=project, env=env, capture_output=True, text=True, timeout=10)
        multi = subprocess.run([sys.executable, "-m", "csvtool.cli", "samples/orders.csv", "--where", "status=paid", "--where", "total=750", "--select", "id,total"], cwd=project, env=env, capture_output=True, text=True, timeout=10)
        multi_rows = list(csv.DictReader(multi.stdout.splitlines())) if multi.stdout.strip() else []
        numeric = subprocess.run([sys.executable, "-m", "csvtool.cli", "samples/orders.csv", "--select", "id,total", "--sort", "total"], cwd=project, env=env, capture_output=True, text=True, timeout=10)
        numeric_rows = list(csv.DictReader(numeric.stdout.splitlines())) if numeric.stdout.strip() else []
        bad = subprocess.run([sys.executable, "-m", "csvtool.cli", "samples/orders.csv", "--where", "status"], cwd=project, env=env, capture_output=True, text=True, timeout=10)
        missing = subprocess.run([sys.executable, "-m", "csvtool.cli", "samples/orders.csv", "--select", "id,missing"], cwd=project, env=env, capture_output=True, text=True, timeout=10)
        hidden_score = sum([
            proc.returncode == 0 and desc_ok,
            empty.returncode == 0 and empty.stdout.strip() == "id,total",
            multi.returncode == 0 and multi_rows == [{"id": "o3", "total": "750"}],
            numeric.returncode == 0 and [r.get("id") for r in numeric_rows] == ["o2", "o3", "o1"],
            bad.returncode != 0 and "where" in bad.stderr.lower(),
            missing.returncode != 0 and "field" in missing.stderr.lower(),
        ]) / 6
        add("hidden_cli_behavior", hidden_score >= 1.0, 0.35, {"score": hidden_score, "stdout": proc.stdout, "bad_stderr": bad.stderr})
    except Exception as exc:
        add("hidden_cli_behavior", False, 0.35, str(exc))

    test_ok = (project / "tests" / "test_cli_existing.py").is_file() and _md5(project / "tests" / "test_cli_existing.py") == EXISTING_TEST_HASH
    add("existing_test_integrity", test_ok, 0.15)

    regression = project / "tests" / "test_cli_regression.py"
    reg_text = regression.read_text(encoding="utf-8", errors="replace").lower() if regression.is_file() else ""
    reg_score = sum(term in reg_text for term in ["quoted", "empty", "-created_at", "bad", "where", "missing", "numeric"]) / 7
    add("regression_tests_added", reg_score >= 0.6, 0.10, {"score": reg_score})

    source = (project / "csvtool" / "cli.py").read_text(encoding="utf-8", errors="replace") + (project / "csvtool" / "filtering.py").read_text(encoding="utf-8", errors="replace")
    quality = (
        0.35 * ("import csv" in source)
        + 0.25 * ("split(',')" not in source and ".split(\",\")" not in source)
        + 0.20 * ("reverse" in source or "startswith(\"-\")" in source or "startswith('-')" in source)
        + 0.10 * ("append" in source or "action=\"append\"" in source)
        + 0.10 * ("pandas" not in source and "click" not in source)
    )
    add("implementation_quality", quality >= 0.75, 0.10, {"score": round(quality, 4)})

    total = pytest_score * 0.25 + hidden_score * 0.35 + (1.0 if test_ok else 0.0) * 0.15 + reg_score * 0.10 + quality * 0.10
    if hidden_score < 1.0:
        total = min(total, 0.65)
    if not test_ok:
        total = min(total, 0.55)
    level = "excellent" if total >= 0.9 else "good" if total >= 0.75 else "pass" if total >= 0.6 else "fail"
    return {"task": "087-cli-parser-bug-tests", "outcome_score": round(total, 4), "level": level, "checks": checks}
