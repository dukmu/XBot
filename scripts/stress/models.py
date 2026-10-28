"""Metrics and correctness evidence emitted by the stress runner."""

from __future__ import annotations

import math
import platform
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def percentile(values: list[float], fraction: float) -> float | None:
    """Return a linearly interpolated percentile in milliseconds."""
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = index - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 3)


@dataclass(slots=True)
class Sample:
    operation: str
    elapsed_ms: float
    ok: bool = True
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScenarioReport:
    name: str
    samples: list[Sample] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    invariants: dict[str, bool] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def count(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    def add_sample(
        self,
        operation: str,
        started: float,
        *,
        ok: bool = True,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        sample = Sample(
            operation=operation,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            ok=ok,
            error=error,
            metadata=metadata or {},
        )
        self.samples.append(sample)
        if not ok:
            self.errors.append(f"{operation}: {error}")

    def finish(self) -> None:
        self.invariants.setdefault("no_errors", not self.errors)

    @property
    def failed(self) -> bool:
        return bool(self.errors) or any(
            not value
            for key, value in self.invariants.items()
            if key != "skipped"
        )

    def as_dict(self) -> dict[str, Any]:
        by_operation: dict[str, list[float]] = {}
        for sample in self.samples:
            by_operation.setdefault(sample.operation, []).append(sample.elapsed_ms)
        timings = {
            operation: {
                "count": len(values),
                "mean_ms": round(statistics.mean(values), 3),
                "min_ms": round(min(values), 3),
                "max_ms": round(max(values), 3),
                "p50_ms": percentile(values, 0.50),
                "p90_ms": percentile(values, 0.90),
                "p95_ms": percentile(values, 0.95),
                "p99_ms": percentile(values, 0.99),
            }
            for operation, values in sorted(by_operation.items())
        }
        return {
            "name": self.name,
            "timings": timings,
            "counters": dict(sorted(self.counters.items())),
            "invariants": dict(sorted(self.invariants.items())),
            "errors": list(self.errors),
            "samples": [asdict(sample) for sample in self.samples],
        }


@dataclass(slots=True)
class StressReport:
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    finished_at: str = ""
    configuration: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    scenarios: list[ScenarioReport] = field(default_factory=list)

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc).isoformat()

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "configuration": self.configuration,
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                **self.environment,
            },
            "scenarios": [scenario.as_dict() for scenario in self.scenarios],
        }
