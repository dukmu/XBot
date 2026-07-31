from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent

QUALITY_SYSTEM = """You are a strict grading assistant for image-understanding QA.
You ONLY output one JSON object, no markdown fences, no extra text.
Score how well the agent's ONE-LINE TEXT ANSWERS (for each shown image) match the semantic reference captions.
Penalize wrong object/color/background descriptions, swapping the two answers, or empty vacuous replies.
Return ONLY: {"quality": <float 0.0-1.0>, "notes": "<one short sentence>"}.
If answer files were empty or unreadable, quality should be very low."""

QUALITY_USER_TMPL = """Task: dual image recognition → one line written to out/image1_answer.txt for the first image, out/image2_answer.txt for the second.

REFERENCE (semantic target, wording may differ in agent answers):
• Image 1 ({img1_fn}): {ref1}
• Image 2 ({img2_fn}): {ref2}

AGENT OUTPUTS ON DISK:
--- image1_answer.txt ---
{ans1}
--- image2_answer.txt ---
{ans2}

Use the appended images ONLY to corroborate the written answers vs the references."""


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt_path = ground_truth_path or (TASK_DIR / "ground_truth.json")
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    files = list(gt.get("image_files") or ["target1.png", "target2.jpg"])
    refs = gt.get("rubric_reference") or {}

    checks = []
    for rel in ("out/image1_answer.txt", "out/image2_answer.txt"):
        path = w / rel
        body = ""
        if path.is_file():
            body = path.read_text(encoding="utf-8", errors="replace").strip()
        checks.append(
            {
                "id": rel.replace("/", "_"),
                "label": f"{rel} exists and is non-empty",
                "pass": bool(body),
                "weight": 0.5,
                "detail": None if body else "missing or empty",
            }
        )
    outcome_score = round(sum(c["weight"] for c in checks if c["pass"]), 4)

    a1_path, a2_path = w / "out/image1_answer.txt", w / "out/image2_answer.txt"
    ans1 = a1_path.read_text(encoding="utf-8", errors="replace").strip() if a1_path.is_file() else ""
    ans2 = a2_path.read_text(encoding="utf-8", errors="replace").strip() if a2_path.is_file() else ""
    img1 = files[0] if len(files) > 0 else "?"
    img2 = files[1] if len(files) > 1 else "?"
    user_text = QUALITY_USER_TMPL.format(
        img1_fn=img1,
        img2_fn=img2,
        ref1=refs.get("image1", ""),
        ref2=refs.get("image2", ""),
        ans1=ans1 or "(empty)",
        ans2=ans2 or "(empty)",
    )

    ql: float | None = None
    q_meta: dict[str, Any] = {}
    try:
        from harnessbench.grading.oracle_quality_llm import run_oracle_quality_llm
        from harnessbench.grading.rubric_llm import build_workspace_image_attachment

        rel_paths = [f"image/{n}" for n in files if n]
        user = build_workspace_image_attachment(w, rel_paths, user_text)
        ql, q_meta = run_oracle_quality_llm(system=QUALITY_SYSTEM, user=user)
    except Exception as e:
        q_meta = {"skipped": False, "error": repr(e), "notes": "oracle quality LLM failed"}

    out: dict[str, Any] = {
        "task": "008-image-recognize",
        "workspace": str(w),
        "checks": checks,
        "outcome_score": outcome_score,
        "quality_rubric_meta": q_meta,
    }
    if ql is not None:
        out["quality"] = ql
    return out
