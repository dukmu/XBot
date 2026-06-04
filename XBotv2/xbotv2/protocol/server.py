"""JSONL stdio server — ported from XBot v1.

Reads ProtocolFrame lines from stdin, dispatches to the engine,
writes ProtocolFrame lines to stdout.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from xbotv2.core.bootstrap import bootstrap
from xbotv2.protocol.frames import (
    ProtocolEncoder,
    ProtocolFrame,
    frame_from_json,
)

logger = logging.getLogger("xbotv2.server")


class RuntimeServer:
    """JSONL-over-stdio server that owns an XBotv2 engine.

    Reads commands from stdin, writes events to stdout.
    Commands: hello, session.open, user.message, shutdown.
    """

    def __init__(
        self,
        data_dir: Path | str = "data",
        personality_id: str = "default",
        provider_name: str = "default",
    ) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._personality_id = personality_id
        self._provider_name = provider_name
        self._engine = None
        self._encoder: ProtocolEncoder | None = None
        self._session_id = "default"
        self._thread_id = "agent"

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Read stdin, dispatch commands, write stdout."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        writer_transport, writer_protocol = await asyncio.get_event_loop().connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(writer_transport, writer_protocol, reader, asyncio.get_event_loop())

        while True:
            try:
                line = await reader.readline()
            except (EOFError, ConnectionError):
                break

            if not line:
                break

            try:
                line = line.decode("utf-8").strip()
                if not line:
                    continue
                frame = frame_from_json(line)
            except Exception as exc:
                logger.error("Failed to parse frame: %s", exc)
                response = self._make_frame("error", {
                    "code": "invalid_frame",
                    "message": f"Invalid protocol frame: {exc}",
                })
                writer.write(response.to_json_line().encode("utf-8"))
                await writer.drain()
                continue

            response = await self._dispatch(frame, writer)

            if response:
                writer.write(response.to_json_line().encode("utf-8"))
                await writer.drain()

            if frame.type == "shutdown":
                break

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, frame: ProtocolFrame, writer: Any) -> ProtocolFrame | None:
        cmd = frame.type

        if cmd == "hello":
            return self._handle_hello(frame)

        if cmd == "session.open":
            return await self._handle_session_open(frame, writer)

        if cmd == "user.message":
            return await self._handle_user_message(frame, writer)

        if cmd == "shutdown":
            return self._handle_shutdown(frame)

        return self._make_frame("error", {
            "code": "unknown_command",
            "message": f"Unknown command: {cmd}",
        }, frame.request_id)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_hello(self, frame: ProtocolFrame) -> ProtocolFrame:
        self._session_id = frame.session_id or frame.payload.get("session_id", "default")
        self._thread_id = frame.thread_id or frame.payload.get("thread_id", "agent")
        self._personality_id = frame.payload.get("personality_id", self._personality_id)

        self._encoder = ProtocolEncoder(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        return self._encoder.encode_hello_ok(request_id=frame.request_id)

    async def _handle_session_open(
        self, frame: ProtocolFrame, writer: Any
    ) -> ProtocolFrame | None:
        """Bootstrap the engine and open a session."""
        try:
            self._engine = await bootstrap(
                config_dir=str(self._data_dir),
                personality_id=self._personality_id,
                provider_name=self._provider_name,
                session_id=self._session_id,
                thread_id=self._thread_id,
            )
            await self._engine.start_session()

            writer.write(
                self._encoder.encode_session_ready(
                    agent_name=getattr(self._engine.config, "agent_name", "XBotv2"),
                    request_id=frame.request_id,
                ).to_json_line().encode("utf-8")
            )
            await writer.drain()
        except Exception as exc:
            logger.exception("Session open failed")
            return self._make_frame("error", {
                "code": "session_open_failed",
                "message": str(exc),
            }, frame.request_id)

        return None

    async def _handle_user_message(
        self, frame: ProtocolFrame, writer: Any
    ) -> ProtocolFrame | None:
        """Process a user message through the engine."""
        if self._engine is None:
            return self._make_frame("error", {
                "code": "no_session",
                "message": "No active session. Send session.open first.",
            }, frame.request_id)

        content = frame.payload.get("content", "")
        if not content.strip():
            return self._make_frame("error", {
                "code": "invalid_request",
                "message": "user.message payload.content must be non-empty.",
            }, frame.request_id)

        encoder = self._encoder

        try:
            async for event in self._engine.run_turn(content):
                event_type = event.get("type", "")
                event_data = event.get("data", {})

                if event_type == "turn_started":
                    f = encoder.encode_turn_started(
                        event_data.get("turn", 0), request_id=frame.request_id
                    )
                elif event_type == "turn_finished":
                    f = encoder.encode_turn_finished(
                        event_data.get("turn", 0), request_id=frame.request_id
                    )
                elif event_type == "assistant_message":
                    f = encoder.encode_assistant_message(
                        event_data.get("content", ""),
                        event_data.get("tool_calls"),
                        request_id=frame.request_id,
                    )
                elif event_type == "tool_calls_started":
                    f = encoder.encode_tool_calls_started(
                        event_data.get("tool_calls", []), request_id=frame.request_id
                    )
                elif event_type == "tool_result":
                    f = encoder.encode_tool_result(
                        event_data.get("tool_call_id", ""),
                        event_data.get("content", ""),
                        event_data.get("status", "success"),
                        request_id=frame.request_id,
                    )
                else:
                    f = encoder.encode(event_type, event_data, request_id=frame.request_id)

                writer.write(f.to_json_line().encode("utf-8"))
                await writer.drain()

        except Exception as exc:
            logger.exception("Turn failed")
            return encoder.encode_error(str(exc), request_id=frame.request_id)

        return None

    def _handle_shutdown(self, frame: ProtocolFrame) -> ProtocolFrame | None:
        if self._encoder:
            return self._encoder.encode_shutdown_ok(request_id=frame.request_id)
        return self._make_frame("shutdown_ok", {}, frame.request_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_frame(
        self, event_type: str, payload: dict[str, Any], request_id: str = ""
    ) -> ProtocolFrame:
        return ProtocolFrame(
            seq=0,
            direction="server_to_client",
            type=event_type,
            session_id=self._session_id,
            thread_id=self._thread_id,
            request_id=request_id,
            payload=payload,
        )


async def run_stdio_server(
    data_dir: Path | str = "data",
    personality_id: str = "default",
    provider_name: str = "default",
) -> None:
    """Entry point for the JSONL stdio server."""
    server = RuntimeServer(
        data_dir=data_dir,
        personality_id=personality_id,
        provider_name=provider_name,
    )
    await server.run()
