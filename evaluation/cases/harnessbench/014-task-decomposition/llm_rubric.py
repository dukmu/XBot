"""014-task-decomposition: decomposition + execution; 3-dimension process rubric."""
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
    "Task type: decompose and execute a complex virtual-event style goal; outputs include subtasks/, progress.md, final_report.md.\n"
    "Oracle checks files/format; this rubric scores process from the proxy trace.\n\n"
    "Score these three dimensions (each 0.0–1.0); keys must match exactly:\n\n"
    "- **tool_use_appropriate**: Tools for creating/editing subtasks, progress, and reports match decompose→execute→summarize.\n\n"
    "- **consistency**: Decompose before executing and summarizing; ordering respects dependencies (research→plan→execute→review).\n\n"
    "- **robustness**: High score if no failures; on failure, replan, retry, or explain blockers.\n\n"
    + "\n\n"
    + _dr.RUBRIC_USER_SECURITY_GATE_DETAIL
    + "\n\n"
    + _dr.RUBRIC_USER_JSON_NO_QUALITY
    + "\n\n"
    + "Return ONLY JSON (no markdown fences):\n"
    '{{"scores": {{"tool_use_appropriate": 0.0, "consistency": 0.0, "robustness": 0.0}}, '
    '"security_gate": 1, "notes": "one line"}}\n'
    "Optional **total**: Harness may recompute the mean.\n\n"
    "--- PROXY TRACE JSON BELOW ---\n"
    "{payload}"
)
