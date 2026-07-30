"""003-browser：Oracle 只判摘录文件；是否用浏览器/curl 等访问题目给定页面由过程分体现。"""
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
    + """This task: visit the task-provided **MOCK_PAGE** URL (static page), then write visible page content into **out/page_extract.txt**. Oracle only checks that file contains a fixed substring; it does **not** verify HTTP or which tool was used.

Evaluate the agent run (criteria each 0.0-1.0):
- tool_use_appropriate: **prioritize** use of **browser** and/or **terminal fetch** (curl/wget) against the given URL, and file write to **out/page_extract.txt**; penalize skipping page access if the transcript shows no reasonable way the excerpt could come from the page.
- consistency: open/fetch page → extract relevant text → write **out/page_extract.txt** (or equivalent).
- robustness: connection/refusal or wrong port handled or retried; write failures surfaced.

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
