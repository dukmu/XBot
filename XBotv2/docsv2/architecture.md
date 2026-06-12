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
- **17 lifecycle stages**: session (4), turn (2), loop (6), message (3),
  system events (2)
- Loop hooks short-circuit on truthy return
- Lifecycle hooks always run all callbacks
- `ON_SESSION_INIT` hooks can register tools

### Plugin System (`xbotv2/plugin/`)
- Plugins declared via `plugin.yaml` manifest
- Each plugin gets isolated `PluginStore` (opaque to core)
- Dependency resolution via topological sort
- Plugins register: hooks, tools, prompt fragments

### Tool System (`xbotv2/tools/`)
- `ToolRegistry` with sandbox/execution metadata
- `SandboxPolicy` for resource access control
- `PermissionSystem` with deny→allow→ask precedence
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
- `base.py`: `ask` tool
- `filesystem.py`: `filesystem_read`, `filesystem_write`, `filesystem_list`
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
| BEFORE_CONTEXT | Yes | Before context assembly (compact) |
| AFTER_CONTEXT | Yes | After context assembly |
| BEFORE_AGENT | Yes | Before LLM call (skills injection) |
| AFTER_AGENT | Yes | After LLM call |
| BEFORE_TOOLS | Yes | Before tool execution (sandbox guard) |
| AFTER_TOOLS | Yes | After tool execution (cache) |
| ON_USER_MESSAGE | No | User input parsed |
| ON_ASSISTANT_MESSAGE | No | LLM response received |
| ON_TOOL_MESSAGE | No | Tool result received |
| ON_ERROR | No | Error occurred |
| ON_CONFIG_RELOAD | No | Config was reloaded |
