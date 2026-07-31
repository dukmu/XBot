from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = json.loads((_TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
_SOURCE_HASH = "d129f06347f714063f397d28db6200b6"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    project = w / "in" / "ordercalc"
    if not project.exists():
        project = w / "ordercalc"
    tests_dir = project / "tests"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    env = os.environ.copy()
    env["PYTHONPATH"] = str(project)
    result = subprocess.run(["python3", "-m", "pytest", "tests"], cwd=project, env=env, capture_output=True, text=True, timeout=20)
    pytest_score = 1.0 if result.returncode == 0 else 0.0
    add("pytest_passes", result.returncode == 0, 0.25, result.stdout[-1000:] + result.stderr[-1000:])

    source_path = project / "ordercalc" / "pricing.py"
    source_ok = source_path.is_file() and _md5(source_path) == _SOURCE_HASH

    mutation_score = 0.0
    mutation_details: list[str] = []
    mutants = [
        ("vip_discount_removed", 'subtotal *= Decimal("0.90")', 'subtotal *= Decimal("1.00")'),
        ("bulk_threshold_off_by_one", ">= 10", "> 10"),
        ("shipping_threshold_before_5000", "subtotal >= 5000", "subtotal > 5000"),
        ("round_half_even", "ROUND_HALF_UP", "ROUND_HALF_EVEN"),
        ("coupon_ignored", 'subtotal = max(Decimal("0"), subtotal - coupon)', 'subtotal = subtotal'),
        ("expedite_ignored", 'shipping += Decimal("1299")', 'shipping += Decimal("0")'),
        ("bulk_per_line_only", 'if sum(int(i["quantity"]) for i in items) >= 10:', 'if max(int(i["quantity"]) for i in items) >= 10:'),
        ("vip_wrong_rate", 'subtotal *= Decimal("0.90")', 'subtotal *= Decimal("0.95")'),
    ]
    killed = 0
    if result.returncode == 0 and source_ok:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_project = Path(tmp) / "ordercalc"
            shutil.copytree(project, tmp_project)
            pricing = tmp_project / "ordercalc" / "pricing.py"
            original = pricing.read_text(encoding="utf-8")
            for name, old, new in mutants:
                if old not in original:
                    mutation_details.append(f"{name}:setup_missing")
                    continue
                pricing.write_text(original.replace(old, new, 1), encoding="utf-8")
                menv = os.environ.copy()
                menv["PYTHONPATH"] = str(tmp_project)
                mres = subprocess.run(["python3", "-m", "pytest", "tests"], cwd=tmp_project, env=menv, capture_output=True, text=True, timeout=20)
                if mres.returncode != 0:
                    killed += 1
                    mutation_details.append(f"{name}:killed")
                else:
                    mutation_details.append(f"{name}:survived")
                pricing.write_text(original, encoding="utf-8")
    elif result.returncode != 0:
        mutation_details.append("skipped:baseline_pytest_failed")
    else:
        mutation_details.append("skipped:source_integrity_failed")
    mutation_score = killed / len(mutants)
    add("mutation_checks", mutation_score >= 0.75, 0.35, {"killed": killed, "details": mutation_details})

    test_text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in tests_dir.glob("test_*.py"))
    term_hits = sum(term.lower() in test_text.lower() for term in _GT["required_test_terms"])
    forbidden_hits = [pat for pat in _GT["forbidden_patterns"] if re.search(re.escape(pat), test_text, re.IGNORECASE)]
    assertion_count = len(re.findall(r"\bassert\b|pytest\.raises", test_text))
    required_behaviors = [
        "coupon_cents" in test_text,
        "expedite" in test_text,
        "bulk" in test_text.lower(),
        "vip" in test_text.lower(),
        "ValueError" in test_text,
    ]
    coverage_score = 0.55 * min(term_hits / len(_GT["required_test_terms"]), 1.0) + 0.25 * min(assertion_count / 10, 1.0) + 0.10 * (sum(required_behaviors) / len(required_behaviors)) + 0.10 * (not forbidden_hits)
    add("test_intent", coverage_score >= 0.70, 0.25, {"term_hits": term_hits, "assertions": assertion_count, "forbidden": forbidden_hits})

    intent_path = tests_dir / "TEST_INTENT.md"
    intent_text = intent_path.read_text(encoding="utf-8", errors="replace").lower() if intent_path.is_file() else ""
    intent_score = sum(term.lower() in intent_text for term in _GT.get("intent_terms", [])) / max(len(_GT.get("intent_terms", [])), 1)
    add("test_intent_doc", intent_score >= 0.85, 0.08, {"score": round(intent_score, 4)})

    add("source_integrity", source_ok, 0.12, "ordercalc/pricing.py must not change")
    total = pytest_score * 0.23 + mutation_score * 0.34 + coverage_score * 0.23 + intent_score * 0.08 + (1.0 if source_ok else 0.0) * 0.12
    if intent_score < 0.60:
        total = min(total, 0.88)
    thresholds = _GT["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "040-test-coverage-fill", "outcome_score": round(total, 4), "level": level, "checks": checks}
