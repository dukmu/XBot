# `mcp-plugin`

Model Context Protocol (MCP) server integration — connects to MCP
servers (stdio or HTTP), registers discovered tools, and bridges
resources, prompts, completions, sampling, elicitation, and logging
to XBot runtime capabilities.

- **Import/profile:** `mcp_plugin`, Agent profile.
- **Source:** `XBotv2/mcp_plugin/plugin.py`,
  `XBotv2/mcp_plugin/mcp_client.py`,
  `XBotv2/mcp_plugin/tool.py`,
  `XBotv2/mcp_plugin/callbacks.py`,
  `XBotv2/mcp_plugin/invariants.py`.
- **Injects/provides:** `tools`, `model`, `interactions`,
  `session` → (none directly; registers Tools).
- **Subscribes to events:** `application/initialized` (MCP server
  initialization), `session/close` (cleanup).
- **Config:** `servers: {<name>: {command|url, ...}}`.
- **Plugin ID:** `MCP_PLUGIN_ID = "mcp_plugin"`.

## Public data models

### `MCPPlugin` (`XBotv2/mcp_plugin/plugin.py:75-250`)

```python
class MCPPlugin:
    inject = ["tools", "model", "interactions", "session"]
    name = "mcp_plugin"
    Config = S.object({
        "servers": S.any().optional(),
    })

    def __init__(self) -> None:
        self._client = MCPClient()
        self._config: dict[str, Any] = {}
        self._server_status: dict[str, dict[str, Any]] = {}
        self._server_tools: dict[str, list[str]] = {}
        self._initialized = False

    def apply(self, ctx, config=None) -> None: ...

    async def _on_session_init(self, _event: ApplicationInitialized) -> None:
        """Connect to each configured server, register tools."""

    async def _on_session_close(self, ctx: EventContext) -> None:
        """Rollback all server connections and tools."""

    def _register_server_tools(
        self, server_name: str, tools: list[dict[str, Any]]
    ) -> list[str]: ...

    def _register_tool(self, tool: Tool, server_name: str) -> str:
        """Register one MCP Tool through the public service."""

    def _register_resource_bridge(
        self, server: str, capability: dict[str, Any]
    ) -> str: ...

    def _register_prompt_bridge(self, server: str) -> str: ...

    def _register_completion_bridge(self, server: str) -> str: ...

    async def _rollback_server(self, server_name: str) -> None: ...
    async def _rollback_all(self) -> None: ...
```

### `MCPClient` (`XBotv2/mcp_plugin/mcp_client.py:43-180`)

```python
class MCPClient:
    def __init__(self) -> None:
        self._transports: dict[str, _Connection] = {}
        self._stderr_handles: list[Any] = []

    async def connect_and_list(
        self,
        name: str,
        cfg: dict[str, Any],
        *,
        callbacks: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any]
    ) -> MCPCallResult: ...

    def server_capabilities(self, server: str) -> dict[str, Any]: ...

    async def list_resources(self, server: str) -> dict[str, Any]: ...
    async def read_resource(self, server: str, uri: str) -> dict[str, Any]: ...
    async def subscribe_resource(self, server: str, uri: str) -> dict[str, Any]: ...
    async def unsubscribe_resource(self, server: str, uri: str) -> dict[str, Any]: ...
    async def list_prompts(self, server: str) -> list[dict[str, Any]]: ...
    async def get_prompt(
        self, server: str, name: str, arguments: dict[str, str] | None = None
    ) -> dict[str, Any]: ...
    async def complete(
        self,
        server: str,
        reference: dict[str, Any],
        argument: dict[str, str],
        context_arguments: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...
    async def set_logging_level(self, server: str, level: str) -> dict[str, Any]: ...
    async def ping(self, server: str) -> dict[str, Any]: ...
    async def disconnect_all(self) -> None: ...
    async def disconnect(self, name: str) -> bool: ...
```

### `MCPCallResult`

```python
@dataclass(frozen=True, slots=True)
class MCPCallResult:
    content: str
    is_error: bool
    data: dict[str, Any]
```

### `MCPTool` (`XBotv2/mcp_plugin/tool.py:10-30`)

```python
class MCPTool:
    def __init__(self, client: Any, server: str, tool_def: dict[str, Any]) -> None:
        self._client = client
        self._server = server
        self._name = tool_def["name"]
        self._description = str(tool_def.get("description", ""))
        self._parameters = dict(tool_def["inputSchema"])

    def as_tool(self, registered_name: str) -> Tool:
        return Tool(
            name=registered_name,
            description=self._description,
            function=self,
            parameters=self._parameters,
        )

    async def __call__(self, **kwargs: Any) -> ToolResult: ...
```

### Transport registration names

Each MCP tool is registered with the prefix `mcp__{server_name}__{tool_name}`.
Bridges are registered as:
- `mcp__{server_name}__protocol_resources`
- `mcp__{server_name}__protocol_prompts`
- `mcp__{server_name}__protocol_complete`

Namespaces are `mcp:{server_name}`.

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

### Connection types

- **stdio** (default): uses `StdioServerParameters(command=..., args=..., env=...)`
  with optional `cwd` and `log_path` for stderr capture.
- **remote**: uses `streamable_http_client(url, http_client)` with
  optional `headers` and `terminate_on_close`.

### Transport validation

