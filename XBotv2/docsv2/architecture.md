# XBotv2 Architecture

## Overview

XBotv2 is a plugin-extensible AI agent runtime with a minimal core ReAct loop.
The core owns provider calls, tool execution, permissions, sandboxing, protocol
streaming, and append-only persistence. Advanced concepts such as planning,
compaction, skills, memory, and subagents remain plugin responsibilities.

## Architecture Principle

```text
Plugins -> import -> Core (xbotv2)
Core -> never imports -> Plugins (builtin_plugins)
```

Core defines interfaces and bootstrap wires configured plugins at runtime via
manifests. Passing `plugin_dirs=[]` explicitly disables plugin discovery and is
the pure-core test mode. The CLI exposes the same boundary with `--no-plugins`.

## Stage 2 Runtime Model

Stage 2 removes the old multi-personality and internal session-workspace model.

Runtime identity is:

```yaml
session_id: generated-or-explicit
thread_id: agent
workspace_root: /actual/project/root
provider: current-provider
```

Configuration sources are:

```text
data/config/system.yaml
data/config/providers.yaml
data/config/permissions.yaml
data/config/sandbox.yaml
<workspace_root>/AGENTS.md
```

No runtime path reads `data/personalities/*`. No runtime creates
`sessions/<sid>/workspace`.

## Core Components

### Engine (`xbotv2/core/engine.py`)

- Minimal 3-node loop: `prepare_context -> agent -> tools -> repeat`.
- Runs hooks around lifecycle, context, model, permission, tool, and persistence
  stages.
- Records `workspace_attached` for the external workspace root.
- Saves provider-facing message history separately from server command results.
- Can stream live interaction events for user input and permission/sandbox
  decisions.

### HTTP Session Runtime (`xbotv2/protocol/http_server.py`)

- One HTTP server can host many sessions with different `workspace_root` values.
- `mode="new"` is the default and creates a generated session id when omitted.
- `mode="resume"` requires an existing session id and returns not found if the
  session state is missing.
- Each session has its own engine, provider, workspace root, turn lock, and live
  command overlays.

### Configuration (`xbotv2/config/`)

- `SystemConfig` describes the general agent prompt, tool selectors, hooks,
  memory, permissions, and sandbox defaults.
- `load_system_config()` merges `system.yaml`, `permissions.yaml`,
  `sandbox.yaml`, and workspace `AGENTS.md`.
- `load_provider_config()` reads `providers.yaml`; provider config is independent
  of system prompt and session state.
- `load_provider_names()` exposes configured provider names for `/provider list`.

### Workspace Model

- `workspace_root` defaults to the process cwd or `--workspace`.
- Shell and filesystem tools resolve relative paths against `workspace_root`.
- Session persistence remains under `data/sessions/<session_id>/state`.
- Sandbox policy controls access inside and outside the workspace.

### Hook System (`xbotv2/hooks/`)

- Core hook stages cover session lifecycle, turn lifecycle, user intake, context,
  model request/response, permissions, tools, client events, and persistence.
- System config may register hooks with `hooks:` entries using `module:function`
  targets; broken targets fail during bootstrap.
- Loop hooks can short-circuit. Unstructured short-circuit values fail closed
  rather than silently continuing.
- Hook context uses `SessionInfo(session_id, thread_id, workspace_root, provider)`.

### Plugin System (`xbotv2/plugin/`)

- Plugins are declared via `plugin.yaml` manifests.
- Plugins register hooks, tools, and prompt fragments.
- Each plugin owns isolated state via `PluginStore`.
- Plugin load is rollback-safe: failed registration removes newly added hooks,
  tools, fragments, and temporary import paths.

### Tool System (`xbotv2/tools/`)

- `ToolRegistry` stores sandbox mode, execution mode, and lock metadata.
- System `tools:` selectors restrict visible and executable tools after core and
  plugin tools are registered.
- `PermissionSystem` applies deny -> allow -> ask -> default precedence.
- `SandboxPolicy` enforces path access with Stage 2 defaults:

```yaml
external_read: ask
external_write: deny
workspace_read: allow
workspace_write: allow
```

- External read attempts emit permission requests and fail closed until a live
  approval allows the current call.
- Workspace symlink escapes remain denied.
- Oversized tool results are cached under state artifacts and replaced with a
  bounded preview before they enter history.

### Built-in Tools (`xbotv2/core/builtin_tools/`)

- `filesystem_read`, `filesystem_write`, `filesystem_list` return structured JSON
  text and operate relative to the session workspace root.
- `shell` runs commands with cwd set to `workspace_root` unless explicitly
  provided.
- `send_message` emits non-blocking client notices.
- `ask_user` emits `user_input_required` and waits for a live client response.

### Persistence (`xbotv2/persistence/`)

- `events.jsonl` is the append-only event log while Stage 3 migration is in progress.
- `messages.jsonl` stores provider-facing LangChain messages for resume.
- No separate `state.yaml` file is written; remaining compatibility paths derive
  a snapshot directly from logs and plugin state.
- Server command results are returned to clients only and are not appended to LLM
  message history.

### Protocol And TUI

- Production TUI transport is HTTP + SSE using FastAPI/httpx.
- `GET /commands` and session command endpoints expose server-owned command
  metadata and execution.
- Local TUI commands are `/exit`, `/clear`, and `/help`.
- Server commands are `/status`, `/provider`, `/permission`, and `/sandbox`.
- Textual and curses clients interact through the transport/session boundary and
  do not import runtime engine modules.

## Derived State Snapshot

The temporary compatibility snapshot is intentionally minimal:

```yaml
schema_version: 2
session_id: string
thread_id: string
workspace_root: string
provider: string
turn_count: integer
event_count: integer
message_count: integer
status: active | error | interrupted | closed
mailbox_pending: integer
pending_interactions: []
permission_overrides: {}
sandbox_overrides: {}
workspace: {}
plugin_states: {}
artifacts_root: string
```

No DAG, plan, task mode, skills, or compaction fields live in core state.

## Data Layout

Checked-in data config uses Stage 2 layout:

```text
data/config/user.yaml
data/config/system.yaml
data/config/providers.yaml
data/config/permissions.yaml
data/config/sandbox.yaml
```

Runtime session/log output is ignored. Config files stay trackable.
