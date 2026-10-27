# `coretools`

The Agent-profile `coretools` component registers filesystem and shell Tools
through the shared Tool registry. Large-text externalization is owned by the
separate `content_cache` component.

- **Source:** `coretools/impl/` and `coretools/api/models.py`.
- **Requires:** `tools`, `session`, `artifacts`, `sandbox`, `jobs`,
  `workspace_root`; `permissions` is optional and is resolved lazily for
  shell escalation.
- **Provides:** registered Tools; no service.
- **Hooks:** workspace-declared callbacks at their declared Agent-loop stage.

## Registered Tools

`filesystem_tools()` registers `read`, `edit`, `path`, and `search`;
`shell_tools()` registers `shell`, `list_shells`, `wait_shell`, `read_shell`,
and `cancel_shell`. Handlers return the shared `ToolOutcome` union
(`ToolSucceeded`, `ToolFailed`, `ToolDenied`, or `ToolCancelled`), not the
retired `ToolResult` shape.

`read(path, mode="utf8", ...)` supports bounded text reads, binary content,
metadata (`stat`), model-visible media (`media`), and bounded directory
listing (`list`). Media input accepts exactly one of `path`, `url`, or `data`;
remote reads require network capability. `edit` supports `write`, exact-text
`replace`, and unified-diff `patch`. `path` supports `move`, `copy`, `delete`,
and `mkdir`; `search` supports content and filename search.

`edit` uses a previously read snapshot to detect external changes. For
`replace`, the old text must match; ambiguous replacement fails unless
`replace_all=True`. Filesystem access remains subject to the sandbox.

The `shell` Tool runs a command in the session workspace by default. A
foreground call returns its result; `background=True` starts a session-owned
job. The four `*_shell` helpers list, wait for, read, or cancel those jobs.
Job identities are runtime-owned and do not survive session shutdown. Captured
bytes are immutable artifacts. Commands write combined stdout/stderr directly
to a controlled spool from process launch, including sandboxed commands.
Completion adopts and hashes that file without a full read/decode/encode copy.
Foreground results carry the complete logical artifact reference and at most
6000 head bytes plus 2000 tail bytes of preview. Invalid UTF-8 is replaced only
for display; artifact bytes are unchanged.

Background terminal results retain only the artifact reference and exit code.
Nonzero exits remain failures; foreground failures and failed background jobs
retain readable output. Timeout kills and reaps the process group and retains
its partial output as a failed result. Cancellation kills and reaps the group,
then removes its unpublished spool. `read_shell` reads at a byte cursor with
`max_bytes` between 1 and 65536 (default 8000), and never rereads the entire
artifact. A page may split a UTF-8 sequence; its display uses replacement
characters, while cursor advancement counts original bytes. The capture is
combined stdout/stderr; the stream selector does not provide separate captures.
`wait_shell` returns status and exit information, not output.

Requesting `sandbox_permissions="require_escalated"` asks to run outside the
sandbox. This is not an approval bypass: the permissions layer must approve
it. If the optional approval layer is absent, the call fails closed.

## Configuration and workspace extensions

`CoreToolsConfig` defaults to `shell_max_timeout_seconds=120`,
`tool_timeout_seconds=60`, no per-tool timeout overrides, all tools enabled,
and empty hook and workspace Tool lists. Hooks use `HookConfig(stage, target)`;
workspace Tools use `WorkspaceToolConfig(target)`. A target uses
`source:export` syntax. Hook
targets may name an importable module or a script under `.xbot/hooks/`;
workspace Tool targets must stay under `.xbot/tools/` and export one `Tool` or
a sequence of `Tool` values. These declarations are loaded at application
composition time. Hook event payloads are producer-owned typed values; consult
the event definition for the selected stage rather than assuming a universal
context dictionary.

There is no `ToolResultCacheHook`, `AFTER_TOOLS` result-cache contract, or
`tool_results` configuration here. Oversized accepted user input and Tool
execution output are externalized by `content_cache` using configured policy
and the artifact store.

## Boundaries

- Tool calls use the standard registry, guard, and execution path.
- Filesystem and process capabilities are bounded by `sandbox`; permissions
  adds allow/deny and approval decisions but does not replace sandbox policy.
- Background shell jobs belong to the active session and are not durable
  history.
- See [permissions](../../permissions/README.md) and [sandbox](../../sandbox/README.md) for their contracts. Physical filesystem operations and the sandbox worker entry point are exposed by `core.api.filesystem`; coretools owns the tool-to-operation mapping in `coretools.api.filesystem`.

For example, an Agent can call `read` with `{"path":"README.md"}` or `shell` with `{"command":"git status"}` through the normal Tool registry and guards.

`read` recognizes managed logical paths such as
`artifact:records/<selection-hash>` and earlier-process cache paths. Permission
path scopes and sandbox checks use the same current physical target as the
read itself; a logical ref cannot gain workspace-path authorization and then
silently read a different data/cache target. The handler pins/rebuilds that
view for the complete operation and still executes through ordinary registry,
guards and sandbox filesystem I/O.

Background shell startup explicitly takes independent use rights for the
current request's exposed paths. Commands are opaque and remain unchanged, so
this transfer conservatively covers those request paths. The runner keeps the
rights until process completion/reaping and releases them on terminal failure
or cancellation, including cancellation before its coroutine starts. The job
spec/result and completion notifications contain no resource leases.

Immutable managed reads do not populate the workspace edit-version observation
map. Their identity and read-only roots own integrity/lifetime, so evicted cache
paths do not accumulate as stale optimistic-edit metadata.
