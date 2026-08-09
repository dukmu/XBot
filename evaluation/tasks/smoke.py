from __future__ import annotations

import os
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import match

from xbot_eval.adapters.xbot import selected_environment, xbot_agent


REPO_ROOT = Path(__file__).resolve().parents[2]


@task
def xbot_smoke() -> Task:
    """Exercise the XBot ACP adapter with one deterministic prompt."""
    return Task(
        dataset=[
            Sample(
                input="Reply with exactly XBOT_INSPECT_OK and nothing else.",
                target="XBOT_INSPECT_OK",
            )
        ],
        solver=xbot_agent(
            command=os.environ.get(
                "XBOT_EVAL_COMMAND",
                str(REPO_ROOT / ".venv" / "bin" / "xbot"),
            ),
            data_dir=os.environ.get(
                "XBOT_EVAL_DATA_DIR",
                str(REPO_ROOT / "XBotv2" / "data"),
            ),
            provider=os.environ.get("XBOT_EVAL_PROVIDER", "minimax"),
            no_plugins=True,
            env=selected_environment("MINIMAX_API_TOKEN"),
        ),
        scorer=match(location="exact"),
        sandbox="local",
    )
