"""Isolated MockLLM server harness and HTTP client construction."""

from __future__ import annotations

import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from xcore import Context

import httpx
import yaml
from XBotv2.application.server import start_server_application
from XBotv2.client import XBotClient
from XBotv2.core.paths import RuntimePaths
from XBotv2.loader import PluginOverlay

from .provider import LoopbackServer, NetworkMockLLM


def _write_config(data_dir: Path, provider_url: str) -> None:
    config = data_dir / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "plugins.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": "llm",
                    "name": "llm",
                    "config": {
                        "default_provider": "default",
                        "providers": {
                            "default": {
                                "protocol": "openai",
                                "base_url": provider_url,
                                "api_key": "stress",
                                "default_model": "stress-model",
                                "models": [
                                    {
                                        "model": "stress-model",
                                        "max_context_tokens": 32768,
                                        "max_output_tokens": 4096,
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
        self._stack = AsyncExitStack()
        self.provider = NetworkMockLLM(
            mock_tools=mock_tools,
            fault="disconnect" if failure_mode == "provider" else "",
        )
        self.data_dir: Path | None = None
        self.workspace: Path | None = None
        self.application: Context | None = None
        self.client: XBotClient | None = None
        self.base_url = "http://xbot-stress"

    async def __aenter__(self) -> "MockServerHarness":
        try:
            if self._external_data_dir is None:
                temporary = self._stack.enter_context(tempfile.TemporaryDirectory(prefix="xbot-stress-"))
                self.data_dir = Path(temporary) / "data"
            else:
                self.data_dir = self._external_data_dir.resolve()
            self.workspace = self.data_dir.parent / "workspace"
            self.workspace.mkdir(parents=True, exist_ok=True)
            await self._stack.enter_async_context(self.provider)
            _write_config(self.data_dir, self.provider.base_url)
            self.application = await start_server_application(
                paths=RuntimePaths.from_data_dir(self.data_dir),
                overrides=PluginOverlay.parse([{
                    "id": "session",
                    "config": {
                        "workspace_root": str(self.workspace),
                        "provider_name": "default",
                    },
                }]),
            )
            self._stack.push_async_callback(self.application.stop)
            transport = httpx.ASGITransport(app=self.application.server)
            self.client = XBotClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=transport,
            )
            self._stack.push_async_callback(self.client.close)
            return self
        except BaseException:
            await self._stack.aclose()
            raise

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.__aexit__(*exc_info)


def real_client(base_url: str, timeout: float) -> XBotClient:
    """Build a client for an already running HTTP service."""
    return XBotClient(base_url=base_url, timeout=timeout)


class LiveMockServerHarness(MockServerHarness):
    """Run the disposable MockLLM application through a real TCP socket."""

    async def __aenter__(self) -> "LiveMockServerHarness":
        await super().__aenter__()
        try:
            if self.application is None or self.client is None:
                raise RuntimeError("live stress server requires an initialized application")
            await self.client.close()
            serve_app: Any = self.application.server
            if self.failure_mode == "http_truncate":
                serve_app = TruncateFirstEventStream(serve_app)
            listener = await self._stack.enter_async_context(LoopbackServer(serve_app))
            self.base_url = listener.base_url
            self.client = XBotClient(base_url=self.base_url, timeout=self.timeout)
            self._stack.push_async_callback(self.client.close)
            return self
        except BaseException:
            await self._stack.aclose()
            raise


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
