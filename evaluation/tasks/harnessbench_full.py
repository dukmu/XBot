from __future__ import annotations

import os
from pathlib import Path

from inspect_ai import Task, task

from xbot_eval.adapter import xbot_server_agent
from xbot_eval.harnessbench import load_cases, workspace_oracle


REPO_ROOT = Path(__file__).resolve().parents[2]
CASES = REPO_ROOT / "evaluation" / "cases" / "harnessbench"
TASK_IDS = {
    path.name
    for path in CASES.iterdir()
    if path.is_dir() and path.name[:3].isdigit()
}


@task
def xbot_harnessbench_full() -> Task:
    """Run all 106 official HarnessBench tasks through XBot."""
    return Task(
        dataset=load_cases(CASES, TASK_IDS),
        solver=xbot_server_agent(
            uds_path=os.environ["XBOT_EVAL_UDS"],
            agent=os.environ.get("XBOT_EVAL_AGENT"),
        ),
        scorer=workspace_oracle(),
        sandbox="local",
        time_limit=2400,
        continue_on_fail=True,
    )
