"""Real-verification server: real LLM endpoint + scripted auxiliary calls.

The main turn streams from the configured real provider; auxiliary calls
(caption, compaction summary) return scripted text so the deterministic
transcript/compaction path is exercised over the real HTTP transport.
"""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path

import uvicorn
import yaml

from XBotv2.application.server import start_server_application
from XBotv2.application.app import create_agent_application
from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.mock import MockLLM

DATA = Path(__file__).resolve().parents[3] / ".verify" / "data"
WORKSPACE = Path(__file__).resolve().parents[3] / ".verify" / "workspace"


class EndlessMock(MockLLM):
    """MockLLM whose response list repeats for every auxiliary call."""

    def next_response(self) -> dict:
        state = self._state
        response = self.responses[state.call_count % len(self.responses)]
        state.call_count += 1
        return response


class RealMainScriptedAux:
    """Real provider for the main turn; scripted text for auxiliary calls."""

    def __init__(self, real: MockLLM) -> None:
        self._real = real
        self._title = MockLLM(responses=[
            {"content": "Python GIL 讨论"},
        ])
        self._summary = EndlessMock(responses=[
            {"content": "Compacted summary of the earlier Python discussion."},
        ])

    @property
    def call_count(self) -> int:
        return self._real.call_count

    def bind_artifacts(self, artifacts):
        self._real.bind_artifacts(artifacts)
        return self

    def bind_tools(self, tools):
        self._real.bind_tools(tools)
        return self

    def _route(self, messages, kwargs) -> MockLLM:
        text = " ".join(str(message.content) for message in messages)
        if "You derive one short, human-readable title" in text:
            return self._title
        if (
            "summary_instructions" in text
            or "historical_context" in text
            or "Produce the conversation summary now" in text
        ):
            return self._summary
        return self._real

    async def astream(self, messages, **kwargs):
        mock = self._route(messages, kwargs)
        if mock is not self._real:
            async for chunk in mock.astream(messages, **kwargs):
                yield chunk
            return
        async for chunk in self._real.astream(messages, **kwargs):
            yield chunk


async def main() -> None:
    application = await start_server_application(
        provider_name="default",
        paths=RuntimePaths.from_data_dir(DATA),
        workspace_root=str(WORKSPACE),
        no_plugins=False,
    )
    application.sessions.application_factory = partial(
        create_agent_application,
        model_override=RealMainScriptedAux(MockLLM(
            responses=[
                {"content": f"Real reply {index}.", "chunk_delay_ms": 2}
                for index in range(1, 500)
            ],
            input_modalities=["text", "image"],
        )),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            application.server,
            host="127.0.0.1",
            port=4098,
            log_level="warning",
        )
    )
    try:
        await server.serve()
    finally:
        await application.stop()


if __name__ == "__main__":
    asyncio.run(main())
