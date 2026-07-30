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
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
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
        option_id = (
            "deny"
            if _external_path(tool_call.title, tool_call.raw_input, self.workspace)
            else "allow_once"
        )
        self.events.append({
            "session_update": "permission_request",
            "tool_call_id": tool_call.tool_call_id,
            "title": tool_call.title,
            "raw_input": tool_call.raw_input,
            "decision": option_id,
        })
        return RequestPermissionResponse(
            outcome=AllowedOutcome(
                outcome="selected",
                option_id=option_id,
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
        sandbox_root = Path(workspace_result.stdout.strip()).resolve()
        workspace = sandbox_root / state.metadata.get("workspace_dir", "")
        workspace = workspace.resolve()
        runtime = None
        case_dir = state.metadata.get("case_dir")
        if case_dir:
            from .harnessbench import HarnessBenchRuntime

            runtime = HarnessBenchRuntime(
                Path(case_dir),
                sandbox_root,
                workspace,
            )
            runtime_variables = runtime.prepare()
        else:
            runtime_variables = {}

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

        client = _InspectACPClient(workspace)
        child_env = dict(env or {})
        child_env.update(runtime_variables)
        child_env.setdefault("PYTHONUTF8", "1")
        child_env["WORKSPACE"] = str(workspace)
        responses = []
        try:
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
                for round_index, prompt in enumerate(prompts):
                    start = len(client.message_parts)
                    rendered_prompt = (
                        runtime.render(str(prompt))
                        if runtime
                        else str(prompt).replace("$WORKSPACE", str(workspace))
                    )
                    responses.append(await connection.prompt(
                        session_id=session.session_id,
                        prompt=[text_block(rendered_prompt)],
                    ))
                    if runtime:
                        runtime.after_round(
                            round_index,
                            session.session_id,
                            "".join(client.message_parts[start:]),
                        )
        finally:
            if runtime:
                runtime.cleanup()

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


@solver
def xbot_server_agent(
    *,
    uds_path: str,
    agent: str | None = None,
) -> Solver:
    """Run each Inspect sample as a session on one shared XBot server."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        from xbotv2.client import XBotClient

        workspace_result = await sandbox().exec(["pwd"])
        if not workspace_result.success:
            raise RuntimeError(
                f"Cannot resolve Inspect workspace: {workspace_result.stderr}"
            )
        sandbox_root = Path(workspace_result.stdout.strip()).resolve()
        workspace = (
            sandbox_root / state.metadata.get("workspace_dir", "")
        ).resolve()
        runtime = None
        case_dir = state.metadata.get("case_dir")
        if case_dir:
            from .harnessbench import HarnessBenchRuntime

            runtime = HarnessBenchRuntime(
                Path(case_dir),
                sandbox_root,
                workspace,
            )
            runtime.prepare()

        messages: list[str] = []
        turns: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        resolved_interactions: set[str] = set()
        session_id = ""
        provider = ""
        model = ""
        usage = None
        async with XBotClient(
            base_url="http://localhost",
            timeout=60.0,
            uds_path=uds_path,
        ) as client:
            opened = await client.open_session(
                workspace_root=str(workspace),
                agent=agent,
            )
            session_id = opened.session_id
            provider = opened.provider
            model = opened.model
            try:
                prompts = [
                    state.input_text,
                    *state.metadata.get("followups", []),
                ]
                for round_index, prompt in enumerate(prompts):
                    rendered = (
                        runtime.render(str(prompt))
                        if runtime
                        else str(prompt).replace(
                            "$WORKSPACE", str(workspace)
                        )
                    )
                    turn_messages: list[str] = []
                    turn_events: list[str] = []
                    async for event in client.send_message(
                        session_id,
                        opened.thread_id,
                        rendered,
                    ):
                        event_data = event.model_dump(
                            mode="json", exclude_none=True
                        )
                        events.append(_trace_value(event_data))
                        turn_events.append(event.type)
                        if event.type == "assistant_message":
                            content = str(event.data.get("content") or "")
                            if content:
                                messages.append(content)
                                turn_messages.append(content)
                        elif event.type == "permission_request":
                            request_id = event.data["request_id"]
                            if request_id in resolved_interactions:
                                continue
                            call = event.data.get("tool_call") or {}
                            permission = event.data.get("permission") or {}
                            tool = str(
                                call.get("name")
                                or permission.get("tool")
                                or ""
                            )
                            args = (
                                call.get("args")
                                if isinstance(call.get("args"), dict)
                                else permission.get("params")
                            )
                            decision = (
                                "deny"
                                if _external_path(tool, args, workspace)
                                else "allow"
                            )
                            await client.respond_permission(
                                session_id,
                                opened.thread_id,
                                request_id=request_id,
                                decision=decision,
                                scope="once",
                            )
                            resolved_interactions.add(request_id)
                        elif event.type == "user_input_required":
                            request_id = event.data["request_id"]
                            if request_id in resolved_interactions:
                                continue
                            options = event.data.get("options") or []
                            answer = (
                                options[0].get("label", "")
                                if options else ""
                            )
                            await client.respond_user_input(
                                session_id,
                                opened.thread_id,
                                request_id=request_id,
                                answer=answer,
                            )
                            resolved_interactions.add(request_id)
                        elif event.type == "error":
                            raise RuntimeError(
                                event.data.get("message")
                                or "XBot turn failed"
                            )
                    turns.append({
                        "events": turn_events,
                        "output": "".join(turn_messages),
                    })
                    if runtime:
                        runtime.after_round(
                            round_index,
                            session_id,
                            "".join(turn_messages),
                        )
                summary = await client.get_thread(
                    session_id, opened.thread_id
                )
                usage = summary.usage
            finally:
                if runtime:
                    runtime.cleanup()
                await client.close_session(session_id)

        state.output = ModelOutput.from_content(
            model=f"xbot/{provider}/{model}".rstrip("/"),
            content="\n".join(messages),
        )
        if usage is not None:
            state.output.usage = ModelUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                input_tokens_cache_read=usage.cache_read_input_tokens,
                input_tokens_cache_write=(
                    usage.cache_creation_input_tokens
                    + usage.prompt_cache_write_tokens
                ),
            )
        state.metadata["xbot"] = {
            "session_id": session_id,
            "turns": turns,
            "events": events,
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


def _external_path(tool: str | None, args: Any, workspace: Path) -> bool:
    if not tool or not tool.startswith("filesystem_") or not isinstance(args, dict):
        return False
    for key in ("path", "source", "destination"):
        value = args.get(key)
        if not isinstance(value, str):
            continue
        path = Path(value)
        if path.is_absolute() and not path.resolve().is_relative_to(workspace):
            return True
    return False
