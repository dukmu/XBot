from __future__ import annotations

import os
from pathlib import Path

from inspect_ai import Task, task

from xbot_eval.adapter import selected_environment, xbot_agent
from xbot_eval.harnessbench import load_cases, workspace_oracle


REPO_ROOT = Path(__file__).resolve().parents[2]
CASES = REPO_ROOT / "evaluation" / "cases" / "harnessbench"
TASK_IDS = {
    "016-code-repair-pytest",
    "057-interruption-resume",
    "060-task-cancellation-cleanup",
    "105-partial-batch-resume-ledger",
    "xbot-background-shell",
}


@task
def xbot_harnessbench_subset() -> Task:
    """Run a small deterministic HarnessBench subset through XBot."""
    return Task(
        dataset=load_cases(CASES, TASK_IDS),
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
        scorer=workspace_oracle(),
        sandbox="local",
        time_limit=900,
    )
