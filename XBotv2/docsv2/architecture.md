# XBotv2 Architecture

## Overview

XBotv2 is a plugin-extensible AI agent runtime. The core engine implements
a minimal ReAct loop. All advanced features (DAG planning, context compaction,
skills, memory, subagents) are implemented as independent plugins.

## Architecture Principle

```
Plugins ──import──→ Core (xbotv2)
Core ──NEVER imports──→ Plugins (builtin_plugins)
```

Core defines interfaces; plugins implement them. The bootstrap sequence
discovers and wires plugins at runtime via `plugin.yaml` manifests.

## Core Components

### Engine (`xbotv2/core/engine.py`)
- **3-node ReAct loop**: `prepare_context → agent → tools → repeat`
- Each stage runs before/after hooks with optional short-circuiting
- No DAG, plan, task-mode, skills, or compaction concepts
- Pure linear execution — works without any plugins

### Hook System (`xbotv2/hooks/`)
- **33 lifecycle stages**: session (4), turn (2), user intake (2),
  loop/request (14), message (3), tool call (4), persistence (2), system
  events (2)
- Loop hooks short-circuit on truthy return
- Guard hooks that short-circuit without a structured result fail closed with a
  bounded `hook_short_circuit_rejected` error instead of continuing silently
- Fine-grained request hooks expose source-tagged context components, final
  provider messages, pre-bind and post-bind tools, provider request metadata,
  provider responses, provider errors, per-tool call lifecycle, and persistence
  boundaries for token plugins
- Token estimation and budget policy remain plugin concerns; the hook surface is
  documented in `docsv2/token_budget_hooks.md`
- Lifecycle hooks always run all callbacks
- `ON_SESSION_INIT` hooks can register tools

### Plugin System (`xbotv2/plugin/`)
- Plugins declared via `plugin.yaml` manifest
- Each plugin gets isolated `PluginStore` (opaque to core)
- Dependency resolution via topological sort
- Plugins register: hooks, tools, prompt fragments

### Tool System (`xbotv2/tools/`)
- `ToolRegistry` with sandbox/execution metadata
- Personality tool selectors restrict visible/executable tools via `ToolRegistry.restrict()`
- `SandboxPolicy` for resource access control
- `PermissionSystem` with deny→allow→ask precedence
- Default `AFTER_TOOLS` hook caches oversized tool results under session artifacts
- Plugin ownership tracking for unload/reload

### Persistence (`xbotv2/persistence/`)
- Append-only `events.jsonl` — source of truth
- `state.yaml` — materialized view (rebuildable from events)
- Plugin states as opaque blobs in `plugin_states/`

### Context Building (`xbotv2/core/context.py`)
- Pluggable fragment injection points
- Cache-friendly: stable prefix memoized, dynamic suffix at end
- Instance-level caches (no module-level globals)

### Built-in Tools (`xbotv2/core/builtin_tools/`)
- `filesystem.py`: `filesystem_read`, `filesystem_write`, `filesystem_list`
  - read/list return JSON text with path, size, mtime, count, and truncation metadata
  - write supports overwrite, append, prepend, insert line, replace lines, regex replace, and unified diff patch modes
- `shell.py`: `shell` tool

## Plugin Architecture

### Directory Layout
```
builtin_plugins/<name>/
  plugin.yaml        # Manifest: name, version, hooks, tools, deps
  plugin.py          # PluginBase subclass (optional)
  hooks.py           # Hook handler functions
  tools.py           # Tool definitions
  prompts/           # Prompt fragment .md files
```

### Plugin Manifest
```yaml
name: planning
version: "1.0.0"
description: "DAG-based task planning"
depends_on: []
hooks:
  - stage: on_session_init
    handler: planning.hooks:on_init
tools:
  - handler: planning.tools:plan_add_nodes
prompt_fragments:
  - stage: dag_suffix
    handler: planning.context:render_dag_state
```

## State Model

Core state is intentionally minimal:
```yaml
schema_version: 2
session_id, thread_id, personality_id: str
turn_count, event_count: int
status: active | error | interrupted | closed
mailbox_pending: int
plugin_states: { <name>: <opaque> }
```

No DAG, plan, task-mode, skills, or compaction fields in core state.
All such concepts live in plugin-owned state namespaces.

## Hook Lifecycle

| Stage | Short-Circuit? | Purpose |
|-------|---------------|---------|
| ON_SESSION_INIT | No | Plugin init, tool registration |
| ON_SESSION_START | No | New session setup |
| ON_SESSION_RESUME | No | Session restored from checkpoint |
| ON_SESSION_CLOSE | No | Cleanup, finalize |
| ON_TURN_START | No | User message received |
| ON_TURN_END | No | Turn complete |
| BEFORE_USER_MESSAGE_ACCEPT | Yes | Validate or rewrite user input before history; silent rejection becomes a bounded error |
| AFTER_USER_MESSAGE_ACCEPT | No | User input accepted into history |
| BEFORE_CONTEXT | Yes | Before context assembly (compact) |
| BEFORE_CONTEXT_BUILD | Yes | Before ContextBuilder runs |
| AFTER_CONTEXT | Yes | After context assembly |
| AFTER_CONTEXT_COMPONENTS_BUILD | No | Source-tagged context components built |
| AFTER_CONTEXT_BUILD | No | Final provider message list built |
| BEFORE_AGENT | Yes | Before LLM call (skills injection) |
| BEFORE_TOOL_SCHEMA_BIND | Yes | Filter or gate tools before provider binding |
| AFTER_TOOL_SCHEMA_BIND | No | Tools selected/bound for the provider request |
| BEFORE_MODEL_REQUEST | Yes | Final gate before provider call; token budget plugins can short-circuit |
| AFTER_MODEL_RESPONSE | No | Raw provider response received; usage plugins can collect metadata |
| ON_MODEL_REQUEST_ERROR | No | Provider request failed |
| AFTER_AGENT | Yes | After LLM call |
| BEFORE_TOOLS | Yes | Before tool execution (sandbox guard) |
| AFTER_TOOLS | Yes | After tool execution; default hook may cache/truncate large results before persistence/protocol emit |
| ON_USER_MESSAGE | No | User input parsed |
| ON_ASSISTANT_MESSAGE | No | LLM response received |
| ON_TOOL_MESSAGE | No | Tool result received |
| ON_TOOL_CALLS_PARSED | No | Assistant tool calls normalized |
| BEFORE_TOOL_CALL | Yes | Per-tool call gate/rewrite |
| AFTER_TOOL_CALL | No | Per-tool call result observed |
| ON_TOOL_DENIED | No | Tool call denied by registry, sandbox, permission, or hook |
| BEFORE_STATE_PERSIST | No | Before message persistence and materialization |
| AFTER_STATE_PERSIST | No | After message persistence and materialization |
| ON_ERROR | No | Error occurred |
| ON_CONFIG_RELOAD | No | Config was reloaded |

Short-circuit guard hooks should return a structured dict when they want to
rewrite context, tools, messages, or emit a custom event. A bare truthy return
from pre-context/pre-request guard stages is treated as a rejection and converted
to a bounded error event so protocol clients do not hang and provider calls do
not continue accidentally.
