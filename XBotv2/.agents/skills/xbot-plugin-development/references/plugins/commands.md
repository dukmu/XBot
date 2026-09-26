# `commands`

Registers human-facing slash commands. Server commands run on the server,
prompt commands are submitted through the message endpoint, and client commands
are local UI affordances. They are not Agent Tools and must not be routed through
a synthetic `ToolCall`.

- **Import/profile:** `commands`, Agent profile.
- **Source:** `XBotv2/commands/plugin.py`,
  `XBotv2/commands/contracts.py`, `XBotv2/commands/protocol.py`.
- **Injects/provides:** (no required services) → `commands`
  (`CommandsService`).
- **Operations:** `commands/list` (`LIST_COMMANDS`,
  `EmptyRequest → CommandCatalog`), `commands/execute`
  (`EXECUTE_COMMAND`, `ExecuteCommand → CommandExecution`).
- **Server routes:** `build_commands_router` in
  `XBotv2/commands/protocol.py` exposes GET/POST under
  `/sessions/{session_id}/threads/{thread_id}/commands`. There is no
  `XBotv2/server/routes/` package — the router is declared in the owning plugin
  package and mounted with `contribute_router(ctx, owner="xbot.commands.http",
  ...)`.

## Public data models (`commands/contracts.py`)

### `Command` — registration

```python
@dataclass(frozen=True, slots=True)
class Command:
    name: str                          # regex ^[a-z0-9][a-z0-9_-]*$
    description: str
    kind: Literal["client", "server", "prompt"] = "server"
    handler: CommandHandler | None = None
    usage: str = ""
    examples: tuple[str, ...] = ()
    parameters: dict[str, str] = field(default_factory=dict)
    effects: tuple[CommandEffect, ...] = ()
    exclusive: bool = True
```

`__post_init__` validates the name and handler contract: `client` and `server`
commands have handlers; `prompt` commands do not.

Declare `effects` for every command you register. They are published in the
catalogue, so a client knows **before** running a command what it can touch
(`history`, `thread`, `agents`, `jobs`, `commands`, `sessions`, `policy`) and can
refresh exactly those panels instead of guessing. The result's `effects` are what
actually happened; the declaration is what *can* happen.

### `CommandResult` — handler return

```python
@dataclass(frozen=True, slots=True)
class CommandResult:
    message: str
    status: Literal["ok", "error"] = "ok"
    effects: tuple[CommandEffect, ...] = ()
```

`CommandEffect = Literal["history", "thread", "agents", "jobs",
"commands", "sessions", "policy"]`.

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
    effects: tuple[CommandEffect, ...] = ()
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

| Method | Path | Operation ID | Body | Returns |
|---|---|---|---|---|
| `GET` | `/sessions/{session_id}/threads/{thread_id}/commands` | `list_commands` | — | `CommandListResponse` |
| `POST` | `/sessions/{session_id}/threads/{thread_id}/commands` | `run_command` | `CommandRequest` | `CommandResponse` |

Both routes are part of the public, typed API surface (they appear in the
OpenAPI schema and in `XBotClient` as `list_commands` / `run_command`). The
*shape* is the contract; the *content* is not: which commands exist is decided
per session and thread by the plugins loaded there, so clients read the
catalogue and never hardcode it.

### `CommandRequest` (POST body)

```python
class CommandRequest(WireModel):
    raw: str = Field(min_length=1)   # the line exactly as the user typed it
```

The HTTP route accepts one raw line and resolves it against the current server
catalogue. `server` entries execute through this route. A `prompt` entry is
submitted by the client through the message endpoint; `client` entries are not
server executable. The Textual TUI merges local commands with the server
catalogue, with local names taking precedence. Its `/help` and `/help
<command>` handlers are local; see [client-runtime.md](../client-runtime.md). A
handler parse error should be a `CommandResult(status="error", ...)`, not a
transport failure.

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

## `CommandsService` (`commands/plugin.py`)

```python
class CommandsService:
    def register(
        self, command: Command, *,
        cleanup: Literal["fiber", "caller"] | None = None,
    ) -> str: ...
    def unregister(self, name: str) -> bool: ...
    def get(self, name: str) -> Command | None: ...
    def all(self) -> tuple[Command, ...]: ...
    def __len__(self) -> int: ...

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

For a `kind="prompt"` command (client-side expansion only), omit `handler`;
the client submits the expanded prompt through the message boundary. The
`client` kind is for local affordances, not third-party server registrations.

## Cross-references

- Depends on: (none).
- Depended on by the plugins that register slash commands. In the bundled tree
  those are: `sandbox` (`/sandbox`), `permissions` (`/permission`), `session`,
  `goal` (`/goal`), `jobs` (`/jobs`), `agents`, `compact`, and `llm` (its
  commands facet). `skills` also registers dynamic `kind="prompt"` commands, one
  per user-invocable discovered skill. The `tui` client plugin registers local
  `client` commands.
- **Not** depended on by `todolist`, `subagents`, `mcp_plugin`, or `browser`:
  none of them registers a slash command. They expose model-facing Tools only.
  `todolist` additionally mounts its own HTTP router
  (`contribute_router(ctx, owner="xbot.todolist.http", ...)`); `browser`,
  `subagents`, and `mcp_plugin` have no HTTP surface at all.
- Pairs with: `interactions` (asynchronous input requests), `permissions`
  (approval flow), and this plugin's own `protocol.py` for HTTP exposure.
- There is no `XBotv2/server/routes/` package. Command HTTP exposure lives in
  `XBotv2/commands/protocol.py` as `build_commands_router`.

## Common pitfalls

- **Reusing a name across plugins**: registration is global; duplicate
  names raise. Pick distinct names (`mcp-list`, `job-stop`).
- **Returning a `ToolOutcome` from a command handler**: command
  handlers return `CommandResult`; `ToolOutcome` is for Tool invocations.
- **Synthesizing a `ToolCall` to invoke a command**: commands bypass
  the model entirely; routing through `ToolCall` would also run
  permissions and Tool guards, neither of which apply.
- **Forgetting `exclusive=False`**: by default a command closes the
  user's pending input — useful for one-shot control (`/sandbox set`,
  `/new`) but surprising for commands that should *append* to a turn.
- **Reading `args` without `split_command_args`**: the handler
  receives the raw post-slash string; parse with
  `split_command_args` for shell-quoted argument semantics.
