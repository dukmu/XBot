# Plugin System

## Overview

XBotv2 plugins are independent Python packages that extend the core engine
through hooks, tools, and prompt fragments. Plugins are discovered,
loaded, and wired at bootstrap time.

Personality files can also register direct hooks with a `hooks:` list using
`stage` and `target: module:function`. These hooks are resolved during
bootstrap and fail loudly if the target cannot be imported. Plugin manifests
remain the preferred mechanism for reusable extension packages.

## Plugin Manifest

Each plugin directory contains a `plugin.yaml`:

```yaml
name: my_plugin
version: "1.0.0"
description: "What this plugin does"
depends_on: []  # Optional dependencies

hooks:
  - stage: before_agent
    handler: my_plugin.hooks:inject_context

tools:
  - handler: my_plugin.tools:my_tool
    sandbox_mode: host
    execution_mode: sequential

prompt_fragments:
  - stage: system_instructions
    file: prompts/instructions.md
  - stage: dag_suffix
    handler: my_plugin.context:render_state
```

## Plugin Lifecycle

1. **Discovery**: `PluginLoader` scans plugin directories for `plugin.yaml`
2. **Resolution**: Topological sort by `depends_on`
3. **Loading**: Plugin class instantiated with isolated `PluginStore`
4. **Initialization**: `on_load(config)` called
5. **Registration**: Hooks → HookManager, Tools → ToolRegistry,
   Fragments → ContextBuilder
6. **Runtime**: Plugin hooks execute during engine lifecycle
7. **Unload**: `on_unload()` called, hooks/tools/fragments removed

## PluginBase

Plugins extend `PluginBase`:

```python
class MyPlugin(PluginBase):
    async def on_load(self, config: dict) -> None:
        """Called at startup. Validate config, init state."""

    async def on_unload(self) -> None:
        """Called at shutdown. Clean up resources."""

    def register_hooks(self, manager: HookManager) -> None:
        """Register hook handlers."""

    def register_tools(self, registry: ToolRegistry) -> None:
        """Register tools."""

    def get_prompt_fragments(self) -> dict[str, str]:
        """Return rendered prompt text by injection stage."""
```

## Default Plugin

If a plugin has no Python class, the system creates a `_DefaultPlugin`
instance that uses manifest-driven registration. This works for simple
plugins that only provide tools and static prompt fragments.

## Plugin Store

Each plugin gets an isolated key-value store:

```python
class PluginStore:
    async def get(key, default=None) -> Any
    async def set(key, value) -> None
    async def delete(key) -> None
    async def all() -> dict
```

Plugin state is persisted as opaque blobs by `CoreStateStore`. Core
never reads or interprets plugin state.

## Built-in Plugins

| Plugin | Purpose | Key Hooks |
|--------|---------|-----------|
| `compact` | Context compaction | `before_context` (check thresholds, summarize) |
| `planning` | DAG task planning | `on_session_init`, `before_context`, `on_turn_end` |
| `skills` | Skill loading | `on_config_reload` |
| `memory` | Long-term memory | (tools only) |
| `summary` | Summary artifacts | (tools only) |
| `mailbox` | Inter-agent messages | `on_turn_start` (check pending) |
| `subagent` | Subagent management | (tools only) |

## Creating a Plugin

1. Create directory: `builtin_plugins/<name>/`
2. Write `plugin.yaml` manifest
3. Create `plugin.py` with PluginBase subclass
4. Define hook handlers and tools
5. Test independently with core engine

Plugins can also be loaded from external directories specified in config.
