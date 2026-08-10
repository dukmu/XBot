from __future__ import annotations

import json
import os
from pathlib import Path

from inspect_ai.agent import AgentState
from inspect_ai.solver import Generate, Solver, TaskState, solver

from .acp import InspectACPClient, run_acp_session
from .base import AdapterContext, AdapterSetup
from .common import (
    PreparedSample,
    allocate_bridge_port,
    local_agent_bridge,
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
        config = data_dir / "opencode.json"
        _write_opencode_config(
            config,
            {
                key: value
                for key, value in context.provider.items()
                if key not in {"api_key", "api_key_env"}
            },
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
        return opencode_bridge_agent(
            command=os.environ["XBOT_EVAL_AGENT_COMMAND"],
            data_dir=str(data_dir),
        )


@solver
def opencode_bridge_agent(
    *,
    command: str,
    data_dir: str,
) -> Solver:
    """Run OpenCode ACP through Inspect's external-Agent model bridge."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample = await PreparedSample.create(state)
        root = Path(data_dir).resolve()
        bridge_state = AgentState(messages=list(state.messages))
        client = InspectACPClient()
        session_id = ""
        try:
            async with local_agent_bridge(
                bridge_state,
                port=allocate_bridge_port(),
            ) as bridge:
                config = root / "opencode.json"
                child_env = {
                    **sample.environment,
                    **_opencode_environment(config, bridge.port),
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
            "state_dir": "runtime",
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
                    "baseURL": "{env:XBOT_EVAL_BRIDGE_URL}",
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


def _opencode_environment(config: Path, port: int) -> dict[str, str]:
    return {
        "ANTHROPIC_API_KEY": "inspect",
        "OPENCODE_CONFIG": str(config),
        "XBOT_EVAL_BRIDGE_URL": f"http://127.0.0.1:{port}/v1",
    }


__all__ = ["OpenCodeAdapter", "opencode_bridge_agent"]
