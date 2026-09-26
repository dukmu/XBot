# `mcp-plugin`

Model Context Protocol (MCP) server integration — connects to configured MCP
servers over stdio or streamable HTTP, registers each discovered server tool as
an XBot Tool, and bridges MCP resources, prompts, completions, sampling,
elicitation, roots, and logging to XBot runtime capabilities.

- **Import/profile:** `mcp_plugin`, Agent profile.
- **Source:** `XBotv2/mcp_plugin/plugin.py` (lifecycle, handler classes, bridge
  Tool construction), `mcp_client.py` (`MCPClient`, `MCPCallResult`,
  `MCPConnectionError`), `contracts.py` (`MCPConfig`, `MCPServerConfig`,
  `MCP_PLUGIN_ID`), `tool.py` (`MCPTool`), `callbacks.py` (`client_callbacks`).
- **Injects/provides:** `tools`, `model`, `interactions`, `session`, `usage`,
  `loop_state` → (none directly; registers Tools through `ctx.tools`).
- **Subscribes to events:** `session/init` (`APPLICATION_INITIALIZED`; MCP
  server initialization), `session/close` (`Events.SESSION_CLOSE`; cleanup).
- **Config:** `servers: {<name>: {command|url, ...}}`; the plugin id is
  `MCP_PLUGIN_ID = "mcp_plugin"`.

Every discovered MCP Tool is registered through the standard `ToolsPort`.
Consequently the `permissions` guard evaluates its canonical name and final
arguments exactly like a built-in Tool. MCP does not register a second
permission requester, add an approval prompt, or bypass the configured
allow/ask/deny rules. `interactions` is injected for MCP's user-input
elicitation callback, which is distinct from permission approval.

## Public data models

### `MCPPlugin` (`XBotv2/mcp_plugin/plugin.py`)

```python
class MCPPlugin:
    inject = ["tools", "model", "interactions", "session", "usage", "loop_state"]
    name = MCP_PLUGIN_ID
    Config = MCPConfig

    def __init__(self) -> None:
        self._client = MCPClient()
        self._config = MCPConfig()
        self._server_status: dict[str, dict[str, JsonValue]] = {}
        self._server_tools: dict[str, list[str]] = {}
        self._initialized = False

    def apply(self, ctx: Context, config: MCPConfig) -> None: ...
    async def _on_session_init(self, _event: ApplicationInitialized) -> None: ...
    async def _on_session_close(self, ctx: SessionLifecycle) -> None: ...
    def _register_server_tools(
        self, server_name: str, tools: list[dict[str, JsonValue]]
    ) -> list[str]: ...
    def _register_tool(self, tool: Tool, server_name: str) -> str: ...
    def _register_resource_bridge(
        self, server: str, capability: dict[str, JsonValue]
    ) -> str: ...
    def _register_prompt_bridge(self, server: str) -> str: ...
    def _register_completion_bridge(self, server: str) -> str: ...
    def _register_bridge(self, server: str, tool: Tool) -> str: ...
    async def _rollback_server(self, server_name: str) -> None: ...
    async def _rollback_all(self) -> None: ...
    def diagnostics(self) -> dict[str, JsonValue]: ...
    async def _dispose(self) -> None: ...
```

`_server_status` values are `JsonValue` (`dict[str, dict[str, JsonValue]]`), not
`Any`. `diagnostics()` reports `{"status": "degraded" | "ready", "servers": ...}`
and is only `"degraded"` when some server status is `"error"`.

### `MCPClient` (`XBotv2/mcp_plugin/mcp_client.py`)

```python
class MCPClient:
    def __init__(self) -> None:
        self._transports: dict[str, _Connection] = {}
        self._stderr_handles: list[Any] = []

    async def connect_and_list(
        self,
        name: str,
        cfg: dict[str, JsonValue],
        *,
        callbacks: dict[str, Any] | None = None,
    ) -> list[dict[str, JsonValue]]: ...

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, JsonValue]
    ) -> MCPCallResult: ...

    def server_capabilities(self, server: str) -> dict[str, JsonValue]: ...
    async def list_resources(self, server: str) -> dict[str, JsonValue]: ...
    async def read_resource(self, server: str, uri: str) -> dict[str, JsonValue]: ...
    async def subscribe_resource(self, server: str, uri: str) -> dict[str, JsonValue]: ...
    async def unsubscribe_resource(self, server: str, uri: str) -> dict[str, JsonValue]: ...
    async def list_prompts(self, server: str) -> list[dict[str, JsonValue]]: ...
    async def get_prompt(
        self, server: str, name: str, arguments: dict[str, str] | None = None
    ) -> dict[str, JsonValue]: ...
    async def complete(
        self,
        server: str,
        reference: dict[str, JsonValue],
        argument: dict[str, str],
        context_arguments: dict[str, str] | None = None,
    ) -> dict[str, JsonValue]: ...
    async def set_logging_level(self, server: str, level: str) -> dict[str, JsonValue]: ...
    async def ping(self, server: str) -> dict[str, JsonValue]: ...
    async def disconnect_all(self) -> None: ...
    async def disconnect(self, name: str) -> bool: ...
```

