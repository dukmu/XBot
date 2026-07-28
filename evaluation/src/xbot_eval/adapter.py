from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import ClientCapabilities
from inspect_ai.model import ModelOutput
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import sandbox


class _InspectACPClient:
    def __init__(self) -> None:
        self.message_parts: list[str] = []

    async def session_update(self, **kwargs: Any) -> None:
        update = kwargs["update"]
        if update.session_update == "agent_message_chunk":
            self.message_parts.append(update.content.text)


@solver
def xbot_agent(
    *,
    command: str,
    data_dir: str,
    provider: str,
    agent: str | None = None,
    no_plugins: bool = False,
    env: Mapping[str, str] | None = None,
    extra_args: Sequence[str] = (),
) -> Solver:
    """Run each Inspect sample through a real XBot ACP session."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        workspace_result = await sandbox().exec(["pwd"])
        if not workspace_result.success:
            raise RuntimeError(f"Cannot resolve Inspect workspace: {workspace_result.stderr}")
        workspace = Path(workspace_result.stdout.strip()).resolve()

        args = ["acp", "--data-dir", str(Path(data_dir).resolve()), "--provider", provider]
        if agent:
            args.extend(["--agent", agent])
        if no_plugins:
            args.append("--no-plugins")
        args.extend(extra_args)

        client = _InspectACPClient()
        child_env = dict(env or {})
        child_env.setdefault("PYTHONUTF8", "1")
        async with spawn_agent_process(
            client,
            str(Path(command).resolve()),
            *args,
            cwd=str(workspace),
            env=child_env,
        ) as (connection, _process):
            await connection.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
            )
            session = await connection.new_session(
                cwd=str(workspace),
                mcp_servers=[],
            )
            response = await connection.prompt(
                session_id=session.session_id,
                prompt=[text_block(state.input_text)],
            )

        content = "".join(client.message_parts)
        state.output = ModelOutput.from_content(
            model=f"xbot/{provider}",
            content=content,
        )
        state.metadata["xbot"] = {
            "session_id": session.session_id,
            "stop_reason": response.stop_reason,
            "usage": response.usage.model_dump() if response.usage else None,
        }
        return state

    return solve


def selected_environment(*names: str) -> dict[str, str]:
    """Return explicitly selected host variables for the XBot subprocess."""
    return {name: os.environ[name] for name in names if name in os.environ}
