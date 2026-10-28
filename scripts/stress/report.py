"""Report rendering and optional JSON persistence."""

from __future__ import annotations

import json
from pathlib import Path

from .models import StressReport


def print_report(report: StressReport) -> None:
    print("XBot stress report")
    for scenario in report.scenarios:
        status = (
            "SKIP" if scenario.invariants.get("skipped")
            else "FAIL" if scenario.failed
            else "PASS"
        )
        print(f"[{status}] {scenario.name}")
        for operation, timing in scenario.as_dict()["timings"].items():
            print(
                f"  {operation}: n={timing['count']} "
                f"p50={timing['p50_ms']}ms p95={timing['p95_ms']}ms "
                f"p99={timing['p99_ms']}ms"
            )
        for key, value in scenario.invariants.items():
            print(f"  invariant {key}: {'ok' if value else 'FAILED'}")
        for error in scenario.errors[:5]:
            print(f"  error: {error}")


def write_report(report: StressReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
