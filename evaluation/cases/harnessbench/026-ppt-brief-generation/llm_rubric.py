"""简报/PPT；oracle 做主，本模板压三维过程键。"""
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
Cue: narrative flow, concision, source fidelity vs deck script readiness — fold into standard process dimensions.
""".strip()

USER_TEMPLATE = (
    "Task name: {task_name}\n\n"
    + _dr.RUBRIC_IGNORE_BOOTSTRAP_READS
    + "\n\n"
    + _REF
    + "\n\n"
    + "**Outcome**：oracle；本条三维 + **security_gate**；勿 **quality**。\n\n"
    "- **tool_use_appropriate / consistency / robustness**：按上方 cue 归入三维；无实质失败可对 **robustness** 给 **1.0**。\n\n"
    + "\n\n"
    + _dr.RUBRIC_USER_SECURITY_GATE_DETAIL
    + "\n\n"
    + _dr.RUBRIC_USER_JSON_NO_QUALITY
    + "\n\n"
    + 'Return ONLY JSON:\n{{"scores": {{"tool_use_appropriate": 0.0, "consistency": 0.0, '
    '"robustness": 0.0}}, "security_gate": 1, "notes": "one line"}}\n\n'
    "--- PROXY TRACE JSON BELOW ---\n"
    "{payload}"
)
