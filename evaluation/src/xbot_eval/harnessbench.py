from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer, stderr
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox


def load_cases(root: Path) -> list[Sample]:
    """Load HarnessBench task assets into Inspect samples."""
    return [load_case(path) for path in sorted(root.iterdir()) if path.is_dir()]


def load_case(path: Path) -> Sample:
    manifest = yaml.safe_load((path / "task.yaml").read_text(encoding="utf-8"))
    prompt_names = manifest.get("prompt_files")
    if not prompt_names:
        prompt_names = [manifest["prompt_file"]]
    prompts = [
        (path / name).read_text(encoding="utf-8")
        for name in prompt_names
    ]
    fixtures = path / manifest.get("fixtures_dir", "fixtures")
    files = {
        str(file.relative_to(fixtures)): file.read_text(encoding="utf-8")
        for file in fixtures.rglob("*")
        if file.is_file()
    }
    return Sample(
        id=manifest["task_id"],
        input=prompts[0],
        files=files,
        setup="mkdir -p out out/tmp",
        metadata={
            "case_dir": str(path.resolve()),
            "followups": prompts[1:],
        },
    )


@scorer(metrics=[mean(), stderr()])
def workspace_oracle() -> Scorer:
    """Run the task's deterministic HarnessBench workspace oracle."""

    async def score(state: TaskState, _target: Target) -> Score:
        result = await sandbox().exec(["pwd"])
        if not result.success:
            return Score(value=0, explanation=result.stderr)
        case_dir = Path(state.metadata["case_dir"])
        try:
            outcome = await asyncio.to_thread(
                _oracle(case_dir).score_workspace,
                Path(result.stdout.strip()),
            )
        except Exception as exc:
            return Score(value=0, explanation=f"Oracle failed: {exc}")
        value = float(outcome.get("outcome_score", outcome.get("score", 0)))
        if case_dir.name == "xbot-background-shell":
            process = _background_process_checks(state)
            value = value * 0.8 + process["score"] * 0.2
            outcome["process_checks"] = process["checks"]
        return Score(
            value=value,
            explanation=str(outcome.get("grade") or outcome.get("level") or ""),
            metadata={"oracle": outcome},
        )

    return score


def _oracle(case_dir: Path) -> ModuleType:
    path = case_dir / "oracle_grade.py"
    spec = importlib.util.spec_from_file_location(
        f"xbot_eval_oracle_{case_dir.name.replace('-', '_')}",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load oracle: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _background_process_checks(state: TaskState) -> dict[str, Any]:
    events = state.metadata.get("xbot", {}).get("events", [])
    started = any(
        event.get("title") == "shell"
        and event.get("raw_input", {}).get("background") is True
        for event in events
    )
    completed = any(
        str(event.get("tool_call_id", "")).startswith("task-")
        and event.get("status") == "completed"
        for event in events
    )
    inspected = any(
        event.get("title") == "list_tasks"
        and event.get("raw_input", {}).get("task_id")
        for event in events
    )
    checks = [
        {"id": "background_started", "pass": started},
        {"id": "completion_observed", "pass": completed},
        {"id": "terminal_state_inspected", "pass": inspected},
    ]
    return {
        "score": sum(bool(check["pass"]) for check in checks) / len(checks),
        "checks": checks,
    }
