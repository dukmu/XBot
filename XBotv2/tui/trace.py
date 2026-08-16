"""Optional JSONL trace helpers for diagnosing TUI/protocol issues."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRACE_ENV = "XBOTV2_TUI_TRACE"


def trace_event(stage: str, payload: dict[str, Any]) -> None:
    """Append a diagnostic event when XBOTV2_TUI_TRACE is set."""
    target = os.environ.get(TRACE_ENV)
    if not target:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "payload": payload,
    }
    try:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        return