`_validate_tool_list()` checks:
1. Result is a dict
2. `tools` is a list
3. Each entry has non-empty `name`
4. Each entry has `inputSchema` of type `"object"`
5. `inputSchema` validates against Draft202012

### Callbacks (`XBotv2/mcp_plugin/callbacks.py`)

```python
def client_callbacks(model: Any, interactions: Any, session: Any) -> dict[str, Any]:
    return {
        "sampling_callback": sample,        # MCP sampling → model.astream()
        "elicitation_callback": elicit,    # MCP elicitation → interactions.request_user_input()
        "list_roots_callback": roots,      # MCP roots → workspace URI
        "logging_callback": log_message,   # MCP logging → logger
    }
```

- **sampling_callback**: receives `types.SamplingMessage` → calls
  `model.astream(messages)` → returns `types.CreateMessageResult`.
  Only accepts `TextContent`; rejects tool calls in sampling.
- **elicitation_callback**: receives `types.ElicitRequestURLParams` →
  calls `interactions.request_user_input(question)` → returns
  `types.ElicitResult(action="accept"|"decline", content=...)`.
- **list_roots_callback**: returns `types.ListRootsResult` with a
  single root at `workspace.as_uri()`.
- **logging_callback**: logs at `logger.info()` level.

### Resource, Prompt, Completion bridges

**Resource bridge** (`MCPResourceHandler`):

```python
async def invoke(self, operation: str, uri: str = "") -> ToolResult:
    # operations: "list" (no uri), "read" (uri), "subscribe" (uri), "unsubscribe" (uri)
```

**Prompt bridge** (`MCPPromptHandler`):

```python
async def invoke(
    self, operation: str, name: str = "",
    arguments: dict[str, str] | None = None
) -> ToolResult:
    # operations: "list" (no args), "get" (name + optional arguments)
```

**Completion bridge** (`MCPCompletionHandler`):

```python
async def invoke(
    self,
    reference_type: str,   # "prompt" or "resource"
    reference: str,
    argument: dict[str, str],
    context_arguments: dict[str, str] | None = None,
) -> ToolResult:
    # Calls self._client.complete(server, ref, argument, context_arguments)
    # ref = {"type": "ref/prompt"|"ref/resource", "name"|"uri": reference}
```

### Server status tracking

```python
self._server_status: dict[str, dict[str, Any]]
# {"my-server": {"status": "ready", "tools": 3, "bridges": 1}}
# {"my-server": {"status": "disabled"}}
# {"my-server": {"status": "error", "error": "connection failed"}}
```

## Initialization lifecycle

```
APPLICATION_INITIALIZED → _on_session_init() →
  for each configured server:
    if not enabled → skip (status="disabled")
    try:
      tools = client.connect_and_list(server, cfg, callbacks) →
        stdio_client(params) OR streamable_http_client(url) →
        ClientSession(read, write, callbacks) →
        session.initialize() →
        session.list_tools() → _validate_tool_list()
      register tools + resource/prompt/completion bridges
      status = {"status": "ready", "tools": N, "bridges": N}
    except:
      _rollback_server(server)
      status = {"status": "error", "error": msg}
      if required → _rollback_all() → re-raise

session/close → _on_session_close() → _rollback_all() →
  for each server: rollback_server(server) →
    unregister all tools → client.disconnect(server)
  client.disconnect_all() → _stderr_handles.clear()
  _initialized = False
```

## How `apply()` works

```python
def apply(self, ctx, config=None):
    self._tools = ctx.tools
    self._model = ctx.model
    self._interactions = ctx.interactions
    self._session = ctx.session
    self._config = dict(config or {})
    ctx.dispose(self._dispose)
    ctx.on(APPLICATION_INITIALIZED, self._on_session_init)
    ctx.on(Events.SESSION_CLOSE, self._on_session_close)
```

## On-disk artifacts

None directly. Server connections are managed in-memory. Stderr
logs go to `log_path` (if configured) or subprocess.DEVNULL.

## Cross-rereferences

- Depends on: `tools`, `model`, `interactions`, `session`,
  `agentloop` (`APPLICATION_INITIALIZED`, `SESSION_CLOSE`).
- Depended on by: the Agent (MCP tools appear in the tool catalog).
- Pairs with: `tools` (Tool registration), `interactions`
  (elicitation callback).

## Common pitfalls

- **`required: true` on server initialization failure**: if a
  server with `required: true` fails to connect, `_rollback_all()`
  is called and the error is re-raised. Other servers that
  succeeded are also rolled back.
- **Sampling only accepts text content**: the `sample` callback
  checks `isinstance(block, types.TextContent)` for all message
  blocks. If any block is not text (image, etc.), returns
  `ErrorData(code=-32602, "XBot sampling currently accepts text content only")`.
- **Sampling rejects tool calls**: if `aggregate.tool_calls` is
  non-empty, sampling returns `ErrorData(code=-32603,
  "Unbound XBot sampling cannot execute tool calls")`.
- **Stdio transport requires `command`**: if the `command` key is
  absent or empty for an stdio server, raises
  `MCPConnectionError("MCP stdio transport requires a command")`.
- **Tool names must have `name` field**: `_validate_tool_list()`
  checks `tool.get("name")` for each entry. Missing or falsy
  names raise `MCPConnectionError`.
- **`inputSchema` must validate Draft202012**: invalid schemas
  raise `MCPConnectionError` with the `SchemaError.message`.
