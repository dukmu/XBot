from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


_INDEX_SHA256 = "d4d3629d3af5a96078a9ff605ea0aa81ab856c29b38ccd6fe969098cc35e5bf4"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _add(checks: list[dict[str, Any]], cid: str, label: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "label": label, "pass": ok, "weight": weight, "detail": None if ok else detail})


def score_workspace(workspace: Path) -> dict[str, Any]:
    truth = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    index = workspace / "in" / "www" / "index.html"
    actual_index_hash = _sha256(index) if index.is_file() else ""
    _add(checks, "source_index", "www/index.html unchanged", actual_index_hash == _INDEX_SHA256, 1.0, f"got {actual_index_hash!r}")

    try:
        result = json.loads((workspace / "out" / "form_result.json").read_text(encoding="utf-8"))
    except Exception as exc:
        result = {}
        _add(checks, "json_parse", "out/form_result.json is valid JSON", False, 1.0, str(exc))
    else:
        _add(checks, "json_parse", "out/form_result.json is valid JSON", isinstance(result, dict), 1.0)

    _add(checks, "query", "query parameters are exact", result.get("query") == truth["query"], 2.0, f"got {result.get('query')!r}")
    _add(checks, "marker", "lookup marker is exact", result.get("marker") == truth["marker"], 2.0, f"got {result.get('marker')!r}")
    _add(checks, "confirm_marker", "confirmation marker is exact", result.get("confirm_marker") == truth["confirm_marker"], 2.0, f"got {result.get('confirm_marker')!r}")
    _add(checks, "result", "extracted result is exact", result.get("result") == truth["result"], 5.0, f"got {result.get('result')!r}")

    dumped = json.dumps(result, ensure_ascii=False)
    no_html = "<html" not in dumped.lower() and "FORM_PORTAL_READY" not in dumped and "Order Lookup" not in dumped
    _add(checks, "no_unrelated_page_text", "output excludes index HTML and unrelated page text", no_html, 1.0)

    log_path = workspace / "out" / "form_access.log"
    seen = log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.is_file() else []
    for path in truth["required_paths"]:
        _add(checks, "visited_" + (path.strip("/") or "index"), f"server log includes GET {path}", path in seen, 1.0, f"seen {seen!r}")
    trace = (workspace / "out" / "interaction_trace.md").read_text(encoding="utf-8", errors="replace").lower() if (workspace / "out" / "interaction_trace.md").is_file() else ""
    _add(checks, "interaction_trace", "interaction_trace.md records token, hidden field, submit, and confirmation", all(term.lower() in trace for term in truth["trace_terms"]), 2.0)

    score = sum(c["weight"] for c in checks if c["pass"]) / sum(c["weight"] for c in checks)
    return {"task": "023-web-form-extraction", "workspace": str(workspace), "checks": checks, "outcome_score": score}
