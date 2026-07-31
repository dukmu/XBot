from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = json.loads((_TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
_NOTES_HASH = "90e4ba65abbc8dfac8821654d50068ba"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    project = w / "in" / "depsvc"
    if not project.exists():
        project = w / "depsvc"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    req_text = (project / "requirements.txt").read_text(encoding="utf-8", errors="replace").strip()
    lines = [line.strip() for line in req_text.splitlines() if line.strip() and not line.strip().startswith("#")]
    forbidden = [term for term in _GT["forbidden_requirement_terms"] if term in req_text.lower()]
    raw_exact = req_text == _GT["expected_requirement"]
    req_score = 1.0 if lines == [_GT["expected_requirement"]] and not forbidden and raw_exact else 0.0
    add("requirements_minimal", req_score == 1.0, 0.25, {"lines": lines, "forbidden": forbidden, "raw_exact": raw_exact})

    env = os.environ.copy()
    env["PYTHONPATH"] = str(project)
    result = subprocess.run(["python3", "-m", "pytest", "tests"], cwd=project, env=env, capture_output=True, text=True, timeout=20)
    pytest_score = 1.0 if result.returncode == 0 else 0.0
    add("pytest", result.returncode == 0, 0.25, result.stdout[-1000:] + result.stderr[-1000:])

    code = (project / "slugger.py").read_text(encoding="utf-8", errors="replace")
    term_hits = sum(term in code for term in _GT["required_code_terms"])
    code_score = min(term_hits / len(_GT["required_code_terms"]), 1.0)
    behavior_score = 0.0
    try:
        spec = importlib.util.spec_from_file_location("slugger_under_test", project / "slugger.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sig = inspect.signature(mod.make_slug)
        params = list(sig.parameters.values())
        signature_ok = (
            [p.name for p in params] == ["title", "preserve_unicode", "separator"]
            and params[1].default is False
            and params[2].default == "-"
        )
        behavior_checks = [
            mod.make_slug("Café API") == "cafe-api",
            mod.make_slug("Café API", preserve_unicode=True) == "café-api",
            mod.make_slug("Hello Billing API", separator="_") == "hello_billing_api",
            mod.make_slug("Hello,   Billing___API") == "hello-billing-api",
            mod.make_slug("") == "",
            signature_ok,
        ]
        try:
            mod.make_slug("Bad Separator", separator="/")
            behavior_checks.append(False)
        except ValueError:
            behavior_checks.append(True)
        try:
            mod.make_slug("Bad Separator", separator="--")
            behavior_checks.append(False)
        except ValueError:
            behavior_checks.append(True)
        try:
            mod.make_slug()
            behavior_checks.append(False)
        except TypeError:
            behavior_checks.append(True)
        try:
            mod.make_slug(None)
            behavior_checks.append(False)
        except ValueError:
            behavior_checks.append(True)
        behavior_checks.append(mod.make_slug("Hello,   Billing___API") == "hello-billing-api")
        behavior_checks.append(mod.make_slug("Hello...Billing", separator="_") == "hello_billing")

        fake_calls: list[dict[str, Any]] = []
        fake_module = types.ModuleType("slugify")
        def fake_slugify(value: str, separator: str = "-", allow_unicode: bool = False) -> str:
            fake_calls.append({"value": value, "separator": separator, "allow_unicode": allow_unicode})
            return f"{value}|{separator}|{allow_unicode}"
        fake_module.slugify = fake_slugify
        old_slugify = sys.modules.get("slugify")
        sys.modules["slugify"] = fake_module
        try:
            fake_spec = importlib.util.spec_from_file_location("slugger_fake_dep", project / "slugger.py")
            assert fake_spec and fake_spec.loader
            fake_mod = importlib.util.module_from_spec(fake_spec)
            fake_spec.loader.exec_module(fake_mod)
            fake_result = fake_mod.make_slug("Café API", preserve_unicode=True, separator="_")
            behavior_checks.append(fake_result == "Café API|_|True")
            behavior_checks.append(fake_calls and fake_calls[-1] == {"value": "Café API", "separator": "_", "allow_unicode": True})
        finally:
            if old_slugify is None:
                sys.modules.pop("slugify", None)
            else:
                sys.modules["slugify"] = old_slugify
        behavior_score = sum(behavior_checks) / len(behavior_checks)
    except Exception:
        behavior_score = 0.0
    compat_score = 0.45 * code_score + 0.55 * behavior_score
    add("compat_code", compat_score >= 0.85, 0.25, {"term_hits": term_hits, "behavior_score": behavior_score})

    decision = project / "compat_decision.md"
    decision_text = decision.read_text(encoding="utf-8", errors="replace").lower() if decision.is_file() else ""
    decision_score = sum(term.lower() in decision_text for term in _GT["decision_terms"]) / len(_GT["decision_terms"])
    add("compat_decision", decision_score >= 0.80, 0.08, {"score": decision_score})
    matrix_score = 0.0
    matrix = project / "compat_matrix.json"
    try:
        matrix_data = json.loads(matrix.read_text(encoding="utf-8"))
        matrix_text = json.dumps(matrix_data, ensure_ascii=False).lower()
        term_hits = sum(term.lower() in matrix_text for term in _GT["matrix_terms"])
        if isinstance(matrix_data, list):
            cases = matrix_data
        elif isinstance(matrix_data, dict):
            cases = matrix_data.get("cases") or matrix_data.get("behavior_cases") or []
        else:
            cases = []
        matrix_score = 0.65 * (term_hits / len(_GT["matrix_terms"])) + 0.35 * (isinstance(cases, list) and len(cases) >= len(_GT["matrix_terms"]))
    except Exception:
        matrix_score = 0.0
    add("compat_matrix", matrix_score >= 0.85, 0.10, {"score": round(matrix_score, 4)})
    risks = project / "compat_risks.md"
    risk_text = risks.read_text(encoding="utf-8", errors="replace").lower() if risks.is_file() else ""
    risk_score = sum(term.lower() in risk_text for term in _GT.get("risk_terms", [])) / max(len(_GT.get("risk_terms", [])), 1)
    add("compat_risks", risk_score >= 0.80, 0.08, {"score": round(risk_score, 4)})
    notes_ok = _md5(project / "compat_notes.md") == _NOTES_HASH
    add("notes_integrity", notes_ok, 0.05)
    total = req_score * 0.22 + pytest_score * 0.24 + compat_score * 0.24 + decision_score * 0.08 + matrix_score * 0.09 + risk_score * 0.08 + (1.0 if notes_ok else 0.0) * 0.05
    if risk_score < 0.60:
        total = min(total, 0.86)
    thresholds = _GT["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "045-dependency-upgrade-compat", "outcome_score": round(total, 4), "level": level, "checks": checks}
