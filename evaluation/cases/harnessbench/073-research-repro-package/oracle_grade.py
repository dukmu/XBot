from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any


TASK_DIR = Path(__file__).resolve().parent


def _check(cid: str, label: str, ok: bool, weight: float, detail: str = "") -> dict[str, Any]:
    return {"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": None if ok else detail}


def _first_line(path: Path) -> str:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    return raw[0].strip() if raw else ""


def _norm(text: str) -> str:
    return text.lower().replace("_", " ").replace("-", " ")


def _claim_sections(md: str) -> dict[str, str]:
    rx = re.compile(r"^###\s+Claim\s+(CLAIM-\d+)\s*$", re.MULTILINE)
    ms = list(rx.finditer(md))
    out: dict[str, str] = {}
    for i, m in enumerate(ms):
        cid = m.group(1)
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(md)
        out[cid] = md[start:end]
    return out


def _expect_rows(rows: list[dict[str, str]], expectations: list[dict[str, Any]]) -> tuple[bool, str]:
    failures: list[str] = []
    for exp in expectations:
        label = exp.get("label", "")
        item_needles = list(exp.get("item_needles") or [])
        type_needles = list(exp.get("type_needles") or [])
        matched = False
        for r in rows:
            impact = str(r.get("impact", "")).strip()
            fix = str(r.get("recommended_fix", "")).strip()
            if not impact or not fix:
                continue
            item_n = _norm(str(r.get("item", "")))
            typ_n = _norm(str(r.get("type", "")))
            if item_needles and not all(_norm(n) in item_n for n in item_needles):
                continue
            if type_needles and not any(_norm(t) in typ_n for t in type_needles):
                continue
            matched = True
            break
        if not matched:
            failures.append(label or "expectation")
    return not failures, "; ".join(failures)


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    report_path = w / gt["report_path"]
    missing_path = w / gt["missing_path"]

    checks: list[dict[str, Any]] = []
    checks.append(_check("report_exists", "reproducibility_report.md exists", report_path.is_file(), 0.04))
    checks.append(_check("missing_exists", "missing_steps.csv exists", missing_path.is_file(), 0.04))

    text = report_path.read_text(encoding="utf-8", errors="replace") if report_path.exists() else ""
    text_l = text.lower()

    sections = _claim_sections(text) if text else {}
    want_claims = set(gt["claim_ids"])
    sections_ok = report_path.is_file() and set(sections.keys()) == want_claims
    checks.append(_check("claim_sections", "report has ### Claim sections for each CLAIM id", sections_ok, 0.10))

    terms_ok = all(term.lower() in text_l for term in gt["required_terms"])
    checks.append(_check("required_terms", "report cites artifacts metrics and failures", terms_ok, 0.10))

    forbid = list(gt.get("forbidden_success_phrases") or [])
    no_false = report_path.is_file() and not any(p.lower() in text_l for p in forbid)
    checks.append(_check("no_false_repro", "does not assert successful reproduction", no_false, 0.10))

    script_audit = report_path.is_file() and ("analyze_main.py" in text or "analyze_main" in text_l) and any(
        k in text_l for k in ["syntax", "parse", "indent", "broken", "invalid", "corrupt", "error"]
    )
    checks.append(_check("script_issue_called_out", "report flags analyze_main packaging defect", script_audit, 0.08))

    rows: list[dict[str, str]] = []
    header_line_ok = missing_path.is_file() and _first_line(missing_path) == gt["missing_steps_header"]
    cols_ok = False
    if missing_path.exists():
        with missing_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        cols_ok = bool(rows) and set(rows[0].keys()) == {"item", "type", "impact", "recommended_fix"}

    checks.append(_check("missing_csv_header_line", "missing_steps header row exact", header_line_ok, 0.04))
    checks.append(_check("missing_csv_columns", "missing_steps DictReader columns", cols_ok, 0.04))

    exp_list = list(gt.get("missing_expectations") or [])
    min_rows_ok = len(rows) >= len(exp_list)
    checks.append(_check("missing_minimum_rows", "missing_steps lists each blocking gap", min_rows_ok, 0.06))

    miss_ok, miss_detail = _expect_rows(rows, exp_list)
    checks.append(
        _check(
            "missing_expectation_coverage",
            "CSV rows cover raw/script/mismatch/subgroup gaps",
            miss_ok,
            0.40,
            miss_detail,
        )
    )

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0

    if miss_detail and not miss_ok:
        for c in checks:
            if c["id"] == "missing_expectation_coverage" and not c["pass"]:
                c["detail"] = miss_detail

    return {"task": "073-research-repro-package", "workspace": str(w), "outcome_score": score, "checks": checks}


