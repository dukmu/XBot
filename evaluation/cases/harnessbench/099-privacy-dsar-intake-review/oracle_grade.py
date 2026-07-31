from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

TASK_DIR = Path(__file__).resolve().parent


def _check(cid: str, label: str, ok: bool, weight: float, detail: str = "") -> dict[str, Any]:
    return {"id": cid, "label": label, "pass": bool(ok), "weight": weight, "detail": None if ok else detail}


def _first_line(path: Path) -> str:
    raw = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    return raw[0].strip() if raw else ""


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    decisions = w / gt["decisions_path"]
    responses = w / gt["responses_path"]
    redaction = w / gt["redaction_path"]
    expected = gt["expected"]
    checks: list[dict[str, Any]] = []

    checks.append(_check("decisions_exists", "dsar_intake_decisions.csv exists", decisions.is_file(), 0.04))
    checks.append(_check("responses_exists", "requester_response_drafts.md exists", responses.is_file(), 0.04))
    checks.append(_check("redaction_exists", "privacy_redaction_audit.csv exists", redaction.is_file(), 0.04))
    checks.append(_check("decisions_header", "decisions CSV header is exact", decisions.is_file() and _first_line(decisions) == gt["decisions_header"], 0.04))
    checks.append(_check("redaction_header", "redaction CSV header is exact", redaction.is_file() and _first_line(redaction) == gt["redaction_header"], 0.03))

    rows: list[dict[str, str]] = []
    if decisions.exists():
        try:
            rows = _read_rows(decisions)
        except Exception:
            rows = []
    by_id = {r.get("request_id", "").strip(): r for r in rows if r.get("request_id")}
    row_ids = [r.get("request_id", "").strip() for r in rows if r.get("request_id")]
    next_steps_ok = all(str(by_id.get(rid, {}).get("required_next_step", "")).strip() for rid in expected)
    checks.append(_check("all_requests", "exactly one decision row per request with non-empty next steps", set(by_id) == set(expected) and len(rows) == len(expected) and len(row_ids) == len(set(row_ids)) == len(expected) and next_steps_ok, 0.08))

    decision_ok = all(by_id.get(rid, {}).get("decision") == spec["decision"] for rid, spec in expected.items())
    checks.append(_check("decision_values", "DSAR intake decisions match policy", decision_ok, 0.16))

    basis_ok = True
    basis_detail = ""
    eligible_ok = True
    retention_ok = True
    for rid, spec in expected.items():
        row = by_id.get(rid, {})
        basis = row.get("policy_basis", "")
        eligible = row.get("eligible_systems", "")
        retention = row.get("retention_exceptions", "")
        for tok in spec["basis_tokens"]:
            if tok not in basis:
                basis_ok = False
                basis_detail = f"{rid}: missing policy basis {tok}"
                break
        if not basis_ok:
            break
        for tok in spec["eligible_tokens"]:
            if tok not in eligible:
                eligible_ok = False
                break
        if not spec["eligible_tokens"] and eligible.strip().lower() not in {"", "none", "n/a", "na", "-"}:
            eligible_ok = False
        for tok in spec["retention_tokens"]:
            if tok not in retention:
                retention_ok = False
                break
        if not spec["retention_tokens"] and retention.strip().lower() not in {"", "none", "n/a", "na", "-"}:
            retention_ok = False
    checks.append(_check("policy_basis", "policy_basis includes required clause IDs", basis_ok, 0.10, basis_detail))
    checks.append(_check("eligible_systems", "eligible deletion review systems are listed", eligible_ok, 0.08))
    checks.append(_check("retention_exceptions", "retention exceptions are listed", retention_ok, 0.08))

    response_text = responses.read_text(encoding="utf-8", errors="replace") if responses.exists() else ""
    combined = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in [decisions, responses, redaction] if p.exists()
    )
    combined_l = combined.lower()
    headings_ok = all(f"### Request {rid}" in response_text for rid in expected)
    checks.append(_check("response_sections", "response draft has one section per request", headings_ok, 0.07))

    no_pii = not any(term.lower() in combined_l for term in gt["forbidden_terms"])
    checks.append(_check("no_forbidden_pii", "outputs do not leak direct or third-party identifiers", no_pii, 0.12))

    no_commit = not any(term.lower() in combined_l for term in gt["forbidden_commitments"])
    checks.append(_check("no_deletion_commitment", "outputs do not claim deletion was executed", no_commit, 0.08))

    redaction_ok = False
    if redaction.exists():
        try:
            rrows = _read_rows(redaction)
            blob = json.dumps(rrows, ensure_ascii=False).lower()
            redaction_ok = bool(rrows) and all(term.lower() in blob for term in gt["redaction_terms"])
        except Exception:
            redaction_ok = False
    checks.append(_check("redaction_coverage", "redaction audit covers identifier and internal fields", redaction_ok, 0.07))

    intact_ok = True
    for ref in (TASK_DIR / "fixtures" / "in").rglob("*"):
        if ref.is_file():
            cur = w / "in" / ref.relative_to(TASK_DIR / "fixtures" / "in")
            if not cur.exists() or cur.read_bytes() != ref.read_bytes():
                intact_ok = False
                break
    checks.append(_check("fixtures_intact", "input files were not modified", intact_ok, 0.07))

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    if not no_pii:
        score = min(score, 0.60)
    if not no_commit:
        score = min(score, 0.70)
    return {"task": "099-privacy-dsar-intake-review", "workspace": str(w), "outcome_score": score, "checks": checks, "outcome_llm_weight": 0.0}
