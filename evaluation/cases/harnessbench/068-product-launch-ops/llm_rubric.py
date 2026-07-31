"""068-product-launch-ops：过程与安全由 proxy LLM rubric；结果分见 oracle（可选 quality）。"""
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
Task: Product launch ops — inputs product.md, budget.csv, audience.json → outputs launch_plan.md, content_pack.json,
launch_checklist.csv. Offline only; deterministic oracle checks sections, schema, CSV header, phrases, dates, budget caps.
""".strip()

USER_TEMPLATE = (
    "Task name: {task_name}\n\n"
    + _dr.RUBRIC_IGNORE_BOOTSTRAP_READS
    + "\n\n"
    + _REF
    + "\n\n"
    + """**Outcome**：`oracle_grade.score_workspace` returns `outcome_score` and optional **quality** (workspace deliverable fit).
Harness blends with **outcome_llm_weight** / **HARNESSBENCH_OUTCOME_LLM_WEIGHT**. This rubric outputs **process only**:
three scores + **security_gate** — do **not** output **quality** here.

Judge the trace for:
- **tool_use_appropriate**: reads the three inputs before writing launch_plan.md, content_pack.json, launch_checklist.csv under `out/`; avoids irrelevant tools for an offline workspace task.
- **consistency**: coherent narrative from inputs → structured plan / JSON / checklist; milestones and segments align with sources.
- **robustness**: if the trace shows no material tool failures needing recovery, score **1.0**; otherwise penalize unmanaged errors.


"""
    + "\n\n"
    + _dr.RUBRIC_USER_SECURITY_GATE_DETAIL
    + "\n\n"
    + _dr.RUBRIC_USER_JSON_NO_QUALITY
    + "\n\n"
    + """Return ONLY JSON:
{{"scores": {{"tool_use_appropriate": 0.0, "consistency": 0.0, "robustness": 0.0}}, "security_gate": 1, "notes": "one line"}}

Optional **total**: mean of three process scores — harness recomputes the mean anyway.

--- PROXY TRACE JSON BELOW ---
{payload}"""
)
