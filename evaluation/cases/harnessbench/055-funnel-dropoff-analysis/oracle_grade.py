from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_DEFAULT_GT = _TASK_DIR / "ground_truth.json"


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _near(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) <= 0.0001
    except Exception:
        return a == b


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixtures_unchanged(workspace: Path, gt: dict[str, Any]) -> bool:
    for rel, digest in gt.get("fixture_hashes", {}).items():
        candidate = workspace / "in" / rel
        if not candidate.is_file() or _sha256(candidate) != digest:
            return False
    return True


def _stage_match(got: dict[str, Any], exp: dict[str, Any]) -> bool:
    for key, exp_value in exp.items():
        if isinstance(exp_value, float):
            if not _near(got.get(key), exp_value):
                return False
        elif got.get(key) != exp_value:
            return False
    return True


def _cohort_rows_match(got_rows: list[dict[str, str]], exp_rows: list[dict[str, str]]) -> bool:
    if len(got_rows) != len(exp_rows):
        return False
    got_by = {r.get("cohort", ""): r for r in got_rows}
    exp_by = {r.get("cohort", ""): r for r in exp_rows}
    if set(got_by) != set(exp_by):
        return False
    for cohort, exp in exp_by.items():
        got = got_by[cohort]
        for key in ("visit_users", "purchase_users", "largest_dropoff_transition"):
            if str(got.get(key, "")) != str(exp.get(key, "")):
                return False
        if not _near(got.get("purchase_rate"), exp.get("purchase_rate")):
            return False
    return True


