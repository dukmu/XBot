# Built-in Tools

Core registers these tools without plugins:

| Tool | Execution | Purpose |
|---|---|---|
| `shell` | session runtime | Run a foreground command or start one with `background=true` |
| `filesystem_read` | sandboxed, sequential | Read bounded UTF-8 text or return non-text metadata |
| `content_read` | sandboxed, sequential | Read image content as a model-visible image part |
| `filesystem_stat` | sandboxed, sequential | Inspect file, symlink, hash, MIME, and image metadata |
| `filesystem_list` | sandboxed, sequential | List bounded directory entries |
| `search_text` | sandboxed, sequential | Search UTF-8 text with structured locations |
| `find_files` | sandboxed, sequential | Find bounded paths by glob |
| `filesystem_write` | sandboxed, sequential | Atomically create or replace UTF-8 text |
| `filesystem_edit` | sandboxed, sequential | Atomically replace exact text |
| `filesystem_patch` | sandboxed, sequential | Apply a validated single-file unified diff |
| `filesystem_move` | sandboxed, sequential | Move or rename a path |
| `filesystem_copy` | sandboxed, sequential | Copy a path without decoding content |
| `filesystem_delete` | sandboxed, sequential | Delete a path |
| `filesystem_mkdir` | sandboxed, sequential | Create an empty directory |
| `send_message` | host, sequential | Emit a non-blocking client message |
| `ask_user` | host, sequential | Wait for client input |
| `request_permission` | host, sequential | Request an exact-tool, parameter-regex permission rule |
| `start_shell` | session runtime | Start a background shell and return its job ID |
| `list_shells` | session runtime | List background shells with lightweight metadata |
| `wait_shell` | session runtime | Wait for background shells and return status/exit codes |
| `read_shell` | session runtime | Read captured background shell output (bounded, cursor-based) |
| `cancel_shell` | session runtime | Cancel one background shell job |

The built-in Agents plugin adds `spawn_subagent`, `list_subagents`,
`wait_subagent`, `read_subagent`, and `cancel_subagent`. Subagent and background
shell jobs share one unified `JobRegistry` lifecycle; the generic `task`/`job`
vocabulary is never exposed to the model.

Background shell and subagent jobs are runtime-only and end with the live
session. They emit bounded previews through `task_updated`. Output is stored in
the job and is never included in list/wait responses; the Agent reads it through
the explicit `read_shell`/`read_subagent` tools, which are the only tools that
return bulk text and each bound the returned characters. A completed subagent
or background shell stages a runtime notice in the agent inbox, so the
Agent can react on the next turn without polling. Starting a job
confirms only that it was accepted. The Agent uses `wait_shell(ids)` /
`wait_subagent(ids)` when subsequent work depends on completion; cancelling a
wait does not stop the job — `cancel_shell`/`cancel_subagent` own that
operation.

Foreground Shell execution has no default time limit. It waits for process
completion and is terminated when the current turn is cancelled. Use
`start_shell` when other Agent work should continue before the command
finishes, not as a workaround for a fixed foreground timeout.

`start_shell` uses the same canonical command arguments, Hooks, and permission
rules as foreground Shell execution. Background mode is not a permission alias
or a second execution path around `shell` policy. An escalated background shell
(`sandbox_permissions=require_escalated`) requests the same human approval as
an escalated foreground command before it is started; a denied request creates
no background job.

`RuntimeConfig.tools` may narrow this registry after plugin initialization. The
shipped configuration keeps both client-interaction tools visible so an agent
can send progress and ask for missing information without a custom tool list.

Tools return `ToolResult`. It separates model-visible text from structured data,
errors, artifacts, and client events. The dispatcher honors each registry
entry's `sandbox_mode`; host tools are never injected with a sandbox backend.

