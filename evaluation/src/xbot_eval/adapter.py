from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.schema import AllowedOutcome, ClientCapabilities, RequestPermissionResponse
from inspect_ai.agent import AgentState, sandbox_agent_bridge
from inspect_ai.model import ModelOutput, ModelUsage
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import sandbox


_BRIDGE_PORTS: set[int] = set()


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
def xbot_bridge_agent(
    *,
    command: str,
    data_dir: str,
    agent: str | None = None,
) -> Solver:
    """Run XBot through Inspect's standard external-agent model bridge."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
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

        bridge_state = AgentState(messages=list(state.messages))
        bridge_port = _allocate_bridge_port()
        bridge_data = sandbox_root / ".xbot-eval"
        client = _InspectACPClient(workspace)
        responses = []
        session_id = ""
        try:
            async with sandbox_agent_bridge(
                bridge_state,
                sandbox="local",
                port=bridge_port,
                forward_generation_config=True,
            ) as bridge:
                _prepare_bridge_data(
                    Path(data_dir).resolve(),
                    bridge_data,
                    bridge.port,
                )
                args = [
                    "acp",
                    "--data-dir",
                    str(bridge_data),
                    "--provider",
                    "inspect",
                ]
                if agent:
                    args.extend(["--agent", agent])
                child_env = {
                    "PYTHONUTF8": "1",
                    "WORKSPACE": str(workspace),
                    **(runtime.variables if runtime else {}),
                }
                async with spawn_agent_process(
                    client,
                    str(Path(command).resolve()),
                    *args,
                    cwd=str(workspace),
                    env=child_env,
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
                            if process.stderr else ""
                        )
                        raise RuntimeError(
                            f"XBot ACP process exited during initialization:\n{stderr}"
                        ) from exc
                    session = await connection.new_session(
                        cwd=str(workspace),
                        mcp_servers=[],
                    )
                    session_id = session.session_id
                    prompts = [
                        state.input_text,
                        *state.metadata.get("followups", []),
                    ]
                    for round_index, prompt in enumerate(prompts):
                        start = len(client.message_parts)
                        rendered = (
                            runtime.render(str(prompt))
                            if runtime
                            else str(prompt).replace(
                                "$WORKSPACE", str(workspace)
                            )
                        )
                        responses.append(await connection.prompt(
                            session_id=session_id,
                            prompt=[text_block(rendered)],
                        ))
                        if runtime:
                            runtime.after_round(
                                round_index,
                                session_id,
                                "".join(client.message_parts[start:]),
                            )
                bridge_state = bridge.state
        finally:
            source = bridge_data / "sessions"
            if source.is_dir():
                target = Path(data_dir).resolve() / "sessions"
                target.mkdir(exist_ok=True)
                for session_dir in source.iterdir():
                    if session_dir.is_dir():
                        shutil.copytree(
                            session_dir,
                            target / session_dir.name,
                            dirs_exist_ok=True,
                        )
            if runtime:
                runtime.cleanup()

        state.messages = bridge_state.messages
        state.output = bridge_state.output
        state.metadata["xbot"] = {
            "session_id": session_id,
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
            "acp_events": client.events,
        }
        return state

    return solve


def _prepare_bridge_data(source: Path, target: Path, port: int) -> None:
    target.mkdir()
    for name in ("config", ".agents", "memory"):
        path = source / name
        if path.exists():
            shutil.copytree(path, target / name)
    providers = {
        "default": "inspect",
        "providers": {
            "inspect": {
                "provider": "anthropic",
                "model": "inspect",
                "base_url": f"http://127.0.0.1:{port}",
                "api_key": "inspect",
                "max_context_tokens": 1_000_000,
                "max_output_tokens": 32_768,
                "input_modalities": ["text", "image"],
            }
        },
    }
    config_dir = target / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "providers.yaml").write_text(
        yaml.safe_dump(providers, sort_keys=False),
        encoding="utf-8",
    )


def _allocate_bridge_port() -> int:
    while True:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port not in _BRIDGE_PORTS:
            _BRIDGE_PORTS.add(port)
            return port


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
