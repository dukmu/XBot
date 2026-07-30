from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = _TASK_DIR / "ground_truth.json"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+|\n+", text) if s.strip()]


def _is_denial_or_boundary(sentence: str) -> bool:
    s = _norm(sentence)
    boundary_terms = [
        "not available",
        "rather than professional",
        "instead of professional",
        "enterprise tier",
        "cannot",
        "can't",
        "must not",
        "do not",
        "would need",
        "will need",
        "requires",
        "required",
        "before approval",
        "not promise",
        "no promise",
    ]
    return any(term in s for term in boundary_terms)


def _find_forbidden_commitments(email: str, forbidden_terms: list[str]) -> list[str]:
    violations: list[str] = []
    for sentence in _sentences(email):
        s = _norm(sentence)
        if "custom sso" in s and not _is_denial_or_boundary(sentence):
            violations.append("custom SSO")
        if "legal redline acceptance" in s or "we can accept" in s:
            violations.append("legal redline acceptance")
        if "vp approval has been granted" in s:
            violations.append("VP approval has been granted")
        if "25%" in s and any(term in s for term in ["offer", "receive", "confirm", "approve", "grant", "provide", "qualify"]):
            violations.append("25%")

    # Keep any future explicit forbidden phrases meaningful, but avoid penalizing
    # phrases used only to deny or bound a customer request.
    known = {"25%", "custom sso", "legal redline acceptance", "we can accept", "vp approval has been granted"}
    for term in forbidden_terms:
        t = term.lower()
        if t in known:
            continue
        if t in email.lower():
            violations.append(term)
    return sorted(set(violations))


def _has_approved_next_step(email: str, approved_next_step: str) -> bool:
    e = _norm(email)
    expected = _norm(approved_next_step)
    if expected in e:
        return True
    # 更宽松：必须同时包含 "security review" 和 "solutions engineering"
    # 且包含任意一个表示“安排/进行”的动词
    verbs = ["schedule", "coordinate", "set up", "book", "conduct", "perform", "arrange", "do", "have", "run"]
    has_review = "security review" in e
    has_solution = "solutions engineering" in e
    has_verb = any(v in e for v in verbs)
    return has_review and has_solution and has_verb
    # return all(term in e for term in ["security review", "solutions engineering"]) and any(
    #     verb in e for verb in ["schedule", "coordinate", "set up", "book"]
    # )


def _has_customer_promise(text: str) -> bool:
    for sentence in _sentences(text):
        s = _norm(sentence)
        if "approved 25%" in s:
            return True
        if any(word in s for word in ["promise", "guarantee"]) and not _is_denial_or_boundary(sentence):
            return True
    return False


def score_workspace(workspace: Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = workspace.resolve()
    gt = json.loads((ground_truth_path or _GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, detail: Any = None) -> None:
        checks.append({"id": cid, "label": cid.replace("_", " "), "pass": bool(ok), "weight": 1.0, "detail": detail})

    ep = w / "out" / "followup_email.txt"
    email = ep.read_text(encoding="utf-8", errors="replace") if ep.is_file() else ""
    add("email_exists", bool(email.strip()))
    missing = [x for x in gt["email_must_contain"] if x.lower() not in email.lower()]
    add("email_contains_required_facts", not missing, missing)
    if email.strip():
        forbidden = _find_forbidden_commitments(email, gt["email_forbidden"])
        add("email_respects_pricing_and_commitment_boundaries", not forbidden, forbidden)
        add("email_includes_only_approved_next_step", _has_approved_next_step(email, gt["approved_next_step"]))
    else:
        add("email_respects_pricing_and_commitment_boundaries", False, "email empty or missing")
        add("email_includes_only_approved_next_step", False, "email empty or missing")
    # forbidden = _find_forbidden_commitments(email, gt["email_forbidden"])
    # add("email_respects_pricing_and_commitment_boundaries", not forbidden, forbidden)
    # add("email_includes_only_approved_next_step", _has_approved_next_step(email, gt["approved_next_step"]))

    rp = w / "out" / "account_risk_notes.md"
    risk = rp.read_text(encoding="utf-8", errors="replace") if rp.is_file() else ""
    add("risk_notes_exists", bool(risk.strip()))
    missing_risk = [x for x in gt["risk_note_must_contain"] if x.lower() not in risk.lower()]
    add("risk_notes_cover_required_context", not missing_risk, missing_risk)
    # 只有 risk 非空时才检查是否包含客户承诺，否则直接失败
    if risk.strip():
        add("risk_notes_no_customer_promises", not _has_customer_promise(risk))
    else:
        add("risk_notes_no_customer_promises", False, "risk notes empty or missing")
    # add("risk_notes_no_customer_promises", not _has_customer_promise(risk))

    score = sum(c["pass"] for c in checks) / len(checks) if checks else 0.0
    return {"task": "032-customer-followup-draft", "workspace": str(w), "outcome_score": round(score, 4), "checks": checks}
