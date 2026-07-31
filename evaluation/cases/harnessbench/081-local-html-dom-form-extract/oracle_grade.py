from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _looks_like_task_url(value: str) -> bool:
    return value.startswith(("http://127.0.0.1:", "http://localhost:", "https://"))


def _add(checks: list[dict[str, Any]], cid: str, label: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "label": label, "pass": ok, "weight": weight, "detail": None if ok else detail})


def score_workspace(workspace: Path) -> dict[str, Any]:
    truth = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    try:
        result = json.loads((workspace / "out" / "dom_extract.json").read_text(encoding="utf-8"))
    except Exception as exc:
        result = {}
        _add(checks, "json_parse", "dom_extract.json parses", False, 1.0, str(exc))
    else:
        _add(checks, "json_parse", "dom_extract.json parses", isinstance(result, dict), 1.0)
    _add(checks, "result_exact", "dom extract exact", result == truth["dom_extract"], 8.0, repr(result))
    dumped = json.dumps(result, ensure_ascii=False)
    _add(checks, "no_bait", "result excludes template bait and whole HTML", not any(t.lower() in dumped.lower() for t in truth["forbidden_terms"]), 2.0)

    seen = (workspace / "out" / "site_access.log").read_text(encoding="utf-8", errors="replace").splitlines() if (workspace / "out" / "site_access.log").is_file() else []
    for path in truth["required_paths"]:
        _add(checks, "visited_" + (path.strip("/") or "index"), f"server log includes {path}", path in seen, 1.0, repr(seen))
    trace = (workspace / "out" / "interaction_trace.md").read_text(encoding="utf-8", errors="replace").lower() if (workspace / "out" / "interaction_trace.md").is_file() else ""
    _add(checks, "trace_terms", "interaction trace records token, hidden field, filters, confirmation", all(t.lower() in trace for t in truth["trace_terms"]), 3.0)
    urls = (workspace / "out" / "source_urls.txt").read_text(encoding="utf-8", errors="replace").splitlines() if (workspace / "out" / "source_urls.txt").is_file() else []
    _add(checks, "source_urls", "source_urls includes mock-site URLs", len(urls) >= 3 and all(_looks_like_task_url(u) for u in urls), 2.0, repr(urls))
    score = sum(c["weight"] for c in checks if c["pass"]) / sum(c["weight"] for c in checks)
    return {"task": "081-local-html-dom-form-extract", "workspace": str(workspace), "checks": checks, "outcome_score": score}
