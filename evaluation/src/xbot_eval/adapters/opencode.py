from __future__ import annotations

import json
import os
from pathlib import Path

from inspect_ai.agent import AgentState, sandbox_agent_bridge
from inspect_ai.solver import Generate, Solver, TaskState, solver

from .acp import InspectACPClient, run_acp_session
from .base import AdapterContext, AdapterSetup
from .common import (
    PreparedSample,
    allocate_bridge_port,
    create_attempt_dir,
    resolve_command,
)


class OpenCodeAdapter:
    name = "opencode"

    def prepare(
        self,
        context: AdapterContext,
        command: str | None = None,
    ) -> AdapterSetup:
        if context.provider.get("provider") != "anthropic":
            raise ValueError(
                "OpenCode comparison currently requires an Anthropic provider"
            )
        data_dir = context.run_data / self.name
        data_dir.mkdir(exist_ok=True)
        config = data_dir / "adapter.json"
        config.write_text(
            json.dumps(
                {
                    "provider": {
                        key: value
                        for key, value in context.provider.items()
                        if key not in {"api_key", "api_key_env"}
                    }
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        executable = resolve_command(
            command or os.environ.get("OPENCODE_EVAL_COMMAND"),
            "opencode",
        )
        return AdapterSetup(
            command=executable,
            data_dir=data_dir,
            environment={
                "XBOT_EVAL_AGENT_COMMAND": executable,
                "XBOT_EVAL_ADAPTER_DATA": str(data_dir),
            },
        )

    def solver(self) -> Solver:
        data_dir = Path(os.environ["XBOT_EVAL_ADAPTER_DATA"])
        document = json.loads(
            (data_dir / "adapter.json").read_text(encoding="utf-8")
        )
        return opencode_bridge_agent(
            command=os.environ["XBOT_EVAL_AGENT_COMMAND"],
            data_dir=str(data_dir),
            provider=document["provider"],
        )


@solver
def opencode_bridge_agent(
    *,
    command: str,
    data_dir: str,
    provider: dict[str, object],
) -> Solver:
    """Run OpenCode ACP through Inspect's external-Agent model bridge."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample = await PreparedSample.create(state)
        root = Path(data_dir).resolve()
        run_dir = create_attempt_dir(root / "samples", state)
        bridge_state = AgentState(messages=list(state.messages))
        client = InspectACPClient()
        session_id = ""
        try:
            async with sandbox_agent_bridge(
                bridge_state,
                sandbox="local",
                port=allocate_bridge_port(),
            ) as bridge:
                config = run_dir / "opencode.json"
                _write_opencode_config(config, provider, bridge.port)
                child_env = {
                    **_opencode_environment(run_dir, config),
                    **sample.environment,
                }
                session_id, responses = await run_acp_session(
                    client=client,
                    command=command,
                    args=["acp", "--pure", "--cwd", str(sample.workspace)],
                    workspace=sample.workspace,
                    env=child_env,
                    prompts=sample.prompts,
                    runtime=sample.runtime,
                    label="OpenCode",
                )
                bridge_state = bridge.state
        finally:
            sample.cleanup()

        state.messages = bridge_state.messages
        state.output = bridge_state.output
        state.metadata["opencode"] = {
            "session_id": session_id,
            "state_dir": str(run_dir.relative_to(root)),
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


def _write_opencode_config(
    path: Path,
    provider: dict[str, object],
    port: int,
) -> None:
    model = str(provider["model"])
    input_modalities = list(provider.get("input_modalities") or ["text"])
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": "anthropic/inspect",
        "small_model": "anthropic/inspect",
        "enabled_providers": ["anthropic"],
        "provider": {
            "anthropic": {
                "options": {
                    "apiKey": "inspect",
                    "baseURL": f"http://127.0.0.1:{port}/v1",
                    "timeout": False,
                },
                "models": {
                    "inspect": {
                        "name": model,
                        "attachment": "image" in input_modalities,
                        "reasoning": False,
                        "temperature": True,
                        "tool_call": True,
                        "limit": {
                            "context": int(provider["max_context_tokens"]),
                            "output": int(
                                provider.get("max_output_tokens") or 32_768
                            ),
                        },
                        "modalities": {
                            "input": input_modalities,
                            "output": ["text"],
                        },
                    }
                },
            }
        },
        "permission": {"external_directory": "deny"},
        "mcp": {},
        "plugin": [],
        "share": "disabled",
        "autoupdate": False,
    }
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _opencode_environment(run_dir: Path, config: Path) -> dict[str, str]:
    roots = {
        "HOME": run_dir / "home",
        "XDG_CONFIG_HOME": run_dir / "config",
        "XDG_DATA_HOME": run_dir / "data",
        "XDG_CACHE_HOME": run_dir / "cache",
        "XDG_STATE_HOME": run_dir / "state",
        "TMPDIR": run_dir / "tmp",
    }
    for path in roots.values():
        path.mkdir()
    return {
        key: str(value)
        for key, value in roots.items()
    } | {
        "ANTHROPIC_API_KEY": "inspect",
        "OPENCODE_CONFIG": str(config),
    }


__all__ = ["OpenCodeAdapter", "opencode_bridge_agent"]
