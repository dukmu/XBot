from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _looks_like_task_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.startswith(("http://127.0.0.1:", "http://localhost:", "https://")) and not value.rstrip("/").endswith("example.com")


def _add(checks: list[dict[str, Any]], cid: str, label: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "label": label, "pass": ok, "weight": weight, "detail": None if ok else detail})


def _artifact_count_ok(value: Any, truth: dict[str, Any]) -> bool:
    # The prompt example says artifact_count is 5 after deduplicating IDs, while
    # the original ground truth used 4 by excluding the orphan from the global
    # total. Accept either interpretation; per-dataset counts still enforce that
    # orphan artifacts are not assigned to a dataset.
    return value in {truth["artifact_count"], truth["artifact_count"] + 1}


def score_workspace(workspace: Path) -> dict[str, Any]:
    truth = json.loads((Path(__file__).resolve().parent / "ground_truth.json").read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    try:
        inv = json.loads((workspace / "out" / "api_inventory.json").read_text(encoding="utf-8"))
        _add(checks, "json_parse", "api_inventory.json parses", isinstance(inv, dict), 1.0)
    except Exception as exc:
        inv = {}
        _add(checks, "json_parse", "api_inventory.json parses", False, 1.0, str(exc))
    _add(checks, "source_url", "source_base_url records the provided mock API", _looks_like_task_url(inv.get("source_base_url")), 1.0, repr(inv.get("source_base_url")))
    _add(checks, "dataset_count", "dataset_count exact", inv.get("dataset_count") == truth["dataset_count"], 2.0, repr(inv.get("dataset_count")))
    _add(checks, "artifact_count", "artifact_count follows a documented global-count interpretation", _artifact_count_ok(inv.get("artifact_count"), truth), 2.0, repr(inv.get("artifact_count")))
    _add(checks, "jobs_by_status", "jobs_by_status exact", inv.get("jobs_by_status") == truth["jobs_by_status"], 2.0, repr(inv.get("jobs_by_status")))
    _add(checks, "datasets", "datasets exact", inv.get("datasets") == truth["datasets"], 3.0, repr(inv.get("datasets")))

    try:
        with (workspace / "out" / "retry_ledger.csv").open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fields = reader.fieldnames
        _add(checks, "ledger_read", "retry_ledger.csv parses", True, 1.0)
    except Exception as exc:
        rows, fields = [], None
        _add(checks, "ledger_read", "retry_ledger.csv parses", False, 1.0, str(exc))
    _add(checks, "ledger_header", "retry ledger header exact", fields == ["endpoint", "cursor_or_page", "status_code", "action", "attempts"], 1.0, repr(fields))
    codes = {r.get("status_code", "") for r in rows}
    actions = " ".join(r.get("action", "").lower() for r in rows)
    cursor_recovery_logged = "410" in codes or "checkpoint" in actions or "cursor" in actions
    _add(checks, "ledger_events", "ledger records 429, 503, and cursor recovery", {"429", "503"} <= codes and cursor_recovery_logged, 3.0, repr(rows))

    seen = (workspace / "out" / "api_access.log").read_text(encoding="utf-8", errors="replace").splitlines() if (workspace / "out" / "api_access.log").is_file() else []
    for endpoint in ("/datasets", "/jobs", "/artifacts", "/checkpoint"):
        weight = 3.0 if endpoint == "/checkpoint" else 1.0
        _add(checks, "visited_" + endpoint.strip("/"), f"server log includes {endpoint}", any(s.startswith(endpoint + "?") for s in seen), weight, repr(seen))
    _add(checks, "retry_observed", "server log shows retries", sum(1 for s in seen if s.startswith("/datasets?cursor=START")) >= 2 and sum(1 for s in seen if s.startswith("/jobs?cursor=2")) >= 2, 2.0, repr(seen))

    report = (workspace / "out" / "data_quality_report.md").read_text(encoding="utf-8", errors="replace").lower() if (workspace / "out" / "data_quality_report.md").is_file() else ""
    _add(checks, "quality_terms", "quality report covers duplicate, orphan, cursor, 429, and 503", all(t in report for t in truth["quality_terms"]), 3.0)
    score = sum(c["weight"] for c in checks if c["pass"]) / sum(c["weight"] for c in checks)
    return {"task": "078-local-api-cursor-retry-ledger", "workspace": str(workspace), "checks": checks, "outcome_score": score}
