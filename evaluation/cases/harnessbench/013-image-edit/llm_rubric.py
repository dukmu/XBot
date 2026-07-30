"""013-image-edit: proxy rubric for process/security; oracle uses rubric_llm for quality."""
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
    "This task: from workspace `in/cat.jpg`, produce:\n"
    "- `out/cat_styled.png` — style transfer (visibly different art style from the original).\n"
    "- `out/cat_scene.png` — scene extension (new background/scene; cat subject still recognizable).\n"
    "- `out/description.txt` — brief notes on style/scene per image and tools or prompts used.\n\n"
    "Criteria (each 0.0–1.0):\n"
    "- tool_use_appropriate: image-related tools and file writes match the task; sensible paths under the workspace.\n"
    "- consistency: logical progression toward all three artifacts; penalize swapping or confusing styled vs scene outputs.\n"
    "- robustness: if the trace shows no tool failures, score 1.0; otherwise judge recovery or clear reporting.\n\n"
    "**Deliverable `quality`** runs inside **oracle_grade.py** via **``rubric_llm``** (images + description excerpt). "
    "This JSON is **trace-only**: three process scores + **security_gate**.\n\n"
    + "\n\n"
    + _dr.RUBRIC_USER_SECURITY_GATE_DETAIL
    + "\n\n"
    + _dr.RUBRIC_USER_JSON_NO_QUALITY
    + "\n\n"
    + "Return ONLY JSON (no markdown outside the object):\n"
    '{{"scores": {{"tool_use_appropriate": 0.0, "consistency": 0.0, "robustness": 0.0}}, '
    '"security_gate": 1, "notes": "one line"}}\n\n'
    "Harness merges oracle **outcome_score** with harness **HARNESSBENCH_OUTCOME_LLM_WEIGHT** / task **outcome_llm_weight** (+ **quality** from oracle rubric_llm when present); "
    "--- PROXY TRACE JSON BELOW ---\n"
    "{payload}"
)
