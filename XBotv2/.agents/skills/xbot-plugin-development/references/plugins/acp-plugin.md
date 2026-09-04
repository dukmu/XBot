# `acp-plugin`

The ACP stdio carrier — exposes XBot as a stable ACP v1 Agent via
JSON-RPC over stdin/stdout. Implements the full ACP protocol surface:
initialize, session management, prompt streaming, MCP server negotiation.

- **Import/profile:** `acp-plugin`, ACP carrier profile.
- **Source:** `XBotv2/acp_plugin/plugin.py`,
  `XBotv2/acp_plugin/xbot_agent.py`,
  `XBotv2/acp_plugin/server.py`,
  `XBotv2/acp_plugin/contracts.py`,
  `XBotv2/acp_plugin/events.py`.
- **Injects/provides:** `sessions`, `acp_launch`, `runtime_log` →
  `acp_agent` (`XBotACPAgent`).
- **Subscribes to events:** none (ACP agent is event-driven via ACP
  protocol itself).

## Public data models

### `XBotACPAgent` (`XBotv2/acp_plugin/xbot_agent.py:80-340`)

```python
class XBotACPAgent:
    def __init__(
        self,
        *,
        sessions: SessionsPort,
        provider_name: str,
        no_plugins: bool = False,
        selected_agent: str | None = None,
        llm_override: Any | None = None,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None: ...

    def on_connect(self, connection: Any) -> None: ...

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **_: Any,
    ) -> InitializeResponse: ...

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **_: Any,
    ) -> NewSessionResponse: ...

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **_: Any,
    ) -> ResumeSessionResponse: ...

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **_: Any,
    ) -> LoadSessionResponse: ...

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **_: Any,
    ) -> ListSessionsResponse: ...

    async def close_session(
        self, session_id: str, **_: Any
    ) -> CloseSessionResponse: ...

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **_: Any,
    ) -> ForkSessionResponse: ...

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **_: Any,
    ) -> PromptResponse: ...

    async def cancel_session(
        self, session_id: str, **_: Any
    ) -> CancelSessionResponse: ...

    async def close(self) -> None: ...
```

### `InitializeResponse` capabilities

```python
InitializeResponse(
    protocol_version=PROTOCOL_VERSION,
    agent_capabilities=AgentCapabilities(
        load_session=True,
        prompt_capabilities=PromptCapabilities(
            image=True,
            audio=False,
            embedded_context=True,
        ),
        mcp_capabilities=McpCapabilities(
            http=not no_plugins,
            sse=False,
        ),
        session_capabilities=SessionCapabilities(
            list=SessionListCapabilities(),
            fork=SessionForkCapabilities(),
            resume=SessionResumeCapabilities(),
            close=SessionCloseCapabilities(),
        ),
    ),
    agent_info=Implementation(
        name="xbot",
        title="XBot",
        version=__version__,
    ),
    auth_methods=[],
)
```

### `ACPLaunch`

```python
@dataclass(frozen=True, slots=True)
class ACPLaunch:
    provider_name: str
    no_plugins: bool
    selected_agent: str | None = None
    llm_override: BaseProvider | None = None
```

### `ACPEventMapper` (`XBotv2/acp_plugin/events.py:17-160`)

```python
class ACPEventMapper:
    def __init__(self, *, context_size: int = 0) -> None:
        self.stop_reason = "end_turn"
        self.error: dict[str, Any] | None = None
        self.usage: dict[str, int] | None = None
        self._streamed_message = False
        self._context_size = context_size
        self._tasks: set[str] = set()

    def updates(self, event: dict[str, Any]) -> list[Any]:
        """Translate one session event into ACP protocol updates."""
```

Event type mappings:

| Event type | ACP update |
|---|---|
| `turn_started` | no-op |
| `assistant_message_delta` | `update_agent_message_text()` + `update_agent_thought_text()` |
| `assistant_message` | `update_agent_message_text()` |
| `client_message` | `update_agent_message_text()` |
| `tool_calls_started` | `start_tool_call()` for each |
| `tool_result` | `update_tool_call()` |
| `task_updated` | `start_tool_call()` or `update_tool_call()` |
| `usage` | `UsageUpdate()` |
| `turn_cancelled` | set `stop_reason = "cancelled"` |
| `error` | set `error` field |

### `replay_history`

```python
def replay_history(items: Iterable[SessionHistoryItem]) -> list[Any]:
    """Translate persisted conversation messages into ACP load updates."""
```

User messages → `update_user_message_text()` or `start_tool_call()` if runtime-injected.
Assistant messages → `update_agent_thought_text()` + `update_agent_message_text()` + `start_tool_call()` for tool calls.
Tool messages → `update_tool_call()`.

## How `apply()` works

```python
def apply(self, ctx: Context, config: object | None = None) -> None:
    launch = ctx.acp_launch
    agent = XBotACPAgent(
        sessions=ctx.sessions,
        provider_name=launch.provider_name,
        no_plugins=launch.no_plugins,
        selected_agent=launch.selected_agent,
        llm_override=launch.llm_override,
        runtime_log=ctx.runtime_log,
    )
    ctx.set("acp_agent", agent)
    ctx.dispose(agent.close)
```

The ACP agent is the stdio carrier's main entry point. It uses
`ctx.sessions` (the `SessionsPort`) for session management and
implements the full ACP protocol surface.

## ACP protocol flow

```
stdin/stdout JSON-RPC →
  initialize() → InitializeResponse with capabilities →
  new_session(resume_session/load_session) → NewSessionResponse →
  prompt() → stream_message() → event stream →
    ACPEventMapper.updates() → client updates →
  cancel_session() → interrupt()
```

## Cross-references

- Depends on: `sessions` (`SessionsPort`), `acp_launch`, `runtime_log`,
  `agents` (`LIST_AGENTS`, `SELECT_AGENT`), `commands` (`LIST_COMMANDS`,
  `EXECUTE_COMMAND`), `llm` (`LIST_PROVIDERS`, `SELECT_PROVIDER`),
  `session.types`, `session.services`, `session.history`,
  `session.event_stream`.
- Depended on by: ACP clients.
- Pairs with: `process-sessions` (`SessionsPort` implementation),
  `mcp-plugin` (MCP server negotiation).

## Common pitfalls

- **`additional_directories` is always rejected**: `XBotACPAgent`
  calls `self._reject_additional_directories(additional_directories)`
  in `new_session`, `resume_session`, `load_session`, `fork_session`.
  This is a security constraint — ACP cannot expand filesystem
  access beyond the session's sandbox.
- **`no_plugins` disables MCP**: `McpCapabilities(http=not no_plugins)`
  means no MCP servers when `no_plugins=True`.
- **`prompt()` raises if a prompt is already running**:
  `self._active_prompts[session_id]` check — one prompt per session.
- **Commands are announced once per session**: `self._commands_announced`
  tracks which sessions have received `AvailableCommandsUpdate`.
- **`replay_history` uses runtime-injected tool calls**: messages
  with `item.runtime` set are rendered as `start_tool_call()` with
  the runtime source and event metadata.
