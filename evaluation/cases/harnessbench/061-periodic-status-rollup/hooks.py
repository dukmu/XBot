from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any


def prepare_runtime(context: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(context["workspace"])
    task_dir = Path(context["task"].task_dir) if "task" in context else Path(__file__).resolve().parent
    inbox = workspace / "in" / "status_updates"
    inbox.mkdir(parents=True, exist_ok=True)
    (workspace / "out").mkdir(parents=True, exist_ok=True)

    batch_1 = task_dir / "fixtures" / "in" / "batch_1"
    for src in sorted(batch_1.glob("*.json")):
        shutil.copy2(src, inbox / src.name)

    start = time.time()

    def inject() -> None:
        schedule = [
            (6, "status_003.json"),
            (10, "status_dup_002.json"),
            (14, "status_old.json"),
            (18, "status_004.json"),
            (22, "status_late.json"),
        ]
        source_dir = task_dir / "fixtures" / "in"
        for delay, name in schedule:
            remaining = start + delay - time.time()
            if remaining > 0:
                time.sleep(remaining)
            shutil.copy2(source_dir / name, inbox / name)

    threading.Thread(target=inject, daemon=True).start()
    return {"STATUS_INJECTION_SECONDS": "6,10,14,18,22"}


def after_round(context: dict[str, Any], runtime_state: dict[str, Any], adapter_result: Any) -> dict[str, Any]:
    return runtime_state


def cleanup_runtime(context: dict[str, Any], runtime_state: dict[str, Any]) -> None:
    pass
