"""会议行动项合并；程序化校验为主。"""
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
Qualitative cues: action wording specific/faithful; prior open items updated not duplicated with sensible reassignment,
deadlines, completion/cancellation; concise owner note; avoid invented owners/deadlines/commitments.
""".strip()

USER_TEMPLATE = (
    "Task name: {task_name}\n\n"
    + _dr.RUBRIC_IGNORE_BOOTSTRAP_READS
    + "\n\n"
    + _REF
    + "\n\n"
    + "**Outcome**：oracle；本 rubric 仅三维过程 + **security_gate**；勿输出 **quality**。\n\n"
    "- **tool_use_appropriate**：读写与会纪/邮件/fixture 是否贴切。\n"
    "- **consistency**：抽取 → 合并/更新条目 → 写出的可追溯性。\n"
    "- **robustness**：无失败满分；失败后是否调整或说明。\n\n"
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
