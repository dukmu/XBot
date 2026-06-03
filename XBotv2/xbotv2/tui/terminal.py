"""Terminal (non-curses) client — ported from XBot v1.

Communicates with the server over JSONL via stdin/stdout subprocess.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from xbotv2.protocol.frames import ProtocolFrame, frame_from_json


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
            self._process.stdin.close()
            await self._process.wait()
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
        return frame_from_json(line.decode("utf-8").strip())


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
        session_id: str = "default",
        thread_id: str = "agent",
    ) -> None:
        self._data_dir = str(data_dir)
        self._personality_id = personality_id
        self._provider_name = provider_name
        self._client: ProtocolClient | None = None
        self._session_id = session_id
        self._thread_id = thread_id

    async def __aenter__(self) -> "TerminalSession":
        await self.connect()
        return self

    async def __aexit__(self, *args) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Start the server and perform handshake."""
        server_cmd = [
            sys.executable, "-m", "xbotv2",
            "--data-dir", self._data_dir,
            "--personality", self._personality_id,
            "--provider", self._provider_name,
            "--mode", "server",
        ]
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
            await self._client.stop()

    async def send_message(self, content: str):
        """Send a user message and yield responses."""
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

            if frame.type in ("turn_finished", "error"):
                break
