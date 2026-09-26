# `coretools`

The Agent-profile `coretools` component registers filesystem and shell Tools
through the shared Tool registry. Large-text externalization is owned by the
separate `content_cache` component.

- **Source:** `XBotv2/coretools/plugin.py`, `filesystem.py`, `shell.py`,
  `contracts.py`.
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
Jobs and captured output are runtime-owned and do not survive session
shutdown. `read_shell` pages output by character cursor; `wait_shell` returns
job status/exit information, not output.

Requesting `sandbox_permissions="require_escalated"` asks to run outside the
sandbox. This is not an approval bypass: the permissions layer must approve
it. If the optional approval layer is absent, the call fails closed.

## Configuration and workspace extensions

`CoreToolsConfig` has three fields: `enabled_tools` (defaults to all registered
tools), `hooks` (`HookConfig(stage, target)`), and `workspace_tools`
(`WorkspaceToolConfig(target)`). A target uses `source:export` syntax. Hook
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
- See [content cache](content-cache.md), [permissions](permissions.md),
  [sandbox](sandbox.md), and [jobs](jobs.md) for their respective contracts.
