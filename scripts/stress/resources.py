"""Small, dependency-free process resource snapshots."""

from __future__ import annotations

import os
import resource
from typing import Any


def snapshot() -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    values: dict[str, Any] = {
        "user_cpu_s": round(usage.ru_utime, 6),
        "system_cpu_s": round(usage.ru_stime, 6),
        "max_rss": usage.ru_maxrss,
        "open_fds": None,
    }
    fd_dir = "/proc/self/fd"
    try:
        values["open_fds"] = len(os.listdir(fd_dir))
    except OSError:
        pass
    return values
