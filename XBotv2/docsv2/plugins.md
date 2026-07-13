# XBotv2 Plugin System

## Architecture

```
Core -> never imports -> Plugins (builtin_plugins)
Plugins -> import -> Stable API (xbotv2.api)
```

Plugins extend the engine via hooks, tools, and prompt fragments.
They live in `builtin_plugins/` and are loaded by `PluginLoader` during bootstrap.
Disable all plugins with `plugin_dirs=[]` or `--no-plugins`.

## PluginBase

```python
class PluginBase:
    async def on_load(self, config): ...     # Optional plugin-owned initialization
    async def on_unload(self): ...           # Optional plugin-owned cleanup
    def setup(self, ctx): ...                # Register all extensions
```

`PluginBase` supplies no-op lifecycle defaults. Override only the callbacks the
plugin needs.

## Lifecycle Contract

| Phase | Plugin responsibility | Loader guarantee |
|---|---|---|
| configuration | Declare `config_schema` in the manifest | Schema and values are validated before module import |
| `on_load(config)` | Create plugin-owned resources from validated config; keep cleanup safe after partial initialization | No core registrations exist yet; `on_unload` is attempted if `on_load` raises |
| `setup(ctx)` | Register hooks, tools, and prompt fragments through `ctx` | Registration is transactional |
| hook execution | Use `ctx.plugin_runtime` for dynamic tools | Dynamic resources join the same ownership record |
| `on_unload()` | Close clients, subprocesses, and other external resources | Core resources are removed even if this callback raises; store data remains |

Loading is atomic across dependency order. If a later plugin fails, already
loaded plugins are unloaded in reverse order. Cleanup failures are attached to
the original load error instead of replacing it. Explicit `unload_all()` also
continues after individual callback failures and reports an exception group
after every plugin has been cleaned.

The setup transaction also rolls back on task cancellation. Hooks, tools, and
prompt fragments registered before a `CancelledError` are removed before the
cancel propagates; the partially initialized plugin still receives
`on_unload()` for its own resources.

Bootstrap remains transactional after loading: failures while creating the LLM
or running `ON_SESSION_INIT` trigger `unload_all()`. This removes runtime tools
registered by initialization hooks and closes plugin-owned external resources.
Normal `Engine.close_session()` runs close hooks, persists messages, and then
calls `unload_all()`. Failure in one phase does not skip later cleanup phases;
the close operation propagates collected failures after cleanup finishes.

`on_unload` must be idempotent enough to handle partial initialization. The
loader invokes it after any entered `on_load`, even when `on_load` itself raises,
so resources created before the failure can be released.

Tool keys are unique. Duplicate registration fails before registry mutation, so
a plugin cannot accidentally replace a core or another plugin's tool.

## Plugin Store

Each plugin receives one isolated `PluginStore` namespace. `set`, `delete`, and
`clear` persist immediately through atomic file replacement; there is no flush
phase at unload. State survives plugin unload and session resume until the
plugin explicitly clears it.

Reads return a fresh persisted snapshot. Mutating a list or mapping returned by
`get` or `all` does not update state; call `set` explicitly. Values must be
YAML-safe, and failed serialization preserves the previous file. Operations in
one session event loop are serialized because a store operation performs no
internal await. Cross-process transactions over the same session directory are
not supported.

Plugin names are validated before a store path is constructed, and persisted
plugin state must contain a mapping.

Plugin configuration uses Draft 2020-12 JSON Schema. An invalid schema prevents
manifest discovery; invalid values raise `PluginConfigError` with the plugin
name and failing path before the plugin module is imported. Schema `default`
keywords are documentation only and are not injected into config; plugins
should retain explicit runtime defaults in `on_load`.

## Plugin Template

