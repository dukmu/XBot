"""Session facade for the TUI over a ``Transport``.

The TUI calls ``TerminalSession`` for all server interaction. v1 ships
only ``HttpTransport``; the transport can be injected for testing.

This module replaces the historical stdio ``ProtocolClient`` with a
``Transport``-based implementation. The stdio path is removed in v1
per the design document §10.5.2.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

from xbotv2.tui.transport import Transport
from xbotv2.tui.transport_http import HttpTransport


@dataclass(frozen=True)
class CommandOutcome:
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] | None = None


def _new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class TerminalSession:
    """High-level session over a ``Transport``.

    Lifecycle::

        session = TerminalSession(base_url="http://127.0.0.1:4096")
        await session.connect()
        async for event in session.send_message("hi"):
            ...
        await session.disconnect()
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
        transport: Transport | None = None,
        token: str | None = None,
        uds_path: str | None = None,
    ) -> None:
        self._session_id = session_id or _new_session_id()
        self._session_mode = session_mode or "new"
        self._thread_id = thread_id
        self._agent = agent
        self._workspace_root = str(Path(workspace_root or Path.cwd()).resolve())
        self._transport: Transport = transport or HttpTransport(
            base_url, token=token, uds_path=uds_path
        )
        self._connected = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def transport(self) -> Transport:
        return self._transport

    async def connect(self) -> dict[str, Any] | None:
        """Perform hello + open_session."""

        if self._connected:
            return None
        hello = await self._transport.hello(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        server_session = str(hello.get("session_id") or self._session_id)
        server_thread = str(hello.get("thread_id") or self._thread_id)
        self._session_id = server_session
        self._thread_id = server_thread
        open_kwargs = dict(
            session_id=self._session_id,
            thread_id=self._thread_id,
            workspace_root=self._workspace_root,
            mode=self._session_mode,
        )
        if self._agent:
            open_kwargs["agent"] = self._agent
        session = await self._transport.open_session(**open_kwargs)
        self._connected = True
        return session

    async def list_commands(self) -> dict[str, Any]:
        return await self._transport.list_commands(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )

    async def run_command(
        self,
        command: str,
        args: list[str],
        raw: str,
        *,
        kind: str = "server",
    ) -> dict[str, Any]:
        return await self._transport.run_command(
            session_id=self._session_id,
            thread_id=self._thread_id,
            command=command,
            args=args,
            raw=raw,
            kind=kind,
        )

    async def run_builtin_command(
        self, command: str, args: list[str]
    ) -> CommandOutcome:
        """Execute a human-facing built-in through typed resource operations."""
        if command == "status" and not args:
            data = await self._thread_status()
            return CommandOutcome(
                " ".join(
                    f"{key}={data[key]}"
                    for key in ("session_id", "thread_id", "provider", "model")
                ),
                data,
            )
        if command == "provider":
            return await self._provider_command(args)
        if command == "agent":
            return await self._agent_command(args)
        if command == "clear" and not args:
            data = await self._transport.clear_history(
                session_id=self._session_id,
                thread_id=self._thread_id,
            )
            return CommandOutcome(
                f"Cleared {data['removed_turns']} conversation turns.",
                data,
                data["messages"],
            )
        if command == "undo" and len(args) <= 1:
            try:
                count = int(args[0]) if args else 1
            except ValueError as exc:
                raise ValueError("Undo count must be a positive integer.") from exc
            data = await self._transport.undo_history(
                session_id=self._session_id,
                thread_id=self._thread_id,
                count=count,
            )
            return CommandOutcome(
                f"Removed {data['removed_turns']} conversation turn(s).",
                data,
                data["messages"],
            )
        if command == "fork" and not args:
            data = await self._transport.fork_session(session_id=self._session_id)
            return CommandOutcome(f"Forked session to {data['session_id']}.", data)
        if command == "tasks" and args in ([], ["ps"]):
            data = await self._transport.list_tasks(
                session_id=self._session_id,
                thread_id=self._thread_id,
            )
            tasks = data["tasks"]
            message = "No background tasks." if not tasks else "\n".join(
                f"{task['kind']}  {task['task_id']}  {task['status']}  {task['command']}"
                for task in tasks
            )
            return CommandOutcome(message, data)
        if command == "task":
            return await self._task_command(args)
        if command in {"permission", "sandbox"}:
            return await self._policy_command(command, args)
        raise ValueError(f"Usage: /{command}")

    async def _thread_status(self) -> dict[str, Any]:
        data = await self._transport.get_thread(
            session_id=self._session_id,
            thread_id=self._thread_id,
        )
        data["workspace_root"] = self._workspace_root
        return data

    async def _provider_command(self, args: list[str]) -> CommandOutcome:
        action = args[0].lower() if args else "status"
        if action == "status" and len(args) <= 1:
            data = await self._thread_status()
            return CommandOutcome(
                f"Provider: {data['provider']} ({data['model']})", data
            )
        if action == "list" and len(args) == 1:
            data = await self._transport.list_providers()
            current = (await self._thread_status())["provider"]
            data["current"] = current
            return CommandOutcome(
                "Providers: " + ", ".join(
                    f"{item['name']}{' (current)' if item['name'] == current else ''}"
                    for item in data["providers"]
                ),
                data,
            )
        if action == "use" and len(args) == 2:
            data = await self._transport.select_provider(
                session_id=self._session_id,
                thread_id=self._thread_id,
                name=args[1],
            )
            return CommandOutcome(
                f"Provider switched to {data['provider']} ({data['model']}).", data
            )
        raise ValueError("Usage: /provider [status|list|use <name>]")

    async def _agent_command(self, args: list[str]) -> CommandOutcome:
        action = args[0].lower() if args else "status"
        if action in {"status", "list"} and len(args) <= 1:
            data = await self._transport.list_agents(
                session_id=self._session_id,
                thread_id=self._thread_id,
            )
            lines = [f"Active Agent: {data['active']}"]
            if action == "list":
                lines.extend(
                    f"{item['name']}  {item['mode']}  {item['description']}"
                    for item in data["agents"]
                )
            return CommandOutcome("\n".join(lines), data)
        target = args[1] if action == "use" and len(args) == 2 else None
        if len(args) == 1 and action not in {"status", "list", "use"}:
            target = args[0]
        if target is None:
            raise ValueError("Usage: /agent [status|list|use <name>|<name>]")
        data = await self._transport.select_agent(
            session_id=self._session_id,
            thread_id=self._thread_id,
            name=target,
        )
        data["agent_name"] = data["agent"]
        return CommandOutcome(f"Active Agent: {data['agent']}.", data)

    async def _task_command(self, args: list[str]) -> CommandOutcome:
        if len(args) == 2 and args[0] == "stop":
            data = await self._transport.stop_task(
                session_id=self._session_id,
                thread_id=self._thread_id,
                task_id=args[1],
            )
            return CommandOutcome(f"Stopped background task {args[1]}.", data)
        if args == ["stopall"]:
            data = await self._transport.stop_all_tasks(
                session_id=self._session_id,
                thread_id=self._thread_id,
            )
            return CommandOutcome(
                f"Stopped {data['matched_count']} background task(s).", data
            )
        raise ValueError("Usage: /task stop <id> | /task stopall")

    async def _policy_command(
        self, command: str, args: list[str]
    ) -> CommandOutcome:
        action = args[0].lower() if args else "status"
        if action in {"status", "list"} and len(args) <= 1:
            data = await self._transport.get_session_policy(
                session_id=self._session_id
            )
            section = "permissions" if command == "permission" else "sandbox"
            return CommandOutcome(f"Session {command} policy: {data[section]}", data)
        if action == "set" and len(args) == 3:
            key, value = args[1], args[2].lower()
            kwargs: dict[str, Any]
            if command == "permission":
                if value not in {"allow", "deny", "ask"}:
                    raise ValueError("Permission value must be allow, deny, or ask.")
                kwargs = {"permissions": {key: value}}
            else:
                kwargs = {"sandbox": {key: _sandbox_value(key, value)}}
            data = await self._transport.update_session_policy(
                session_id=self._session_id, **kwargs
            )
            return CommandOutcome(f"{command} policy set: {key}={value}", data)
        if action == "reset" and len(args) <= 2:
            if command == "permission":
                if len(args) != 2:
                    raise ValueError("Usage: /permission reset <tool>")
                kwargs = {"remove_permissions": [args[1]]}
            else:
                keys = [args[1]] if len(args) == 2 else [
                    "enabled",
                    "network",
                    "external_read",
                    "external_write",
                    "workspace_read",
                    "workspace_write",
                ]
                kwargs = {"remove_sandbox": keys}
            data = await self._transport.update_session_policy(
                session_id=self._session_id, **kwargs
            )
            return CommandOutcome(f"{command} session policy reset.", data)
        raise ValueError(
            f"Usage: /{command} [status|set <key> <value>|reset [key]]"
        )

    async def disconnect(self) -> None:
        """Best-effort session shutdown + transport close."""

        if not self._connected:
            await self._transport.close()
            return
        try:
            await self._transport.shutdown(session_id=self._session_id)
        except Exception:
            pass
        self._connected = False
        await self._transport.close()

    async def __aenter__(self) -> "TerminalSession":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.disconnect()

    async def send_message(
        self,
        content: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send one user message and yield every non-transport SSE event."""

        request_id = f"tui-{self._session_id}-{secrets.token_hex(8)}"
        stream = self._transport.send_message(
            session_id=self._session_id,
            thread_id=self._thread_id,
            content=content,
            request_id=request_id,
        )
        async for event in stream:
            event_type = str(event.get("type") or "")
            if event_type == "end":
                return
            yield event

    async def session_events(self) -> AsyncIterator[dict[str, Any]]:
        """Yield turns initiated by runtime general messages."""

        async for event in self._transport.session_events(
            session_id=self._session_id,
            thread_id=self._thread_id,
        ):
            if str(event.get("type") or "") != "end":
                yield event

    async def submit_user_input(self, request_id: str, answer: Any) -> dict[str, Any]:
        return await self._transport.send_user_input(
            session_id=self._session_id,
            thread_id=self._thread_id,
            request_id=request_id,
            answer=answer,
        )

    async def respond_permission(
        self,
        request_id: str,
        decision: str,
        *,
        scope: str = "once",
    ) -> dict[str, Any]:
        return await self._transport.send_permission_response(
            session_id=self._session_id,
            thread_id=self._thread_id,
            request_id=request_id,
            decision=decision,
            scope=scope,
        )


def _sandbox_value(key: str, value: str) -> bool | str:
    if key in {"enabled", "network"}:
        if value in {"true", "yes", "1"}:
            return True
        if value in {"false", "no", "0"}:
            return False
        raise ValueError(f"sandbox.{key} must be true or false")
    if key not in {
        "external_read", "external_write", "workspace_read", "workspace_write"
    } or value not in {"allow", "deny", "ask", "readonly", "readwrite"}:
        raise ValueError(f"Invalid value {value!r} for sandbox.{key}")
    return value
