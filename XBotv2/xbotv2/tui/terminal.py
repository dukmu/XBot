"""Terminal (non-curses) client — ported from XBot v1.

Communicates with the server over JSONL via stdin/stdout subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, TextIO

from xbotv2.protocol.frames import ProtocolFrame, frame_from_json
from xbotv2.tui.trace import trace_event


class ProtocolClient:
    """JSONL client that talks to an xbotv2 server subprocess."""

    def __init__(self, server_cmd: list[str]) -> None:
        self._server_cmd = server_cmd
        self._process: asyncio.subprocess.Process | None = None
        self._seq = 0

    async def start(self) -> None:
        """Launch the server subprocess."""
        self._process = await asyncio.create_subprocess_exec(
            *self._server_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
        )

    async def stop(self) -> None:
        """Terminate the server."""
        if self._process:
            process = self._process
            if process.stdin and not process.stdin.is_closing():
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionError, RuntimeError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            await self._drain_process_pipes(process)
            self._process = None

    async def send(
        self,
        frame_type: str,
        session_id: str,
        thread_id: str,
        payload: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> ProtocolFrame:
        """Build and send a frame. Returns the frame (for request_id tracking)."""
        self._seq += 1
        frame = ProtocolFrame(
            seq=self._seq,
            direction="client_to_server",
            type=frame_type,
            session_id=session_id,
            thread_id=thread_id,
            request_id=request_id,
            payload=payload or {},
        )
        if self._process and self._process.stdin:
            self._process.stdin.write(frame.to_json_line().encode("utf-8"))
            await self._process.stdin.drain()
        trace_event("protocol.send", {"frame": frame.model_dump(mode="json")})
        return frame

    async def read_frame(self) -> ProtocolFrame | None:
        """Read one frame from the server's stdout."""
        if not self._process or not self._process.stdout:
            return None
        try:
            line = await self._process.stdout.readline()
        except (EOFError, ConnectionError):
            return None
        if not line:
            return None
        frame = frame_from_json(line.decode("utf-8").strip())
        trace_event("protocol.recv", {"frame": frame.model_dump(mode="json")})
        return frame

    async def _drain_process_pipes(self, process: asyncio.subprocess.Process) -> None:
        for pipe in (process.stdout, process.stderr):
            if pipe is None:
                continue
            try:
                await asyncio.wait_for(pipe.read(), timeout=1)
            except (asyncio.TimeoutError, ValueError):
                continue


class TerminalSession:
    """Interactive terminal session using the JSONL protocol.

    Usage::

        async with TerminalSession() as session:
            await session.connect()
            async for response in session.send_message("hello"):
                print(response)
    """

    def __init__(
        self,
        data_dir: Path | str = "data",
        personality_id: str = "default",
        provider_name: str = "default",
        session_id: str | None = None,
        thread_id: str = "agent",
        no_plugins: bool = False,
    ) -> None:
        self._data_dir = str(data_dir)
        self._personality_id = personality_id
        self._provider_name = provider_name
        self._no_plugins = no_plugins
        self._client: ProtocolClient | None = None
        self._session_id = session_id or uuid.uuid4().hex
        self._thread_id = thread_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    async def __aenter__(self) -> "TerminalSession":
        await self.connect()
        return self

    async def __aexit__(self, *args) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Start the server and perform handshake."""
        server_entrypoint = Path(__file__).resolve().parents[2] / "main.py"
        server_cmd = [
            sys.executable, str(server_entrypoint),
            "--data-dir", self._data_dir,
            "--personality", self._personality_id,
            "--provider", self._provider_name,
            "--mode", "server",
        ]
        if self._no_plugins:
            server_cmd.append("--no-plugins")
        self._client = ProtocolClient(server_cmd)
        await self._client.start()

        # Handshake
        await self._client.send(
            "hello",
            self._session_id,
            self._thread_id,
            {"client_name": "terminal", "personality_id": self._personality_id},
        )
        hello_ok = await self._client.read_frame()
        if hello_ok is None:
            raise RuntimeError("Handshake failed: server closed stdout")
        if hello_ok.type != "hello_ok":
            raise RuntimeError(f"Handshake failed: {hello_ok}")

        # Open session
        await self._client.send(
            "session.open",
            self._session_id,
            self._thread_id,
        )
        ready = await self._client.read_frame()
        if ready is None:
            raise RuntimeError("Session open failed: server closed stdout")
        if ready.type != "session_ready":
            raise RuntimeError(f"Session open failed: {ready}")

    async def disconnect(self) -> None:
        """Shut down the server."""
        if self._client:
            await self._client.send("shutdown", self._session_id, self._thread_id)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._client.read_frame(), timeout=2)
            await self._client.stop()

    async def send_message(self, content: str):
        """Send a user message and yield responses."""
        async for event in self.send_message_with_input(content):
            yield event

    async def send_message_with_input(
        self,
        content: str,
        input_provider: Callable[[dict[str, Any]], Awaitable[Any] | Any] | None = None,
        permission_provider: Callable[[dict[str, Any]], Awaitable[Any] | Any] | None = None,
    ):
        """Send a user message and optionally answer live interaction requests."""
        if not self._client:
            raise RuntimeError("Not connected")

        await self._client.send(
            "user.message",
            self._session_id,
            self._thread_id,
            {"content": content},
        )

        while True:
            frame = await self._client.read_frame()
            if frame is None:
                break

            yield {
                "type": frame.type,
                "data": frame.payload,
            }

            if frame.type == "user_input_required" and input_provider is not None:
                answer = input_provider(frame.payload)
                if hasattr(answer, "__await__"):
                    answer = await answer
                await self._client.send(
                    "user.input",
                    self._session_id,
                    self._thread_id,
                    {
                        "request_id": frame.payload.get("request_id", ""),
                        "answer": answer,
                    },
                )
            elif frame.type == "permission_request" and permission_provider is not None:
                parsed = permission_provider(frame.payload)
                if hasattr(parsed, "__await__"):
                    parsed = await parsed
                if isinstance(parsed, dict):
                    decision = str(parsed.get("decision") or "deny")
                    scope = str(parsed.get("scope") or "once")
                else:
                    decision = str(parsed)
                    scope = "once"
                await self._client.send(
                    "permission.response",
                    self._session_id,
                    self._thread_id,
                    {
                        "request_id": frame.payload.get("request_id", ""),
                        "decision": decision,
                        "scope": scope,
                    },
                )

            if frame.type in ("turn_finished", "error"):
                break

    async def submit_user_input(self, request_id: str, answer: Any) -> dict[str, Any]:
        """Submit an answer for a pending ask_user request."""
        if not self._client:
            raise RuntimeError("Not connected")

        await self._client.send(
            "user.input",
            self._session_id,
            self._thread_id,
            {"request_id": request_id, "answer": answer},
        )
        frame = await self._client.read_frame()
        if frame is None:
            raise RuntimeError("Server closed stdout before user input response")
        return {"type": frame.type, "data": frame.payload}

    async def respond_permission(
        self,
        request_id: str,
        decision: str,
        *,
        scope: str = "once",
    ) -> dict[str, Any]:
        """Submit allow/deny for a pending permission request."""
        if not self._client:
            raise RuntimeError("Not connected")

        await self._client.send(
            "permission.response",
            self._session_id,
            self._thread_id,
            {"request_id": request_id, "decision": decision, "scope": scope},
        )
        frame = await self._client.read_frame()
        if frame is None:
            raise RuntimeError("Server closed stdout before permission response")
        return {"type": frame.type, "data": frame.payload}
