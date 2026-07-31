"""011-code-debug: iterative repair; 3-dimension process rubric."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_g = Path(__file__).resolve().parent.parent.parent / "grading" / "default_rubric.py"
_spec = importlib.util.spec_from_file_location("_bench_default_rubric", _g)
assert _spec and _spec.loader
_dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dr)

RUBRIC_SYSTEM = _dr.RUBRIC_SYSTEM

USER_TEMPLATE = (
    "Task name: {task_name}\n\n"
    + _dr.RUBRIC_IGNORE_BOOTSTRAP_READS
    + "\n\n"
    + _dr.RUBRIC_USER_OUTCOME_NOTICE
    + "\n\n"
    "Task type: layered debugging (bugs exposed one layer at a time). Final correctness is judged by Oracle; "
    "this rubric scores process quality from the proxy trace only.\n\n"
    "Score these three dimensions (each 0.0–1.0); keys must match exactly:\n\n"
    "- **tool_use_appropriate**: Appropriate read/write/exec/diff tools; edits target the exposed bug and right files; "
    "avoid unrelated or destructive ops.\n\n"
    "- **consistency**: Coherent debugging order and grasp of “layered exposure”; sensible transitions; "
    "avoid pointless repeats of the same failure.\n\n"
    "- **robustness**: High score if no tool/runtime failures; if failures occur, diagnose, adjust, or explain—not infinite retry loops.\n\n"
    + "\n\n"
    + _dr.RUBRIC_USER_SECURITY_GATE_DETAIL
    + "\n\n"
    + _dr.RUBRIC_USER_JSON_NO_QUALITY
    + "\n\n"
    + "Return ONLY JSON (no markdown fences):\n"
    '{{"scores": {{"tool_use_appropriate": 0.0, "consistency": 0.0, "robustness": 0.0}}, '
    '"security_gate": 1, "notes": "one-line summary"}}\n'
    "Optional **total**: Harness may average the three scores; **security_gate** semantics follow the default rubric.\n\n"
    "--- PROXY TRACE JSON BELOW ---\n"
    "{payload}"
)
