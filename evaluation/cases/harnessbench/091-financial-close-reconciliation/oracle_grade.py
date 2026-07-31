from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


EXPECTED_RECON = [
    {"invoice_id": "INV001", "customer_id": "C001", "invoice_usd": "100.00", "payment_usd": "100.00", "refund_usd": "0.00", "bank_fee_usd": "2.00", "net_cash_usd": "98.00", "reconciliation_status": "matched"},
    {"invoice_id": "INV002", "customer_id": "C002", "invoice_usd": "220.00", "payment_usd": "220.00", "refund_usd": "55.00", "bank_fee_usd": "3.00", "net_cash_usd": "162.00", "reconciliation_status": "matched_with_refund"},
    {"invoice_id": "INV003", "customer_id": "C003", "invoice_usd": "125.00", "payment_usd": "0.00", "refund_usd": "0.00", "bank_fee_usd": "0.00", "net_cash_usd": "0.00", "reconciliation_status": "missing_payment"},
    {"invoice_id": "INV004", "customer_id": "C004", "invoice_usd": "0.00", "payment_usd": "80.00", "refund_usd": "0.00", "bank_fee_usd": "1.00", "net_cash_usd": "79.00", "reconciliation_status": "void_invoice_cash_received"},
    {"invoice_id": "MISSING_INVOICE:P999", "customer_id": "", "invoice_usd": "0.00", "payment_usd": "50.00", "refund_usd": "0.00", "bank_fee_usd": "0.50", "net_cash_usd": "49.50", "reconciliation_status": "missing_invoice"},
]
EXPECTED_REJECTS = [
    {"item_id": "INV005", "item_type": "invoice", "reason": "fx_rate_missing", "notes": "missing JPY rate for 2026-03-07"},
    {"item_id": "R404", "item_type": "refund", "reason": "missing_invoice", "notes": "refund references INV404"},
]


def _add(checks: list[dict[str, Any]], cid: str, ok: bool, weight: float, detail: str | None = None) -> None:
    checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": None if ok else detail})


def _rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), [{k: (v or "").strip() for k, v in row.items()} for row in r]


def _close(got: Any, exp: float, tol: float = 0.01) -> bool:
    try:
        return abs(float(got) - exp) <= tol
    except Exception:
        return False


