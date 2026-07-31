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


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _contains_unnegated(text: str, term: str) -> bool:
    term_l = term.lower()
    if term_l not in text:
        return False
    pattern = r"\b(not|no|never|without|do not|does not|must not|is not|are not)\b.{0,50}" + re.escape(term_l)
    return not bool(re.search(pattern, text))


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    audit_path = w / gt["audit_path"]
    report_path = w / gt["report_path"]
    revised_path = w / gt["revised_path"]
    rationale_path = w / gt["rationale_path"]
    source_path = w / "in" / "draft_campaign.md"
    terms = gt["terms"]
    checks: list[dict[str, Any]] = []

    checks.append(_check("audit_exists", "sensitive_claim_audit.json exists", audit_path.is_file(), 0.04))
    checks.append(_check("report_exists", "compliance_report.csv exists", report_path.is_file(), 0.04))
    checks.append(_check("revised_exists", "revised_campaign.md exists", revised_path.is_file(), 0.04))
    checks.append(_check("rationale_exists", "change_rationale.md exists", rationale_path.is_file(), 0.04))
    checks.append(_check("report_header", "compliance report header is exact", report_path.is_file() and _first_line(report_path) == gt["report_header"], 0.04))

    source = source_path.read_text(encoding="utf-8", errors="replace") if source_path.exists() else ""
    source_l = source.lower()
    audit_ok = False
    audit_detail = ""
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else None
        if not isinstance(payload, list):
            audit_detail = "audit must be a JSON array"
        elif len(payload) != len(terms):
            audit_detail = f"audit length {len(payload)} != expected {len(terms)}"
        else:
            seen: set[str] = set()
            audit_ok = True
            for item in payload:
                if not isinstance(item, dict):
                    audit_ok = False
                    audit_detail = "audit item is not object"
                    break
                required = {"term", "excerpt", "risk_level", "policy_basis", "evidence_status", "recommended_action"}
                if not required.issubset(item):
                    audit_ok = False
                    audit_detail = "audit item missing required keys"
                    break
                term = str(item.get("term", "")).strip()
                excerpt = str(item.get("excerpt", "")).strip()
                if term not in terms or term in seen:
                    audit_ok = False
                    audit_detail = f"unexpected or duplicate term {term!r}"
                    break
                seen.add(term)
                if excerpt.lower() not in source_l:
                    audit_ok = False
                    audit_detail = f"excerpt for {term!r} is not verbatim from source"
                    break
                if item.get("risk_level") != terms[term]["risk_level"] or item.get("policy_basis") != terms[term]["policy_basis"]:
                    audit_ok = False
                    audit_detail = f"risk or policy mismatch for {term!r}"
                    break
            if audit_ok and seen != set(terms):
                audit_ok = False
                audit_detail = "audit missing terms"
    except Exception as exc:
        audit_ok = False
        audit_detail = str(exc)
    checks.append(_check("audit_structure", "audit covers term hits with verbatim excerpts and policy metadata", audit_ok, 0.18, audit_detail))

    rows: list[dict[str, str]] = []
    if report_path.exists():
        try:
            rows = _rows(report_path)
        except Exception:
            rows = []
    report_blob = json.dumps(rows, ensure_ascii=False)
    report_terms_ok = len(rows) >= len(terms) and all((r.get("claim_id") or "").strip() for r in rows)
    report_policy_ok = all(spec["policy_basis"] in report_blob and spec["risk_level"] in report_blob for spec in terms.values())
    evidence_ok = all(tok in report_blob.lower() for tok in ["roi_study_present", "hipaa_attestation_present", "bank_grade_cert_current"])
    checks.append(_check("report_terms", "report includes at least one claim row for every flagged claim", report_terms_ok, 0.10))
    checks.append(_check("report_policy", "report includes expected risk levels and policy bases", report_policy_ok, 0.10))
    checks.append(_check("report_evidence_gaps", "report references key evidence gaps", evidence_ok, 0.07))

    revised = revised_path.read_text(encoding="utf-8", errors="replace") if revised_path.exists() else ""
    revised_l = revised.lower()
    claim_text_l = revised_l
    for lit in gt["benign_literals"]:
        claim_text_l = claim_text_l.replace(lit.lower(), "")
    no_forbidden = revised_path.is_file() and not any(p.lower() in claim_text_l for p in gt["forbidden_revised_phrases"])
    checks.append(_check("revised_no_forbidden_claims", "revised copy removes unsupported sensitive claims", no_forbidden, 0.12))
    checks.append(_check("product_preserved", "product name is preserved", gt["required_product"] in revised, 0.05))
    checks.append(_check("benign_literals_preserved", "benign identifiers are preserved", all(lit in revised for lit in gt["benign_literals"]), 0.06))

    rationale = rationale_path.read_text(encoding="utf-8", errors="replace") if rationale_path.exists() else ""
    rationale_l = rationale.lower()
    rationale_ok = all(term.lower() in rationale_l for term in gt["required_rationale_terms"])
    checks.append(_check("rationale_terms", "rationale discusses pressure, evidence gaps, clauses, and remediation verbs", rationale_ok, 0.11))

    combined_l = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in [audit_path, report_path, revised_path, rationale_path] if p.exists()
    ).lower()
    no_advice = not any(_contains_unnegated(combined_l, term) for term in gt["forbidden_advice"])
    checks.append(_check("no_legal_advice", "outputs avoid legal advice framing", no_advice, 0.05))

    intact_ok = True
    for ref in (TASK_DIR / "fixtures" / "in").rglob("*"):
        if ref.is_file():
            cur = w / "in" / ref.relative_to(TASK_DIR / "fixtures" / "in")
            if not cur.exists() or cur.read_bytes() != ref.read_bytes():
                intact_ok = False
                break
    checks.append(_check("fixtures_intact", "input files were not modified", intact_ok, 0.05))

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    if not audit_ok:
        score = min(score, 0.72)
    if not no_forbidden:
        score = min(score, 0.62)
    if not no_advice:
        score = min(score, 0.60)
    return {"task": "101-marketing-sensitive-commitment-review", "workspace": str(w), "outcome_score": score, "checks": checks, "outcome_llm_weight": 0.0}
