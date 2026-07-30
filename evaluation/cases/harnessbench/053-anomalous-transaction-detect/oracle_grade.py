from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_DEFAULT_GT = _TASK_DIR / "ground_truth.json"
_REASON_TERMS = {
    "R1_HIGH_VALUE": ("high", "value", "amount", "10000", "threshold"),
    "R2_GEO_AMOUNT": ("geo", "country", "non-us", "amount", "2000", "ng"),
    "R3_CARD_VELOCITY": ("velocity", "card", "10", "minute", "window"),
    "R4_COUNTRY_MISMATCH": ("mismatch", "billing", "ip", "country"),
}


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append({k: ("" if v is None else str(v).strip()) for k, v in row.items() if k is not None})
        return list(reader.fieldnames or []), rows


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixtures_unchanged(workspace: Path, gt: dict[str, Any]) -> bool:
    for rel, digest in gt.get("fixture_hashes", {}).items():
        candidate = workspace / "in" / rel
        if not candidate.is_file() or _sha256(candidate) != digest:
            return False
    return True


def _reason_explains_rule(row: dict[str, str]) -> bool:
    reason = row.get("reason", "").lower()
    rule_id = row.get("rule_id", "")
    if rule_id.lower() in reason:
        return True
    terms = _REASON_TERMS.get(rule_id, ())
    return bool(reason) and sum(term in reason for term in terms) >= 2


def _notes_count_ok(text: str) -> bool:
    t = " ".join(text.lower().split())
    return (
        any(phrase in t for phrase in ("18 suspicious", "eighteen suspicious", "18 transactions", "eighteen transactions"))
        or re.search(r"suspicious transactions[^0-9a-z]+detected[^0-9a-z]+18\b", t) is not None
        or re.search(r"total[^0-9a-z]+suspicious transactions[^0-9a-z]+18\b", t) is not None
    )


