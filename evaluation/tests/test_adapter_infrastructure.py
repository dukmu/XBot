from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import yaml

from xbot_eval.adapter import _InspectACPClient, _prepare_bridge_data


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
    assert bridge["model"] == "Minimax-M3"
    assert bridge["max_context_tokens"] == 204800
    assert bridge["max_output_tokens"] == 32768
    assert bridge["input_modalities"] == ["text", "image"]
    assert bridge["base_url"] == "http://127.0.0.1:12345"


def test_acp_permission_adapter_trusts_xbot_policy() -> None:
    client = _InspectACPClient()
    response = asyncio.run(
        client.request_permission(
            tool_call=SimpleNamespace(
                tool_call_id="call-1",
                title="filesystem_write",
                raw_input={"path": "../outside"},
            )
        )
    )

    assert response.outcome.outcome == "selected"
    assert response.outcome.option_id == "allow_once"
    assert client.events[-1]["decision"] == "allow_once"