`_Connection` is the private `(stack, session, initialize_result)` holder for one
open transport. `connect_and_list` raises `MCPConnectionError` if the server is
already connected and wraps any other initialization failure into
`MCPConnectionError(f"MCP server '{name}' initialization failed: ...")`.

### `MCPCallResult` (`XBotv2/mcp_plugin/mcp_client.py`)

```python
@dataclass(frozen=True, slots=True)
class MCPCallResult:
    content: str
    is_error: bool
    data: dict[str, JsonValue]
```

### `MCPTool` (`XBotv2/mcp_plugin/tool.py`)

```python
class MCPTool:
    def __init__(
        self, client: MCPClient, server: str, tool_def: dict[str, JsonValue]
    ) -> None:
        self._client = client
        self._server = server
        self._name = tool_def["name"]
        self._description = str(tool_def.get("description", ""))
        self._parameters = dict(tool_def["inputSchema"])
        self.__doc__ = self._description

    def as_tool(self, registered_name: str) -> Tool: ...

    async def __call__(self, **kwargs: JsonValue) -> ToolSucceeded | ToolFailed: ...
```

`__call__` returns `ToolFailed(error=ToolError(code="mcp_tool_error",
message=result.content), ...)` for an MCP error result and `ToolSucceeded` with a
single `TextPart` otherwise. It is not typed as the `ToolOutcome` alias.

### Registered names

Each MCP server tool is registered as `mcp__{server_name}__{tool_name}`
(`tool_name = tool_def["name"]`). Bridge Tools are registered as:

- `mcp__{server_name}__protocol_resources`
- `mcp__{server_name}__protocol_prompts`
- `mcp__{server_name}__protocol_complete`

