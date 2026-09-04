# `commands`

Registers and executes server-side slash commands. The model never sees
this; commands are direct human control. Do not route a command through
a synthetic `ToolCall`.

- **Import/profile:** `commands`, Agent profile.
- **Source:** `XBotv2/commands/plugin.py`,
  `XBotv2/commands/contracts.py`, `XBotv2/commands/protocol.py`.
- **Injects/provides:** (no required services) → `commands`
  (`CommandsService`).
- **Operations:** `commands/list` (`LIST_COMMANDS`,
  `EmptyRequest → CommandCatalog`), `commands/execute`
  (`EXECUTE_COMMAND`, `ExecuteCommand → CommandExecution`).
- **Server routes:** `XBotv2/commands/protocol.py:build_commands_router`
  exposes GET/POST under `/sessions/{id}/threads/{tid}/commands`.

## Public data models (`commands/contracts.py`)

### `Command` — registration

```python
@dataclass(frozen=True, slots=True)
class Command:
    name: str                          # regex ^[a-z0-9][a-z0-9_-]*$
    description: str
    kind: Literal["server", "prompt"] = "server"
    handler: CommandHandler | None = None
    usage: str = ""
    examples: tuple[str, ...] = ()
    parameters: dict[str, str] = field(default_factory=dict)
    exclusive: bool = True
```

`__post_init__` validates the name regex and that `kind="server"`
implies `handler is not None` (and vice versa for `kind="prompt"`).

### `CommandResult` — handler return

```python
@dataclass(frozen=True, slots=True)
class CommandResult:
    message: str
    status: Literal["ok", "error"] = "ok"
    effects: tuple[CommandEffect, ...] = ()
```

`CommandEffect = Literal["history", "thread", "agents", "tasks",
"commands", "sessions"]`.

### `CommandHandler` and helpers

```python
CommandHandler = Callable[[str], Awaitable["CommandResult"]]

def split_command_args(raw_args: str) -> list[str]: ...
def command_error(message: str) -> CommandResult: ...
def command_usage(usage: str) -> CommandResult: ...
def guard_command(handler: CommandHandler) -> CommandHandler: ...
```

`guard_command` wraps a handler so `OperationError` and `ValueError`
become `CommandResult(status="error")`.

### `CommandDescription` — wire model for the catalog

```python
class CommandDescription(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str                         # bare name (no /)
    slash: str                        # formatted with leading /
    kind: Literal["client", "server", "prompt"]
    description: str
    usage: str
    examples: tuple[str, ...] = ()
    parameters: dict[str, str] = Field(default_factory=dict)
    exclusive: bool
```

### Operations

```python
LIST_COMMANDS = Operation("commands/list", EmptyRequest, CommandCatalog)
EXECUTE_COMMAND = Operation(
    "commands/execute",
    ExecuteCommand,
    CommandExecution,
    exclusive=lambda request: request.exclusive,
)

@dataclass(frozen=True, slots=True)
class ExecuteCommand:
    command: str
    kind: Literal["server", "prompt"]
    raw_args: str
    exclusive: bool = True

class CommandExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    command: str
    status: Literal["ok", "error"]
    message: str
    effects: tuple[CommandEffect, ...] = ()

@dataclass(frozen=True, slots=True)
class CommandCatalog:
    commands: tuple[CommandDescription, ...]
```

## Server / client interface models (`commands/protocol.py`)

### HTTP routes (built by `build_commands_router`)

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/sessions/{session_id}/threads/{thread_id}/commands` | — | `CommandListResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/commands` | `CommandRequest` | `CommandResponse` |

Both routes are `include_in_schema=False` (internal TUI/web surface).

### `CommandRequest` (POST body)

```python
class CommandRequest(WireModel):
    command: str = ""               # bare name; leading / is stripped
    args: list[str] | None = None   # pre-split; None → shlex.split(raw)
    raw: str = ""                   # raw tail after the slash command
    kind: Literal["server", "prompt"] = "server"
```

### `CommandListResponse`

```python
class CommandListResponse(WireModel):
    commands: list[CommandDescription]
```

### `CommandResponse`

```python
class CommandResponse(WireModel):
    type: Literal["command_result"] = "command_result"
    data: CommandExecution
```

## `CommandsService` (`commands/plugin.py:25-94`)

```python
class CommandsService:
    def register(self, command: Command) -> object: ...   # disposer
    def unregister(self, name: str) -> bool: ...
    def resolve(self, name: str) -> Command | None: ...
    def names(self) -> tuple[str, ...]: ...
    def descriptions(self) -> tuple[CommandDescription, ...]: ...
    def execute(
        self, name: str, raw_args: str, *, exclusive: bool = True
    ) -> CommandResult: ...

class CommandOperations:
    # Handler bindings for LIST_COMMANDS / EXECUTE_COMMAND.
```

Names must be stable and unique; duplicate registrations raise.

## Typical extension: register one command

```python
from XBotv2.commands import (
    Command, CommandResult, command_usage, split_command_args,
)

class GreetingCommands:
    async def greet(self, raw_args: str) -> CommandResult:
        args = split_command_args(raw_args)
        if len(args) != 1:
            return command_usage("/greet <name>")
        return CommandResult(f"Hello, {args[0]}!")


class GreetingPlugin:
    name = "greeting-command"
    inject = ["commands"]

    def apply(self, ctx, config):
        ctx.commands.register(Command(
            name="greet",
            description="Greet one person",
            usage="/greet <name>",
            examples=("/greet Ada",),
            parameters={"name": "Person to greet"},
            handler=GreetingCommands().greet,
        ))
```

For a `kind="prompt"` command (client-side expansion only), omit
`handler` — the client expands `/foo ...` into model-visible text.

## Cross-references

- Depends on: (none).
- Depended on by: every plugin that exposes a slash command
  (`sandbox`, `session`, `goal`, `todolist`, `subagents`, `jobs`,
  `compact`, `llm-commands`, `mcp-plugin`, `browser`, etc.).
- Pairs with: `interactions` (asynchronous input requests),
  `permission_request` (approval flow), `server.routes.commands` for
  HTTP exposure (already wired here).

## Common pitfalls

- **Reusing a name across plugins**: registration is global; the
  second wins or raises. Pick namespaced names (`mcp-list`, `job-stop`).
- **Returning a `ToolResult` from a command handler**: command
  handlers return `CommandResult`; `ToolResult` is for Tool invocations.
- **Synthesizing a `ToolCall` to invoke a command**: commands bypass
  the model entirely; routing through `ToolCall` would also run
  permissions and Tool guards, neither of which apply.
- **Forgetting `exclusive=False`**: by default a command closes the
  user's pending input — useful for one-shot control (`/sandbox set`,
  `/new`) but surprising for commands that should *append* to a turn.
- **Reading `args` without `split_command_args`**: the handler
  receives the raw post-slash string; parse with
  `split_command_args` for shell-quoted argument semantics.
