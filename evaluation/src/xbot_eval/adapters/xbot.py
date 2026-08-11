from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from inspect_ai.agent import AgentState
from inspect_ai.model import ModelOutput, ModelUsage
from inspect_ai.solver import Generate, Solver, TaskState, solver

from .acp import InspectACPClient, run_acp_session
from .base import AdapterContext, AdapterSetup
from .common import (
    PreparedSample,
    allocate_bridge_port,
    local_agent_bridge,
    resolve_command,
)


EVALUATION_AGENT = "harnessbench"
EVALUATION_TOOLS = (
    "filesystem_read",
    "filesystem_stat",
    "filesystem_list",
    "search_text",
    "find_files",
    "filesystem_write",
    "filesystem_edit",
    "filesystem_patch",
    "filesystem_move",
    "filesystem_copy",
    "filesystem_delete",
    "filesystem_mkdir",
    "shell",
    "start_shell",
    "list_shells",
    "wait_shell",
    "read_shell",
    "cancel_shell",
    "update_todos",
)


class XBotAdapter:
    name = "xbot"

    def prepare(
        self,
        context: AdapterContext,
        command: str | None = None,
    ) -> AdapterSetup:
        data_dir = context.run_data / self.name
        if not data_dir.exists():
            data_dir.mkdir()
            for name in ("config", ".agents", "memory"):
                source = context.source_data / name
                if source.exists():
                    shutil.copytree(source, data_dir / name)
        _configure_bridge_provider(data_dir, context.provider_name)
        _configure_evaluation_sandbox(data_dir)
        _configure_evaluation_agent(data_dir)
        executable = resolve_command(
            command or os.environ.get("XBOT_EVAL_COMMAND"),
            "xbot",
        )
        return AdapterSetup(
            command=executable,
            data_dir=data_dir,
            environment={
                "XBOT_EVAL_AGENT_COMMAND": executable,
                "XBOT_EVAL_ADAPTER_DATA": str(data_dir),
                "XBOT_EVAL_AGENT": os.environ.get(
                    "XBOT_EVAL_AGENT", EVALUATION_AGENT
                ),
            },
        )

    def solver(self) -> Solver:
        return xbot_bridge_agent(
            command=os.environ["XBOT_EVAL_AGENT_COMMAND"],
            data_dir=os.environ["XBOT_EVAL_ADAPTER_DATA"],
            agent=os.environ.get("XBOT_EVAL_AGENT"),
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
    """Run each Inspect sample through a direct XBot ACP session."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample = await PreparedSample.create(state)
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

        client = InspectACPClient()
        child_env = {
            **dict(env or {}),
            **sample.environment,
        }
        child_env.setdefault("PYTHONUTF8", "1")
        try:
            session_id, responses = await run_acp_session(
                client=client,
                command=command,
                args=args,
                workspace=sample.workspace,
                env=child_env,
                prompts=sample.prompts,
                runtime=sample.runtime,
                label="XBot",
            )
        finally:
            sample.cleanup()

        state.output = ModelOutput.from_content(
            model=f"xbot/{provider}",
            content="".join(client.message_parts),
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
        state.metadata["xbot"] = _metadata(session_id, responses, client.events)
        return state

    return solve


@solver
def xbot_bridge_agent(
    *,
    command: str,
    data_dir: str,
    agent: str | None = None,
) -> Solver:
    """Run XBot ACP through Inspect's external-Agent model bridge."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample = await PreparedSample.create(state)
        bridge_state = AgentState(messages=list(state.messages))
        root = Path(data_dir).resolve()
        client = InspectACPClient()
        session_id = ""
        try:
            port = allocate_bridge_port()
            async with local_agent_bridge(
                bridge_state,
                port=port,
            ) as bridge:
                args = [
                    "acp",
                    "--data-dir",
                    str(root),
                    "--provider",
                    "inspect",
                ]
                if agent:
                    args.extend(["--agent", agent])
                session_id, responses = await run_acp_session(
                    client=client,
                    command=command,
                    args=args,
                    workspace=sample.workspace,
                    env={
                        **sample.environment,
                        "PYTHONUTF8": "1",
                        "XBOT_EVAL_BRIDGE_URL": f"http://127.0.0.1:{bridge.port}",
                    },
                    prompts=sample.prompts,
                    runtime=sample.runtime,
                    label="XBot",
                )
                bridge_state = bridge.state
        finally:
            sample.cleanup()

        state.messages = bridge_state.messages
        state.output = bridge_state.output
        metadata = _metadata(
            session_id,
            responses,
            client.events,
            event_key="acp_events",
        )
        metadata["state_dir"] = f"sessions/{session_id}"
        state.metadata["xbot"] = metadata
        return state

    return solve


def _configure_bridge_provider(
    data_dir: Path,
    provider_name: str | None = None,
) -> None:
    provider = _load_provider(data_dir, provider_name)
    provider_type = str(provider.get("provider", "anthropic"))
    max_output_tokens = provider.get("max_output_tokens")
    if max_output_tokens is None and provider_type == "anthropic":
        max_output_tokens = 32_768
    path = data_dir / "config" / "providers.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    providers = document.setdefault("providers", {})
    providers["inspect"] = {
        "provider": provider_type,
        "model": "inspect",
        "base_url": _bridge_base_url(provider_type),
        "api_key": "inspect",
        "max_context_tokens": provider.get("max_context_tokens", 200_000),
        "max_output_tokens": max_output_tokens,
        "input_modalities": provider.get("input_modalities", ["text"]),
    }
    path.write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )


