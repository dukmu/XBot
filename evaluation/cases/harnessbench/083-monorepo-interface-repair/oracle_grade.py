from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any


TEST_HASHES = {
    "tests/test_catalog_contract.py": "ba4aa4d37f5787f0b1e2badc5b408818",
    "tests/test_orders_report_integration.py": "54ce50bca13c03936b276fcb43d49d6b",
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    project = Path(workspace).resolve() / "in" / "shopmono"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    env = os.environ.copy()
    env["PYTHONPATH"] = ":".join([
        str(project / "packages" / "catalog"),
        str(project / "packages" / "orders"),
        str(project / "packages" / "reports"),
    ])
    result = subprocess.run(["python3", "-m", "pytest", "tests"], cwd=project, env=env, capture_output=True, text=True, timeout=20)
    pytest_score = 1.0 if result.returncode == 0 else 0.0
    add("pytest", result.returncode == 0, 0.30, result.stdout[-1000:] + result.stderr[-1000:])

    hidden_score = 0.0
    try:
        script = """
from catalog.models import Money, Product
from orders.adapter import currency, price_cents
from orders.service import price_order
from reports.monthly import summarize_orders

catalog = {
    "A": Product("A", "A", Money(101, "USD")),
    "B": {"sku": "B", "name": "B", "price_cents": "205", "currency": "USD"},
    "C": Product("C", "C", Money(333, "CAD")),
}
assert price_cents(catalog["A"]) == 101
assert currency(catalog["A"]) == "USD"
assert price_order([{"sku": "A", "quantity": "2"}, {"sku": "B", "quantity": 1}], catalog) == {"total_cents": 407, "currency": "USD"}
try:
    price_order([{"sku": "A", "quantity": 1}, {"sku": "C", "quantity": 1}], catalog)
except ValueError as exc:
    assert "mixed" in str(exc).lower()
else:
    raise AssertionError("mixed currency not rejected")
assert summarize_orders([{"lines": [{"sku": "C", "quantity": 2}]}], catalog)["revenue_by_currency"] == {"CAD": 666}
"""
        hidden = subprocess.run(["python3", "-c", script], cwd=project, env=env, capture_output=True, text=True, timeout=20)
        hidden_score = 1.0 if hidden.returncode == 0 else 0.0
        add("hidden_interface_behavior", hidden.returncode == 0, 0.40, hidden.stdout[-500:] + hidden.stderr[-500:])
    except Exception as exc:
        add("hidden_interface_behavior", False, 0.40, str(exc))

    hashes_ok = []
    for rel, digest in TEST_HASHES.items():
        path = project / rel
        hashes_ok.append(path.is_file() and _md5(path) == digest)
    integrity = sum(hashes_ok) / len(hashes_ok)
    add("test_integrity", integrity == 1.0, 0.15, {"score": integrity})

    source = "\n".join((project / rel).read_text(encoding="utf-8", errors="replace") for rel in [
        "packages/orders/orders/adapter.py",
        "packages/orders/orders/service.py",
        "packages/reports/reports/monthly.py",
    ])
    quality_terms = ["amount_cents", "currency", "price_cents", "mixed"]
    forbidden = ["sku ==", "PEN", "MUG"]
    quality = 0.8 * (sum(term in source for term in quality_terms) / len(quality_terms)) + 0.2 * (not any(term in source for term in forbidden))
    add("implementation_quality", quality >= 0.75, 0.10, {"score": round(quality, 4)})

    report = Path(workspace).resolve() / "out" / "interface_fix_report.md"
    text = report.read_text(encoding="utf-8", errors="replace").lower() if report.is_file() else ""
    report_score = sum(term in text for term in ["money", "legacy", "currency", "pytest"]) / 4
    add("fix_report", report_score >= 0.75, 0.05, {"score": report_score})

    total = pytest_score * 0.30 + hidden_score * 0.40 + integrity * 0.15 + quality * 0.10 + report_score * 0.05
    if hidden_score < 1.0:
        total = min(total, 0.68)
    if integrity < 1.0:
        total = min(total, 0.60)
    level = "excellent" if total >= 0.9 else "good" if total >= 0.75 else "pass" if total >= 0.6 else "fail"
    return {"task": "083-monorepo-interface-repair", "outcome_score": round(total, 4), "level": level, "checks": checks}
