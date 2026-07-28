from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import AllowedOutcome, ClientCapabilities, RequestPermissionResponse
from inspect_ai.model import ModelOutput, ModelUsage
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import sandbox


class _InspectACPClient:
    def __init__(self) -> None:
        self.message_parts: list[str] = []
        self.events: list[dict[str, Any]] = []

    async def session_update(self, **kwargs: Any) -> None:
        update = kwargs["update"]
        if update.session_update == "agent_message_chunk":
            self.message_parts.append(update.content.text)
        event = update.model_dump(mode="json", exclude_none=True)
        event.pop("content", None)
        event.pop("raw_output", None)
        if "raw_input" in event:
            event["raw_input"] = _trace_value(event["raw_input"])
        self.events.append(event)

    async def request_permission(self, **kwargs: Any) -> RequestPermissionResponse:
        tool_call = kwargs["tool_call"]
        self.events.append({
            "session_update": "permission_request",
            "tool_call_id": tool_call.tool_call_id,
            "title": tool_call.title,
            "raw_input": tool_call.raw_input,
            "decision": "deny",
        })
        return RequestPermissionResponse(
            outcome=AllowedOutcome(
                outcome="selected",
                option_id="deny",
            )
        )


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
            raise RuntimeError(
                f"Cannot resolve Inspect workspace: {workspace_result.stderr}"
            )
        workspace = Path(workspace_result.stdout.strip()).resolve()

        args = [
            "acp",
            "--data-dir",
            str(Path(data_dir).resolve()),
            "--provider",
            provider,
        ]
        if agent:
            args.extend(["--agent", agent])
        if no_plugins:
            args.append("--no-plugins")
        args.extend(extra_args)

        client = _InspectACPClient()
        child_env = dict(env or {})
        child_env.setdefault("PYTHONUTF8", "1")
        child_env["WORKSPACE"] = str(workspace)
        responses = []
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
            prompts = [state.input_text, *state.metadata.get("followups", [])]
            for prompt in prompts:
                rendered_prompt = str(prompt).replace(
                    "$WORKSPACE",
                    str(workspace),
                )
                responses.append(await connection.prompt(
                    session_id=session.session_id,
                    prompt=[text_block(rendered_prompt)],
                ))

        content = "".join(client.message_parts)
        state.output = ModelOutput.from_content(
            model=f"xbot/{provider}",
            content=content,
        )
        usages = [response.usage for response in responses if response.usage]
        state.output.usage = ModelUsage(
            input_tokens=sum(usage.input_tokens for usage in usages),
            output_tokens=sum(usage.output_tokens for usage in usages),
            total_tokens=sum(usage.total_tokens for usage in usages),
            input_tokens_cache_read=sum(
                usage.cached_read_tokens or 0 for usage in usages
            ),
            input_tokens_cache_write=sum(
                usage.cached_write_tokens or 0 for usage in usages
            ),
        )
        state.metadata["xbot"] = {
            "session_id": session.session_id,
            "turns": [
                {
                    "stop_reason": response.stop_reason,
                    "usage": (
                        response.usage.model_dump()
                        if response.usage else None
                    ),
                }
                for response in responses
            ],
            "events": client.events,
        }
        return state

    return solve


def selected_environment(*names: str) -> dict[str, str]:
    """Return explicitly selected host variables for the XBot subprocess."""
    return {name: os.environ[name] for name in names if name in os.environ}


def _trace_value(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 1000 else f"{value[:1000]}... ({len(value)} chars)"
    if isinstance(value, dict):
        return {key: _trace_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trace_value(item) for item in value]
    return value
