"""Run the XBotv2 Phase 1-3 freeze gate.

This script intentionally mirrors the documented gate instead of adding new
policy:

1. Core tests.
2. Compile all runtime modules.
3. Check staged/unstaged diff whitespace.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


COMMANDS = (
    ("uv", "run", "pytest", "XBotv2/tests/core/", "-q"),
    (sys.executable, "-m", "compileall", "-q", "XBotv2/xbotv2"),
    ("git", "diff", "--check"),
)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    for command in COMMANDS:
        print(f"$ {' '.join(command)}", flush=True)
        completed = subprocess.run(command, cwd=repo_root, check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
