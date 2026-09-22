"""Isolated MockLLM server harness and HTTP client construction."""

from __future__ import annotations

import tempfile
import asyncio
import socket
from contextlib import AsyncExitStack
from functools import partial
from pathlib import Path
from typing import Any
from xcore import Context

import httpx
import yaml
from XBotv2.application.server import start_server_application
from XBotv2.application.app import create_agent_application
from XBotv2.client import XBotClient
from XBotv2.core.paths import RuntimePaths
from XBotv2.llm.mock import MockLLM
from XBotv2.core.messages import ModelChunk


def _write_config(data_dir: Path) -> None:
    config = data_dir / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "plugins.yaml").write_text(
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
                                "base_url": "http://stress.invalid",
                                "api_key": "stress",
                                "default_model": "stress-model",
                                "models": [
                                    {
                                        "model": "stress-model",
                                        "max_context_tokens": 32768,
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
                            "user_id": "stress",
                            "user_name": "Stress Runner",
                            "platform": "stress",
                            "session_type": "noninteractive",
                        }
                    },
                },
                {"id": "caption", "name": "caption", "config": {"auto": False}},
                {"id": "sandbox", "name": "sandbox", "config": {"enabled": False, "resources": []}},
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )


class FailingMockLLM(MockLLM):
    """Mock provider that fails once after publishing a partial delta.

    The failure is intentionally one-shot: a follow-up turn proves that the
    runtime released its turn lock and that clients can continue after the
    broken provider stream.  This remains a provider-level fault, separate
    from the TCP truncation probe below.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._failed_once = False

    async def _astream_once(self, messages: list[Any], **kwargs: Any):
        if self._failed_once:
            async for chunk in super()._astream_once(messages, **kwargs):
                yield chunk
            return
        self._failed_once = True
        response = self.next_response()
        self.call_history.append(list(messages))
        chunks = response.get("chunks")
        if isinstance(chunks, list) and chunks:
            first = chunks[0]
            if isinstance(first, dict):
                yield self.to_chunk(first)
            else:
                yield ModelChunk(content=str(first))
        else:
            yield ModelChunk(content="partial response before injected provider failure")
        await asyncio.sleep(0.01)
        raise RuntimeError("injected provider stream failure after partial output")


class TruncateFirstEventStream:
    """ASGI wrapper that closes the first GET ``/events`` stream.

    It forwards headers and the first body frame, then raises while the
    event stream is still open.  A real uvicorn socket therefore sees an
    incomplete HTTP/SSE stream instead of a synthetic client exception.
    Subsequent event-stream requests pass through unchanged.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._used = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        is_event_stream = (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and "/sessions/stress-stream-failure-" in path
            and path.endswith("/events")
        )
        if not is_event_stream or self._used:
            await self.app(scope, receive, send)
            return
        self._used = True
        body_frames = 0

        async def truncating_send(message: dict[str, Any]) -> None:
            nonlocal body_frames
            if message.get("type") == "http.response.body" and message.get("body"):
                body_frames += 1
                if body_frames == 1:
                    await send(message)
                    return
                raise ConnectionError("injected incomplete GET /events stream")
            await send(message)

        await self.app(scope, receive, truncating_send)


