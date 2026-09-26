# `acp_plugin`

The ACP stdio carrier — exposes XBot as an ACP v1 Agent over JSON-RPC on
stdin/stdout. Implements the ACP protocol surface: initialize, session
management (new/resume/load/list/fork/close), prompt streaming, slash-command
execution, permission and elicitation bridging, and MCP server negotiation.

- **Tree id/name:** `acp` / `acp_plugin` (the page filename is
  `acp-plugin.md`); ACP carrier profile only.
- **Source:** `XBotv2/acp_plugin/plugin.py` (`ACPComponent`),
  `XBotv2/acp_plugin/xbot_agent.py` (`XBotACPAgent`, `ActivePrompt`),
  `XBotv2/acp_plugin/events.py` (`ACPEventMapper`, `replay_history`),
  `XBotv2/acp_plugin/contracts.py` (`ACPLaunch`),
  `XBotv2/acp_plugin/server.py` (`run_acp`).
- **Injects/provides:** `sessions`, `acp_launch`, `runtime_log` → sets
  `ctx.set("acp_agent", agent)` (`XBotACPAgent`).
- **Subscribes to events:** none. The agent reacts to the per-session event
  frames it subscribes to through `SessionsPort.stream_events`.

## `ACPComponent` (`XBotv2/acp_plugin/plugin.py`)

```python
class ACPComponent:
    name = "xbot.acp"
    inject = ["sessions", "acp_launch", "runtime_log"]

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
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


plugin = ACPComponent()
```

There is no `ACPPlugin` class here, and the component registers no event
listeners — the event mapper is constructed per prompt and per session event
subscription inside `XBotACPAgent`.

## `XBotACPAgent` (`XBotv2/acp_plugin/xbot_agent.py`)

```python
class XBotACPAgent:
    """Expose XBot as a stable ACP v1 Agent."""

    def __init__(
        self,
        *,
        sessions: SessionsPort,
        provider_name: str | None,
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
        self, session_id: str, prompt: list[Any], **_: Any
    ) -> PromptResponse: ...

    async def cancel(self, session_id: str, **_: Any) -> None: ...

    async def set_session_mode(
        self, session_id: str, mode_id: str, **_: Any
    ) -> None: ...

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **_: Any
    ) -> SetSessionConfigOptionResponse: ...

    async def authenticate(self, method_id: str, **_: Any) -> None: ...

    async def ext_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...
```

`provider_name` is `str | None`, not `str`. There is no
`cancel_session(...) -> CancelSessionResponse`: the cancellation entry point is
`cancel(self, session_id, **_) -> None`, which calls
`self.sessions.interrupt(session_id, "agent")` and returns nothing.

`set_session_mode` always raises
`RequestError.method_not_found("session/set_mode")`; `authenticate` raises
`RequestError.method_not_found(...)`; `ext_notification` is a no-op. `close()`
fails every live prompt with `RuntimeError("ACP event delivery stopped")` and
cancels the per-session event tasks.

### Per-session runtime state

```python
@dataclass(slots=True)
class ActivePrompt:
    request_id: str
    mapper: ACPEventMapper
    completed: asyncio.Event
    turn_id: TurnId | None = None
    failure: Exception | None = None
```

The agent holds `_commands_announced: set[str]`,
`_event_tasks: dict[str, asyncio.Task[None]]`, and
`_active_prompts: dict[str, ActivePrompt]`. Public attributes are `sessions`,
`provider_name`, `no_plugins`, `selected_agent`, `llm_override`, `connection`,
and `client_capabilities`.

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
            http=not self.no_plugins,
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

The `protocol_version` and `client_info` arguments are discarded
(`del protocol_version`, `del client_info`); the response always advertises the
SDK's `PROTOCOL_VERSION`. `client_capabilities` is retained on the agent because
elicitation support is read from it.

### `ACPLaunch` (`XBotv2/acp_plugin/contracts.py`)

```python
@dataclass(frozen=True, slots=True)
class ACPLaunch:
    provider_name: str | None
    no_plugins: bool
    selected_agent: str | None = None
    llm_override: BaseProvider | None = None
```

### `ACPEventMapper` (`XBotv2/acp_plugin/events.py`)

