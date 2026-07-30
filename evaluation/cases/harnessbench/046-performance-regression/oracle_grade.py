from __future__ import annotations

import hashlib
import importlib.util
import json
import time
from pathlib import Path
from typing import Any

_TASK_DIR = Path(__file__).resolve().parent
_GT = json.loads((_TASK_DIR / "ground_truth.json").read_text(encoding="utf-8"))
_EXPECTED_HASH = "e2d29aa2505ac59996069153345286a7"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _orders(n: int = 30000) -> list[dict[str, Any]]:
    orders = []
    for i in range(n):
        lines = []
        for j in range(5):
            lines.append({"sku": f"SKU-{(i + j) % 100:03d}", "qty": (i + j) % 4 + 1})
        orders.append({"id": f"O-{i:05d}", "lines": lines})
    return orders


def _edge_orders() -> list[dict[str, Any]]:
    return [
        {"id": "EDGE-001", "lines": [{"sku": "SKU-EDGE", "qty": 1}, {"sku": "SKU-EDGE", "qty": 3}]},
        {"id": "EDGE-002", "lines": []},
        {"id": "EDGE-003"},
        {"id": "EDGE-004", "lines": [{"sku": "odd sku/with spaces", "qty": 1}]},
        {"id": "EDGE-005", "lines": [{"sku": "SKU-EDGE", "qty": 2}, {"sku": "OTHER", "qty": 9}]},
    ]


def _expected(orders: list[dict[str, Any]], sku: str) -> list[str]:
    return [order["id"] for order in orders if any(line.get("sku") == sku for line in order.get("lines", []))]


def score_workspace(workspace: Path) -> dict[str, Any]:
    w = Path(workspace).resolve()
    project = w / "in" / "perfcase"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    correctness = 0.0
    perf_score = 0.0
    edge_score = 0.0
    try:
        spec = importlib.util.spec_from_file_location("slow_under_test", project / "slow.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        edge_index = mod.build_catalog_index(_edge_orders())
        edge_outputs = {
            "duplicate_once": mod.find_orders_for_sku(edge_index, "SKU-EDGE"),
            "arbitrary_sku": mod.find_orders_for_sku(edge_index, "odd sku/with spaces"),
            "missing": mod.find_orders_for_sku(edge_index, "NOT-PRESENT"),
        }
        edge_expected = {
            "duplicate_once": ["EDGE-001", "EDGE-005"],
            "arbitrary_sku": ["EDGE-004"],
            "missing": [],
        }
        edge_score = 1.0 if edge_outputs == edge_expected else 0.0
        add("edge_semantics", edge_score == 1.0, 0.20, {"got": edge_outputs})

        data = _orders()
        start = time.perf_counter()
        index = mod.build_catalog_index(data)
        outputs = [mod.find_orders_for_sku(index, f"SKU-{i:03d}") for i in range(100)]
        elapsed = time.perf_counter() - start
        expected_outputs = [_expected(data, f"SKU-{i:03d}") for i in range(100)]
        correctness = 1.0 if outputs == expected_outputs else 0.0
        perf_score = 1.0 if elapsed <= _GT["max_seconds"] else 0.0
        add("generated_correctness", correctness == 1.0, 0.30)
        add("performance", elapsed <= _GT["max_seconds"], 0.30, {"elapsed": round(elapsed, 6), "max": _GT["max_seconds"]})
    except Exception as exc:
        add("edge_semantics", False, 0.20, str(exc))
        add("generated_correctness", False, 0.30, str(exc))
        add("performance", False, 0.30, str(exc))

    source = (project / "slow.py").read_text(encoding="utf-8", errors="replace")
    function_hits = sum(name in source for name in _GT["required_functions"])
    forbidden = [term for term in _GT["forbidden_terms"] if term in source]
    expected_ok = _md5(project / "benchmark_expected.json") == _EXPECTED_HASH
    index_hint = any(term in source for term in ("dict", "defaultdict", "setdefault", "{}"))
    shape_score = 0.40 * min(function_hits / len(_GT["required_functions"]), 1) + 0.30 * index_hint + 0.30 * (not forbidden and expected_ok)
    add("implementation_shape", shape_score >= 0.70, 0.20, {"function_hits": function_hits, "forbidden": forbidden, "expected_ok": expected_ok})
    total = edge_score * 0.20 + correctness * 0.30 + perf_score * 0.30 + shape_score * 0.20
    if not perf_score:
        total = min(total, 0.59)
    if not expected_ok:
        total = min(total, 0.59)
    thresholds = _GT["scoring"]["thresholds"]
    level = "excellent" if total >= thresholds["excellent"] else "good" if total >= thresholds["good"] else "pass" if total >= thresholds["pass"] else "fail"
    return {"task": "046-performance-regression", "outcome_score": round(total, 4), "level": level, "checks": checks}