Provider-visible built-ins use their function docstring as the single source of
Tool guidance. The description covers intended use, limits, result behavior,
and failure behavior, and is passed intact as the Tool description. Parameter
schemas come only from the Python signature and type annotations; `Literal`
annotations become JSON Schema enums. XBot does not parse or reinterpret
docstring sections. Core validates final Hook-transformed arguments against that
schema before permissions or execution. Invalid model arguments return a
structured Tool error for correction and never reach SSE.

Model requests retry at most once for a connection error, timeout, 429, or 5xx,
and only before any content or Tool-call delta was emitted. The client receives
a warning through the existing `client_message` event. Schema/history 400s and
requests that already produced output are not retried.

`ToolResult.content` is the complete model-visible business result and may be
empty when a successful operation has no output. The common message formatter
represents that outcome as a minimal structured Tool result; individual tools
must not invent explanatory text. `data` is a runtime, persistence, and client
sidecar and is never copied into model content. A tool that needs the model to
consume structured output must render that information in `content` explicitly.
`data`, `error`, and `artifacts` remain on the runtime tool message and are
emitted as optional fields on the client-visible `tool_result` event. Errors
and artifact references are also included in the model-visible result because
they affect the Agent's next action. Client events are emitted separately in
their original order. `ToolError`, `ArtifactRef`, and `ClientEvent` expose
`to_dict()` for this boundary conversion.

Dictionary-returning external tools are normalized at the same boundary for
`data`, `error`, `artifact`/`artifacts`, and `events`. New built-ins and plugin
templates should return `ToolResult` directly.

`filesystem_read` never attaches image bytes to provider messages. Use
`content_read` for model-visible image input. It accepts exactly one of a local
path, an `http`/`https` URL, or base64 image data (optionally as a
`data:image/*;base64,` URL). Supported types are GIF, JPEG, PNG, and WebP; the
bytes are stored under `session/artifacts/media/` and sent to image-capable
providers as a native image block. `filesystem_stat` still reports recognized
image dimensions for discovery.

Tool results larger than `tool_results.max_inline_chars` (12,000 by default) are
stored under the session's `state/artifacts/tool_results` directory before
history persistence and SSE emission. `tool_results.preview_chars` controls the
bounded beginning and ending preview (4,000 characters by default). Both are
global settings in `data/config/config.yaml`, and the preview may not
exceed the inline threshold. The model receives the preview plus a `cache_path` relative to
the current session state, such as `session/artifacts/tool_results/<file>`. That
path is readable through the filesystem read, list, search, and find tools;
callers should use `offset`, `char_offset`, `limit`, and `max_chars` to inspect
only the required range, including long single-line artifacts.
Cached Tool results preserve their original representation. String results and
explicit original text payloads, such as the `content` returned by
`filesystem_read`, are stored verbatim in a `.txt` artifact without JSON
encoding or escaped lines. Only an original object or array is serialized as
JSON. A string that already contains JSON text remains that exact string and is
not encoded a second time. The cached value becomes a relative artifact
reference instead of being duplicated in history and SSE. The
single read-only `session/` namespace maps the current session state directory;
other relative paths remain workspace-relative. It is intentionally not a
general virtual filesystem. Policy updates preserve the mount, and cached-result
metadata survives restoration.

Provider-bound context uses a 48,000-character boundary for user messages and a
12,000-character boundary for assistant content, Tool results, and assistant
reasoning content. Model-authored ToolCall arguments are passed through
unchanged. Oversized values are stored under
`session/artifacts/context/`; only a beginning/ending preview, digest, size, and
session-relative `cache_path` are sent to the provider. This projection does
not mutate persisted messages, so resume retains the exact original input. The
marker tells the Agent to inspect omitted sections with bounded
`filesystem_read` calls before acting when needed.
History compaction remains responsible for semantic summaries across many
messages; context caching is deterministic externalization, not a second model
summarizer.