```python
class ACPEventMapper:
    """Stateful event mapper for one ACP prompt turn."""

    def __init__(self, *, context_size: int = 0) -> None:
        self.stop_reason = "end_turn"
        self.error: LoopError | None = None
        self.usage: TokenCounters | None = None
        self._streamed_message = False
        self._streamed_reasoning = False
        self._context_size = context_size
        self._jobs: set[str] = set()

    def updates(
        self,
        event: object,
        *,
        fallback_context_size: int | None = None,
    ) -> list[Any]:
        """Translate one typed session event into ACP protocol updates."""
```

That field list is exhaustive. `error` is `LoopError | None` (a typed protocol
error, not a `dict`), `usage` is `TokenCounters | None`, there are **two**
streaming flags (`_streamed_message`, `_streamed_reasoning`), and the job-id set
is `_jobs`, not `_tasks`.

The mapper consumes typed loop/session event objects, not string-keyed
dictionaries. Its mappings are:

| Typed event | ACP update |
|---|---|
| `LoopTurnStarted` | reset both streaming flags |
| `AssistantTextDelta` | `update_agent_message_text(event.text)`, sets `_streamed_message` |
| `AssistantReasoningDelta` | `update_agent_thought_text(event.text)`, sets `_streamed_reasoning` |
| `AssistantCompleted` | emit reasoning text and/or message text that was not already streamed |
| `ClientNotice` | `update_agent_message_text(event.message)` |
| `ToolCallsStarted` | `start_tool_call(...)` per call with `kind=started.category or None` and `status="pending"`; resets `_streamed_message` |
| `ToolCompleted` | `update_tool_call(..., status="completed"|"failed", raw_output=outcome.model_dump(...))` |
| `JobUpdatedEvent`, `JobCompletedEvent` | first sighting → `start_tool_call`; later → `update_tool_call`; `kind="execute"` for `view.kind == "shell"`, otherwise `"other"` |
| `UsageObserved` | accumulate `TokenCounters`; emit `UsageUpdate(used=current.input, size=...)` only when the size is `> 0` |
| `LoopTurnEnded` | `"cancelled"` for `TurnCancelled`, otherwise `TurnFinished.stop_reason` |
| `LoopError` | store the typed error on `self.error`; emit nothing |

Any other event — including an unrecognized loop event — produces an empty
list. Job status mapping uses `_task_status`: `queued → pending`,
`succeeded → completed`, `failed*`/`cancelled* → failed`, otherwise
`in_progress`.

### `replay_history`

```python
def replay_history(items: Iterable[ConversationRecord]) -> list[Any]:
    """Translate persisted conversation messages into ACP load updates."""
```

`HumanInputRecord` becomes `update_user_message_text`; `RuntimeNoticeRecord`
becomes a completed `start_tool_call` titled
`f"Injected context · {source} / {event}"`; `AssistantRecord` restores reasoning
text, message text, and `start_tool_call(..., status="pending")` for each pending
tool call (with no `kind` — replay has no registry to consult, so it does not
invent a taxonomy); `CompactionSummaryRecord` restores the summary as agent
message text; `ToolRecord` emits the matching `update_tool_call`. Entries are
processed in the order given.

No `item.runtime` attribute is involved anywhere. An earlier version of this
page claimed runtime-injected tool calls are keyed on `item.runtime`; that field
does not exist — the runtime-notice branch keys on `RuntimeNoticeRecord` and
reads `item.content`, `item.source`, and `item.event`.

## How `apply()` works

`ACPComponent.apply` reads `ctx.acp_launch`, builds the agent from
`ctx.sessions` and `ctx.runtime_log`, publishes it as `ctx.acp_agent`, and
registers `agent.close` for disposal. The agent uses `ctx.sessions`
(`SessionsPort`) for session management and session event cursors for
subscription; `XBotv2/acp_plugin/server.py` drives it with
`run_agent(context.acp_agent, use_unstable_protocol=True)`.

## ACP protocol flow

```
stdin/stdout JSON-RPC →
  initialize() → InitializeResponse with capabilities →
  new_session / resume_session / load_session / fork_session →
    sessions.open(OpenSession(...)) + _prepare_session(event_cursor) →
  prompt() →
    _slash_command() hit → LIST_COMMANDS + EXECUTE_COMMAND → agent message text
    otherwise → SendMessage(request_id="acp:{session_id}") →
      _forward_session_events() → ACPEventMapper.updates() → client updates →
      LoopTurnEnded / LoopError → prompt.completed.set()
  cancel(session_id) → sessions.interrupt(session_id, "agent")
  close_session() → sessions.close_session() + stop the session's event task
```

