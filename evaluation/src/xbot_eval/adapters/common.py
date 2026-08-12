from __future__ import annotations

import shutil
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio
from inspect_ai.agent import AgentState, SandboxAgentBridge, sandbox_agent_bridge
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox

from ..harnessbench import HarnessBenchRuntime


_BRIDGE_PORTS: set[int] = set()
_BRIDGE_START_LOCK = anyio.Lock()


@dataclass
class PreparedSample:
    sandbox_root: Path
    workspace: Path
    prompts: list[str]
    runtime: HarnessBenchRuntime | None

    @classmethod
    async def create(cls, state: TaskState) -> PreparedSample:
        result = await sandbox().exec(["pwd"])
        if not result.success:
            raise RuntimeError(f"Cannot resolve Inspect workspace: {result.stderr}")
        sandbox_root = Path(result.stdout.strip()).resolve()
        workspace = (
            sandbox_root / state.metadata.get("workspace_dir", "")
        ).resolve()
        runtime = None
        if case_dir := state.metadata.get("case_dir"):
            runtime = HarnessBenchRuntime(Path(case_dir), sandbox_root, workspace)
            runtime.prepare()
        return cls(
            sandbox_root=sandbox_root,
            workspace=workspace,
            prompts=[
                str(state.input_text),
                *(str(item) for item in state.metadata.get("followups", [])),
            ],
            runtime=runtime,
        )

    @property
    def environment(self) -> dict[str, str]:
        return {
            "WORKSPACE": str(self.workspace),
            **(self.runtime.variables if self.runtime else {}),
        }

    def cleanup(self) -> None:
        if self.runtime:
            self.runtime.cleanup()


def allocate_bridge_port() -> int:
    while True:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port not in _BRIDGE_PORTS:
            _BRIDGE_PORTS.add(port)
            return port


@asynccontextmanager
async def local_agent_bridge(
    state: AgentState,
    *,
    port: int,
) -> AsyncIterator[SandboxAgentBridge]:
    """Start an Inspect bridge without racing its shared local tool install."""

    async with AsyncExitStack() as stack:
        async with _BRIDGE_START_LOCK:
            bridge = await stack.enter_async_context(
                sandbox_agent_bridge(state, sandbox="local", port=port)
            )
        yield bridge


def resolve_command(command: str | None, default: str | Path | None) -> str:
    candidate = command or (str(default) if default is not None else "")
    if not candidate:
        raise RuntimeError("Agent executable not found; use --agent-command")
    if Path(candidate).parent != Path("."):
        path = Path(candidate).resolve()
        if path.is_file():
            return str(path)
    elif found := shutil.which(candidate):
        return str(Path(found).resolve())
    raise RuntimeError(f"Agent executable not found: {candidate}")