```python
from typing import Any

from xbotv2.api import (
    HookContext,
    HookStage,
    PluginBase,
    PluginSetupContext,
    Tool,
    ToolRegistrationOptions,
)


class ExamplePlugin(PluginBase):
    async def on_load(self, config: dict[str, Any]) -> None:
        self._config = dict(config)

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.ON_SESSION_INIT, self._on_session_init)
        ctx.register_tool(
            Tool.from_function(self._run, name="example"),
            options=ToolRegistrationOptions(namespace="plugin:example"),
        )

    async def on_unload(self) -> None:
        # Close only resources owned directly by this plugin.
        pass

    async def _on_session_init(self, ctx: HookContext) -> None:
        # Dynamic tools, when needed, use ctx.plugin_runtime.register_tool(...).
        pass

    async def _run(self, value: str) -> str:
        return value

    def diagnostics(self) -> dict[str, Any]:
        return {"status": "ready"}
```

Manifest-only plugins declare hooks, tools, and prompt fragments in
`plugin.yaml`. Python plugins override `setup(ctx)` and use
`ctx.register_hook`, `ctx.register_tool`, and `ctx.add_prompt_fragment`.
The setup transaction should record every resource for rollback and unload.

Prompt fragments use the public `PromptFragmentStage` values, in render order:
`system_prefix`, `system_instructions`, `system_rules`, and `context_suffix`.
Each manifest declaration provides exactly one non-empty `file` or `handler`.
The suffix stage renders after message history and before the core current-state
text. Unknown stages are rejected during manifest validation; Python plugins
using `ctx.add_prompt_fragment()` receive the same validation from the context
builder.
After assembly, `AFTER_CONTEXT_COMPONENTS_BUILD` receives immutable public
`ContextComponent` values. A Hook may replace the component list, but every
replacement entry must remain a `ContextComponent`; provider conversion does
not accept ad hoc dictionaries or private core objects.
Runtime registrations performed from hooks must either use a recorded plugin
capability or be moved into setup; otherwise unload and failure rollback cannot
be complete.

Plugin hooks receive `ctx.plugin_runtime` when they are invoked through the
loader. Dynamic tools discovered at runtime must be registered through that
capability:

```python
ctx.plugin_runtime.register_tool(
    tool,
    options=ToolRegistrationOptions(namespace="plugin:my-plugin"),
)
```

Those runtime registrations are appended to the plugin record and are removed
during plugin unload. A plugin that owns a shorter-lived dynamic resource may
call `ctx.plugin_runtime.unregister_tool(registered_name)`. The capability
rejects names outside that plugin's ownership record.

Tool registration options are explicit:

```python
from xbotv2.api import ToolRegistrationOptions

ctx.register_tool(
    tool,
    options=ToolRegistrationOptions(
        sandbox_mode="sandboxed",
        namespace="plugin:my-plugin",
    ),
)
```

The loader records each returned registered name in the plugin's single
ownership record so unload and rollback can remove every resource.

## plugin.yaml Manifest

```yaml
name: my-plugin
version: "1.0.0"
description: What this plugin does
hooks:
  - stage: on_session_init
    handler: my_plugin.hooks:on_init
tools:
  - handler: my_plugin.tools:my_tool
    sandbox_mode: host
prompt_fragments:
  - stage: system_instructions
    file: prompts/system.md
config_schema:
  type: object
  additionalProperties: false
  properties:
    endpoint:
      type: string
      minLength: 1
```

## Built-in Plugins

### CompactPlugin (`builtin_plugins/compact/`)

Compacts a completed history prefix through the public `BEFORE_CONTEXT`
contract. The `compact` tool requests a manual compaction; current provider input
usage triggers it automatically, with a character fallback when usage is absent.
Recent complete user turns remain verbatim, the
auxiliary model call has no tools, and only a successful summary is returned as
a message replacement. Engine persistence then atomically rewrites history, so
resume observes the same summary and recent tail. See
[Compact plugin](compact.md).

### TodolistPlugin (`builtin_plugins/todolist/`)

Provides explicit list, create, update, and remove tools backed by one
immediately persisted `PluginStore` value. Items retain creation order and
stable identifiers across session resume. Invalid mutations return structured
errors without changing stored state. See [TodoList plugin](todolist.md).

