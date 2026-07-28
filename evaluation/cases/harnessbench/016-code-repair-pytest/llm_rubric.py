"""016-code-repair-pytest: process rubric aligned with grading/default_rubric (four standard scores)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_g = Path(__file__).resolve().parent.parent.parent / "grading" / "default_rubric.py"
_spec = importlib.util.spec_from_file_location("_bench_default_rubric", _g)
assert _spec and _spec.loader
_dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dr)

RUBRIC_SYSTEM = _dr.RUBRIC_SYSTEM

_REF = """
Task: run pytest on app/test_config.py; fix deep_update in app/config_manager.py for recursive dict merge; do not change tests; rerun until green. Document in out/progress.md.
""".strip()

USER_TEMPLATE = (
    "Task name: {task_name}\n\n"
    + _dr.RUBRIC_IGNORE_BOOTSTRAP_READS
    + "\n\n"
    + _REF
    + "\n\n"
    + _dr.RUBRIC_USER_OUTCOME_NOTICE
    + "\n\n"
    + """Evaluate the agent run from the **proxy trace JSON** below (criteria each 0.0-1.0):
- tool_use_appropriate: pytest/editor/exec usage appropriate; fix targets config_manager.py not tests.
- consistency: reproduce failure → localize bug → minimal recursive fix → verify with pytest.
- robustness: score 1.0 if clean; else iterative debugging quality.

"""
    + "\n\n"
    + _dr.RUBRIC_USER_SECURITY_GATE_DETAIL
    + "\n\n"
    + _dr.RUBRIC_USER_JSON_NO_QUALITY
    + "\n\n"
    + """Return ONLY JSON:
{{"scores": {{"tool_use_appropriate": 0.0, "consistency": 0.0, "robustness": 0.0}}, "security_gate": 1, "notes": "one line"}}

Optional **total**: harness recomputes mean of three process scores.

--- PROXY TRACE JSON BELOW ---
{payload}"""
)
