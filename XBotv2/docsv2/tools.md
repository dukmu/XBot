# Built-in Tools

Core registers these tools without plugins:

| Tool | Execution | Purpose |
|---|---|---|
| `shell` | sandboxed, sequential | Run a command |
| `filesystem_read` | sandboxed, sequential | Read UTF-8 text with metadata |
| `filesystem_write` | sandboxed, sequential | Write, patch, or replace text |
| `filesystem_list` | sandboxed, sequential | List directory entries |
| `search_text` | sandboxed, sequential | Search UTF-8 text by regular expression |
| `find_files` | sandboxed, sequential | Find files by glob |
| `send_message` | host, sequential | Emit a non-blocking client message |
| `ask_user` | host, sequential | Wait for client input |

`SystemConfig.tools` may narrow this registry after plugin initialization. The
shipped configuration keeps both client-interaction tools visible so an agent
can send progress and ask for missing information without a custom tool list.

Tools return `ToolResult`. It separates model-visible text from structured data,
errors, artifacts, and client events. The dispatcher honors each registry
entry's `sandbox_mode`; host tools are never injected with a sandbox backend.

`ToolResult.content` enters model history. `data`, `error`, and `artifacts` are
preserved on the runtime tool message and emitted as optional fields on the
client-visible `tool_result` event. Client events are emitted separately in
their original order. `ToolError`, `ArtifactRef`, and `ClientEvent` expose
`to_dict()` for this boundary conversion.

Dictionary-returning external tools are normalized at the same boundary for
`data`, `error`, `artifact`/`artifacts`, and `events`. New built-ins and plugin
templates should return `ToolResult` directly.

Tool results larger than 12,000 characters are stored under the session's
`state/artifacts/tool_results` directory before history persistence and SSE
emission. The model receives a bounded preview plus an absolute `cache_path`.
That path is readable through `filesystem_read`; callers should use `offset`
and `limit` to inspect only the required lines. Session data remains read-only
to sandboxed tools, and cached-result metadata survives session restoration.

Filesystem write modes have the same semantics with or without the session
sandbox. Successful writes retain mode-specific metadata such as `changed` and
`replacements`; read/write failures retain their structured `data` and `error`
instead of exposing sandbox process output as an untyped string.

Disabling the session sandbox is an explicit policy choice. Permission checks
still run before every tool call.

`ask_user` is itself a tool call, so a restrictive permission policy may emit
and resolve `permission_request` before the tool emits
`user_input_required`. Clients must support both interactions on the same SSE
turn; answering the question does not bypass tool authorization.

Registered tools use one canonical string name:

- builtin core tools keep bare keys such as `shell`;
- plugin setup tools use keys such as `plugin:skills:skill`;
- discovered skills use keys such as `skills:global:find-skills`;
- MCP tools use keys such as `mcp:github:mcp__github__search`.

Canonical names and provider-visible tool names are unique.
`ToolRegistry.register()` returns the registered name and rejects either form
of duplication before changing the registry. Explicit replacement is not part
of the registration contract; callers must unregister the current owner first.

Command discovery includes the canonical `registered_name` and registration
`namespace` for tool, skill, and MCP commands. The existing `name` and `slash`
fields remain display and invocation values.

The dispatcher executes a tool batch sequentially. Registration exposes no
parallel or lock metadata because the runtime has no corresponding guarantee.
Any future parallel scheduler must define ordering, Hook concurrency,
interaction serialization, and lock-key behavior before adding public options.
