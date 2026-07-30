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
- The agent must separate schema-level drift from row-level rejects.
- Schema drift must be grounded in the approved schema, schema change log, and extract headers.
- Row rejects must be grounded in row content issues, not treated as schema drift.
- The final summary must explain extract dates, source change log dates, field additions/removals/type changes, and rejected rows.
- Penalize outputs that infer undocumented schema changes, miss the reject ledger, or modify input files.
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