def score_workspace(workspace: str | Path, *, ground_truth_path: Path | None = None) -> dict[str, Any]:
    w = Path(workspace).resolve()
    gt = json.loads((ground_truth_path or _DEFAULT_GT).read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    _add(checks, "fixtures_present", (w / "in" / "transactions.csv").is_file() and (w / "in" / "rules.md").is_file(), 0.08, "missing fixture")
    _add(checks, "fixtures_unchanged", _fixtures_unchanged(w, gt), 0.08, "one or more input files are missing or modified")
    out = w / gt["outputs"]["csv"]
    audit = w / gt["outputs"].get("audit", "out/rule_audit.json")
    notes = w / gt["outputs"]["notes"]
    _add(checks, "csv_exists", out.is_file(), 0.08, "missing suspicious_transactions.csv")
    _add(checks, "rule_audit_exists", audit.is_file(), 0.08, "missing rule_audit.json")
    _add(checks, "notes_exists", notes.is_file(), 0.06, "missing case_notes.md")
    if out.is_file():
        try:
            header, rows = _read(out)
            slim = sorted(
                [{k: r.get(k, "") for k in ["transaction_id", "customer_id", "rule_id", "risk_level"]} for r in rows],
                key=lambda r: r["transaction_id"],
            )
            expected = sorted(gt["rows"], key=lambda r: r["transaction_id"])
            ids = {r["transaction_id"] for r in rows}
            _add(checks, "exact_header", header == gt["header"], 0.08, f"got {header}")
            by_id = {r.get("transaction_id", ""): r for r in rows}
            _add(checks, "expected_suspicious_set", slim == expected, 0.30, f"got {slim}")
            _add(checks, "no_false_positives", not (ids & set(gt["non_suspicious"])), 0.10, f"false positives {sorted(ids & set(gt['non_suspicious']))}")
            _add(checks, "reason_explains_rule", all(_reason_explains_rule(r) for r in rows), 0.08, "each reason must explain the triggered rule")
            _add(checks, "velocity_inclusive_boundary", {"T010", "T011", "T012"} <= ids and by_id.get("T010", {}).get("rule_id") == "R3_CARD_VELOCITY", 0.06, "inclusive 10-minute same-card window missed")
            _add(checks, "velocity_exclusive_boundary", not ({"T013", "T014", "T015"} & ids), 0.04, "10 minutes and 1 second should not qualify")
            _add(checks, "velocity_by_card_not_customer", not ({"T016", "T017", "T018"} & ids), 0.04, "velocity must group by card_id, not customer_id")
            _add(checks, "timestamp_order_velocity", {"T019", "T020", "T021"} <= ids, 0.06, "velocity must use timestamp order, not CSV order")
            _add(checks, "second_unsorted_velocity_window", {"T023", "T024", "T025"} <= ids, 0.05, "second out-of-order same-card velocity window missed")
            _add(checks, "near_miss_velocity_and_thresholds", not ({"T026", "T027", "T028", "T029", "T030"} & ids), 0.05, "near-miss velocity or threshold transactions should not be suspicious")
            _add(checks, "multi_rule_highest_risk", by_id.get("T011", {}).get("rule_id") == "R1_HIGH_VALUE" and by_id.get("T012", {}).get("rule_id") == "R2_GEO_AMOUNT", 0.04, "highest-risk rule not selected")
            t022_reason = by_id.get("T022", {}).get("reason", "").lower()
            _add(checks, "high_risk_tiebreak", by_id.get("T022", {}).get("rule_id") == "R1_HIGH_VALUE" and "r2" in t022_reason and "r4" in t022_reason, 0.04, "same-risk tie or secondary rule mention wrong for T022")
            t031_reason = by_id.get("T031", {}).get("reason", "").lower()
            _add(checks, "second_high_risk_tiebreak", by_id.get("T031", {}).get("rule_id") == "R1_HIGH_VALUE" and "r2" in t031_reason and "r4" in t031_reason, 0.04, "same-risk tie or secondary rule mention wrong for T031")
            secondary_ok = True
            secondary_detail = {}
            for txid, rules in gt.get("secondary_rules", {}).items():
                reason = by_id.get(txid, {}).get("reason", "").lower()
                missing = [rid for rid in rules if rid.lower() not in reason and rid.split("_", 1)[0].lower() not in reason]
                secondary_detail[txid] = missing
                secondary_ok = secondary_ok and not missing
            _add(checks, "secondary_rules_mentioned", secondary_ok, 0.05, f"missing secondary rules {secondary_detail}")
        except Exception as exc:
            _add(checks, "csv_readable", False, 0.30, str(exc))
    else:
        for cid, weight in [
            ("exact_header", 0.08),
            ("expected_suspicious_set", 0.30),
            ("no_false_positives", 0.10),
            ("reason_explains_rule", 0.08),
            ("velocity_inclusive_boundary", 0.06),
            ("velocity_exclusive_boundary", 0.04),
            ("velocity_by_card_not_customer", 0.04),
            ("timestamp_order_velocity", 0.06),
            ("second_unsorted_velocity_window", 0.05),
            ("near_miss_velocity_and_thresholds", 0.05),
            ("multi_rule_highest_risk", 0.04),
            ("high_risk_tiebreak", 0.04),
            ("second_high_risk_tiebreak", 0.04),
            ("secondary_rules_mentioned", 0.05),
        ]:
            _add(checks, cid, False, weight, "missing suspicious_transactions.csv")
    if notes.is_file():
        text = notes.read_text(encoding="utf-8", errors="replace")
        for rid in gt["rule_ids"]:
            _add(checks, f"notes_mentions_{rid}", rid in text, 0.015, f"missing {rid}")
        _add(checks, "notes_count", _notes_count_ok(text), 0.04, "notes should mention thirteen suspicious transactions")
    if audit.is_file():
        try:
            data = json.loads(audit.read_text(encoding="utf-8"))
            expected = gt["rule_audit_expected"]
            _add(checks, "rule_audit_keys", isinstance(data, dict) and set(data) == set(expected), 0.04, f"got keys {sorted(data) if isinstance(data, dict) else type(data)}")
            _add(checks, "rule_audit_suspicious_ids", data.get("suspicious_transaction_ids") == expected["suspicious_transaction_ids"], 0.08, f"got {data.get('suspicious_transaction_ids')}")
            _add(checks, "rule_audit_non_suspicious_ids", data.get("non_suspicious_transaction_ids") == expected["non_suspicious_transaction_ids"], 0.08, f"got {data.get('non_suspicious_transaction_ids')}")
            _add(checks, "rule_audit_counts", data.get("rule_counts") == expected["rule_counts"], 0.08, f"got {data.get('rule_counts')}")
            _add(checks, "rule_audit_secondary_rules", data.get("secondary_rule_ids") == expected["secondary_rule_ids"], 0.08, f"got {data.get('secondary_rule_ids')}")
        except Exception as exc:
            _add(checks, "rule_audit_parseable", False, 0.20, str(exc))
    else:
        for cid, weight in [
            ("rule_audit_keys", 0.04),
            ("rule_audit_suspicious_ids", 0.08),
            ("rule_audit_non_suspicious_ids", 0.08),
            ("rule_audit_counts", 0.08),
            ("rule_audit_secondary_rules", 0.08),
        ]:
            _add(checks, cid, False, weight, "missing rule_audit.json")
    total_w = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total_w, 4) if total_w else 0.0
    if any(c["id"].startswith("rule_audit_") and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "053-anomalous-transaction-detect", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