def _bridge_base_url(provider_type: str) -> str:
    """Inspect's Agent Bridge serves OpenAI-compatible requests under ``/v1``
    and Anthropic/Google requests at the root."""
    base = "${XBOT_EVAL_BRIDGE_URL}"
    return f"{base}/v1" if provider_type == "openai" else base


def _load_provider(source: Path, provider_name: str | None) -> dict[str, Any]:
    path = source / "config" / "providers.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    providers = document.get("providers") or {}
    selected = provider_name or str(document.get("default") or "")
    provider = providers.get(selected)
    if not isinstance(provider, dict):
        available = ", ".join(sorted(str(name) for name in providers))
        raise RuntimeError(
            f"Unknown evaluation bridge provider {selected!r}; "
            f"available providers: {available or '(none)'}"
        )
    return provider


def _metadata(
    session_id: str,
    responses: Sequence[Any],
    events: list[dict[str, Any]],
    *,
    event_key: str = "events",
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "turns": [
            {
                "stop_reason": response.stop_reason,
                "usage": response.usage.model_dump() if response.usage else None,
            }
            for response in responses
        ],
        event_key: events,
    }


def _configure_evaluation_sandbox(data_dir: Path) -> None:
    path = data_dir / "config" / "config.yaml"
    if not path.is_file():
        return
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    network = (
        config.setdefault("plugins", {})
        .setdefault("browser", {})
        .setdefault("config", {})
        .setdefault("network", {})
    )
    network["allow_private"] = True
    configured_cache = os.environ.get("XDG_CACHE_HOME")
    cache_dir = Path(configured_cache) if configured_cache else Path.home() / ".cache"
    resources = config.setdefault("sandbox", {}).setdefault("resources", [])
    cache_resource = {"path": str(cache_dir), "access": "readwrite"}
    if cache_dir.is_dir() and cache_resource not in resources:
        resources.append(cache_resource)
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _configure_evaluation_agent(data_dir: Path) -> None:
    agents_dir = data_dir / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    tools = "\n".join(f"  - {name}" for name in EVALUATION_TOOLS)
    (agents_dir / f"{EVALUATION_AGENT}.md").write_text(
        "---\n"
        "description: HarnessBench coding and artifact evaluation\n"
        "mode: primary\n"
        "tools:\n"
        f"{tools}\n"
        "---\n"
        "Complete the requested workspace task using only observed evidence.\n",
        encoding="utf-8",
    )


def selected_environment(*names: str) -> dict[str, str]:
    """Return explicitly selected host variables for a direct XBot run."""
    return {name: os.environ[name] for name in names if name in os.environ}


__all__ = [
    "XBotAdapter",
    "selected_environment",
    "xbot_agent",
    "xbot_bridge_agent",
]
