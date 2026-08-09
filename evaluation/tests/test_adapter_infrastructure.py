from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import yaml
from acp.schema import PermissionOption

from xbot_eval.adapters.acp import InspectACPClient
from xbot_eval.adapters.opencode import (
    _opencode_environment,
    _write_opencode_config,
)
from xbot_eval.adapters.xbot import _prepare_bridge_data


def test_bridge_provider_uses_selected_provider_config(tmp_path: Path) -> None:
    source = tmp_path / "source"
    config = source / "config"
    config.mkdir(parents=True)
    (config / "providers.yaml").write_text(
        yaml.safe_dump({
            "default": "minimax",
            "providers": {
                "minimax": {
                    "provider": "anthropic",
                    "model": "Minimax-M3",
                    "max_context_tokens": 204800,
                    "max_output_tokens": 32768,
                    "temperature": 0.2,
                    "thinking_enabled": True,
                    "input_modalities": ["text", "image"],
                }
            },
        }),
        encoding="utf-8",
    )
    target = tmp_path / "target"

    _prepare_bridge_data(source, target, port=12345, provider_name="minimax")

    generated = yaml.safe_load(
        (target / "config" / "providers.yaml").read_text(encoding="utf-8")
    )
    bridge = generated["providers"]["inspect"]
    assert bridge["provider"] == "anthropic"
    assert bridge["model"] == "inspect"
    assert bridge["max_context_tokens"] == 204800
    assert bridge["max_output_tokens"] == 32768
    assert bridge["input_modalities"] == ["text", "image"]
    assert bridge["base_url"] == "http://127.0.0.1:12345"


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


def test_opencode_config_and_state_are_isolated(tmp_path: Path) -> None:
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
            "thinking_enabled": True,
            "input_modalities": ["text", "image"],
        },
        port=12345,
    )
    env = _opencode_environment(run_dir, config_path)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    model = config["provider"]["anthropic"]["models"]["inspect"]
    assert config["model"] == "anthropic/inspect"
    assert model["name"] == "Minimax-M3"
    assert config["provider"]["anthropic"]["options"]["baseURL"] == (
        "http://127.0.0.1:12345/v1"
    )
    assert config["permission"]["external_directory"] == "deny"
    assert model["limit"] == {"context": 204800, "output": 32768}
    assert model["modalities"]["input"] == ["text", "image"]
    assert env["ANTHROPIC_API_KEY"] == "inspect"
    assert all(
        Path(value).is_relative_to(run_dir)
        for key, value in env.items()
        if key not in {"ANTHROPIC_API_KEY"}
    )
