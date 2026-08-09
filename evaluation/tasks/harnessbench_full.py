from __future__ import annotations

import os
from pathlib import Path

from inspect_ai import Task, task

from xbot_eval.adapters import get_adapter
from xbot_eval.harnessbench import load_cases, workspace_oracle


REPO_ROOT = Path(__file__).resolve().parents[2]
CASES = REPO_ROOT / "evaluation" / "cases" / "harnessbench"
TASK_IDS = {
    path.name
    for path in CASES.iterdir()
    if path.is_dir() and path.name[:3].isdigit()
}


@task
def harnessbench_full() -> Task:
    """Run all 106 official HarnessBench tasks through one Agent framework."""
    adapter_name = os.environ.get("XBOT_EVAL_ADAPTER", "xbot")
    adapter = get_adapter(adapter_name)
    return Task(
        dataset=load_cases(CASES, TASK_IDS),
        solver=adapter.solver(),
        scorer=workspace_oracle(),
        sandbox="local",
        time_limit=2400,
        continue_on_fail=True,
        tags=[adapter_name, "harnessbench", "full"],
        metadata={
            "adapter": "inspect-agent-bridge",
            "agent_adapter": adapter_name,
        },
    )
