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
    + """Evaluate the agent run for logistics delay response. Deterministic checks cover delay detection, customer tiers, actions, compensation, and message coverage. Score only qualitative aspects:
- tool_use_appropriate: uses shipments, carrier updates, and tiers.
- consistency: joins data correctly before drafting actions.
- robustness: distinguishes on-track shipments from delayed ones.

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
{payload}
"""
)
