from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import AllowedOutcome, ClientCapabilities, RequestPermissionResponse
from inspect_ai.util import time_limit

from ..harnessbench import HarnessBenchRuntime


class InspectACPClient:
    """Collect ACP output and approve one offered operation at a time."""

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
            event["raw_input"] = trace_value(event["raw_input"])
        self.events.append(event)

    async def request_permission(self, **kwargs: Any) -> RequestPermissionResponse:
        tool_call = kwargs["tool_call"]
        option = next(
            (
                item
                for item in kwargs.get("options", ())
                if item.kind == "allow_once"
            ),
            None,
        )
        if option is None:
            raise RuntimeError("ACP agent did not offer an allow-once permission option")
        self.events.append({
            "session_update": "permission_request",
            "tool_call_id": tool_call.tool_call_id,
            "title": tool_call.title,
            "raw_input": trace_value(tool_call.raw_input),
            "decision": option.option_id,
        })
        return RequestPermissionResponse(
            outcome=AllowedOutcome(
                outcome="selected",
                option_id=option.option_id,
            )
        )


async def run_acp_session(
    *,
    client: InspectACPClient,
    command: str | Path,
    args: Sequence[str],
    workspace: Path,
    env: Mapping[str, str],
    prompts: Sequence[str],
    runtime: HarnessBenchRuntime | None = None,
    label: str,
) -> tuple[str, list[Any]]:
    """Run one ACP session while preserving its multi-round conversation."""

    responses: list[Any] = []
    async with spawn_agent_process(
        client,
        str(Path(command).resolve()),
        *args,
        cwd=str(workspace),
        env=env,
    ) as (connection, process):
        try:
            await connection.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
            )
        except ConnectionError as exc:
            stderr = (
                (await process.stderr.read()).decode(
                    encoding="utf-8", errors="replace"
                )
                if process.stderr
                else ""
            )
            raise RuntimeError(
                f"{label} ACP process exited during initialization:\n{stderr}"
            ) from exc

        session = await connection.new_session(
            cwd=str(workspace),
            mcp_servers=[],
        )
        for round_index, prompt in enumerate(prompts):
            start = len(client.message_parts)
            rendered = (
                runtime.render(prompt)
                if runtime
                else prompt.replace("$WORKSPACE", str(workspace))
            )
            timeout_sec = runtime.task.timeout_sec if runtime else None
            with time_limit(timeout_sec):
                response = await connection.prompt(
                    session_id=session.session_id,
                    prompt=[text_block(rendered)],
                )
            responses.append(response)
            if runtime:
                runtime.after_round(
                    round_index,
                    session.session_id,
                    "".join(client.message_parts[start:]),
                )
    return session.session_id, responses


def trace_value(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 1000 else f"{value[:1000]}... ({len(value)} chars)"
    if isinstance(value, dict):
        return {key: trace_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [trace_value(item) for item in value]
    return value