Every one of them is registered with namespace `mcp:{server_name}` and
`cleanup="caller"`, because registration happens during `session/init` (outside
this plugin's `apply`) and the plugin owns release through `_rollback_all`.

## Server configuration

```python
# In xcore.yaml or tree config:
servers:
  my-server:
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    # OR for remote:
    # type: remote
    # url: http://localhost:3000/mcp
    timeout: 30
    enabled: true
    required: false
```

```python
class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: bool = True
    required: bool = False


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
```

`MCPServerConfig` deliberately allows extra keys: the stdio/HTTP option set
(`command`, `args`-style list, `env`, `cwd`, `log_path`, `type`, `url`,
`headers`, `terminate_on_close`, `timeout`) belongs to `MCPClient` and is
validated when the selected transport opens. `MCPConfig` forbids extras.

### Connection types

- **stdio** (default): selected whenever `cfg["type"]` is anything other than
  `"remote"`. Builds `StdioServerParameters(command=command[0],
  args=command[1:], env=..., cwd=...)` and opens `stdio_client(params,
  errlog=...)`.
- **remote**: `cfg["type"] == "remote"` opens an `httpx.AsyncClient` with
  `headers` and `timeout`, then `streamable_http_client(url,
  http_client=client, terminate_on_close=...)`. `terminate_on_close` defaults
  to `True`.
- `timeout` defaults to 30 seconds and is used both as the HTTP client timeout
  and as the `ClientSession(read_timeout_seconds=...)` value.

### Transport validation

`_validate_tool_list()` checks each `tools/list` entry:

1. The overall result is a dict and `result["tools"]` is a list.
2. Each entry is a dict with a non-empty `name`.
3. Each entry has an `inputSchema` dict whose `type` is `"object"`.
4. `inputSchema` passes `Draft202012Validator.check_schema`.

Any failure raises `MCPConnectionError`. When the server advertises no `tools`
capability, `_list_tools()` returns `[]` without calling `tools/list`.

## Callbacks (`XBotv2/mcp_plugin/callbacks.py`)

```python
def client_callbacks(
    model: ModelPort,
    interactions: InteractionsPort,
    session: SessionPort,
    usage: UsagePort,
    model_selection: Callable[[], ResolvedModelSelection],
) -> dict[str, Any]:
    return {
        "sampling_callback": sample,       # MCP sampling → llm.invoke_llm()
        "elicitation_callback": elicit,    # uses RequestContext.request_id
        "list_roots_callback": roots,      # MCP roots → workspace URI
        "logging_callback": log_message,   # MCP logging → logger
    }
```

Five positional parameters. `MCPPlugin` passes `self._model`,
`self._interactions`, `self._session`, `self._usage`, and
`lambda: self._loop_state.metadata.value.runtime_selection.model`.

- **`sample(request_context, params)`**: builds `ProviderSystem` /
  `ProviderUser` / `ProviderAssistant` messages, then calls
  `invoke_llm(model, model_request)` — the `llm` package's single-shot calling
  convention — not `model.astream()`. It does not stream to ACP. A
  `params.maxTokens` value is copied into the selection's
  `max_output_tokens`; usage is recorded through `UsagePort.record(...)` with
  `AuxiliaryRequest(owner="mcp_sampling", operation_id=...)`. Only text content
  is accepted.
- **`elicit(request_context, params)`**: reads `request_context.request_id` as
  the correlation id and calls
  `interactions.request_user_input(question, source="mcp_elicitation",
  tool_call_id=tool_call_id)`. URL-mode elicitation appends the URL to the
  question and accepts `y`/`yes`/`accept`/`ok`; form mode builds the content
  dict from the answer and `params.requestedSchema`.
- **`roots(_request_context)`**: returns a single `types.Root` at
  `Path(session.workspace_root).resolve().as_uri()` named `"workspace"`.
- **`log_message(params)`**: `logger.info("MCP server log [%s]: %s", ...)`.

MCP elicitation is not a separate Agent Tool call. The MCP SDK supplies a
request context for each server request; its `request_id` is used directly as
the interaction correlation field. This avoids changing the generic interaction
contract or fabricating a ToolCall. An elicitation request without a request id,
or a non-`Answered` resolution, returns `types.ElicitResult(action="cancel")`.

## Resource, prompt, and completion bridges

These three handler classes are declared in `plugin.py`, not in `tool.py` or
`callbacks.py`.

**Resource bridge** (`MCPResourceHandler`):

```python
def __init__(self, client: MCPClient, server: str, *, subscriptions: bool) -> None:
    self.operations = ["list", "read"]        # + ["subscribe", "unsubscribe"] when subscriptions

async def invoke(self, operation: str, uri: str = "") -> ToolOutcome: ...
```

The bridge is only registered when the server advertises the `resources`
capability, and `subscribe`/`unsubscribe` are only accepted when that
capability's `subscribe` flag is true. Tool parameters are `operation`
(enum = `handler.operations`, required) and `uri`.

**Prompt bridge** (`MCPPromptHandler`):

```python
async def invoke(
    self, operation: str, name: str = "",
    arguments: dict[str, str] | None = None,
) -> ToolOutcome: ...
```

Tool parameters are `operation` (enum `["list", "get"]`, required), `name`, and
`arguments` (object of strings).

**Completion bridge** (`MCPCompletionHandler`):

```python
async def invoke(
    self,
    reference_type: str,   # "prompt" or "resource"
    reference: str,
    argument: dict[str, str],
    context_arguments: dict[str, str] | None = None,
) -> ToolOutcome: ...
```

Required parameters are `reference_type`, `reference`, and `argument`;
`context_arguments` is optional. The handler builds
`{"type": "ref/resource" | "ref/prompt", "uri" | "name": reference}` and calls
`self._client.complete(server, ref, argument, context_arguments)`.

Each bridge Tool's description is the handler method's own `__doc__`.

Unsupported operation/uri/name combinations return
`failed_text("invalid_mcp_resource_request", ...)` or
`failed_text("invalid_mcp_prompt_request", ...)` rather than raising.

## Server status tracking

```python
self._server_status: dict[str, dict[str, JsonValue]]
# {"my-server": {"status": "ready", "tools": 3, "bridges": 1}}
# {"my-server": {"status": "disabled"}}
# {"my-server": {"status": "error", "error": "connection failed"}}
```

`bridges` is computed as `len(registered_names) - len(tools)`, so it counts only
the protocol bridge Tools registered for that server.

## Initialization lifecycle

```
APPLICATION_INITIALIZED → _on_session_init() →
  (returns immediately when self._initialized, or when no servers are configured)
  for each configured server:
    raise ValueError if the server name is empty
    if not server_cfg.enabled → status = {"status": "disabled"}; continue
    try:
      tools = client.connect_and_list(server, server_cfg.model_dump(mode="python"),
                                      callbacks=client_callbacks(...)) →
        stdio_client(params) OR streamable_http_client(url) →
        ClientSession(read, write, read_timeout_seconds=..., **callbacks) →
        session.initialize() → list_tools() → _validate_tool_list()
      register server tools + resource/prompt/completion bridges
      status = {"status": "ready", "tools": N, "bridges": M}
    except Exception:
      _rollback_server(server)
      status = {"status": "error", "error": str(exc)}
      if server_cfg.required → _rollback_all() → re-raise
  _initialized = True

session/close → _on_session_close() → _rollback_all() → _server_status.clear()
  _rollback_all(): for each server in reverse: _rollback_server(server);
                   client.disconnect_all(); _initialized = False
  _rollback_server(server): unregister the server's registered Tools in reverse;
                            client.disconnect(server)
```

Construction order in `connect_and_list` is transport → `ClientSession` →
`session.initialize()` → `_list_tools()`, all inside one `AsyncExitStack` that is
closed if any step raises.

## How `apply()` works

```python
def apply(self, ctx: Context, config: MCPConfig) -> None:
    self._tools = ctx.tools
    self._model = ctx.model
    self._interactions = ctx.interactions
    self._session = ctx.session
    self._usage = ctx.usage
    self._loop_state = ctx.loop_state
    self._config = config
    ctx.dispose(self._dispose)
    ctx.on(APPLICATION_INITIALIZED, self._on_session_init)
    ctx.on(Events.SESSION_CLOSE, self._on_session_close)
```

There is no `config or {}` merge: `apply` takes the validated `MCPConfig` model
and relies on its field defaults. `_dispose` runs `_rollback_all()` and clears
`_server_status`, `_server_tools`, and `_initialized`, so an unload without
`session/close` (dependency restart, plugin reload) never leaves stale Tools
pointing at a disconnected client.

## On-disk artifacts

None directly. Server connections are managed in-memory. Stderr is sent to the
configured `log_path` (opened append-only in UTF-8) when present; otherwise to
the process `sys.stderr` when that stream has a usable `fileno()`, and to
`subprocess.DEVNULL` when it does not.

## Cross-references

- Depends on: `tools`, `model`, `interactions`, `session`, `usage`,
  `loop_state`, `agentloop` (`APPLICATION_INITIALIZED`, `SESSION_CLOSE`), and
  `XBotv2.llm.invoke_llm` for sampling.
- Depended on by: the Agent (MCP Tools appear in the Tool catalog) and the ACP
  carrier, which injects `plugin_configs[MCP_PLUGIN_ID]` for client-requested
  MCP servers.
- Pairs with: `tools` (Tool registration), `interactions` (elicitation
  callback).

## Common pitfalls

- **`required: true` on a failing server**: that server is rolled back, its
  status is recorded as `"error"`, then `_rollback_all()` runs and the original
  exception is re-raised. Servers initialized earlier in the same loop are rolled
  back too. Servers are iterated in configuration order, so earlier servers are
  the ones that get torn down.
- **A server connects but registers no Tools**: `_validate_tool_list` still
  runs over `tools/list`; only a missing `tools` capability short-circuits to an
  empty list.
- **Sampling only accepts text content**: `_sampling_text()` rejects any
  non-`TextContent` block and the callback returns
  `ErrorData(code=-32602, "XBot sampling currently accepts text content only")`.
  An unknown message role returns the same code with
  `"XBot sampling does not support role ..."`.
- **Sampling rejects tool calls**: if any part of the aggregated response is a
  `ToolCall`, the callback returns
  `ErrorData(code=-32603, "Unbound XBot sampling cannot execute tool calls")`.
  A `RuntimeError` raised by `invoke_llm` is also returned as `-32603`.
- **Stdio transport requires `command`**: an empty or missing `command` list
  raises `MCPConnectionError("MCP stdio transport requires a command")`.
- **Tool entries need `name` and an object `inputSchema`**: missing names,
  non-dict schemas, or a schema whose `type` is not `"object"` raise
  `MCPConnectionError` naming the failing entry index.
- **`inputSchema` must validate Draft202012**: an invalid schema raises
  `MCPConnectionError` carrying `SchemaError.message`.
- **Re-registering the same server**: `connect_and_list` raises
  `MCPConnectionError(f"MCP server '{name}' is already connected")`, so a second
  `session/init` on the same client cannot silently duplicate transports.
