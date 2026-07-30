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
- The agent must sort JSONL events by timestamp before sessionization.
- It must stitch identities across stable user IDs and anonymous IDs when the local rules require it.
- It must respect the 30-minute inactivity boundary and avoid merging bot or malformed events.
- It must produce both correct session outputs and a reject ledger for dirty events.
- Penalize outputs that fabricate events, omit rejects, ignore ordering, or modify input files.
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
