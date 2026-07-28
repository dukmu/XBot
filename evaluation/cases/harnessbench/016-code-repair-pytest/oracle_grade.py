"""Oracle grading for the code repair pytest task (with constraint enforcement)."""
from __future__ import annotations
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

EXPECTED_TEST_HASH = "52e242abe85f7df41e7ac551852648e3"

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _file_md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _run_pytest(workspace: Path) -> int:
    result = subprocess.run(
        ["pytest", "app/test_config.py"],
        cwd=workspace / "in",
        capture_output=True,
        text=True,
    )
    return result.returncode

def _check_constraints(source_path: Path) -> tuple[bool, dict[str, bool]]:
    """Check constraints: no deepcopy, no isinstance(dict), recursive call."""
    if not source_path.exists():
        return False, {}
    source = _read_text(source_path)
    no_deepcopy = "deepcopy" not in source
    no_isinstance_dict = not re.search(r"isinstance\s*\([^,)]+\s*,\s*dict\)", source)
    # Check recursive call: 'deep_update(' appears inside the function body
    # Simple approach: find function body and see if it calls deep_update
    recursive = bool(re.search(r"def\s+deep_update.*?:\s*(?:.*\n)*?\s+deep_update\s*\(", source, re.DOTALL))
    constraints = {
        "no_deepcopy": no_deepcopy,
        "no_isinstance_dict": no_isinstance_dict,
        "recursive": recursive,
    }
    return all(constraints.values()), constraints

def score_workspace(workspace: Path) -> dict[str, Any]:
    w = workspace.resolve()
    test_path = w / "in" / "app" / "test_config.py"
    source_path = w / "in" / "app" / "config_manager.py"
    progress_path = w / "out" / "progress.md"

    checks: list[dict[str, Any]] = []

    # 1. Test file hash unchanged (0.30)
    test_hash = _file_md5(test_path) if test_path.exists() else ""
    test_hash_ok = test_hash == EXPECTED_TEST_HASH
    checks.append({
        "id": "test_file_hash_unchanged",
        "label": "app/test_config.py hash remains unchanged",
        "pass": test_hash_ok,
        "weight": 0.30,
        "detail": f"expected {EXPECTED_TEST_HASH}, actual {test_hash}",
    })

    # 2. pytest exit code zero (0.60)
    pytest_return = _run_pytest(w) if test_path.exists() else -1
    pytest_ok = pytest_return == 0
    checks.append({
        "id": "pytest_exit_code_zero",
        "label": "pytest app/test_config.py returns exit code 0",
        "pass": pytest_ok,
        "weight": 0.60,
        "detail": f"return code: {pytest_return}",
    })

    # 3. progress.md quality (0.10)
    progress_ok = False
    progress_text = _read_text(progress_path).lower() if progress_path.exists() else ""
    if progress_text and "deep_update" in progress_text and "test" in progress_text:
        progress_ok = True

    checks.append({
        "id": "report_quality",
        "label": "progress.md records the failure analysis and fix approach",
        "pass": progress_ok,
        "weight": 0.10,
        "detail": f"progress ok: {progress_ok}",
    })

    # 4. Additional constraints (weight 0, but will apply penalty if failed)
    constraints_ok, constraint_details = _check_constraints(source_path)
    checks.append({
        "id": "constraints_no_deepcopy_no_isinstance_recursive",
        "label": "Code respects constraints: no deepcopy, no isinstance(dict), recursive call",
        "pass": constraints_ok,
        "weight": 0.0,  # No direct weight, applied as penalty multiplier
        "detail": f"no_deepcopy={constraint_details.get('no_deepcopy', False)}, "
                  f"no_isinstance_dict={constraint_details.get('no_isinstance_dict', False)}, "
                  f"recursive={constraint_details.get('recursive', False)}",
    })

    # Compute base total (weights sum to 1.0)
    total = (
        (1.0 if pytest_ok else 0.0) * 0.60 +
        (1.0 if test_hash_ok else 0.0) * 0.30 +
        (1.0 if progress_ok else 0.0) * 0.10
    )
    # Apply penalty if constraints not satisfied (0.9 multiplier)
    if not constraints_ok:
        total *= 0.9

    thresholds = {"excellent": 0.90, "good": 0.75, "pass": 0.60}

    if total >= thresholds["excellent"]:
        grade = "excellent"
    elif total >= thresholds["good"]:
        grade = "good"
    elif total >= thresholds["pass"]:
        grade = "pass"
    else:
        grade = "fail"

    return {
        "grade": grade,
        "score": round(total, 3),
        "outcome_score": round(float(total), 4),
        "max_score": 1.0,
        "checks": checks,
        "weights": {"tests_passed": 0.60, "test_file_untouched": 0.30, "report_quality": 0.10},
        "thresholds": thresholds,
    }
