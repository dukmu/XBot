"""Session facade for the TUI over the public HTTP client."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from pydantic import JsonValue, TypeAdapter

from XBotv2.client import XBotClient, thread_path
from XBotv2.commands import CommandListResponse, CommandRequest, CommandResponse
from XBotv2.protocol import ServerEvent, WireModel
from XBotv2.session.protocol import SessionMode
from XBotv2.session.contracts import new_session_id
from XBotv2.tui.trace import trace_event


class TerminalSession:
    """High-level session over :class:`XBotClient`.

    Lifecycle::

        session = TerminalSession(base_url="http://127.0.0.1:4096")
        await session.connect()
        events = asyncio.create_task(consume(session.session_events()))
        await session.send_message("hi")
        await session.disconnect()

    ``session_events`` is the authoritative resumable event channel for the
    built-in TUI. Commands return after the server accepts them; output is
    delivered only through that event channel.
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        thread_id: str = "agent",
        agent: str | None = None,
        workspace_root: Path | str | None = None,
        session_mode: str | None = None,
        base_url: str = "http://127.0.0.1:4096",
        client: XBotClient | None = None,
        token: str | None = None,
        uds_path: str | None = None,
    ) -> None:
        self._session_id = session_id or new_session_id()
        self._session_mode = session_mode or "new"
        self._thread_id = thread_id
        self._agent = agent
        self._workspace_root = (
            None
            if self._session_mode == "resume" and workspace_root is None
            else str(Path(workspace_root or Path.cwd()).resolve())
        )
        headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = client or XBotClient(
            base_url, uds_path=uds_path, headers=headers
        )
        self._session_attached = False
        self._event_cursor = 0
        # Cursor for the read-only thread view stream; independent of the
        # attached thread's own cursor.
        self._view_cursor = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def client(self) -> XBotClient:
        return self._client

    async def connect(self) -> dict[str, JsonValue] | None:
        """Perform hello + open_session."""

        if self._session_attached:
            return None
        hello = await self._client.hello(
            client_name="xbotv2-tui",
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        server_session = hello.session_id or self._session_id
        server_thread = hello.thread_id or self._thread_id
        open_kwargs = dict(
            session_id=server_session,
            thread_id=server_thread,
            workspace_root=self._workspace_root,
            mode=self._session_mode,
        )
        if self._agent:
            open_kwargs["agent"] = self._agent
        session = await self._client.open_session(**open_kwargs)
        self._session_id = server_session
        self._thread_id = server_thread
        self._session_attached = True
        self._event_cursor = session.event_cursor
        return _dump(session)

    async def refresh_descriptor(self) -> dict[str, JsonValue] | None:
        """Re-read the open session descriptor (title/agent/provider).

        Same protocol call the WebUI uses to reconcile a live session; the
        descriptor is the single source of a session's identity fields, so a
        title written mid-session (caption) becomes visible without inventing
        a client-only event.
        """
        if not self._session_attached:
            return None
        session = await self._client.open_session(
            session_id=self._session_id,
            thread_id=self._thread_id,
            workspace_root=self._workspace_root,
            mode="resume",
        )
        self._event_cursor = session.event_cursor
        return _dump(session)

    async def list_commands(self) -> dict[str, JsonValue]:
        return _dump(await self._client._request(
            "GET", f"{self.thread_path}/commands", CommandListResponse
        ))

    async def list_sessions(self) -> dict[str, JsonValue]:
        return _dump(await self._client.list_sessions())

    async def list_workspaces(self) -> dict[str, JsonValue]:
        return _dump(await self._client.list_workspaces())

    async def stream_catalog_events(
        self,
        *,
        after: int | None = None,
    ) -> AsyncIterator[dict[str, JsonValue]]:
        """Yield process catalog changes (session title/workspace updates)."""
        stream = self._client.stream_workspace_events(after=after)
        async for event in self._events(stream, "catalog_events"):
            yield event

    async def switch(
        self,
        *,
        session_id: str | None,
        thread_id: str,
        workspace_root: str | None = None,
        mode: SessionMode = "resume",
    ) -> dict[str, JsonValue] | None:
        """Attach to another session/thread without destroying the current runtime.

        The HTTP transport remains open so switching does not invalidate the
        client connection. Persisted sessions retain their recorded workspace
        when ``workspace_root`` is omitted; ``new`` sessions use the supplied
        workspace or the current working directory.
        """
        target_session = session_id or new_session_id()
        target_workspace = (
            None
            if mode == "resume" and workspace_root is None
            else str(Path(workspace_root or Path.cwd()).resolve())
        )
        opened = await self._client.open_session(
            session_id=target_session,
            thread_id=thread_id,
            workspace_root=target_workspace,
            mode=mode,
            **({"agent": self._agent} if self._agent else {}),
        )
        self._session_id = target_session
        self._thread_id = thread_id
        self._workspace_root = target_workspace
        self._session_mode = mode
        self._session_attached = True
        self._event_cursor = opened.event_cursor
        return _dump(opened)

    async def run_command(
        self,
        command: str,
        args: list[str],
        raw: str,
        *,
        kind: Literal["server", "prompt"] = "server",
    ) -> dict[str, JsonValue]:
        return _dump(await self._client._request(
            "POST",
            f"{self.thread_path}/commands",
            CommandResponse,
            CommandRequest(command=command, args=args, raw=raw, kind=kind),
        ))

    async def disconnect(self) -> None:
        """Detach this client and close its transport without destroying a session."""

        self._session_attached = False
        await self._client.close()

    async def __aenter__(self) -> "TerminalSession":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    async def send_message(
        self,
        content: str,
        *,
        images: list[dict[str, str]] | None = None,
    ) -> None:
        """Submit input; output is delivered by :meth:`session_events`."""

        request_id = f"tui-{self._session_id}-{secrets.token_hex(8)}"
        trace_event("tui.http", {
            "stage": "messages.request",
            "request_id": request_id,
            "content": content,
        })
        await self._client.send_message(
            self._session_id,
            self._thread_id,
            content,
            request_id=request_id,
            images=images,
        )

    def rewind_event_cursor(self, sequence: int) -> None:
        """Resume the shared event stream from a server-provided recovery point."""
        self._event_cursor = max(0, sequence)

    async def refresh_baseline(self) -> dict[str, JsonValue] | None:
        """Re-open this session and adopt the snapshot as the new baseline.

        An evicted cursor means the client can no longer describe a contiguous
        stream. Re-opening reuses the live session runtime and returns the
        current event cursor, history, pending inputs, and unanswered
        interactions, so the client can resynchronize instead of failing.
        """
        if not self._session_attached:
            return None
        open_kwargs: dict[str, Any] = dict(
            session_id=self._session_id,
            thread_id=self._thread_id,
            workspace_root=self._workspace_root,
            mode="resume",
        )
        if self._agent:
            open_kwargs["agent"] = self._agent
        opened = await self._client.open_session(**open_kwargs)
        self._event_cursor = opened.event_cursor
        return _dump(opened)

    async def session_events(self) -> AsyncIterator[dict[str, JsonValue]]:
        """Yield turns initiated by runtime general messages."""

        stream = self._client.stream_events(
            self._session_id,
            self._thread_id,
            after=self._event_cursor,
        )
        async for event in self._events(stream, "session_events"):
            self._event_cursor = max(
                self._event_cursor,
                int(event.get("sequence") or 0),
            )
            yield event

    async def list_threads(self, session_id: str | None = None) -> dict[str, JsonValue]:
        return _dump(await self._client.list_threads(session_id or self._session_id))

    async def list_providers(self) -> dict[str, JsonValue]:
        return _dump(await self._client.list_providers())

    def stream_thread_events(
        self,
        thread_id: str,
    ) -> AsyncIterator[dict[str, JsonValue]]:
        """Live frames of another thread of this session, without claiming it."""
        stream = self._client.stream_events(
            self._session_id,
            thread_id,
            after=self._view_cursor,
        )

        async def _frames():
            async for event in self._events(stream, "thread_view"):
                self._view_cursor = max(
                    self._view_cursor,
                    int(event.get("sequence") or 0),
                )
                yield event

        return _frames()

    async def read_thread_compactions(
        self,
        thread_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, JsonValue]]:
        """Durable compaction summaries of a thread (read-only, lock-free).

        Returns one record per compaction in trajectory order with at least
        ``position`` and ``summary``, so a resumed client can rebuild the
        expandable transcript entries the live stream showed earlier.
        """
        response = await self._client.list_trajectory(
            self._session_id,
            thread_id,
            cursor=None,
            limit=limit,
        )
        payload = _dump(response)
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        compactions: list[dict[str, JsonValue]] = []
        for item in items:
            if not isinstance(item, dict) or item.get("kind") != "surface_replace":
                continue
            summary = str(item.get("summary") or "")
            if not summary:
                continue
            compactions.append({
                "position": int(item.get("position") or 0),
                "summary": summary,
            })
        return compactions

    async def read_thread_trajectory(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int = 200,
    ) -> tuple[list[dict[str, JsonValue]], str | None]:
        """Durable trajectory records for a read-only transcript view.

        Unlike :meth:`read_thread_history`, this preserves compaction and
        event records so the viewed thread renders the same transcript shape
        as the main client surface. The cursor still pages older records.
        """
        response = await self._client.list_trajectory(
            self._session_id,
            thread_id,
            cursor=cursor,
            limit=limit,
        )
        payload = _dump(response)
        items = payload.get("items")
        records = [
            dict(item) for item in items if isinstance(item, dict)
        ] if isinstance(items, list) else []
        next_cursor = payload.get("next_cursor")
        return records, str(next_cursor) if next_cursor else None

    async def read_thread_history(
        self,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int = 200,
    ) -> tuple[list[dict[str, JsonValue]], str | None]:
        """Persisted message records of a thread (read-only, lock-free).

        Returns the message records and the cursor for older pages, so a long
        subagent thread can be paged back lazily.
        """
        records, next_cursor = await self.read_thread_trajectory(
            thread_id,
            cursor=cursor,
            limit=limit,
        )
        return (
            [item for item in records if item.get("kind") == "message"],
            next_cursor,
        )

    async def submit_user_input(self, request_id: str, answer: JsonValue) -> dict[str, JsonValue]:
        return _dump(
            await self._client.respond_user_input(
                self._session_id,
                self._thread_id,
                request_id=request_id,
                answer=answer,
            )
        )

    async def respond_permission(
        self,
        request_id: str,
        decision: Literal["allow", "deny"],
        *,
        scope: Literal["once", "session"] = "once",
    ) -> dict[str, JsonValue]:
        return _dump(
            await self._client.respond_permission(
                self._session_id,
                self._thread_id,
                request_id=request_id,
                decision=decision,
                scope=scope,
            )
        )

    async def interrupt(self) -> dict[str, JsonValue]:
        return _dump(await self._client.interrupt(self._session_id, self._thread_id))

    @property
    def thread_path(self) -> str:
        return thread_path(self._session_id, self._thread_id)

    async def _events(
        self,
        stream: AsyncIterator[ServerEvent],
        label: str,
        body: dict[str, JsonValue] | None = None,
    ) -> AsyncIterator[dict[str, JsonValue]]:
        trace_event("tui.http", {"stage": f"{label}.request", "body": body})
        async for event in stream:
            trace_event(
                "tui.http",
                {
                    "stage": f"{label}.event",
                    "event": event.type,
                    "id": event.sequence,
                },
            )
            if event.type == "end":
                return
            yield _json_payload(event)


def _dump(model: WireModel) -> dict[str, JsonValue]:
    payload = _json_payload(model)
    trace_event("tui.http", {"status": 200, "payload": payload})
    return payload


# The wire->JSON normalization contract is static, so the validating adapter is
# built once. Constructing a ``TypeAdapter`` per event recompiles a core schema
# (~950us measured) and caps the client at roughly 1000 events/s. A streaming
# turn publishes far faster than that, the client falls behind, and the backlog
# overflows the server's bounded replay window -- surfacing to the user as
# ``session_event_cursor_expired``.
_JSON_PAYLOAD_ADAPTER = TypeAdapter(dict[str, JsonValue])


def _json_payload(model: WireModel) -> dict[str, JsonValue]:
    """Normalize a wire model through the JSON contract at the client edge."""
    return _JSON_PAYLOAD_ADAPTER.validate_python(
        model.model_dump(mode="json")
    )
