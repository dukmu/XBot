"""Transport protocol abstracting how the TUI talks to the server.

The TUI does not know whether the server is in-process (loopback) or
remote (SSH tunnel); it only knows the ``Transport`` interface.

See ``docsv2/tui_opencode_requirements.md`` §10.5.5.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """Abstract TUI-to-server transport.

    All methods are coroutines except ``send_message`` which is an
    async iterator over the server's SSE event stream (one event per
    ``data:`` line; the iterator ends when the server closes the
    stream or emits an ``end`` event).
    """

    async def hello(
        self,
        *,
        session_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        """Handshake with the server. Returns the server's greeting."""

    async def open_session(
        self,
        *,
        session_id: str,
        thread_id: str,
        workspace_root: str,
        mode: str = "new",
    ) -> dict[str, Any]:
        """Open or resume a session. Returns the agent_name and status."""

    async def list_commands(self) -> dict[str, Any]:
        """Return server command metadata for completion/help."""

    async def run_command(
        self,
        *,
        session_id: str,
        command: str,
        args: list[str],
        raw: str,
    ) -> dict[str, Any]:
        """Execute a server-side slash command."""

    def send_message(
        self,
        *,
        session_id: str,
        content: str,
        request_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a user message; yield every server event for this turn.

        Each yielded dict has the shape ``{"type": str, "data": dict}``.
        The iterator ends after the server emits ``turn_finished`` or an
        ``end`` sentinel. Callers should ``aclose()`` the iterator when
        they're done.
        """

    async def send_permission_response(
        self,
        *,
        session_id: str,
        request_id: str,
        decision: str,
        scope: str,
    ) -> dict[str, Any]:
        """Resolve a live ``permission_request`` with allow/deny."""

    async def send_user_input(
        self,
        *,
        session_id: str,
        request_id: str,
        answer: Any,
    ) -> dict[str, Any]:
        """Resolve a live ``user_input_required`` with the user's answer."""

    async def shutdown(self, *, session_id: str) -> dict[str, Any]:
        """Close a session."""

    async def interrupt(self, *, session_id: str) -> dict[str, Any]:
        """Cancel the running turn in ``session_id`` if any.

        Returns a status dict (``{"status": "interrupting", ...}``).
        The in-flight ``send_message`` async iterator will close on
        the next event boundary; the client should consume the
        remaining events and treat the stream end as a turn_finished.
        """

    async def close(self) -> None:
        """Release any resources held by the transport."""
