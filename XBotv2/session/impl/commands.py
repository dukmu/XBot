"""Session lifecycle commands; turn and working-set commands belong to agentloop."""
from XBotv2.commands.api.contracts import Command, CommandResult, command_usage, guard_command
from XBotv2.session.api.contracts import SessionsPort, ThreadInfo

def build_session_commands(sessions: SessionsPort, info: ThreadInfo):
    async def fork_command(raw_args):
        if raw_args.strip():
            return command_usage("/fork")
        session_id = await sessions.fork_session(info.session_id)
        return CommandResult(f"Forked session to {session_id}.", effects=("sessions",))
    return (Command("fork", "Fork the persisted session", handler=guard_command(fork_command), usage="/fork", effects=("sessions",)),)
