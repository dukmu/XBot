from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or _GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, detail: Any = None) -> None:
        checks.append({"id": cid, "label": cid.replace("_", " "), "pass": bool(ok), "weight": 1.0, "detail": detail})

    data: dict[str, Any] = {}
    p = w / "out" / "slides_outline.json"
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            data = raw if isinstance(raw, dict) else {}
            add("outline_json_parseable", isinstance(raw, dict))
        except Exception as exc:
            add("outline_json_parseable", False, str(exc))
    else:
        add("outline_exists", False, "missing")
    slides = data.get("slides")
    add("six_slides", isinstance(slides, list) and len(slides) == 6, len(slides) if isinstance(slides, list) else None)
    titles = [str(s.get("title", "")) for s in slides] if isinstance(slides, list) else []
    add("titles_follow_template_order", titles == gt["titles_in_order"], titles)
    schema_ok = isinstance(slides, list) and all(isinstance(s, dict) and all(k in s for k in ["slide_number", "title", "bullets", "metric_refs"]) for s in slides)
    add("slide_schema_complete", schema_ok)
    refs = set()
    if isinstance(slides, list):
        for s in slides:
            refs.update(str(x) for x in (s.get("metric_refs") or []))
    add("all_metrics_referenced", set(gt["metric_ids"]).issubset(refs), sorted(refs))
    add("no_unknown_metric_refs", refs.issubset(set(gt["metric_ids"])), sorted(refs - set(gt["metric_ids"])))

    notes_path = w / "out" / "speaker_notes.md"
    text = notes_path.read_text(encoding="utf-8", errors="replace") if notes_path.is_file() else ""
    add("speaker_notes_exists", bool(text.strip()))
    add("speaker_notes_cover_all_slides", all(f"Slide {i}" in text or f"slide {i}" in text.lower() for i in range(1, 7)))
    missing_terms = [t for t in gt["speaker_note_terms"] if t.lower() not in text.lower()]
    add("speaker_notes_cover_value_props", not missing_terms, missing_terms)
    forbidden = [t for t in gt["forbidden_terms"] if t.lower() in (json.dumps(data) + text).lower()]
    add("forbidden_unapproved_feature_omitted", not forbidden, forbidden)

    score = sum(c["pass"] for c in checks) / len(checks) if checks else 0.0
    return {"task": "026-ppt-brief-generation", "workspace": str(w), "outcome_score": round(score, 4), "checks": checks}
