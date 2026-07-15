# Built-in Tools

Core registers these tools without plugins:

| Tool | Execution | Purpose |
|---|---|---|
| `shell` | session runtime | Run a foreground command or start one with `background=true` |
| `filesystem_read` | sandboxed, sequential | Read UTF-8 text with metadata |
| `filesystem_write` | sandboxed, sequential | Write, patch, or replace text |
| `filesystem_list` | sandboxed, sequential | List directory entries |
| `search_text` | sandboxed, sequential | Search UTF-8 text by regular expression |
| `find_files` | sandboxed, sequential | Find files by glob |
| `send_message` | host, sequential | Emit a non-blocking client message |
| `ask_user` | host, sequential | Wait for client input |
| `list_tasks` | session runtime | List tasks or read one full result |
| `stop_task` | session runtime | Stop one background task |

Background tasks are runtime-only and end with the live session. They emit
bounded previews through `task_updated`; `list_tasks(task_id)` returns the
captured output through the normal ToolResult cache boundary. Shell capture is
bounded at 100,000 bytes and marks truncation explicitly. Task completion enters
the runtime mailbox as a general message, so the Agent can react while the
client is connected without polling.

`shell(background=true)` uses the same canonical Tool name, command arguments,
Hooks, and permission rules as foreground shell execution. Background mode is
not a permission alias or a second execution path around `shell` policy.

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
emission. The model receives a bounded preview plus a `cache_path` relative to
the current session state, such as `session/artifacts/tool_results/<file>`. That
path is readable through the filesystem read, list, search, and find tools;
callers should use `offset` and `limit` to inspect only the required lines. The
single read-only `session/` namespace maps the current session state directory;
other relative paths remain workspace-relative. It is intentionally not a
general virtual filesystem. Policy updates preserve the mount, and cached-result
metadata survives restoration.

Provider-bound context uses the same 12,000-character boundary for message
content, string values inside historical ToolCall arguments, and assistant
reasoning content. Oversized values are stored under
`session/artifacts/context/`; only a beginning/ending preview, digest, size, and
session-relative `cache_path` are sent to the provider. This projection does
not mutate persisted messages, so resume retains the exact original input.
History compaction remains responsible for semantic summaries across many
messages; context caching is deterministic externalization, not a second model
summarizer.

Filesystem write modes have the same semantics with or without the session
sandbox. Successful writes retain mode-specific metadata such as `changed` and
`replacements`; read/write failures retain their structured `data` and `error`
instead of exposing sandbox process output as an untyped string.

Disabling the session sandbox is an explicit policy choice. Permission checks
still run before every tool call.

The shipped permission policy pre-approves internal state tools, client
interaction tools, and workspace filesystem tools. Shell commands, discovered
Skills, MCP tools, and unknown tools remain subject to explicit policy. The
sandbox implicitly mounts only the workspace (read-write), the current session
state (read-only, exposed through relative `session/...` cache paths), and the
minimal system files required to execute commands. Other paths require an
explicit sandbox `resources` entry; the complete data directory is not added as
a separate mount. Keep the runtime data directory outside the workspace when
session-to-session filesystem isolation is required.

`ask_user` is itself a tool call, so a restrictive permission policy may emit
and resolve `permission_request` before the tool emits
`user_input_required`. Clients must support both interactions on the same SSE
turn; answering the question does not bypass tool authorization.

Registered tools use one canonical string name:

- builtin core tools keep bare keys such as `shell`;
- plugin setup tools use keys such as `plugin:goal:goal`;
- discovered skills use keys such as `skills:global:find-skills`;
- MCP tools use keys such as `mcp:github:mcp__github__search`.

Canonical names and provider-visible tool names are unique.
`ToolRegistry.register()` returns the registered name and rejects either form
of duplication before changing the registry. Explicit replacement is not part
of the registration contract; callers must unregister the current owner first.

Tools are Agent-facing structured capabilities. They are not server commands or
prompt expansions, and Tool registration has no command metadata. A plugin may
register a separate human command that reuses its private business methods, but
the command dispatcher never constructs or executes a Tool call.

`model_visible=False` removes a Tool from provider schemas and model execution
lookup. It does not create a hidden command surface.

The dispatcher executes a tool batch sequentially. Registration exposes no
parallel or lock metadata because the runtime has no corresponding guarantee.
Any future parallel scheduler must define ordering, Hook concurrency,
interaction serialization, and lock-key behavior before adding public options.

Synchronous tool functions run through `asyncio.to_thread` and do not block
streaming. A timed-out Python function cannot be killed inside its worker
thread and may finish later. Cancellable long-lived work should use background
shell tasks, whose process groups are owned and stopped by the live session.
