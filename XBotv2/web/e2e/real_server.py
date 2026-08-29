"""Disposable XBot HTTP server for the real-browser WebUI smoke test."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
import yaml

from XBotv2.application.server import start_server_application
from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.mock import MockLLM
from XBotv2.server.http import set_llm_override


async def main() -> None:
    with TemporaryDirectory(prefix="xbot-web-e2e-") as temporary:
        root = Path(temporary)
        data_dir = root / "data"
        config_dir = data_dir / "config"
        workspace = root / "workspace"
        config_dir.mkdir(parents=True)
        workspace.mkdir()
        (config_dir / "plugins.yaml").write_text(
            yaml.safe_dump(
                [
                    {
                        "id": "llm",
                        "name": "llm",
                        "config": {
                            "default": "default",
                            "providers": {
                                "default": {
                                    "protocol": "openai",
                                    "base_url": "http://test",
                                    "api_key": "test",
                                    "default_model": "test",
                                    "models": [
                                        {
                                            "model": "test",
                                            "max_context_tokens": 4096,
                                            "input_modalities": ["text", "image"],
                                        }
                                    ],
                                }
                            },
                        },
                    },
                    {
                        "id": "config",
                        "name": "config",
                        "config": {
                            "user": {
                                "user_id": "web-e2e",
                                "user_name": "Web E2E",
                                "platform": "web",
                                "session_type": "interactive",
                            }
                        },
                    },
                    {
                        "id": "sandbox",
                        "name": "sandbox",
                        "config": {"sandbox": {"enabled": False, "resources": []}},
                    },
                ],
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        application = await start_server_application(
            provider_name="default",
            paths=RuntimePaths.from_data_dir(data_dir),
            workspace_root=str(workspace),
            no_plugins=True,
        )
        set_llm_override(
            application.server,
            MockLLM(
                responses=[
                    {
                        "content": "A real MockLLM response through the XBot HTTP stream.",
                        "usage_metadata": {
                            "input_tokens": 12,
                            "output_tokens": 10,
                            "total_tokens": 22,
                        },
                    },
                    {"content": "The clipboard image reached the real MockLLM."},
                ],
                input_modalities=["text", "image"],
            ),
        )
        server = uvicorn.Server(
            uvicorn.Config(
                application.server,
                host="127.0.0.1",
                port=4097,
                log_level="warning",
            )
        )
        try:
            await server.serve()
        finally:
            await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
