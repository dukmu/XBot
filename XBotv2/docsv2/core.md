# Core Engine

## ReAct Loop

The core engine implements a minimal ReAct loop:

```text
prepare_context -> agent -> tools -> repeat
                         \-> end when no tool calls
```

Hooks run around each stage. Loop hooks can short-circuit with structured
results; invalid short-circuits fail closed.

## Bootstrap

`bootstrap()` builds one engine for one session:

1. Validate runtime identifiers.
2. Resolve `workspace_root` from `--workspace` or process cwd.
3. Load system config from `data/config/system.yaml` and workspace `AGENTS.md`.
4. Load provider config from `data/config/providers.yaml`.
5. Merge global and session permission/sandbox policy.
6. Create `CoreStateStore` under `data/sessions/<sid>/state`.
7. Register core tools and configured hooks.
8. Load plugins unless `plugin_dirs=[]` or `--no-plugins` is used.
9. Create the LLM client or use a test override.
10. Run `ON_SESSION_INIT` and return `Engine`.

No personality id or internal session workspace is part of bootstrap.

## Configuration

Core reads these files:

```text
data/config/system.yaml
data/config/providers.yaml
data/config/permissions.yaml
data/config/sandbox.yaml
data/config/user.yaml
```

Workspace `AGENTS.md` is optional. If present, it is appended to system
instructions and sent to the provider as part of the system context.

## Workspace

The workspace is external and real:

- Default: process current working directory.
- Override: `--workspace PATH` or HTTP `workspace_root`.
- Shell cwd defaults to `workspace_root`.
- Filesystem paths resolve relative to `workspace_root`.
- Session state persists separately under `data/sessions/<sid>/state`.

## Permissions

Permission decisions use `allow`, `deny`, and `ask`.

Effective policy is:

```text
built-in defaults -> global config -> session overrides -> one-shot live decision
```

During an active turn, `ask` emits `permission_request` and waits for a live
approval. Allow continues the current tool call. Deny, timeout, disconnect, or
non-live execution fails closed.

`/permission set <tool> <allow|deny|ask>` updates the active session override and
the in-memory permission system for that session. `/permission reset` removes the
active override and rebuilds the active policy.

## Sandbox

Default sandbox behavior:

```yaml
enabled: true
external_read: ask
external_write: deny
workspace_read: allow
workspace_write: allow
```

External read attempts request approval through the same live permission flow.
External writes are denied. Workspace symlink escapes are denied even when
workspace access is otherwise allowed.

`/sandbox set <key> <allow|readwrite|readonly|deny|ask>` updates the active
session override and the in-memory sandbox policy for that session. `/sandbox
reset` removes the active override and rebuilds the active policy.

## Built-In Tools

- `filesystem_read`: structured JSON with content, metadata, and truncation info.
- `filesystem_write`: overwrite, append, prepend, insert line, replace lines,
  regex replace, and single-file unified diff patch modes.
- `filesystem_list`: structured JSON directory listing.
- `shell`: executes with cwd set to `workspace_root` unless explicit cwd is
  provided.
- `send_message`: emits a non-blocking client message event.
- `ask_user`: emits `user_input_required` and waits for live client input.

## Context Building

Context is assembled from:

```text
system prompt
system instructions + AGENTS.md
runtime rules
sandbox summary
plugin fragments
sanitized message history
current derived state snapshot
```

The builder memoizes stable prefixes and invalidates on fragment/config changes.

## Persistence

`CoreStateStore` manages:

- `events.jsonl`: append-only source of truth.
- `messages.jsonl`: provider-facing message history for resume.
- `plugin_states/`: plugin-owned opaque blobs.
- `artifacts/`: cached large tool outputs.

No separate `state.yaml` file is written. Remaining compatibility paths derive a
snapshot with session metadata, counts, status, pending interactions, latest
workspace attachment, plugin states, and artifact root.

Provider-facing history does not include server command results.

## Events

Important core events:

- `workspace_attached`
- `turn_started`, `turn_finished`, `turn_cancelled`
- `session_closed`
- `assistant_message`, `tool_result`, `tool_result_cached`
- `client_message`, `user_input_required`, `user_input_response`,
  `user_input_cancelled`
- `permission_request`, `permission_response`, `permission_denied`
- `provider_switched`
- `permission_override_set`, `permission_overrides_reset`
- `sandbox_override_set`, `sandbox_overrides_reset`

Status is derived from ordered events. A later `turn_started` reactivates prior
`error` or `interrupted` state; `turn_finished` does not hide an interruption
raised during the same turn.
