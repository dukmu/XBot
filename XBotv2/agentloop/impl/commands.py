"""Human commands for execution status and the active working set."""
from XBotv2.commands.api.contracts import Command, CommandResult, command_usage, guard_command, split_command_args

def build_loop_commands(loop):
    async def status(raw_args):
        if raw_args.strip():
            return command_usage("/status")
        value = loop.status()
        return CommandResult("\n".join(("Session", f"  Title: {value.title}", f"  ID: {value.session_id}", f"  Thread: {value.thread_id}", f"  Workspace: {value.workspace_root}", f"  Agent: {value.agent}", "Runtime", f"  State: {value.status} ({'resumed' if value.resumed else 'new'})", f"  History: {value.turn_count} turns, {value.message_count} messages", f"  Queued inputs: {value.pending_inputs}", "Model", f"  Provider: {value.provider}", f"  Model: {value.model}", f"  Mode: {value.model_mode or 'default'}", f"  Context window: {value.context_window}")))
    async def clear(raw_args):
        if raw_args.strip():
            return command_usage("/clear")
        result = await loop.clear_history()
        return CommandResult(f"Cleared {result.removed_turns} conversation turns.", effects=("history", "thread", "sessions"))
    async def undo(raw_args):
        parts = split_command_args(raw_args)
        if len(parts) > 1:
            return command_usage("/undo [count]")
        try:
            count = int(parts[0]) if parts else 1
        except ValueError:
            return CommandResult("Undo count must be a positive integer.", status="error")
        if count < 1:
            return CommandResult("Undo count must be a positive integer.", status="error")
        await loop.undo_history(count)
        return CommandResult(f"Removed {count} conversation turn(s).", effects=("history", "thread", "sessions"))
    return (Command("status", "Show current session and thread status", handler=guard_command(status), usage="/status"), Command("clear", "Clear conversation history", handler=guard_command(clear), usage="/clear", effects=("history", "thread", "sessions")), Command("undo", "Remove recent conversation turns", handler=guard_command(undo), usage="/undo [count]", effects=("history", "thread", "sessions")))