### GoalPlugin (`builtin_plugins/goal/`)

Maintains one durable session objective through a single `goal` state-machine
Tool, discovered as `/goal` through the shared registry. Active goals continue through Core
mailbox turns; ESC pauses them and `/goal resume` reactivates them. Active,
paused, complete, and blocked states append concise public context; terminal
context prevents repeated work and remains until resume, replacement, or clear. See
[Goal plugin](goal.md).

### SkillsPlugin (`builtin_plugins/skills/`)

Discovers SKILL.md files (agentskills.io format) and registers them as tools.

**Files:**
- `plugin.yaml`: manifest
- `plugin.py`: SkillsPlugin class
- `registry.py`: SkillRegistry — YAML frontmatter parsing, directory scanning
- `skill_tool.py`: `load_skill()` with `` !`cmd` `` shell injection preprocessing
- `permission_scope.py`: per-turn tool permission overrides

**Hooks:**
- `ON_SESSION_INIT`: transactionally discover SKILL.md files from 6 paths and
  register each discovered skill once
- `BEFORE_USER_MESSAGE_ACCEPT`: detect `/skill-name` prefix, expand content
- `AFTER_CONTEXT`: inject active skill content into context
- `ON_TURN_END`: clear active skills and permission scopes
- `BEFORE_TOOL_CALL`: enforce active-skill tool restrictions before core
  permission checks

**Tools:**
- `skill` (namespace `plugin:skills:skill`): load a skill by name
- Each model-invocable skill is registered as a tool (namespace
  `skills:<scope>:<name>`) with its SKILL.md description in the provider schema.
  Generic, dedicated, and explicit `/skill-name` invocation all preprocess and
  activate the same per-turn skill state.

**Features:**
- Repeated initialization on the same loaded plugin is idempotent; partial
  dynamic registration failure rolls back that discovery attempt.
- Shell injection: `` !`command` `` placeholders run only through the enabled
  session sandbox. There is no host subprocess fallback.
- `allowed-tools` is an additional allowlist while the skill is active;
  unmatched calls are denied. `disallowed-tools` takes precedence. These fields
  restrict calls and never bypass core permission policy. Parameter patterns
  currently use the real `shell(command)` form, for example
  `shell(git *)`; compatibility aliases such as `Bash` are not provided.
  Patterns must be non-empty strings with balanced parameter parentheses;
  malformed permission metadata causes the skill to be ignored during
  discovery, and scope updates are atomic.
- `disable-model-invocation: true` keeps a skill out of the model tool list and
  blocks the generic `skill` tool from loading it; explicit `/skill-name`
  invocation remains available. The value must be a YAML boolean.
- Generic and dedicated skill tools return `ToolResult`. Successful loads
  expose the skill name and scope in `data`; unknown and manual-only requests
  use stable structured errors instead of successful `Error:` text.

### MCPPlugin (`builtin_plugins/mcp/`)

Connects to MCP (Model Context Protocol) servers and registers their tools.

**Files:**
- `plugin.yaml`: manifest
- `plugin.py`: MCPPlugin class
- `client.py`: MCPClient with StdioTransport and HttpTransport
- `tool.py`: MCP tool adapter returning `ToolResult`

**Hooks:**
- `ON_SESSION_INIT`: connect to enabled MCP servers, validate tool definitions,
  and register each server transactionally
- `ON_SESSION_CLOSE`: unregister session tools and disconnect all servers

Initialization is idempotent within an open session. A registration failure
rolls back every tool and the connection for that server. Optional server
failures leave diagnostics degraded and allow bootstrap to continue; a server
with `required: true` rolls back every server initialized by that hook call and
fails bootstrap. Session close resets the plugin so a later initialization can
reconnect and register a fresh tool set. `on_unload` remains a final cleanup
path for bootstrap failures and abnormal shutdown.

