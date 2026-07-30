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


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    report = w / gt["report_path"]
    revised = w / gt["revised_path"]
    rationale_path = w / gt["rationale_path"]
    hit_audit = w / gt["hit_audit_path"]
    expected_keys = {"term", "risk_level", "location", "policy_basis", "recommended_fix"}
    benign = list(gt.get("benign_literals") or ["guaranteed_delivery_flag"])

    checks: list[dict[str, Any]] = []
    checks.append(_check("report_exists", "compliance_report.csv exists", report.is_file(), 0.05))
    checks.append(_check("revised_exists", "revised_copy.md exists", revised.is_file(), 0.05))
    checks.append(_check("rationale_exists", "change_rationale.md exists", rationale_path.is_file(), 0.04))
    checks.append(_check("hit_audit_exists", "term_hit_audit.json exists", hit_audit.is_file(), 0.03))

    header_line_ok = report.is_file() and _first_line(report) == gt["report_header"]
    checks.append(_check("csv_header_line", "report first row matches required header", header_line_ok, 0.05))

    rows: list[dict[str, str]] = []
    dict_keys_ok = False
    if report.exists():
        rows = _rows(report)
        dict_keys_ok = bool(rows) and set(rows[0].keys()) == expected_keys
    checks.append(_check("csv_columns", "report DictReader columns match", dict_keys_ok, 0.05))

    found = {r.get("term", "").strip().lower(): r for r in rows if r.get("term", "").strip()}
    dup_ok = len(found) == len(rows)
    checks.append(_check("csv_unique_terms", "at most one CSV row per term key", dup_ok, 0.03))

    terms_map: dict[str, Any] = gt["terms"]
    all_terms = all(t.lower() in found for t in terms_map)
    checks.append(_check("all_terms", "report includes every regulated term hit", all_terms, 0.135))

    risk_ok = all(
        found.get(t.lower(), {}).get("risk_level") == spec["risk_level"]
        and found.get(t.lower(), {}).get("policy_basis") == spec["policy_basis"]
        for t, spec in terms_map.items()
    )
    checks.append(_check("risk_policy", "risk levels and policy bases match schedule", risk_ok, 0.135))

    fixes_ok = all(found.get(t.lower(), {}).get("recommended_fix", "").strip() for t in terms_map)
    loc_ok = all(found.get(t.lower(), {}).get("location", "").strip() for t in terms_map)
    checks.append(_check("recommended_fixes", "each finding includes recommended_fix", fixes_ok, 0.05))
    checks.append(_check("locations_non_empty", "each finding includes location", loc_ok, 0.05))

    source_lc = ""
    src_path = w / gt["marketing_copy_source"]
    if src_path.is_file():
        source_lc = src_path.read_text(encoding="utf-8", errors="replace").lower()

    hit_ok = False
    hit_detail = ""
    if hit_audit.is_file() and source_lc:
        try:
            payload = json.loads(hit_audit.read_text(encoding="utf-8"))
            need = {t.lower() for t in terms_map}
            if not isinstance(payload, list):
                hit_detail = "term_hit_audit.json must be a JSON array"
            elif len(payload) != len(need):
                hit_detail = f"hit audit length {len(payload)} != expected {len(need)}"
            else:
                seen: set[str] = set()
                hit_ok = True
                for item in payload:
                    if not isinstance(item, dict):
                        hit_ok = False
                        hit_detail = "each hit audit entry must be an object"
                        break
                    term_k = str(item.get("term", "")).strip().lower()
                    excerpt = str(item.get("excerpt", "")).strip()
                    if term_k not in need:
                        hit_ok = False
                        hit_detail = f"unexpected audit term {term_k!r}"
                        break
                    if term_k in seen:
                        hit_ok = False
                        hit_detail = f"duplicate audit term {term_k!r}"
                        break
                    seen.add(term_k)
                    if len(excerpt) < 2:
                        hit_ok = False
                        hit_detail = f"{term_k}: excerpt too short"
                        break
                    if excerpt.lower() not in source_lc:
                        hit_ok = False
                        hit_detail = f"{term_k}: excerpt not verbatim from marketing_copy.md"
                        break
                if hit_ok and seen != need:
                    hit_ok = False
                    hit_detail = "hit audit missing some regulated terms"
        except Exception as exc:
            hit_ok = False
            hit_detail = str(exc)

    checks.append(_check("hit_audit_structure", "term_hit_audit mirrors source spans", hit_ok, 0.05))

    text = revised.read_text(encoding="utf-8", errors="replace") if revised.exists() else ""
    text_l = text.lower()
    claim_text_l = text_l
    for lit in benign:
        claim_text_l = claim_text_l.replace(lit.lower(), "")
    no_forbidden = revised.is_file() and not any(p.lower() in claim_text_l for p in gt["forbidden_revised_phrases"])
    checks.append(_check("revised_no_forbidden", "revised copy removes prohibited phrases", no_forbidden, 0.09))

    checks.append(_check("product_preserved", "revised copy preserves product name", revised.is_file() and gt["required_product"] in text, 0.05))

    benign_ok = revised.is_file() and all(lit in text for lit in benign)
    checks.append(_check("benign_literals_preserved", "benign identifiers stay in revised copy", benign_ok, 0.05))

    comparative_ok = (
        revised.is_file()
        and ("may help" in text_l or "can help" in text_l or "helps" in text_l)
        and "faster than" not in text_l
    )
    checks.append(_check("comparative_softened", "comparative claim stays qualified", comparative_ok, 0.04))

    rationale = rationale_path.read_text(encoding="utf-8", errors="replace").lower() if rationale_path.exists() else ""
    rationale_ok = all(term.lower() in rationale for term in gt["required_rationale_terms"])
    checks.append(_check("rationale_policy_context", "rationale cites clauses and remediation vocabulary", rationale_ok, 0.05))

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0

    if hit_detail and not hit_ok:
        for c in checks:
            if c["id"] == "hit_audit_structure" and not c["pass"]:
                c["detail"] = hit_detail

    return {"task": "069-legal-compliance-review", "workspace": str(w), "outcome_score": score, "checks": checks}