def score_workspace(workspace: str | Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((ground_truth_path or _DEFAULT_GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    _add(checks, "fixture_present", (w / "in" / "events.jsonl").is_file(), 0.08, "missing events.jsonl")
    _add(checks, "fixtures_unchanged", _fixtures_unchanged(w, gt), 0.08, "events.jsonl is missing or modified")
    out = w / gt["outputs"]["json"]
    insights = w / gt["outputs"]["insights"]
    cohort_path = w / gt["outputs"]["cohort"]
    stage_sets_path = w / gt["outputs"].get("stage_user_sets", "out/stage_user_sets.json")
    _add(checks, "json_exists", out.is_file(), 0.08, "missing funnel_metrics.json")
    _add(checks, "insights_exists", insights.is_file(), 0.04, "missing dropoff_insights.md")
    _add(checks, "cohort_exists", cohort_path.is_file(), 0.06, "missing cohort_comparison.csv")
    _add(checks, "stage_user_sets_exists", stage_sets_path.is_file(), 0.08, "missing stage_user_sets.json")
    if out.is_file():
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
            stages = data.get("stages", [])
            largest = data.get("largest_dropoff", {})
            _add(checks, "stage_count", len(stages) == 5, 0.08, f"got {len(stages)} stages")
            _add(checks, "stage_order", [s.get("stage") for s in stages] == [s["stage"] for s in gt["stages"]], 0.08, "stage order is wrong")
            _add(checks, "stage_metrics_exact", len(stages) == 5 and all(_stage_match(g, e) for g, e in zip(stages, gt["stages"])), 0.24, f"got {stages}")
            largest_ok = all(_near(largest.get(k), v) if isinstance(v, float) else largest.get(k) == v for k, v in gt["largest_dropoff"].items())
            _add(checks, "largest_dropoff_exact", largest_ok, 0.12, f"got {largest}")
            _add(checks, "bot_exclusion_recorded", set(data.get("excluded_bot_users") or []) == set(gt["excluded_bot_users"]), 0.06, f"got {data.get('excluded_bot_users')}")
            dq = data.get("data_quality", {})
            _add(checks, "ignored_order_events_exact", int(dq.get("ignored_order_events", -1)) == gt["data_quality"]["ignored_order_events"], 0.04, f"got {dq}")
            _add(checks, "deduplicated_stage_events_exact", int(dq.get("deduplicated_stage_events", -1)) == gt["data_quality"]["deduplicated_stage_events"], 0.04, f"got {dq}")
            _add(checks, "largest_dropoff_lost_users", largest.get("lost_users") == 3, 0.03, f"got {largest}")
        except Exception as exc:
            _add(checks, "json_parseable", False, 0.30, str(exc))
    else:
        for cid, weight in [
            ("stage_count", 0.08),
            ("stage_order", 0.08),
            ("stage_metrics_exact", 0.24),
            ("largest_dropoff_exact", 0.12),
            ("bot_exclusion_recorded", 0.06),
            ("ignored_order_events_exact", 0.04),
            ("deduplicated_stage_events_exact", 0.04),
            ("largest_dropoff_lost_users", 0.03),
        ]:
            _add(checks, cid, False, weight, "missing funnel_metrics.json")
    if cohort_path.is_file():
        try:
            with cohort_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                rows = [{k: (v or "").strip() for k, v in row.items()} for row in reader]
                header = list(reader.fieldnames or [])
            _add(checks, "cohort_header", header == gt["cohort_header"], 0.05, f"got {header}")
            _add(checks, "cohort_rows", _cohort_rows_match(rows, gt["cohort_rows"]), 0.12, f"got {rows}")
            _add(checks, "third_cohort_present", [r.get("cohort") for r in rows] == ["beta", "control", "variant"], 0.04, f"got cohorts {[r.get('cohort') for r in rows]}")
        except Exception as exc:
            _add(checks, "cohort_parseable", False, 0.10, str(exc))
    else:
        for cid, weight in [("cohort_header", 0.05), ("cohort_rows", 0.12), ("third_cohort_present", 0.04)]:
            _add(checks, cid, False, weight, "missing cohort_comparison.csv")
    if stage_sets_path.is_file():
        try:
            data = json.loads(stage_sets_path.read_text(encoding="utf-8"))
            expected = gt["stage_user_sets"]
            _add(checks, "stage_user_sets_exact", data.get("stage_users") == expected["stage_users"], 0.20, f"got {data.get('stage_users')}")
            _add(checks, "largest_dropoff_lost_user_ids_exact", data.get("largest_dropoff_lost_user_ids") == expected["largest_dropoff_lost_user_ids"], 0.08, f"got {data.get('largest_dropoff_lost_user_ids')}")
            _add(checks, "ignored_order_user_ids_exact", data.get("ignored_order_user_ids") == expected["ignored_order_user_ids"], 0.06, f"got {data.get('ignored_order_user_ids')}")
            _add(checks, "deduplicated_stage_user_ids_exact", data.get("deduplicated_stage_user_ids") == expected["deduplicated_stage_user_ids"], 0.06, f"got {data.get('deduplicated_stage_user_ids')}")
        except Exception as exc:
            _add(checks, "stage_user_sets_parseable", False, 0.20, str(exc))
    else:
        for cid, weight in [
            ("stage_user_sets_exact", 0.20),
            ("largest_dropoff_lost_user_ids_exact", 0.08),
            ("ignored_order_user_ids_exact", 0.06),
            ("deduplicated_stage_user_ids_exact", 0.06),
        ]:
            _add(checks, cid, False, weight, "missing stage_user_sets.json")
    if insights.is_file():
        text = insights.read_text(encoding="utf-8", errors="replace").lower()
        _add(checks, "insights_mentions_transition", "trial_start" in text and "purchase" in text, 0.03, "largest transition missing")
        _add(checks, "insights_has_followup", any(word in text for word in ["follow", "test", "experiment", "investigate", "survey"]), 0.03, "no product follow-up")
        cohort_ok = all(term in text for term in ["control", "variant", "beta"])
        sample_ok = any(term in text for term in ["sample", "small n", "small cohort", "low volume"])
        order_ok = any(term in text for term in ["order", "ordering", "sequence", "out-of-sequence", "timestamp"])
        _add(checks, "insights_cohort_caveat", cohort_ok and sample_ok and order_ok, 0.05, "missing cohort comparison or caveat")
    else:
        for cid, weight in [("insights_mentions_transition", 0.03), ("insights_has_followup", 0.03), ("insights_cohort_caveat", 0.05)]:
            _add(checks, cid, False, weight, "missing dropoff_insights.md")
    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    return {"task": "055-funnel-dropoff-analysis", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
