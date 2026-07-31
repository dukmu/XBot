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


def _header_cells(line: str) -> list[str]:
    return [p.strip() for p in line.split(",")]


def _split_basis(cell: str) -> list[str]:
    parts = [p.strip() for p in cell.replace(",", ";").split(";")]
    return [p for p in parts if p]


def _extract_case_sections(text: str) -> dict[str, str]:
    rx = re.compile(r"^###\s*Case\s+([A-Z]-\d+)\s*$", re.MULTILINE)
    ms = list(rx.finditer(text))
    out: dict[str, str] = {}
    for i, m in enumerate(ms):
        cid = m.group(1)
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out[cid] = text[start:end]
    return out


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    decisions = w / gt["decisions_path"]
    explanations = w / gt["explanations_path"]
    redaction = w / gt["redaction_path"]
    expected = gt["expected"]
    valid_ids = set(gt["valid_clause_ids"])
    checks: list[dict[str, Any]] = []

    checks.append(_check("decisions_exists", "appeal_decisions.csv exists", decisions.is_file(), 0.04))
    checks.append(_check("explanations_exists", "explanations.md exists", explanations.is_file(), 0.04))
    checks.append(_check("redaction_exists", "redaction_audit.csv exists", redaction.is_file(), 0.04))

    want_dec_hdr = _header_cells(gt["decisions_header"])
    dh_ok = decisions.is_file() and _header_cells(_first_line(decisions)) == want_dec_hdr
    checks.append(_check("decisions_header", "appeal_decisions.csv has required columns", dh_ok, 0.03))

    want_red_hdr = _header_cells(gt["redaction_header"])
    rh_ok = redaction.is_file() and _header_cells(_first_line(redaction)) == want_red_hdr
    checks.append(_check("redaction_header", "redaction_audit.csv has required columns", rh_ok, 0.03))

    rows: list[dict[str, str]] = []
    if decisions.exists():
        with decisions.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    by_id = {r.get("case_id", "").strip(): r for r in rows if r.get("case_id")}
    checks.append(_check("all_cases", "exactly one row per expected case id", set(by_id) == set(expected), 0.06))

    decision_ok = all(by_id.get(cid, {}).get("decision") == exp["decision"] for cid, exp in expected.items())
    checks.append(_check("decisions", "appeal decisions are correct", decision_ok, 0.14))

    basis_tokens_ok = True
    basis_detail = ""
    whitelist_ok = True
    whitelist_detail = ""
    for cid, exp in expected.items():
        cell = by_id.get(cid, {}).get("policy_basis", "")
        tokens = _split_basis(cell)
        req = set(exp["policy_basis_tokens"])
        got = set(tokens)
        if not req <= got:
            basis_tokens_ok = False
            basis_detail = f"{cid}: expected clause tokens {sorted(req)} not all present in policy_basis"
            break
        if not got <= valid_ids:
            whitelist_ok = False
            whitelist_detail = f"{cid}: unknown clause id in policy_basis ({sorted(got - valid_ids)})"
            break

    checks.append(_check("policy_basis_tokens", "policy_basis lists required clause IDs", basis_tokens_ok, 0.09))
    checks.append(_check("policy_basis_whitelist", "policy_basis tokens are from policy/amendments ID set", whitelist_ok, 0.05))

    final_ok = all(
        exp["final_action_contains"].lower() in by_id.get(cid, {}).get("final_action", "").lower()
        for cid, exp in expected.items()
    )
    checks.append(_check("final_action", "final actions match outcomes", final_ok, 0.09))

    conf_ok = all(
        by_id.get(cid, {}).get("confidence", "").strip().lower() == exp["confidence"]
        for cid, exp in expected.items()
    )
    checks.append(_check("confidence_expected", "confidence matches calibration rubric per case", conf_ok, 0.05))

    text = explanations.read_text(encoding="utf-8", errors="replace") if explanations.exists() else ""
    text_l = text.lower()
    sections = _extract_case_sections(text) if text else {}
    headings_ok = explanations.is_file() and set(sections.keys()) == set(expected.keys())
    checks.append(_check("explanation_headings", "each case uses ### Case <id> heading", headings_ok, 0.06))

    sec_tokens_ok = True
    sec_detail = ""
    if headings_ok:
        for cid, exp in expected.items():
            body = sections.get(cid, "")
            bl = body.lower()
            for tok in exp["policy_basis_tokens"]:
                if tok.lower() not in bl:
                    sec_tokens_ok = False
                    sec_detail = f"{cid}: missing clause mention {tok} inside its section"
                    break
            if not sec_tokens_ok:
                break
    else:
        sec_tokens_ok = False
        sec_detail = "fix ### Case headings so every expected case id has its own section"

    checks.append(_check("explanations_clause_sections", "each section cites its row's clause IDs", sec_tokens_ok, 0.06))

    why_ok = False
    why_detail = ""
    if headings_ok:
        why_ok = True
        for cid in expected:
            bl = sections.get(cid, "").lower()
            if "rather than" not in bl and "instead of" not in bl:
                why_ok = False
                why_detail = f"{cid}: section needs contrast phrasing (rather than / instead of)"
                break
    else:
        why_detail = sec_detail or "missing structured sections"

    checks.append(_check("why_not_per_section", "each section contrasts alternatives", why_ok, 0.06))

    privacy_ok = explanations.is_file() and not any(term.lower() in text_l for term in gt["forbidden_terms"])
    checks.append(_check("privacy", "sensitive identifiers not copied into explanations", privacy_ok, 0.05))

    redaction_ok = False
    red_fields_ok = False
    red_detail = ""
    if redaction.exists():
        try:
            with redaction.open("r", encoding="utf-8-sig", newline="") as f:
                rrows = list(csv.DictReader(f))
            ids_seen = {r.get("case_id", "").strip() for r in rrows if r.get("case_id")}
            nonempty = bool(rrows) and all(
                (r.get("redacted_field") or "").strip() and (r.get("redaction_reason") or "").strip() for r in rrows
            )
            min_rows = sum(len(v) for v in gt["redaction_fields_min"].values())
            redaction_ok = ids_seen >= set(gt["redaction_cases"]) and nonempty and len(rrows) >= min_rows

            red_fields_ok = True
            for cid, needles in gt["redaction_fields_min"].items():
                case_rows = [r for r in rrows if r.get("case_id", "").strip() == cid]
                fields_seen = " ".join((r.get("redacted_field") or "").lower() for r in case_rows)
                for needle in needles:
                    if needle.lower() not in fields_seen:
                        red_fields_ok = False
                        red_detail = f"{cid}: redacted_field must reference {needle!r}"
                        break
                if not red_fields_ok:
                    break
        except Exception:
            redaction_ok = False
            red_fields_ok = False

    checks.append(_check("redaction_audit", "redaction_audit covers cases with minimum rows", redaction_ok, 0.06))
    checks.append(_check("redaction_field_coverage", "redaction lists sensitive JSON paths", red_fields_ok, 0.05))

    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0

    if basis_detail and not basis_tokens_ok:
        for c in checks:
            if c["id"] == "policy_basis_tokens" and not c["pass"]:
                c["detail"] = basis_detail
    if whitelist_detail and not whitelist_ok:
        for c in checks:
            if c["id"] == "policy_basis_whitelist" and not c["pass"]:
                c["detail"] = whitelist_detail
    if sec_detail and not sec_tokens_ok:
        for c in checks:
            if c["id"] == "explanations_clause_sections" and not c["pass"]:
                c["detail"] = sec_detail
    if why_detail and not why_ok:
        for c in checks:
            if c["id"] == "why_not_per_section" and not c["pass"]:
                c["detail"] = why_detail
    if red_detail and not red_fields_ok:
        for c in checks:
            if c["id"] == "redaction_field_coverage" and not c["pass"]:
                c["detail"] = red_detail

    return {
        "task": "075-platform-appeal-review",
        "workspace": str(w),
        "outcome_score": score,
        "checks": checks,
        "outcome_llm_weight": 0.0,
    }
