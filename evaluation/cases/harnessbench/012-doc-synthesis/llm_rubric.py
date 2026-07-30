"""012-doc-synthesis: multi-document synthesis rubric (3 dimensions)."""
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
    "Task type: synthesize multiple sources, judge credibility, detect contradictions, produce structured reports.\n\n"
    "Score these three dimensions (each 0.0–1.0); keys must match exactly:\n\n"
    "- **tool_use_appropriate**: Suitable tools to read/search documents and write outputs; matches multi-source comparison.\n\n"
    "- **consistency**: Explore before synthesizing; clear reasoning chain from sources to conclusions; avoid chaotic jumps or useless repeats.\n\n"
    "- **robustness**: High score if no failures; if reads/tools fail, recover, reroute, or state limits.\n\n"
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