Filesystem operations use one implementation with or without bwrap and write
atomically. The runtime records the version of each file returned by read or
stat. A later write, edit, or patch is guarded against external changes without
exposing hashes in the Agent-facing Tool schema. A repeated read reports when
the file changed since its previous observation. Non-text reads return MIME,
size, hash, and recognized image metadata without placing binary content in
context. Text must be valid UTF-8
and must not contain binary control data. Existing files are read before
mutation, complete content is sent only to `filesystem_write`, and relevant
ranges are read again before a change is reported as verified.

Disabling the session sandbox is an explicit policy choice. Permission checks
still run before every tool call.

Bubblewrap inherits the environment of the XBot process by default, including
`PATH`, `HOME`, provider variables, proxy settings, and active
virtual-environment variables. It mounts the complete host filesystem read-only,
then overlays the workspace, `/tmp`, and explicitly configured resources with
their requested access. This keeps interpreters, libraries, certificates, and
user configuration readable without hard-coded installation or home paths.
Home caches remain read-only unless their actual runtime path is explicitly
configured as a writable resource. The data directory is reapplied read-only;
an environment located inside a writable workspace remains writable according
to the workspace policy. Filesystem Tool permissions remain separate from the
mount policy.

Foreground `shell` calls have no default duration limit and remain cancellable
with the active turn. Everything inside a writable workspace, including a
Python environment, is writable. Paths outside the workspace are read-only by
default because arbitrary shell text is not parsed for paths. A command that
genuinely requires external writes must set
`sandbox_permissions=require_escalated` with a justification. This checks the
normal `external_write` allow, deny, or ask policy before starting the process.
An approved command runs on the host, outside bwrap; this applies equally to
foreground calls and `start_shell`. A denied request creates no process.
The default `use_default` mode always retains the configured sandbox.

An allow-once decision applies only to that invocation. A session decision
updates the session's `external_write` overlay, so later explicit escalation
requests follow that decision. The Agent must still
set `sandbox_permissions=require_escalated`; ordinary shell calls remain
sandboxed. Foreground commands run until completion or turn cancellation.
Background commands return a job ID after authorization and remain manageable
through `list_shells`, `wait_shell`, `read_shell`, and `cancel_shell`.

A session-scoped approval for a mutating filesystem tool records only its Tool
name, source/destination paths, and destructive flags such as `recursive` or
`overwrite`. Content, replacement text, and patch bodies are never persisted
in permission rules.

The shipped permission policy pre-approves internal state tools, client
interaction tools, shell, and workspace filesystem operations. The special
permission scope `paths: ${workspace}` matches only when every path argument of
a filesystem call resolves inside the active workspace; external mutations
still require an explicit decision. Any exact built-in directory reference has
the same directory-tree semantics. Other `paths` values are full-match regular
expressions over resolved absolute paths and may embed runtime variables.
Discovered Skills, MCP tools, and unknown tools remain subject to explicit
policy. The workspace mount follows
`workspace_read` and `workspace_write`; current session state remains read-only
through relative `session/...` paths. External `ask` paths use the normal
ordered permission interaction and record only the approved path. For atomic
filesystem mutations, the trusted filesystem worker receives a temporary parent
directory mount for that call; shell commands do not inherit it. The complete
data directory is visible but read-only inside shell sandboxes.

`ask_user` is itself a tool call, so a restrictive permission policy may emit
and resolve `permission_request` before the tool emits
`user_input_required`. Clients must support both interactions on the same SSE
turn; answering the question does not bypass tool authorization.
Its required choices are structured `{label, description}` objects. Empty
questions, empty choices, fewer than two choices, and non-positive timeouts are
rejected by the Tool schema before an interaction is opened. A timeout,
cancellation, or unsupported live client is not reported as
a successful Tool result.

`request_permission` accepts the complete model-visible Tool name, a mapping of
parameter names to full-match regular expressions, and a reason. The Tool name
is treated literally, not as a regular expression. It never constructs or
executes a target ToolCall. An allow-once response installs a rule consumed by
the next matching call; an allow-session response uses normal session policy
persistence. Explicit deny rules and sandbox checks still take precedence.
Non-interactive runtimes do not expose this Tool.

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
