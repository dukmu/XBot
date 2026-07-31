from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any


def prepare_runtime(context: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(context["workspace"])
    task_dir = Path(context["task"].task_dir) if "task" in context else Path(__file__).resolve().parent
    inbox = workspace / "in" / "ops_updates"
    inbox.mkdir(parents=True, exist_ok=True)
    (workspace / "out").mkdir(parents=True, exist_ok=True)

    initial_dir = task_dir / "fixtures" / "in" / "batch_initial"
    for src in sorted(initial_dir.glob("*.json")):
        shutil.copy2(src, inbox / src.name)

    start = time.time()

    def inject() -> None:
        schedule = [
            (6, "update_003.json"),
            (11, "update_dup_002.json"),
            (16, "update_old.json"),
            (22, "update_004.json"),
            (28, "update_late.json"),
            (29, "update_005.json"),
        ]
        source_dir = task_dir / "delayed_updates"
        for delay, name in schedule:
            remaining = start + delay - time.time()
            if remaining > 0:
                time.sleep(remaining)
            shutil.copy2(source_dir / name, inbox / name)

    threading.Thread(target=inject, daemon=True).start()
    return {"OPS_UPDATE_INJECTION_SECONDS": "6,11,16,22,28,29"}


def after_round(context: dict[str, Any], runtime_state: dict[str, Any], adapter_result: Any) -> dict[str, Any]:
    return runtime_state


def cleanup_runtime(context: dict[str, Any], runtime_state: dict[str, Any]) -> None:
    pass
