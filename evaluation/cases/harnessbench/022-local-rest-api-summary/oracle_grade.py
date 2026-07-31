from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _add(checks: list[dict[str, Any]], cid: str, label: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "label": label, "pass": ok, "weight": weight, "detail": None if ok else detail})


def score_workspace(workspace: Path) -> dict[str, Any]:
    truth = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    out = workspace / "out" / "api_summary.json"

    try:
        summary = json.loads(out.read_text(encoding="utf-8"))
    except Exception as exc:
        summary = {}
        _add(checks, "json_parse", "out/api_summary.json is valid JSON", False, 1.0, str(exc))
    else:
        _add(checks, "json_parse", "out/api_summary.json is valid JSON", isinstance(summary, dict), 1.0)

    _add(checks, "source_url", "source_base_url records local API", isinstance(summary.get("source_base_url"), str) and summary["source_base_url"].startswith("http://127.0.0.1:"), 1.0, repr(summary.get("source_base_url")))
    for key in ("project_count", "active_budget_usd", "open_incidents"):
        _add(checks, key, f"{key} is correct", summary.get(key) == truth[key], 2.0, f"got {summary.get(key)!r}")

    _add(checks, "projects", "joined project rows are exact", summary.get("projects") == truth["projects"], 6.0, f"got {summary.get('projects')!r}")
    projects = summary.get("projects")
    sorted_ok = isinstance(projects, list) and [p.get("id") for p in projects] == sorted(p.get("id") for p in projects)
    _add(checks, "project_sort", "projects sorted by id", sorted_ok, 1.0)

    log_path = workspace / "out" / "api_access.log"
    seen = log_path.read_text(encoding="utf-8", errors="replace").splitlines() if log_path.is_file() else []
    for endpoint in truth["required_endpoints"]:
        _add(checks, "visited_" + endpoint.strip("/"), f"server log includes GET {endpoint}", endpoint in seen, 1.0, f"seen {seen!r}")
    retry_ok = seen.count("/projects") >= 2 and seen.count("/incidents") >= 2
    _add(checks, "retry_observed", "server log shows retry after transient failures", retry_ok, 2.0, f"seen {seen!r}")
    quality = (workspace / "out" / "data_quality_report.md").read_text(encoding="utf-8", errors="replace").lower() if (workspace / "out" / "data_quality_report.md").is_file() else ""
    _add(checks, "quality_report", "data_quality_report.md covers duplicates, missing owners, orphan incidents, and retries", all(term.lower() in quality for term in truth["quality_terms"]), 3.0)

    score = sum(c["weight"] for c in checks if c["pass"]) / sum(c["weight"] for c in checks)
    return {"task": "022-local-rest-api-summary", "workspace": str(workspace), "checks": checks, "outcome_score": score}