`ActivePrompt.request_id` is `f"acp:{session_id}"`. When the first
`MessagePublishedEvent` whose record id equals that request id arrives with a
`TurnScope`, the agent latches `active.turn_id` and thereafter routes that turn's
frames through the prompt's own mapper; every other frame goes to the shared
per-session mapper.

## Event and interaction bridging

- **Session events**: `_prepare_session` cancels any existing task for the
  session, opens `sessions.stream_events(session_id, "agent", after=event_cursor)`,
  and starts `_forward_session_events`. That loop resolves interaction requests
  first (`_resolve_interaction`), tracks `AgentConfiguredEvent` to refresh the
  fallback context window, then forwards `mapper.updates(...)`. Its `finally`
  block releases an active prompt even if the stream ends without a terminal
  frame.
- **Permissions**: `_handle_interaction` calls
  `connection.request_permission(...)` with `allow_once`, `allow_session`
  (`kind="allow_always"`), and `deny` options, mapping the outcome to
  `Allowed(scope="once" | "session")` or `Denied(reason=...)`.
- **Elicitation**: a `UserInputRequest` requires
  `client_capabilities.elicitation.form`; otherwise it resolves
  `InputCancelled("ACP client does not support form elicitation")`. An accepted
  answer must carry a `{"answer": ...}` content payload.
- **Disconnected client**: with `self.connection is None`, a permission request
  resolves `Denied("ACP client disconnected")` and any other request
  `InputCancelled("ACP client disconnected")`.
- **Commands**: `/name args` is treated as a server command only when the
  content starts with `/`, contains no newline, and matches a catalog entry with
  `kind == "server"`. The result message is sent back through
  `update_agent_message_text`, and the response is
  `PromptResponse(stop_reason="end_turn")`. `AvailableCommandsUpdate` is
  announced once per session.

## Cross-references

- Depends on: `sessions` (`SessionsPort`), `acp_launch`, `runtime_log`,
  `agents` (`LIST_AGENTS`, `SELECT_AGENT`), `commands` (`LIST_COMMANDS`,
  `EXECUTE_COMMAND`), `llm` (`LIST_PROVIDERS`, `SELECT_PROVIDER`),
  `mcp_plugin` (`MCP_PLUGIN_ID`), `XBotv2.session.contracts` (public session
  DTOs and ports), and `XBotv2.session` (`conversation_replay`).
- Depended on by: ACP clients, via `run_acp` in `XBotv2/acp_plugin/server.py`.
- Pairs with: `process-sessions` (`SessionsPort` implementation) and
  `mcp-plugin` (MCP server negotiation).

## Common pitfalls

- **`additional_directories` is always rejected**: any non-empty value raises
  `RequestError.invalid_params({"additionalDirectories": "not supported"})` from
  `new_session`, `resume_session`, `load_session`, and `fork_session`. ACP cannot
  expand filesystem access beyond the session's workspace.
- **`cwd` must be an existing absolute directory**: `_workspace()` expands `~`
  and requires an absolute path that `is_dir()`, otherwise raising
  `RequestError.invalid_params({"cwd": cwd})`.
- **The session workspace is sticky**: `resume_session`, `load_session`, and
  `fork_session` compare the resolved stored workspace with `cwd` and reject a
  mismatch with `invalid_params` carrying `expectedCwd`.
- **`no_plugins` disables MCP**: `McpCapabilities(http=not no_plugins)`, and
  `_mcp_plugin_config` raises `invalid_params({"mcpServers": "plugins are
  disabled"})` when servers are requested. SSE transports are rejected with
  `"SSE transport is not supported"`, and server names must match
  `^[A-Za-z0-9._-]+$`.
- **`prompt()` raises if a prompt is already running**:
  `RequestError.invalid_request(...)` while `session_id in self._active_prompts`
  — one prompt per session.
- **Commands are announced once per session**: `self._commands_announced`
  tracks which sessions have received `AvailableCommandsUpdate`.
- **Empty prompts are rejected**: `_prompt_content` raises
  `invalid_params({"prompt": "prompt is empty"})` when neither text nor images
  were supplied; binary embedded resources and unknown content types are
  rejected the same way.
- **`list_sessions` ignores paging**: a non-empty `cursor` returns an empty
  `ListSessionsResponse`, and sessions without a workspace root are filtered out.
