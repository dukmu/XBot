from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = json.loads((_TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    dbdir = w / "in" / "db"
    if not dbdir.exists():
        dbdir = w / "db"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    conn = sqlite3.connect(":memory:")
    execute_score = 0.0
    data_score = 0.0
    orders_score = 0.0
    constraint_score = 0.0
    try:
        conn.executescript((dbdir / "schema.sql").read_text(encoding="utf-8"))
        conn.executescript((dbdir / "migration.sql").read_text(encoding="utf-8"))
        execute_score = 1.0
        add("migration_executes", True, 0.22)
        rows = conn.execute("select id, email, name, created_at, status from users order by id").fetchall()
        data_score = 1.0 if rows == [tuple(row) for row in _GT["expected_users"]] else 0.0
        add("data_preserved", data_score == 1.0, 0.30, rows)
        try:
            order_rows = conn.execute("select id, user_id, total_cents, created_at from orders order by id").fetchall()
        except sqlite3.Error:
            order_rows = []
        orders_score = 1.0 if order_rows == [tuple(row) for row in _GT["expected_orders"]] else 0.0
        add("orders_preserved", orders_score == 1.0, 0.08, order_rows)
        cols = conn.execute("pragma table_info(users)").fetchall()
        status_not_null = any(col[1] == "status" and col[3] == 1 for col in cols)
        email_not_null = any(col[1] == "email" and col[3] == 1 for col in cols)
        duplicate_rejected = False
        null_rejected = False
        blank_rejected = False
        try:
            conn.execute("insert into users(id, email, name, created_at, status) values ('u4','ada@example.com','Dup','2024-05-01','active')")
        except sqlite3.IntegrityError:
            duplicate_rejected = True
        try:
            conn.execute("insert into users(id, email, name, created_at, status) values ('u5',NULL,'Null','2024-05-01','active')")
        except sqlite3.IntegrityError:
            null_rejected = True
        try:
            conn.execute("insert into users(id, email, name, created_at, status) values ('u6','','Blank','2024-05-01','active')")
        except sqlite3.IntegrityError:
            blank_rejected = True
        constraint_score = (status_not_null + email_not_null + duplicate_rejected + null_rejected + blank_rejected) / 5
        add("constraints", constraint_score == 1.0, 0.22, {"status_not_null": status_not_null, "email_not_null": email_not_null, "duplicate_rejected": duplicate_rejected, "null_rejected": null_rejected, "blank_rejected": blank_rejected})
        idempotent_ok = False
        try:
            conn.executescript((dbdir / "migration.sql").read_text(encoding="utf-8"))
            rows_after_second = conn.execute("select id, email, name, created_at, status from users order by id").fetchall()
            try:
                orders_after_second = conn.execute("select id, user_id, total_cents, created_at from orders order by id").fetchall()
            except sqlite3.Error:
                orders_after_second = []
            idempotent_ok = rows_after_second == [tuple(row) for row in _GT["expected_users"]] and orders_after_second == [tuple(row) for row in _GT["expected_orders"]]
        except Exception:
            idempotent_ok = False
        add("idempotent", idempotent_ok, 0.08, "migration must be safe to run twice")
    except Exception as exc:
        add("migration_executes", False, 0.22, str(exc))
        idempotent_ok = False

    sql = (dbdir / "migration.sql").read_text(encoding="utf-8", errors="replace").lower()
    term_hits = sum(term in sql for term in _GT["required_sql_terms"])
    forbidden_hits = [term for term in _GT["forbidden_sql_terms"] if term in sql]
    quality_score = 0.8 * min(term_hits / len(_GT["required_sql_terms"]), 1.0) + 0.2 * (not forbidden_hits)
    add("sql_quality", quality_score >= 0.70, 0.12, {"term_hits": term_hits, "forbidden": forbidden_hits})
    preflight_text = (dbdir / "preflight_report.md").read_text(encoding="utf-8", errors="replace").lower() if (dbdir / "preflight_report.md").is_file() else ""
    preflight_score = float(all(term in preflight_text for term in ["duplicate", "null", "blank", "u4", "u5", "u6", "order"]))
    add("preflight_report", preflight_score == 1.0, 0.08, "preflight must mention duplicate/null/blank u4/u5/u6 issues and dependent orders")
    rollback_text = (dbdir / "rollback.sql").read_text(encoding="utf-8", errors="replace").lower() if (dbdir / "rollback.sql").is_file() else ""
    rollback_sql = _strip_sql_comments(rollback_text)
    rollback_exec_ok = False
    try:
        rconn = sqlite3.connect(":memory:")
        rconn.executescript((dbdir / "schema.sql").read_text(encoding="utf-8"))
        rconn.executescript((dbdir / "migration.sql").read_text(encoding="utf-8"))
        rconn.executescript((dbdir / "rollback.sql").read_text(encoding="utf-8"))
        rollback_cols = [row[1] for row in rconn.execute("pragma table_info(users)").fetchall()]
        rollback_rows = rconn.execute("select id, email, name, created_at from users order by id").fetchall()
        try:
            rollback_orders = rconn.execute("select id, user_id, total_cents, created_at from orders order by id").fetchall()
        except sqlite3.Error:
            rollback_orders = []
        rollback_exec_ok = rollback_cols == ["id", "email", "name", "created_at"] and len(rollback_rows) == len(_GT["expected_users"]) and rollback_orders == [tuple(row) for row in _GT["expected_orders"]]
    except Exception:
        rollback_exec_ok = False
    rollback_score = float(all(term in rollback_sql for term in ["create table", "users", "created_at"]) and "status" not in rollback_sql and rollback_exec_ok)
    add("rollback_sql", rollback_score == 1.0, 0.06, "rollback should restore old schema shape")
    postcheck_text = (dbdir / "postcheck.sql").read_text(encoding="utf-8", errors="replace").lower() if (dbdir / "postcheck.sql").is_file() else ""
    postcheck_score = 0.0
    if postcheck_text:
        term_hits = sum(term in postcheck_text for term in _GT.get("postcheck_terms", []))
        postcheck_exec_ok = False
        try:
            pconn = sqlite3.connect(":memory:")
            pconn.executescript((dbdir / "schema.sql").read_text(encoding="utf-8"))
            pconn.executescript((dbdir / "migration.sql").read_text(encoding="utf-8"))
            pconn.executescript((dbdir / "postcheck.sql").read_text(encoding="utf-8"))
            postcheck_exec_ok = True
        except Exception:
            postcheck_exec_ok = False
        postcheck_score = 0.75 * (term_hits / max(len(_GT.get("postcheck_terms", [])), 1)) + 0.25 * bool(postcheck_exec_ok)
        add("postcheck_sql", postcheck_score >= 0.85, 0.06, {"score": round(postcheck_score, 4), "term_hits": term_hits, "executes": postcheck_exec_ok})
    else:
        add("postcheck_sql", False, 0.06, "missing")
    report_text = (dbdir / "migration_report.md").read_text(encoding="utf-8", errors="replace").lower() if (dbdir / "migration_report.md").is_file() else ""
    report_hits = sum(term in report_text for term in _GT.get("migration_report_terms", []))
    report_score = report_hits / max(len(_GT.get("migration_report_terms", [])), 1)
    add("migration_report", report_score >= 0.85, 0.04, {"score": round(report_score, 4), "hits": report_hits})
    fixture_intact = [_md5(dbdir / rel) == digest for rel, digest in _GT.get("fixture_hashes", {}).items()]
    fixture_score = sum(fixture_intact) / max(len(fixture_intact), 1)
    add("fixture_integrity", fixture_score == 1.0, 0.05, {"score": fixture_score})
    total = execute_score * 0.13 + data_score * 0.20 + orders_score * 0.08 + constraint_score * 0.17 + quality_score * 0.09 + preflight_score * 0.06 + rollback_score * 0.06 + postcheck_score * 0.06 + report_score * 0.04 + (1.0 if idempotent_ok else 0.0) * 0.07 + fixture_score * 0.04
    if postcheck_score < 0.60 or report_score < 0.60:
        total = min(total, 0.84)
    thresholds = _GT["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "043-db-migration-safety", "outcome_score": round(total, 4), "level": level, "checks": checks}
