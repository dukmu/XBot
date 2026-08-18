from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import yaml
from acp.schema import PermissionOption

from xbot_eval.adapters.acp import InspectACPClient
from xbot_eval.adapters import common
from xbot_eval.adapters.common import local_agent_bridge
from xbot_eval.adapters.opencode import (
    _opencode_environment,
    _write_opencode_config,
)
from xbot_eval.adapters.xbot import _configure_bridge_provider


def test_bridge_provider_uses_selected_provider_config(tmp_path: Path) -> None:
    source = tmp_path / "source"
    config = source / "config"
    config.mkdir(parents=True)
    (config / "plugins.yaml").write_text(
        yaml.safe_dump([
            {
                "id": "llm",
                "name": "llm",
                "config": {
                    "default": "minimax",
                    "providers": {
                        "minimax": {
                            "protocol": "anthropic",
                            "default_model": "Minimax-M3",
                            "models": [
                                {
                                    "model": "Minimax-M3",
                                    "max_context_tokens": 204800,
                                    "max_output_tokens": 32768,
                                    "temperature": 0.2,
                                    "thinking": "adaptive",
                                    "input_modalities": ["text", "image"],
                                }
                            ],
                        }
                    },
                },
            },
        ]),
        encoding="utf-8",
    )
    _configure_bridge_provider(source, provider_name="minimax")

    generated = yaml.safe_load(
        (source / "config" / "plugins.yaml").read_text(encoding="utf-8")
    )
    llm_entry = next(item for item in generated if item["id"] == "llm")
    bridge = llm_entry["config"]["providers"]["inspect"]
    assert bridge["protocol"] == "anthropic"
    assert bridge["default_model"] == "inspect"
    model = bridge["models"][0]
    assert model["model"] == "inspect"
    assert model["max_context_tokens"] == 204800
    assert model["max_output_tokens"] == 32768
    assert model["input_modalities"] == ["text", "image"]
    assert bridge["base_url"] == "${XBOT_EVAL_BRIDGE_URL}"


def test_acp_permission_adapter_selects_standard_allow_once_option() -> None:
    client = InspectACPClient()
    response = asyncio.run(
        client.request_permission(
            tool_call=SimpleNamespace(
                tool_call_id="call-1",
                title="filesystem_write",
                raw_input={"path": "../outside"},
            ),
            options=[
                PermissionOption(
                    option_id="approve-this-call",
                    name="Allow once",
                    kind="allow_once",
                )
            ],
        )
    )

    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == "approve-this-call"
    assert client.events[-1]["decision"] == "approve-this-call"


def test_local_bridge_serializes_startup_without_serializing_sessions(
    monkeypatch,
) -> None:
    entering = 0
    max_entering = 0
    active_sessions = 0
    both_active = asyncio.Event()

    @asynccontextmanager
    async def fake_bridge(*args, **kwargs):
        nonlocal entering, max_entering, active_sessions
        entering += 1
        max_entering = max(max_entering, entering)
        await asyncio.sleep(0)
        entering -= 1
        active_sessions += 1
        if active_sessions == 2:
            both_active.set()
        try:
            yield SimpleNamespace()
        finally:
            active_sessions -= 1

    async def run() -> None:
        async def session() -> None:
            async with local_agent_bridge(SimpleNamespace(), port=12345):
                await asyncio.wait_for(both_active.wait(), timeout=1)

        await asyncio.gather(session(), session())

    monkeypatch.setattr(common, "sandbox_agent_bridge", fake_bridge)
    asyncio.run(run())

    assert max_entering == 1


def test_opencode_environment_only_sets_bridge_configuration(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "sample"
    run_dir.mkdir()
    config_path = run_dir / "opencode.json"
    _write_opencode_config(
        config_path,
        {
            "provider": "anthropic",
            "model": "Minimax-M3",
            "max_context_tokens": 204800,
            "max_output_tokens": 32768,
            "thinking": "adaptive",
            "input_modalities": ["text", "image"],
        },
    )
    env = _opencode_environment(config_path, port=12345)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    model = config["provider"]["anthropic"]["models"]["inspect"]
    assert config["model"] == "anthropic/inspect"
    assert model["name"] == "Minimax-M3"
    assert config["provider"]["anthropic"]["options"]["baseURL"] == (
        "{env:XBOT_EVAL_BRIDGE_URL}"
    )
    assert config["permission"]["external_directory"] == "deny"
    assert model["limit"] == {"context": 204800, "output": 32768}
    assert model["modalities"]["input"] == ["text", "image"]
    assert env["ANTHROPIC_API_KEY"] == "inspect"
    assert env["OPENCODE_CONFIG"] == str(config_path)
    assert env["XBOT_EVAL_BRIDGE_URL"] == "http://127.0.0.1:12345/v1"
