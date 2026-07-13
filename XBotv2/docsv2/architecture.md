# XBotv2 Architecture

## Overview

XBotv2 is a plugin-extensible AI agent runtime with a minimal core ReAct loop.
The core owns provider calls, tool execution, permissions, sandboxing (BubblewrapBackend),
protocol streaming (HTTP/SSE + Unix domain socket), and append-only persistence
(`messages.jsonl`). Skills, MCP tools, and plugin extensions live in `builtin_plugins/`.

## Architecture Principle

```text
Plugins -> import -> Stable API (xbotv2.api)
Core -> never imports -> Plugins (builtin_plugins)
```

Core defines interfaces; bootstrap wires plugins at runtime via manifests.
`plugin_dirs=[]` disables plugin discovery (pure-core test mode).
`--no-plugins` CLI flag equivalent.

## Runtime Identity

```yaml
session_id: generated-or-explicit
thread_id: agent
workspace_root: /actual/project/root
provider: current-provider
```

Configuration:

```text
data/config/system.yaml
data/config/providers.yaml
data/config/permissions.yaml
data/config/sandbox.yaml
<workspace_root>/AGENTS.md
```

Without `providers.yaml`, the `default` provider name uses the built-in OpenAI
configuration. Any other name, or a name missing from an existing provider
file, fails at bootstrap and reports the configured names; provider selection
never silently falls back to a different model.

## Core Components

### Engine (`xbotv2/core/engine.py`)

ReAct loop: user message accept → context build (with hook injection) →
LLM call (streaming) → tool execution → repeat. Uses the provider-neutral
`Message`, `ToolCall`, and `Tool` types from `xbotv2.api`.

The turn implementation is an orchestrator over stage-specific methods:
message admission/start, context construction, model-request preparation,
streamed model handling, tool-batch execution, and turn finish. Each method
interprets only the Hook stages it owns; there is no shared catch-all Hook
result interpreter. Internal model/tool completion records are consumed by the
orchestrator and never cross the C/S event boundary.

Key hooks: `BEFORE_USER_MESSAGE_ACCEPT`, `AFTER_CONTEXT`, `BEFORE_MODEL_REQUEST`,
`AFTER_AGENT`, `BEFORE_TOOLS`, `ON_STOP`, `ON_STOP_FAILURE`, `ON_TOOL_CALL_FAILURE`,
`PRE_COMPACT`, `POST_COMPACT`.

### Tool System (`xbotv2/tools/`)

- **Tool** (`api/tools.py`): native tool dataclass with `from_function()`, supports
  async functions and keyword-only parameter injection (sandbox, skill_registry).
- **ToolRegistry** (`registry.py`): namespace-aware canonical names and
  `restrict()` with wildcard selectors.
- **SandboxPolicy** (`sandbox.py`): integrates **BubblewrapBackend** (`sandbox_bwrap.py`)
  for process isolation. Provides capability methods: `run_shell`, `read_file`,
  `write_file`, `list_dir`.
- **PermissionSystem** (`permissions.py`): deny/allow/ask with regex pattern matching.

### Hooks (`xbotv2/hooks/`)

43 `HookStage` values cover session, turn, mailbox, tool, context, and compaction
lifecycle. Guard control flow uses explicit `HookDecision`; critical lifecycle
stages aggregate failures with `ExceptionGroup`.

### Runtime Mailbox (`xbotv2/core/mailbox.py`)

Buffers `user_message` and `general` inputs while a session is alive. A session
worker turns one message at a time into an Engine turn; user input has priority
over runtime notifications. Queue contents are destroyed on disconnect and are
not restored from the append-only diagnostic log.

### LLM Provider (`xbotv2/llm/`)

- `OpenAICompatibleProvider`: streaming (`stream=True`) with `reasoning_effort` and
  `thinking_enabled` config. Yields per-token deltas including reasoning content.
- `AnthropicProvider`: same interface for Anthropic models.
- `MockLLM`: deterministic test provider, supports chunk streaming with
  `additional_kwargs`.
- `Message` dataclass (`api/messages.py`): XBot-owned, persisted to `messages.jsonl`.

### Context Builder (`xbotv2/core/context.py`)

Assembles provider messages: system prefix → plugin fragments → runtime rules →
history → `context_suffix` plugin fragments → current state. SHA256 cache
replaced with tuple key.

## Plugin System

### CompactPlugin (`builtin_plugins/compact/`)

Uses the public `BEFORE_CONTEXT` compaction result and the controlled
`HookContext.invoke_model()` capability to summarize a completed history
prefix. It supports a model-visible request tool and a configurable automatic
character threshold. Core remains responsible for Hook bracketing and atomic
history persistence.

### TodolistPlugin (`builtin_plugins/todolist/`)

Provides four explicit session-scoped todo tools. One `PluginStore` value holds
the ordered items and next stable identifier, so every successful mutation is
one immediate persisted write. It does not infer state from conversation text
or duplicate goal ownership.