class MockServerHarness:
    """Own a disposable XBot application and typed HTTP client."""

    def __init__(
        self,
        timeout: float,
        data_dir: Path | None = None,
        *,
        mock_tools: bool = False,
        failure_mode: str = "",
    ) -> None:
        self.timeout = timeout
        self.mock_tools = mock_tools
        self.failure_mode = failure_mode
        self._external_data_dir = data_dir
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.data_dir: Path | None = None
        self.workspace: Path | None = None
        self.application: Context | None = None
        self.client: XBotClient | None = None
        self.base_url = "http://xbot-stress"

    async def __aenter__(self) -> "MockServerHarness":
        if self._external_data_dir is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="xbot-stress-")
            self.data_dir = Path(self._temporary.name) / "data"
        else:
            self.data_dir = self._external_data_dir.resolve()
        self.workspace = self.data_dir.parent / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        _write_config(self.data_dir)
        self.application = await start_server_application(
            provider_name="default",
            paths=RuntimePaths.from_data_dir(self.data_dir),
            workspace_root=str(self.workspace),
            no_plugins=False,
        )
        text_response = {
            "content": "stress response",
            "reasoning": "stress reasoning",
            "chunks": [
                {"reasoning": "stress "},
                {"content": "response"},
            ],
            # Keep a turn active long enough for concurrent-session probes to
            # reach the busy path instead of racing after a completed turn.
            "chunk_delay_ms": 50,
            "usage_metadata": {
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
            },
        }
        tool_response = {
            "tool_calls": [{
                "id": "stress-shell",
                "name": "shell",
                "args": {"command": "printf stress-tool"},
            }],
        }
        responses = ([tool_response, text_response] if self.mock_tools else [text_response]) * 10000
        provider: MockLLM
        if self.failure_mode == "provider":
            provider = FailingMockLLM(
                responses=responses,
                input_modalities=["text", "image"],
            )
        else:
            provider = MockLLM(
                responses=responses,
                input_modalities=["text", "image"],
            )
        self.application.sessions.application_factory = partial(
            create_agent_application,
            model_override=provider,
        )
        transport = httpx.ASGITransport(app=self.application.server)
        self.client = XBotClient(
            base_url=self.base_url,
            timeout=self.timeout,
            transport=transport,
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self.client is not None:
            await self.client.close()
        if self.application is not None:
            await self.application.stop()
        if self._temporary is not None:
            self._temporary.cleanup()


def real_client(base_url: str, timeout: float) -> XBotClient:
    """Build a client for an already running HTTP service."""
    return XBotClient(base_url=base_url, timeout=timeout)


class LiveMockServerHarness(MockServerHarness):
    """Run the disposable MockLLM application through a real TCP socket."""

    def __init__(self, timeout: float, data_dir: Path | None = None, *, mock_tools: bool = False, failure_mode: str = "") -> None:
        super().__init__(timeout, data_dir, mock_tools=mock_tools, failure_mode=failure_mode)
        self._server: Any = None
        self._server_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "LiveMockServerHarness":
        await super().__aenter__()
        import uvicorn

        if self.application is None or self.client is None:
            raise RuntimeError("live stress server requires an initialized application")
        await self.client.close()
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        serve_app: Any = self.application.server
        if self.failure_mode == "http_truncate":
            serve_app = TruncateFirstEventStream(serve_app)
        self._server = uvicorn.Server(
            uvicorn.Config(
                serve_app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )
        self._server_task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                self.client = XBotClient(
                    base_url=f"http://127.0.0.1:{port}",
                    timeout=self.timeout,
                )
                self.base_url = f"http://127.0.0.1:{port}"
                return self
            await asyncio.sleep(0.01)
        raise RuntimeError("live stress server did not start")

    async def __aexit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            await self._server_task
        await super().__aexit__(*exc_info)


class MultiMockServerHarness:
    """Own independent application instances sharing one persisted data root."""

    def __init__(
        self,
        count: int,
        timeout: float,
        *,
        live_http: bool,
        data_dir: Path | None = None,
        mock_tools: bool = False,
        failure_mode: str = "",
    ) -> None:
        self.count = count
        self.timeout = timeout
        self.live_http = live_http
        self._external_data_dir = data_dir
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.data_dir: Path | None = None
        self._stack: AsyncExitStack | None = None
        self.harnesses: list[MockServerHarness] = []
        self.clients: list[XBotClient] = []
        self.base_urls: list[str] = []
        self.mock_tools = mock_tools
        self.failure_mode = failure_mode

    async def __aenter__(self) -> "MultiMockServerHarness":
        if self._external_data_dir is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="xbot-stress-multi-")
            self.data_dir = Path(self._temporary.name) / "data"
        else:
            self.data_dir = self._external_data_dir.resolve()
        harness_type = LiveMockServerHarness if self.live_http else MockServerHarness
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for _ in range(self.count):
            harness = await self._stack.enter_async_context(
                harness_type(
                    self.timeout,
                    self.data_dir,
                    mock_tools=self.mock_tools,
                    failure_mode=self.failure_mode,
                )
            )
            if harness.client is None:
                raise RuntimeError("multi-server harness did not create a client")
            self.harnesses.append(harness)
            self.clients.append(harness.client)
            self.base_urls.append(harness.base_url)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(*exc_info)
        if self._temporary is not None:
            self._temporary.cleanup()
