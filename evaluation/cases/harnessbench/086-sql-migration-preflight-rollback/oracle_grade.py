from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any


EXPECTED_INVOICES = [
    ("inv1", "cust-a", 1200, "2024-01-03T10:00:00Z", "open"),
    ("inv2", "cust-b", 5000, "2024-01-04T10:00:00Z", "open"),
    ("inv3", "cust-a", 800, "2024-01-05T10:00:00Z", "open"),
]
EXPECTED_PAYMENTS = [
    ("p1", "inv1", 1200, "2024-01-06T10:00:00Z"),
    ("p2", "inv2", 2500, "2024-01-06T11:00:00Z"),
    ("p3", "inv2", 2500, "2024-01-06T12:00:00Z"),
]
EXPECTED_ORPHANS = [("p4", "missing-invoice", 700, "2024-01-07T10:00:00Z", "missing invoice")]
SCHEMA_HASH = "60c816960f941b2ffbc31a035a76ad42"
POLICY_HASH = "75b8f98e14a1f6a8dd2fbda6a88d10bd"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)


def _orphans_match(rows: list[tuple[Any, ...]]) -> bool:
    if len(rows) != len(EXPECTED_ORPHANS):
        return False
    for row, expected in zip(rows, EXPECTED_ORPHANS):
        if tuple(row[:4]) != expected[:4]:
            return False
        reason = str(row[4] if len(row) > 4 else "").lower()
        if not (("missing" in reason or "non-existent" in reason or "nonexistent" in reason) and "invoice" in reason):
            return False
    return True


