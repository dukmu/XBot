from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any


TEST_HASHES = {
    "tests/test_scheduler.py": "ae346899f6d1490cdd78b620e0ee5293",
    "tests/test_retry_order.py": "e697a15c70f270710aecaaa965f1eb36",
}


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score_workspace(workspace: Path) -> dict[str, Any]:
    project = Path(workspace).resolve() / "in" / "flakyqueue"
    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, weight: float, detail: Any = None) -> None:
        checks.append({"id": cid, "pass": bool(ok), "weight": weight, "detail": detail})

    env = os.environ.copy()
    env["PYTHONPATH"] = str(project)
    runs = []
    for _ in range(8):
        proc = subprocess.run(["python3", "-m", "pytest", "tests"], cwd=project, env=env, capture_output=True, text=True, timeout=20)
        runs.append(proc.returncode)
        if proc.returncode != 0:
            last_output = proc.stdout[-800:] + proc.stderr[-800:]
            break
    else:
        last_output = ""
    stable_score = 1.0 if all(code == 0 for code in runs) else 0.0
    add("repeated_pytest_stable", stable_score == 1.0, 0.35, {"returncodes": runs, "last_output": last_output})

    hidden_score = 0.0
    script = """
from flakyqueue.scheduler import Scheduler

class Clock:
    def __init__(self): self.value = 10.0
    def now(self): return self.value
class Rand:
    def __init__(self): self.values = [0.75, 0.125]
    def random(self): return self.values.pop(0)

clock = Clock()
s = Scheduler(clock=clock, random_source=Rand())
s.add("z", 2)
s.add("a", 2)
s.add("m", 5)
assert [t.id for t in s.ready()] == ["m", "a", "z"]
t = s.schedule_retry(s.ready()[0], base_delay=3)
assert t.run_at == 13.75
clock.value = 13.74
assert [x.id for x in s.ready()] == ["a", "z"]
clock.value = 13.75
assert [x.id for x in s.ready()] == ["m", "a", "z"]
"""
    hidden = subprocess.run(["python3", "-c", script], cwd=project, env=env, capture_output=True, text=True, timeout=20)
    hidden_score = 1.0 if hidden.returncode == 0 else 0.0
    add("hidden_determinism", hidden_score == 1.0, 0.35, hidden.stdout[-500:] + hidden.stderr[-500:])

    integrity_items = []
    for rel, digest in TEST_HASHES.items():
        path = project / rel
        integrity_items.append(path.is_file() and _md5(path) == digest)
    integrity = sum(integrity_items) / len(integrity_items)
    add("test_integrity", integrity == 1.0, 0.15, {"score": integrity})

    source = (project / "flakyqueue" / "scheduler.py").read_text(encoding="utf-8", errors="replace")
    bad_terms = ["pytest.skip", "xfail", "sleep(", "time.time()"]
    quality = 0.55 * (not any(term in source for term in bad_terms)) + 0.45 * (sum(term in source for term in ["clock.now", "random_source", "created_at", "id"]) / 4)
    add("implementation_quality", quality >= 0.75, 0.10, {"score": round(quality, 4)})

    report = Path(workspace).resolve() / "out" / "root_cause.md"
    text = report.read_text(encoding="utf-8", errors="replace").lower() if report.is_file() else ""
    report_score = sum(term in text for term in ["clock", "order", "jitter", "pytest"]) / 4
    add("root_cause_report", report_score >= 0.75, 0.05, {"score": report_score})

    total = stable_score * 0.35 + hidden_score * 0.35 + integrity * 0.15 + quality * 0.10 + report_score * 0.05
    if stable_score < 1.0 or hidden_score < 1.0:
        total = min(total, 0.62)
    if integrity < 1.0:
        total = min(total, 0.55)
    level = "excellent" if total >= 0.9 else "good" if total >= 0.75 else "pass" if total >= 0.6 else "fail"
    return {"task": "085-flaky-test-root-cause", "outcome_score": round(total, 4), "level": level, "checks": checks}