**Transport types:**
- `local` (stdio): official MCP SDK stdio transport.
- `remote` (HTTP): official MCP SDK Streamable HTTP transport, including
  negotiated protocol headers, JSON/SSE responses, session termination, and
  server notifications.

The maintained SDK owns JSON-RPC, lifecycle negotiation, pagination, transport
sessions, cancellation, progress, and notifications. The XBot client exposes
the negotiated tools, resources, resource templates, prompts, completions,
subscriptions, logging level, and ping primitives. Invalid tool schemas or
failed XBot registration still abort that server transaction. MCP `inputSchema`
is preserved as the public `Tool.parameters` schema. Successful calls retain
the raw MCP result in `ToolResult.data`; MCP `isError` becomes a structured
`mcp_tool_error`.

Negotiated server capabilities are Agent-facing without per-item registration:
each server may add stable `protocol_resources`, `protocol_prompts`, and
`protocol_complete` tools. They query the live MCP session, preserve structured
results, and expose subscription operations only when the server advertises
them. Bidirectional client capabilities are advertised only when the
corresponding XBot callback is installed.

MCP client requests reuse public runtime capabilities: roots contain only the
current workspace, sampling uses the unbound current provider, server logs enter
the XBot log, and form/URL elicitation uses the existing live
`user_input_required` C/S flow. Elicitation is connection-owned and is cancelled
on disconnect. Non-text sampling and sampling tool execution return protocol
errors instead of silently losing content.

**Configuration** (in `system.yaml` plugins section):
```yaml
plugins:
  mcp:
    servers:
      github:
        type: local
        command: ["npx", "-y", "@modelcontextprotocol/server-github"]
        enabled: true
```

### TokenManagerPlugin (`builtin_plugins/token_manager/`)

Uses public model-request, model-response, tool-call, and turn Hooks to estimate
request size and collect usage. It is the template for observation/policy
plugins that do not need runtime internals. Diagnostics explicitly report
`mode: observe_only`: threshold violations are logged but do not compact,
filter, reject, or otherwise alter the request. In-memory statistics are reset
on unload. See [Token manager plugin](token_manager.md) for its current
contract and remaining behavior gaps.

## Tool Namespace Convention

Tools use one canonical registered name:

| Source | Name | Key | Slash |
|---|---|---|---|
| `builtin` | `core` | `shell` | `/shell` |
| `plugin` | plugin-name | `plugin:skills:skill` | `/skill` |
| `skills` | scope | `skills:global:find-skills` | `/find-skills` |
| `mcp` | server-name | `mcp:github:mcp__github__search` | `/mcp__github__search` |

Plugins should register tools through setup or another recorded plugin
capability:
```python
ctx.register_tool(
    tool,
    options=ToolRegistrationOptions(namespace="plugin:skills"),
)
```

`PluginSetupContext` deliberately does not expose `ToolRegistry`, `HookManager`,
or `ContextBuilder`. The loader owns those implementations and records every
registration so a failed setup can be rolled back atomically.

The built-in Skills, MCP, and token-manager plugins are the reference templates
for third-party plugins. When those plugins need behavior that the public API
cannot express, first verify that the gap is shared rather than plugin-local.
Shared gaps belong in the public API; plugin-local concerns stay inside the
plugin instead of receiving special runtime access or a new public wrapper.

- Compact demonstrates an auxiliary model call, transform Hook, structured
  request tool, and core-owned atomic persistence.
- Skills demonstrates setup tools, lifecycle hooks, runtime-discovered tools,
  per-turn state, diagnostics, and unload reset.
- MCP demonstrates external client ownership, degraded diagnostics, dynamic
  tools, session cleanup, and unload cleanup as a final safety net.
- Token Manager demonstrates a hook-only plugin with configuration,
  diagnostics, model-request inspection, public collector methods, and
  unload-time in-memory state reset.
  It does not write into `HookContext.state`: that mapping is a stage payload,
  not a generic plugin persistence channel.
