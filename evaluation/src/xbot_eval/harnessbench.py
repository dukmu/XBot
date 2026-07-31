from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from types import ModuleType
from typing import Any

import yaml
from inspect_ai.dataset import Sample
from inspect_ai.event import SampleLimitEvent
from inspect_ai.log import transcript
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer, stderr
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox


def load_cases(
    root: Path,
    task_ids: set[str] | None = None,
) -> list[Sample]:
    """Load HarnessBench task assets into Inspect samples."""
    return [
        load_case(path)
        for path in sorted(root.iterdir())
        if path.is_dir()
        and (task_ids is None or path.name in task_ids)
    ]


def load_case(path: Path) -> Sample:
    manifest = yaml.safe_load((path / "task.yaml").read_text(encoding="utf-8"))
    prompt_names = manifest.get("prompt_files")
    if not prompt_names:
        prompt_names = [manifest.get("prompt_file", "prompt.txt")]
    prompts = [
        (path / name).read_text(encoding="utf-8")
        for name in prompt_names
    ]
    fixtures = path / manifest.get("fixtures_dir", "fixtures")
    files = {"workspace": str(fixtures.resolve())} if fixtures.is_dir() else {}
    return Sample(
        id=manifest["task_id"],
        input=prompts[0],
        files=files,
        setup="mkdir -p workspace/in workspace/out workspace/out/tmp",
        metadata={
            "case_dir": str(path.resolve()),
            "followups": prompts[1:],
            "prompt_names": prompt_names,
            "workspace_dir": "workspace",
        },
    )


@scorer(metrics=[mean(), stderr()])
def workspace_oracle() -> Scorer:
    """Run the task's deterministic HarnessBench workspace oracle."""

    async def score(state: TaskState, _target: Target) -> Score:
        if any(
            isinstance(event, SampleLimitEvent) and event.type == "time"
            for event in transcript().events
        ):
            raise RuntimeError("HarnessBench sample exceeded its time limit")
        result = await sandbox().exec(["pwd"])
        if not result.success:
            return Score(value=0, explanation=result.stderr)
        case_dir = Path(state.metadata["case_dir"])
        workspace = Path(result.stdout.strip()) / state.metadata.get(
            "workspace_dir", ""
        )
        outcome = await asyncio.to_thread(
            _oracle(case_dir).score_workspace,
            workspace,
        )
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


class HarnessBenchRuntime:
    """Run one task's official lifecycle hooks around its ACP turns."""

    def __init__(
        self,
        case_dir: Path,
        sandbox_root: Path,
        workspace: Path,
    ) -> None:
        manifest = yaml.safe_load(
            (case_dir / "task.yaml").read_text(encoding="utf-8")
        )
        self.case_dir = case_dir
        self.sandbox_root = sandbox_root
        self.workspace = workspace
        self.prompt_names = manifest.get("prompt_files") or [
            manifest.get("prompt_file", "prompt.txt")
        ]
        task_data = dict(manifest)
        task_data.update({
            "task_dir": case_dir,
            "prompt_file": manifest.get("prompt_file", "prompt.txt"),
            "prompt_files": list(manifest.get("prompt_files") or []),
            "fixtures_dir": manifest.get("fixtures_dir", "fixtures"),
            "oracle_module": manifest.get("oracle_module", "oracle_grade.py"),
            "hooks_module": manifest.get("hooks_module", "hooks.py"),
        })
        self.task = SimpleNamespace(**task_data)
        self.hooks = _module(
            case_dir / self.task.hooks_module,
            f"xbot_eval_hooks_{case_dir.name.replace('-', '_')}",
        )
        self.state: dict[str, Any] = {}

    def prepare(self) -> dict[str, str]:
        self.workspace.joinpath("in").mkdir(parents=True, exist_ok=True)
        self.workspace.joinpath("out").mkdir(parents=True, exist_ok=True)
        callback = getattr(self.hooks, "prepare_runtime", None)
        if callable(callback):
            prepared = callback(self._context())
            if isinstance(prepared, dict):
                self.state.update(prepared)
        return self.variables

    def after_round(
        self,
        round_index: int,
        session_id: str,
        output: str,
    ) -> None:
        callback = getattr(self.hooks, "after_round", None)
        if not callable(callback):
            return
        context = self._context()
        context.update({
            "session_id": session_id,
            "round_index": round_index,
            "prompt_name": self.prompt_names[round_index],
        })
        updated = callback(
            context,
            self.state,
            SimpleNamespace(ok=True, stdout=output, stderr="", metadata={}),
        )
        if isinstance(updated, dict):
            self.state.update(updated)

    def cleanup(self) -> None:
        callback = getattr(self.hooks, "cleanup_runtime", None)
        if callable(callback):
            callback(self._context(), self.state)

    @property
    def variables(self) -> dict[str, str]:
        return {
            key: value
            for key, value in self.state.items()
            if isinstance(value, str)
        }

    def render(self, prompt: str) -> str:
        rendered = prompt.replace("$WORKSPACE", str(self.workspace))
        for key, value in self.variables.items():
            rendered = rendered.replace(f"${key}", value)
        return rendered

    def _context(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "sandbox": self.sandbox_root,
            "workspace": self.workspace,
        }


def _module(path: Path, name: str) -> ModuleType | None:
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {path}")
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
