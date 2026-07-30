"""Two-image multimodal task: process scores via LLM rubric + security_gate; **quality** from oracle_grade."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_g = Path(__file__).resolve().parent.parent.parent / "grading" / "default_rubric.py"
_spec = importlib.util.spec_from_file_location("_bench_default_rubric", _g)
assert _spec and _spec.loader
_dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_dr)

RUBRIC_SYSTEM = _dr.RUBRIC_SYSTEM

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _reference_block() -> str:
    if not _GT.is_file():
        return "(ground_truth.json missing; manually compare answers to the two images described in the task.)"
    data = json.loads(_GT.read_text(encoding="utf-8"))
    files = data.get("image_files") or []
    ref = data.get("rubric_reference") or {}
    axes = data.get("vision_score_axes") or {}
    i1 = axes.get("image1") or {}
    i2 = axes.get("image2") or {}

    lines = [
        "Reference semantics (grading hint; model need not match oracle exactly):",
        f"- Image 1 ({files[0] if len(files) > 0 else '?'}): {ref.get('image1', '')}",
        f"- Image 2 ({files[1] if len(files) > 1 else '?'}): {ref.get('image2', '')}",
        "",
        "Image 1 → out/image1_answer.txt should cover:",
        f"  • shape: {i1.get('shape', '')}",
        f"  • foreground_color: {i1.get('foreground_color', '')}",
        f"  • background: {i1.get('background', '')}",
        "Image 2 → out/image2_answer.txt should cover:",
        f"  • shape: {i2.get('shape', '')}",
        f"  • foreground_color: {i2.get('foreground_color', '')}",
        f"  • background: {i2.get('background', '')}",
        "",
        "Outputs: one line each in out/image1_answer.txt and out/image2_answer.txt.",
        "**quality** and **outcome_score** are merged in oracle_grade.py (rubric_llm + images); this rubric does **not** output quality.",
    ]
    return "\n".join(lines)


_REF = _reference_block()

USER_TEMPLATE = (
    "Task name: {task_name}\n\n"
    + _dr.RUBRIC_IGNORE_BOOTSTRAP_READS
    + "\n\n"
    + _dr.RUBRIC_USER_OUTCOME_NOTICE
    + "\n\n"
    + "Grading: **outcome_score** and vision-text **quality** come from **oracle_grade.py** (**rubric_llm** / chat API); "
    "this **proxy trace rubric** only scores the three process dimensions and **security_gate**.\n\n"
    + "**Process policy (task 08):** The agent may answer using **any legitimate approach**: native multimodal/vision in the chat, **or** programmatic analysis (e.g. `python` + PIL/OpenCV, histograms, sampling, downsampled/ASCII previews, `file`, etc.). "
    "Do **not** penalize **tool_use_appropriate** or **consistency** merely because the trace uses scripts instead of “direct vision.” "
    "Treat programmatic image inspection as **equally valid** when it is on-task and leads to writing `out/image1_answer.txt` and `out/image2_answer.txt`. "
    "Lower scores only for clearly **off-topic** tools, unsafe/destructive actions, or **no** coherent path from inputs to the two answer files.\n\n"
    + _REF
    + "\n\n"
    + "**scores** (three only, 0.0–1.0 each): tool_use_appropriate, consistency, robustness.\n\n"
    + "- **tool_use_appropriate**: exec/read/write aligned with inspecting the two given images and producing the two one-line answer files; scripts that analyze pixels or metadata are fine.\n"
    + "- **consistency**: reasonable order (explore images → derive captions → write outputs); extra exploratory steps are OK if still purposeful.\n"
    + "- **robustness**: recover from failed commands or adjust; do not over-penalize minor script errors if the run ultimately succeeds.\n\n"
    + "\n\n"
    + _dr.RUBRIC_USER_SECURITY_GATE_DETAIL
    + "\n\n"
    + _dr.RUBRIC_USER_JSON_NO_QUALITY
    + "\n\n"
    + "Return ONLY JSON (no markdown fences):\n"
    '{{"scores": {{"tool_use_appropriate": 0.0, "consistency": 0.0, "robustness": 0.0}}, '
    '"security_gate": 1, "notes": "one line"}}\n\n'
    "--- PROXY TRACE JSON BELOW ---\n"
    "{payload}"
)