def score_workspace(workspace: Path) -> dict[str, Any]:
    db = Path(workspace).resolve() / "in" / "billingdb"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    migration = (db / "migration.sql").read_text(encoding="utf-8", errors="replace")
    conn = sqlite3.connect(":memory:")
    exec_score = data_score = constraint_score = idempotent_score = 0.0
    try:
        conn.executescript((db / "schema.sql").read_text(encoding="utf-8"))
        conn.executescript(migration)
        exec_score = 1.0
        add("migration_executes", True, 0.15)
        invoices = conn.execute("select id, customer_id, total_cents, created_at, status from invoices order by id").fetchall()
        payments = conn.execute("select id, invoice_id, amount_cents, created_at from payments order by id").fetchall()
        orphans = conn.execute("select id, invoice_id, amount_cents, created_at, reason from payment_orphans order by id").fetchall()
        data_score = sum([
            invoices == EXPECTED_INVOICES,
            payments == EXPECTED_PAYMENTS,
            _orphans_match(orphans),
        ]) / 3
        add("data_preservation", data_score == 1.0, 0.25, {"invoices": invoices, "payments": payments, "orphans": orphans})
        cols = conn.execute("pragma table_info(invoices)").fetchall()
        status_not_null = any(col[1] == "status" and col[3] == 1 for col in cols)
        fk_list = conn.execute("pragma foreign_key_list(payments)").fetchall()
        fk_exists = any(row[2] == "invoices" for row in fk_list)
        bad_insert_rejected = False
        try:
            conn.execute("insert into payments(id, invoice_id, amount_cents, created_at) values ('px','nope',1,'2024')")
        except sqlite3.IntegrityError:
            bad_insert_rejected = True
        constraint_score = sum([status_not_null, fk_exists, bad_insert_rejected]) / 3
        add("constraints", constraint_score == 1.0, 0.20, {"status_not_null": status_not_null, "fk_exists": fk_exists, "bad_insert_rejected": bad_insert_rejected})
        conn.executescript(migration)
        invoices2 = conn.execute("select id, customer_id, total_cents, created_at, status from invoices order by id").fetchall()
        payments2 = conn.execute("select id, invoice_id, amount_cents, created_at from payments order by id").fetchall()
        orphans2 = conn.execute("select id, invoice_id, amount_cents, created_at, reason from payment_orphans order by id").fetchall()
        idempotent_score = 1.0 if (invoices2 == EXPECTED_INVOICES and payments2 == EXPECTED_PAYMENTS and _orphans_match(orphans2)) else 0.0
        add("idempotent", idempotent_score == 1.0, 0.10)
    except Exception as exc:
        add("migration_executes", False, 0.15, str(exc))

    sql = _strip_comments(migration.lower())
    terms = ["begin", "commit", "payment_orphans", "foreign key", "status"]
    sql_quality = 0.8 * (sum(term in sql for term in terms) / len(terms)) + 0.2 * ("delete from payments" not in sql)
    add("sql_quality", sql_quality >= 0.60, 0.08, {"score": round(sql_quality, 4)})

    rollback_score = 0.0
    try:
        rconn = sqlite3.connect(":memory:")
        rconn.executescript((db / "schema.sql").read_text(encoding="utf-8"))
        rconn.executescript(migration)
        rconn.executescript((db / "rollback.sql").read_text(encoding="utf-8"))
        inv_cols = [row[1] for row in rconn.execute("pragma table_info(invoices)").fetchall()]
        pay_cols = [row[1] for row in rconn.execute("pragma table_info(payments)").fetchall()]
        inv_rows = rconn.execute("select id, customer_id, total_cents, created_at from invoices order by id").fetchall()
        rollback_score = 1.0 if inv_cols == ["id", "customer_id", "total_cents", "created_at"] and pay_cols == ["id", "invoice_id", "amount_cents", "created_at"] and len(inv_rows) == 3 else 0.0
    except Exception:
        rollback_score = 0.0
    add("rollback_executes", rollback_score == 1.0, 0.08)

    postcheck_score = 0.0
    try:
        pconn = sqlite3.connect(":memory:")
        pconn.executescript((db / "schema.sql").read_text(encoding="utf-8"))
        pconn.executescript(migration)
        pconn.executescript((db / "postcheck.sql").read_text(encoding="utf-8"))
        post_text = (db / "postcheck.sql").read_text(encoding="utf-8", errors="replace").lower()
        postcheck_score = sum(term in post_text for term in ["count", "payment_orphans", "foreign_key_check", "status"]) / 4
    except Exception:
        postcheck_score = 0.0
    add("postcheck_sql", postcheck_score >= 0.75, 0.06, {"score": postcheck_score})

    preflight = (db / "preflight_report.md").read_text(encoding="utf-8", errors="replace").lower() if (db / "preflight_report.md").is_file() else ""
    report = (db / "migration_report.md").read_text(encoding="utf-8", errors="replace").lower() if (db / "migration_report.md").is_file() else ""
    doc_score = (
        sum(term in preflight for term in ["orphan", "p4", "missing-invoice", "row count"]) / 4 * 0.45
        + sum(term in report for term in ["idempotent", "rollback", "postcheck", "payment_orphans", "foreign key"]) / 5 * 0.55
    )
    add("documentation", doc_score >= 0.75, 0.05, {"score": round(doc_score, 4)})

    integrity = ((db / "schema.sql").is_file() and _md5(db / "schema.sql") == SCHEMA_HASH) and ((db / "migration_policy.md").is_file() and _md5(db / "migration_policy.md") == POLICY_HASH)
    add("fixture_integrity", integrity, 0.03)

    total = exec_score * 0.15 + data_score * 0.25 + constraint_score * 0.20 + idempotent_score * 0.10 + sql_quality * 0.08 + rollback_score * 0.08 + postcheck_score * 0.06 + doc_score * 0.05 + (1.0 if integrity else 0.0) * 0.03
    if data_score < 1.0 or constraint_score < 1.0:
        total = min(total, 0.70)
    if not integrity:
        total = min(total, 0.60)
    level = "excellent" if total >= 0.9 else "good" if total >= 0.75 else "pass" if total >= 0.6 else "fail"
    return {"task": "086-sql-migration-preflight-rollback", "outcome_score": round(total, 4), "level": level, "checks": checks}
