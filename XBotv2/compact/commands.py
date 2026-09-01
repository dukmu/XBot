"""Human-facing /compact command."""

from __future__ import annotations

from typing import Any, Protocol

from XBotv2.commands import CommandResult
from XBotv2.core import HistoryCheckpoint


class _CompactCommandOwner(Protocol):
    async def _compact_current_history(
        self,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]: ...

    def _compaction_checkpoints(self) -> tuple[HistoryCheckpoint, ...]: ...

    async def _restore_compaction(
        self,
        checkpoint_id: str = "",
    ) -> HistoryCheckpoint: ...


def compact_result_message(metrics: dict[str, Any]) -> str:
    if not metrics:
        return "Conversation history compacted."
    usage = metrics.get("model_usage") or {}
    return (
        "Conversation history compacted "
        f"from about {metrics.get('context_tokens_before', 0)} to "
        f"{metrics.get('context_tokens_after_estimate', 0)} context tokens; "
        f"summary model used {usage.get('input_tokens', 0)} input and "
        f"{usage.get('output_tokens', 0)} output tokens."
    )


async def run_compact_command(
    owner: _CompactCommandOwner,
    raw_args: str,
) -> CommandResult:
    args = raw_args.split()
    if args and args[0] == "list" and len(args) == 1:
        checkpoints = owner._compaction_checkpoints()
        if not checkpoints:
            return CommandResult("No compaction checkpoints.")
        return CommandResult("\n".join(
            f"{item.checkpoint_id}  {item.status}  {item.reason}  "
            f"{item.messages_before} -> {item.messages_after}"
            for item in checkpoints
        ))
    if args and args[0] == "restore" and len(args) <= 2:
        try:
            checkpoint = await owner._restore_compaction(
                args[1] if len(args) == 2 else ""
            )
        except Exception as exc:
            return CommandResult(
                f"Conversation compaction restore failed: {exc}",
                status="error",
            )
        return CommandResult(
            f"Restored compaction checkpoint {checkpoint.checkpoint_id}.",
            effects=("history", "thread"),
        )
    if args:
        return CommandResult(
            "Usage: /compact | /compact list | /compact restore [checkpoint-id]",
            status="error",
        )

    try:
        result, metrics = await owner._compact_current_history()
    except Exception as exc:
        return CommandResult(
            f"Conversation compaction failed: {exc}",
            status="error",
        )

    if isinstance(result, dict) and result.get("event"):
        event = result.get("event") or {}
        data = event.get("data") or {}
        return CommandResult(
            str(data.get("message") or "Conversation compaction was rejected."),
            status="error",
        )
    if not (isinstance(result, dict) and result.get("rebuild")):
        return CommandResult(
            "Conversation history is too short to compact.",
        )

    return CommandResult(
        compact_result_message(metrics),
        effects=("history", "thread"),
    )


__all__ = ["compact_result_message", "run_compact_command"]
