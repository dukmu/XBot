"""Human session commands and their typed service binding factory."""

from __future__ import annotations

from XBotv2.commands import (
    Command,
    CommandResult,
    command_usage,
    guard_command,
    split_command_args,
)
from XBotv2.session.services import SessionPort


def build_session_commands(session: SessionPort) -> tuple[Command, ...]:
    async def status_command(raw_args: str) -> CommandResult:
        if raw_args.strip():
            return command_usage("/status")
        status = session.status()
        return CommandResult(
            " ".join(
                f"{key}={value}"
                for key, value in (
                    ("session_id", status.session_id),
                    ("thread_id", status.thread_id),
                    ("provider", status.provider),
                    ("model", status.model),
                )
            )
        )

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
        Command("status", "Show the current session and thread status", handler=guard_command(status_command), usage="/status"),
        Command("clear", "Clear conversation history", handler=guard_command(clear_command), usage="/clear"),
        Command("undo", "Remove recent conversation turns", handler=guard_command(undo_command), usage="/undo [count]"),
        Command("fork", "Fork the persisted session", handler=guard_command(fork_command), usage="/fork"),
    )


__all__ = ["build_session_commands"]