def _by_invoice(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {r.get("invoice_id", ""): r for r in rows}


def _status_semantic(invoice_id: str, status: str) -> bool:
    text = status.lower()
    allowed = {
        "INV001": ["matched", "reconciled"],
        "INV002": ["refund", "partial"],
        "INV003": ["missing_payment", "unpaid", "missing payment"],
        "INV004": ["void", "cash", "exception"],
        "MISSING_INVOICE:P999": ["missing_invoice", "missing invoice"],
    }
    return any(term in text for term in allowed.get(invoice_id, []))


def _recon_amounts_ok(rows: list[dict[str, str]]) -> bool:
    actual = _by_invoice(rows)
    if set(actual) != {r["invoice_id"] for r in EXPECTED_RECON}:
        return False
    fields = ["customer_id", "invoice_usd", "payment_usd", "refund_usd", "bank_fee_usd", "net_cash_usd"]
    for exp in EXPECTED_RECON:
        row = actual.get(exp["invoice_id"], {})
        for field in fields:
            if row.get(field) != exp[field]:
                return False
    return True


def _recon_statuses_ok(rows: list[dict[str, str]]) -> bool:
    actual = _by_invoice(rows)
    return all(_status_semantic(inv, row.get("reconciliation_status", "")) for inv, row in actual.items())


def _norm_reason(value: str) -> str:
    return value.lower().replace("missing_fx_rate", "fx_rate_missing")


def score_workspace(workspace: str | Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    checks: list[dict[str, Any]] = []
    for rel in ["invoices.csv", "payments.csv", "refunds.csv", "fx_rates.csv", "bank_statement.csv", "close_policy.md"]:
        _add(checks, f"fixture_present_{rel}", (w / "in" / rel).is_file(), 0.01, f"missing {rel}")

    recon = w / "out" / "close_reconciliation.csv"
    rejects = w / "out" / "reject_ledger.csv"
    summary = w / "out" / "reconciliation_summary.json"
    notes = w / "out" / "close_notes.md"
    _add(checks, "recon_exists", recon.is_file(), 0.05, "missing close_reconciliation.csv")
    if recon.is_file():
        try:
            header, rows = _rows(recon)
            _add(checks, "recon_header", header == ["invoice_id", "customer_id", "invoice_usd", "payment_usd", "refund_usd", "bank_fee_usd", "net_cash_usd", "reconciliation_status"], 0.06, f"got {header}")
            _add(checks, "recon_amounts", _recon_amounts_ok(rows), 0.30, f"got {rows}")
            _add(checks, "recon_statuses", _recon_statuses_ok(rows), 0.06, f"got {rows}")
            _add(checks, "refund_fx_fee_handled", any(r.get("invoice_id") == "INV002" and r.get("refund_usd") == "55.00" and r.get("bank_fee_usd") == "3.00" for r in rows), 0.08, "INV002 refund/fee wrong")
            _add(checks, "missing_invoice_row", any(r.get("invoice_id") == "MISSING_INVOICE:P999" and _status_semantic("MISSING_INVOICE:P999", r.get("reconciliation_status", "")) for r in rows), 0.06, "missing invoice payment absent")
        except Exception as exc:
            _add(checks, "recon_parseable", False, 0.30, str(exc))
    else:
        for cid, weight in [("recon_header", 0.06), ("recon_amounts", 0.30), ("recon_statuses", 0.06), ("refund_fx_fee_handled", 0.08), ("missing_invoice_row", 0.06)]:
            _add(checks, cid, False, weight, "missing recon")

    _add(checks, "rejects_exists", rejects.is_file(), 0.04, "missing reject_ledger.csv")
    if rejects.is_file():
        try:
            header, rows = _rows(rejects)
            _add(checks, "rejects_header", header == ["item_id", "item_type", "reason", "notes"], 0.04, f"got {header}")
            keys = {(r.get("item_id", ""), r.get("item_type", ""), _norm_reason(r.get("reason", ""))) for r in rows}
            expected_keys = {(r["item_id"], r["item_type"], _norm_reason(r["reason"])) for r in EXPECTED_REJECTS}
            reject_notes = " ".join(r.get("notes", "").lower() for r in rows)
            notes_ok = "jpy" in reject_notes and "2026-03-07" in reject_notes and "inv404" in reject_notes
            _add(checks, "rejects_exact", keys == expected_keys and len(rows) == len(EXPECTED_REJECTS) and notes_ok, 0.14, f"got {rows}")
        except Exception as exc:
            _add(checks, "rejects_parseable", False, 0.10, str(exc))
    else:
        _add(checks, "rejects_header", False, 0.04, "missing")
        _add(checks, "rejects_exact", False, 0.14, "missing")

    _add(checks, "summary_exists", summary.is_file(), 0.05, "missing summary")
    if summary.is_file():
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
            nums = {
                "total_invoice_usd": 445.00,
                "total_payment_usd": 450.00,
                "total_refund_usd": 55.00,
                "total_bank_fee_usd": 6.50,
                "total_net_cash_usd": 388.50,
            }
            _add(checks, "summary_numbers", all(_close(data.get(k), v) for k, v in nums.items()), 0.12, f"got {data}")
            _add(checks, "summary_exceptions", data.get("unreconciled_count") in {4, 5} and data.get("missing_invoice_payment_ids") == ["P999"] and data.get("rejected_invoice_ids") == ["INV005"], 0.08, f"got {data}")
        except Exception as exc:
            _add(checks, "summary_parseable", False, 0.14, str(exc))
    else:
        _add(checks, "summary_numbers", False, 0.12, "missing")
        _add(checks, "summary_exceptions", False, 0.08, "missing")

    _add(checks, "notes_exists", notes.is_file(), 0.03, "missing notes")
    if notes.is_file():
        text = notes.read_text(encoding="utf-8", errors="replace").lower()
        _add(checks, "notes_required_topics", all(term in text for term in ["refund", "fx", "missing invoice", "void"]), 0.05, "notes must mention refunds, FX, missing invoice, void")
    else:
        _add(checks, "notes_required_topics", False, 0.05, "missing")

    total = sum(c["weight"] for c in checks)
    score = round(sum(c["weight"] for c in checks if c["pass"]) / total, 4)
    if any(c["id"] == "recon_amounts" and not c["pass"] for c in checks):
        score = min(score, 0.69)
    return {"task": "091-financial-close-reconciliation", "workspace": str(w), "checks": checks, "outcome_score": score, "score": score}
