"""Human session commands and their typed service binding factory."""

from __future__ import annotations

from collections.abc import Callable

from XBotv2.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)
from XBotv2.session.contracts import SessionPort


def build_session_commands(
    session: SessionPort,
    *,
    pending_input_count: Callable[[], int],
) -> tuple[Command, ...]:
    async def status_command(raw_args: str) -> CommandResult:
        if raw_args.strip():
            return command_usage("/status")
        # The engine is the single owner of pending input state; the command
        # asks it live instead of projecting a frozen snapshot.
        status = session.status(pending_input_count=pending_input_count())
        return CommandResult("\n".join((
            "Session",
            f"  Title: {status.title}",
            f"  ID: {status.session_id}",
            f"  Thread: {status.thread_id}",
            f"  Workspace: {status.workspace_root}",
            f"  Agent: {status.agent or 'default'}",
            "Runtime",
            f"  State: {status.status} ({'resumed' if status.resumed else 'new'})",
            f"  History: {status.turn_count} turns, {status.message_count} messages",
            f"  Queued inputs: {status.pending_inputs}",
            "Model",
            f"  Provider: {status.provider}",
            f"  Model: {status.model or 'default'}",
            f"  Mode: {status.model_mode or 'default'}",
            f"  Context window: {status.context_window or 'provider default'}",
        )))

    async def clear_command(raw_args: str) -> CommandResult:
        if raw_args.strip():
            return command_usage("/clear")
        removed = await session.clear_history()
        return CommandResult(
            f"Cleared {removed} conversation turns.",
            effects=("history", "thread", "sessions"),
        )

    async def undo_command(raw_args: str) -> CommandResult:
        parts = split_command_args(raw_args)
        if len(parts) > 1:
            return command_usage("/undo [count]")
        try:
            count = int(parts[0]) if parts else 1
        except ValueError:
            return CommandResult("Undo count must be a positive integer.", status="error")
        if count < 1:
            return CommandResult("Undo count must be a positive integer.", status="error")
        await session.undo_history(count)
        return CommandResult(
            f"Removed {count} conversation turn(s).",
            effects=("history", "thread", "sessions"),
        )

    async def fork_command(raw_args: str) -> CommandResult:
        if raw_args.strip():
            return command_usage("/fork")
        session_id = await session.fork()
        return CommandResult(
            f"Forked session to {session_id}.",
            effects=("sessions",),
        )

    return (
        Command("status", "Show the current session and thread status", handler=guard_command(status_command), usage="/status", effects=()),
        Command("clear", "Clear conversation history", handler=guard_command(clear_command), usage="/clear", effects=("history", "thread", "sessions")),
        Command("undo", "Remove recent conversation turns", handler=guard_command(undo_command), usage="/undo [count]", effects=("history", "thread", "sessions")),
        Command("fork", "Fork the persisted session", handler=guard_command(fork_command), usage="/fork", effects=("sessions",)),
    )


__all__ = ["build_session_commands"]
