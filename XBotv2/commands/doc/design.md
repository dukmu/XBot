# Commands

Commands owns the thread's human-facing slash-command registry and the list and execute operations. Plugins register `Command` values through `ctx.commands`; registrations are tied to the registering fiber unless an explicit owner or caller cleanup is supplied. Duplicate names fail. The registry is runtime state and is removed with its owner.

The plugin provides `commands` without required services. It binds `LIST_COMMANDS` and `EXECUTE_COMMAND` and mounts its HTTP router when server and sessions are available. GET and POST `/sessions/{session_id}/threads/{thread_id}/commands` discover and run server commands. It has no configuration defaults, tools, or prompt text; registrations take effect as plugins are mounted. Command effects describe the resources a handler may change; successful results report actual effects.

```python
from XBotv2.commands.api.contracts import COMMANDS, Command, CommandResult

async def greet(raw_args: str) -> CommandResult:
    return CommandResult(f"Hello, {raw_args}!")

ctx.require(COMMANDS).register(Command(name="greet", description="Greet someone", handler=greet))
```

Server commands use this execution path; prompt expansions go through message submission and client commands run locally. Agent tools remain separate.
