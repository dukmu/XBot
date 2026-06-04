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
    Commands: hello, session.open, user.message, user.input,
    permission.response, shutdown.
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
            frame, response = await self._read_client_frame(reader)
            if frame is None:
                if response:
                    writer.write(response.to_json_line().encode("utf-8"))
                    await writer.drain()
                    continue
                break

            response = await self._dispatch(frame, reader, writer)

            if response:
                writer.write(response.to_json_line().encode("utf-8"))
                await writer.drain()

            if frame.type == "shutdown":
                break

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        frame: ProtocolFrame,
        reader: Any,
        writer: Any,
    ) -> ProtocolFrame | None:
        cmd = frame.type

        if cmd == "hello":
            return self._handle_hello(frame)

        if cmd == "session.open":
            return await self._handle_session_open(frame, writer)

        if cmd == "user.message":
            return await self._handle_user_message(frame, reader, writer)

        if cmd == "user.input":
            return await self._handle_user_input(frame)

        if cmd == "permission.response":
            return await self._handle_permission_response(frame)

        if cmd == "shutdown":
            return await self._handle_shutdown(frame)

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
        self, frame: ProtocolFrame, reader: Any, writer: Any
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
        previous_sink = self._engine.set_client_event_sink(
            self._make_live_user_input_sink(
                reader=reader,
                writer=writer,
                turn_request_id=frame.request_id,
            )
        )

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
        finally:
            self._engine.set_client_event_sink(previous_sink)

        return None

    async def _handle_user_input(self, frame: ProtocolFrame) -> ProtocolFrame | None:
        """Record a standalone answer for a materialized ask_user request."""
        if self._engine is None:
            return self._make_frame("error", {
                "code": "no_session",
                "message": "No active session. Send session.open first.",
            }, frame.request_id)

        request_id = str(frame.payload.get("request_id", "")).strip()
        if not request_id:
            return self._make_frame("error", {
                "code": "invalid_request",
                "message": "user.input payload.request_id must be non-empty.",
            }, frame.request_id)

        answer = frame.payload.get("answer", "")
        store = self._engine.state_store
        pending = _find_pending_interaction(
            store.read_state().get("pending_interactions", []),
            request_id,
            "user_input_required",
        )
        if pending is None:
            return self._make_frame("error", {
                "code": "invalid_request",
                "message": f"No pending user input request: {request_id}",
            }, frame.request_id)

        store.append_event("user_input_response", {
            "request_id": request_id,
            "answer": answer,
            **_pending_response_context(pending),
        })
        state = store.materialize()

        encoder = self._encoder
        if encoder:
            return encoder.encode(
                "user_input_recorded",
                {
                    "request_id": request_id,
                    "resume_supported": False,
                    "pending_interactions": state.get("pending_interactions", []),
                },
                request_id=frame.request_id,
            )
        return self._make_frame("user_input_recorded", {
            "request_id": request_id,
            "resume_supported": False,
            "pending_interactions": state.get("pending_interactions", []),
        }, frame.request_id)

    async def _handle_permission_response(self, frame: ProtocolFrame) -> ProtocolFrame | None:
        """Record a client approval/denial for a pending permission request."""
        if self._engine is None:
            return self._make_frame("error", {
                "code": "no_session",
                "message": "No active session. Send session.open first.",
            }, frame.request_id)

        request_id = str(frame.payload.get("request_id", "")).strip()
        decision = str(frame.payload.get("decision", "")).strip().lower()
        if not request_id:
            return self._make_frame("error", {
                "code": "invalid_request",
                "message": "permission.response payload.request_id must be non-empty.",
            }, frame.request_id)
        if decision not in {"allow", "deny"}:
            return self._make_frame("error", {
                "code": "invalid_request",
                "message": "permission.response payload.decision must be allow or deny.",
            }, frame.request_id)

        store = self._engine.state_store
        pending = _find_pending_interaction(
            store.read_state().get("pending_interactions", []),
            request_id,
            "permission_request",
        )
        if pending is None:
            return self._make_frame("error", {
                "code": "invalid_request",
                "message": f"No pending permission request: {request_id}",
            }, frame.request_id)

        store.append_event("permission_response", {
            "request_id": request_id,
            "decision": decision,
            **_pending_response_context(pending),
        })
        state = store.materialize()

        encoder = self._encoder
        if encoder:
            return encoder.encode(
                "permission_response_recorded",
                {
                    "request_id": request_id,
                    "decision": decision,
                    "resume_supported": False,
                    "pending_interactions": state.get("pending_interactions", []),
                },
                request_id=frame.request_id,
            )
        return self._make_frame("permission_response_recorded", {
            "request_id": request_id,
            "decision": decision,
            "resume_supported": False,
            "pending_interactions": state.get("pending_interactions", []),
        }, frame.request_id)

    async def _handle_shutdown(self, frame: ProtocolFrame) -> ProtocolFrame | None:
        if self._engine is not None:
            try:
                await self._engine.close_session()
            except Exception as exc:
                logger.exception("Session close failed")
                encoder = self._encoder
                if encoder:
                    return encoder.encode_error(
                        str(exc),
                        code="session_close_failed",
                        request_id=frame.request_id,
                    )
                return self._make_frame(
                    "error",
                    {"code": "session_close_failed", "message": str(exc)},
                    frame.request_id,
                )

        if self._encoder:
            return self._encoder.encode_shutdown_ok(request_id=frame.request_id)
        return self._make_frame("shutdown_ok", {}, frame.request_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _read_client_frame(
        self,
        reader: Any,
    ) -> tuple[ProtocolFrame | None, ProtocolFrame | None]:
        try:
            line = await reader.readline()
        except (EOFError, ConnectionError):
            return None, None

        if not line:
            return None, None

        try:
            text = line.decode("utf-8").strip()
            if not text:
                return None, self._make_frame("error", {
                    "code": "invalid_frame",
                    "message": "Empty protocol frame.",
                })
            return frame_from_json(text), None
        except Exception as exc:
            logger.error("Failed to parse frame: %s", exc)
            return None, self._make_frame("error", {
                "code": "invalid_frame",
                "message": f"Invalid protocol frame: {exc}",
            })

    async def _write_frame(self, writer: Any, frame: ProtocolFrame) -> None:
        writer.write(frame.to_json_line().encode("utf-8"))
        await writer.drain()

    def _make_live_user_input_sink(
        self,
        *,
        reader: Any,
        writer: Any,
        turn_request_id: str,
    ) -> Any:
        async def sink(
            client_event: dict[str, Any],
            *,
            timeout_seconds: float | None = None,
            tool_call_id: str = "",
        ) -> dict[str, Any]:
            del tool_call_id
            encoder = self._encoder
            event_type = str(client_event.get("type") or "client_event")
            event_data = client_event.get("data") or {}
            request_id = str(event_data.get("request_id") or "")
            try:
                await self._write_frame(
                    writer,
                    encoder.encode(event_type, event_data, request_id=turn_request_id),
                )
            except (BrokenPipeError, ConnectionError, RuntimeError):
                return {
                    "request_id": request_id,
                    "status": "disconnected",
                    "reason": "client_disconnected",
                }

            response = await self._wait_for_live_user_input(
                reader=reader,
                writer=writer,
                request_id=request_id,
                client_event=client_event,
                timeout_seconds=timeout_seconds,
            )
            state = self._engine.record_user_input_result(client_event, response)
            if response["status"] != "disconnected":
                await self._write_frame(
                    writer,
                    encoder.encode(
                        "user_input_recorded",
                        {
                            "request_id": request_id,
                            "status": response["status"],
                            "resume_supported": True,
                            "pending_interactions": state.get("pending_interactions", []),
                        },
                        request_id=turn_request_id,
                    ),
                )
            response["persisted"] = True
            return response

        return sink

    async def _wait_for_live_user_input(
        self,
        *,
        reader: Any,
        writer: Any,
        request_id: str,
        client_event: dict[str, Any],
        timeout_seconds: float | None,
    ) -> dict[str, Any]:
        while True:
            try:
                frame, error = await asyncio.wait_for(
                    self._read_client_frame(reader),
                    timeout=None if timeout_seconds is None else float(timeout_seconds),
                )
            except asyncio.TimeoutError:
                return {
                    "request_id": request_id,
                    "status": "timeout",
                    "reason": "timeout",
                }
            if frame is None:
                if error is not None:
                    await self._write_frame(writer, error)
                    continue
                return {
                    "request_id": request_id,
                    "status": "disconnected",
                    "reason": "client_disconnected",
                }

            if frame.type == "shutdown":
                return {
                    "request_id": request_id,
                    "status": "disconnected",
                    "reason": "shutdown",
                }

            if frame.type != "user.input":
                await self._write_frame(
                    writer,
                    self._make_frame(
                        "error",
                        {
                            "code": "interaction_pending",
                            "message": (
                                "A user.input response is required before other "
                                "commands can be processed."
                            ),
                            "request_id": request_id,
                        },
                        frame.request_id,
                    ),
                )
                continue

            payload_request_id = str(frame.payload.get("request_id", "")).strip()
            if payload_request_id != request_id:
                await self._write_frame(
                    writer,
                    self._make_frame(
                        "error",
                        {
                            "code": "invalid_request",
                            "message": f"No pending user input request: {payload_request_id}",
                            "expected_request_id": request_id,
                        },
                        frame.request_id,
                    ),
                )
                continue

            return {
                "request_id": request_id,
                "status": "answered",
                "answer": frame.payload.get("answer", ""),
            }

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


def _find_pending_interaction(
    pending_interactions: list[dict[str, Any]],
    request_id: str,
    expected_type: str,
) -> dict[str, Any] | None:
    for interaction in pending_interactions:
        if (
            interaction.get("request_id") == request_id
            and interaction.get("type") == expected_type
        ):
            return interaction
    return None


def _pending_response_context(pending: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_type": pending.get("type", ""),
        "request_source": pending.get("source", ""),
        "request_payload": pending.get("payload", {}),
        "request_event_id": pending.get("event_id"),
        "request_ts": pending.get("ts", ""),
    }


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