### GoalPlugin (`builtin_plugins/goal/`)

Persists one session objective and exposes one `goal` state-machine Tool, which
the shared ToolRegistry inventory discovers as `/goal`. Active goals schedule their next turn through
the Core mailbox until completed, blocked, or paused. Terminal context retains
the execution summary and prevents repeated work. It does not own todo steps.

### SkillsPlugin (`builtin_plugins/skills/`)

Discovers SKILL.md files (agentskills.io standard) from:
`.claude/skills/`, `.agents/skills/`, `.opencode/skills/` (project + global `~/.`).

- Registers `skill` via the stable `Tool` API (namespace `plugin:skills:skill`)
- Registers discovered skills as ToolRegistry entries (namespace `skills:<scope>:<name>`)
- BEFORE_USER_MESSAGE_ACCEPT hook: detects `/skill-name` prefix, expands SKILL.md content
- Shell injection via `` !`cmd` `` syntax in SKILL.md (sandboxed)
- active-skill `allowed-tools` / `disallowed-tools` restrictions, applied before
  the authoritative core permission check
- `disable-model-invocation: true` for skills available only through explicit
  `/skill-name` user invocation

### MCPPlugin (`builtin_plugins/mcp/`)

Connects through the official MCP SDK using stdio or Streamable HTTP.
- The SDK owns lifecycle negotiation, pagination, cancellation, progress, and
  server notifications.
- Registers MCP tools in ToolRegistry (namespace `mcp:<server>:<tool>`)
- Eager connection at bootstrap with per-server diagnostics. Optional failures
  mark the plugin degraded; servers configured with `required: true` fail startup.
- Performs the required initialize/initialized handshake before discovery
- Preserves MCP input schemas and adapts call data/errors to `ToolResult`
- Keeps optional server and client features capability-gated; XBot advertises
  them only when the corresponding Agent bridge is installed.
- Exposes resources, prompts, and completion through one stable bridge tool per
  negotiated feature instead of dynamically registering every remote item.

## Namespace Protocol

Tools use canonical registered names:

| Source | Name | Example | Slash |
|---|---|---|---|
| `builtin` | `core` | `shell` | `/shell` |
| `plugin` | plugin name | `plugin:skills:skill` | `/skill` |
| `skills` | scope/path | `skills:global:find-skills` | `/find-skills` |
| `mcp` | server name | `mcp:github:search` | `/search` |

The callable name is the slash command display name and may be ambiguous.
Command discovery exposes `registered_name`; non-core names use
`source:owner:name`, while built-in core names remain bare.

## Transport

### HTTP/SSE (`xbotv2/protocol/http_server.py`)

FastAPI app with SSE streaming. `SessionManager` owns one core `SessionRuntime`
per live session. `SessionRuntime` owns the Engine, runtime-only Mailbox, turn
task, interaction sink, and event stream; HTTP only maps that lifecycle to wire
requests. Once mode uses the same runtime so immediate Goal continuations are
not lost after the first model turn.
Wire DTOs are owned by `protocol/models.py`; `api/` contains no transport types.
The HTTP bridge owns the Engine async stream and closes it when the SSE
consumer completes or disconnects.

### Unix Domain Socket (`__main__.py`)

Default TUI transport: auto-generates `/tmp/xbotv2-{pid}.sock`, spawns server
subprocess bound to it. No TCP port needed for local use. `--server URL` for
remote HTTP connection.

### Session Resume

Session creation uses explicit `new` and `resume` modes. The server does not
silently change the requested mode. Resume closes any in-memory runtime with the
same session id and rebuilds from persisted history; pending interactions and
turn coroutines are connection-owned and are never restored.

## Unified Command System

`CommandSpec` with `kind` field (`client`, `server`, `skill`, `tool`, `mcp`).
TUI fetches commands from `GET /sessions/{id}/commands` which enumerates
ToolRegistry entries. `/help [name]` shows detailed help. Server commands use
the command endpoint; Tool, Skill, and MCP entries remain Agent turns and rely
on their existing ToolRegistry registration for execution.

## Persistence

```
data/sessions/<sid>/state/
├── messages.jsonl          # XBot-owned Message objects, append-only
├── plugin_states/          # per-plugin YAML state
└── artifacts/              # cached tool outputs and provider context
```

No `events.jsonl`, `state.yaml`, or materializer. `CoreStateStore` appends new
messages in normal turns and uses an atomic replacement only after compaction or
history mutation.

## Streaming & Reasoning

Provider uses `stream=True` with `async for chunk in response`.
Reasoning content (DeepSeek thinking mode) extracted from `delta.reasoning_content`,
emitted as regular content deltas with `## Thinking` header.
Reasoning preserved in `Message.additional_kwargs.reasoning_content` and
re-passed to API for tool-call turns via `provider_messages`.

TUI uses timer-based rendering (`_stream_timer` at 50ms intervals) —
streaming events are near-zero-cost on the hot path.
