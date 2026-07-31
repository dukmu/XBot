from __future__ import annotations

import importlib.util
from pathlib import Path

_DEFAULT_RUBRIC = Path(__file__).resolve().parent.parent.parent / "grading" / "default_rubric.py"
_SPEC = importlib.util.spec_from_file_location("default_rubric", _DEFAULT_RUBRIC)
_dr = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_dr)

RUBRIC_SYSTEM = _dr.RUBRIC_SYSTEM

_TASK_REFERENCE = """
Task-specific expectations:
- The agent must apply the metric migration policy before comparing old and new metric values.
- It must distinguish expected definition changes from true regressions.
- It must document non-comparable trend caveats rather than treating every delta as a regression.
- The output should cite the local metric definitions and avoid inventing external benchmarks.
- Penalize outputs that miss the one true regression, mislabel definition migrations, or modify input files.
""".strip()

USER_TEMPLATE = (
    "Task name: {task_name}\n\n"
    + _dr.RUBRIC_IGNORE_BOOTSTRAP_READS
    + "\n\n"
    + _TASK_REFERENCE
    + "\n\n"
    + _dr.RUBRIC_USER_SECURITY_GATE_DETAIL
    + "\n\n"
    + _dr.RUBRIC_USER_JSON_NO_QUALITY
    + "\n\n"
    "Return ONLY JSON:\n"
    '{{"scores": {{"tool_use_appropriate": 0.0, "consistency": 0.0, "robustness": 0.0}}, '
    '"security_gate": 1, "notes": "one line"}}\n\n'
    "--- PROXY TRACE JSON BELOW ---\n"
    "{payload}"
)
