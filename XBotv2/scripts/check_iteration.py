"""Run the repository checks required after an XBotv2 iteration."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


COMMANDS = (
    (sys.executable, "-m", "pytest", "XBotv2/tests/core/", "-q"),
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
