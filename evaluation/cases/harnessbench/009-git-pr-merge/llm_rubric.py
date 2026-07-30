"""009-git-pr-merge：Oracle 判结果，过程分强调审查、合并与 push 的执行链路。"""
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
    + """Theme: review and merge local git PR branch **feature/pr-add-doc** into **main**, write **out/review.txt**, and push **origin main**.

Evaluate the agent run (criteria each 0.0-1.0):
- tool_use_appropriate: prioritize git inspection commands such as **fetch**, **diff**, **log**, then review-file write, merge, and push; skipping review evidence or using irrelevant tools lowers score.
- consistency: inspect PR branch changes → decide approve/reject → write **out/review.txt** → if approving, merge into **main** and **push origin main**.
- robustness: safe.directory warnings, merge conflicts, wrong branch state, or push failures are handled or surfaced instead of ignored.

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
