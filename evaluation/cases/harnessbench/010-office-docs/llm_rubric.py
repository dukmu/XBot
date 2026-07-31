"""办公文档任务（CSV / PDF / DOCX）专用 LLM rubric。"""
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
    + """This task: read **sales.csv** and **policy.pdf**, write **out/summary.json** and **out/report.docx** with correct regional totals and policy citation.

Evaluate the agent run (criteria each 0.0-1.0):
- tool_use_appropriate: tools fit reading CSV/PDF, writing JSON and DOCX; irrelevant tools score lower.
- consistency: logical order (inspect policy → parse CSV → compute totals → write summary + memo); penalize useless repeated reads of the same large files.
- robustness: failures on missing files or bad formats recovered or reported; outputs present when expected.

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
