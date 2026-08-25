# XBotv2 Plugin System

## Architecture

```
Core -> never imports -> Plugins (goal/, todolist/, skills/, ...)
Plugins -> import -> Stable contracts (core/)
```

Plugins extend the engine via hooks, tools, and prompt fragments.
They live in top-level plugin packages and are loaded by application startup.
`plugin_dirs` supplies external Python import roots; an empty list does not
disable bundled capabilities. `--no-plugins` selects the core Agent
composition without optional capabilities or external plugins.

## Plugin Shape

```python
class Plugin:
    name = "example"
    inject = ["tools"]

    def apply(self, ctx: Context, config=None): ...
```

There is no required base class. A plugin exports a `plugin` object, a `Plugin`
class, or an object/module with `apply`. The loader creates a fresh instance for
object plugins, mounts it through XCore, and activates it when every declared
required service is available. Prefer required dependencies: they let XCore
hold the plugin pending until its complete contract is present. Use an optional
dependency only when absence is an intentional feature mode, and resolve that
single optional boundary explicitly instead of scattering fallback checks.

## Lifecycle Contract

| Phase | Plugin responsibility | Loader guarantee |
|---|---|---|
| `apply(ctx)` | Register hooks, tools, commands, prompt fragments, and agents through `ctx` services | Registration is a fiber effect; unload undoes it |
| event listeners | Register `ctx.on(Events.X, handler)` listeners | Listeners are fiber effects; unload undoes them |
| disposer | Register plugin-owned cleanup with `ctx.dispose(callback)` | Disposers run when setup fails or the plugin unloads; store data remains |

Loading is atomic across dependency order. If a later plugin fails, already
loaded plugins are unloaded in reverse order. Cleanup failures are attached to
the original load error instead of replacing it. Explicit `unload_all()` also
continues after individual callback failures and reports an exception group
after every plugin has been cleaned.

The setup transaction also rolls back on task cancellation. Event
listeners, tools, and prompt fragments registered before a `CancelledError`
are removed before the cancel propagates. A disposer registered before the
failure still runs for plugin-owned resources.

Application boot remains transactional after loading: failures while Agent
construction creates the model client or publishes `APPLICATION_INITIALIZED`
destroy the partial root Context. This removes runtime tools
registered by initialization hooks and closes plugin-owned external resources.
Normal `Engine.close_session()` emits loop lifecycle events only. The owning
application context stops plugin fibers; persistence independently observes
state changes and explicit application-level flushes. Failure in one cleanup
phase does not authorize Engine to take ownership of plugin teardown.

Disposers must tolerate partial initialization and repeated owner cleanup. Add
the disposer before opening resources whose construction can fail.

Tool keys are unique. Duplicate registration fails before registry mutation, so
a plugin cannot accidentally replace a core or another plugin's tool.

## Plugin Store

Each plugin receives one isolated namespace in the recoverable state service
(`ctx.state.namespace(plugin_name)`).
`set`, `delete`, and
`clear` persist immediately through atomic file replacement; there is no flush
phase at unload. State survives plugin unload and session resume until the
plugin explicitly clears it.

Reads return a fresh persisted snapshot. Mutating a list or mapping returned by
`get` or `all` does not update state; call `set` explicitly. Values must satisfy
the strict JSON state contract, and failed validation or persistence preserves
the previous snapshot. Operations in
one session event loop are serialized because a store operation performs no
internal await. Cross-process transactions over the same session directory are
not supported.

Plugin names are validated before a store path is constructed, and persisted
plugin state must contain a mapping.

Plugins may expose `Config` using XCore's schema helpers. Configuration comes
from the plugin-tree entry and is passed to `apply`; plugins should retain
explicit runtime defaults rather than relying on schema documentation.

## Plugin Template

```python
from XBotv2.core import Tool, ToolCall


class ExampleHandler:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    async def run(self, value: str, *, tool_call: ToolCall) -> str:
        """Return one prefixed value."""
        return f"{self._prefix} ({tool_call.id}): {value}"


class ExamplePlugin:
    name = "example"
    inject = ["tools"]

    def apply(self, ctx, config=None) -> None:
        prefix = str((config or {}).get("prefix") or "example")
        handler = ExampleHandler(prefix)
        ctx.tools.register(Tool.from_function(handler.run, name="example"))
```

Python plugins override `apply(ctx)` and register through the XCore context:
`ctx.on(Events.X, callback)` for runtime event listeners,
`ctx.tools.register(...)`, `ctx.commands.register(...)`,
`ctx.prompts.add(...)`, and `ctx.agents.register(...)` for agents. Every
registration during `apply` is a fiber effect: unload (or a setup failure)
undoes it automatically, and `ctx.dispose(...)` runs cleanup once as the
fiber disposer. Plugin-tree entries supply configuration and profile selection.

Agent definitions follow the same ownership rules as other resources: names
are unique, setup failure rolls them back, and unload unregisters them. Core
owns Agent execution; a plugin registers definitions, not a separate loop.
Workspace-scoped plugins discover Markdown definitions outside the data root
through `ctx.agents.register_markdown(directory, overlay=True)`, so workspace
definitions replace a same-named base definition without mutating it.

Prompt fragments use the public `PromptFragmentStage` values as compatible
ordering zones:
`system_prefix`, `system_instructions`, `system_rules`, and `context_suffix`.
Each prompt-fragment declaration provides exactly one non-empty `file` or
`handler`.
All fragments are rendered as escaped `plugin_instruction` sections in the one
leading system context; no stage grants core authority or places a system
message after history. Unknown stages are rejected during configuration
validation.
Python plugins receive the same validation from the context builder and may
provide a source label through `ctx.prompts.add(stage, text, source=...)`.
After assembly, the context-builder-owned `CONTEXT_COMPONENTS_BUILT` event
receives immutable public `ContextComponent` values in a typed payload. A
listener may replace the component list, but
every replacement entry must remain a `ContextComponent`; provider conversion
does not accept ad hoc dictionaries or private core objects.
Runtime registrations performed from event listeners must be tracked by the
plugin and unregistered in its disposer; otherwise unload and failure
rollback cannot be complete.

Dynamic tools discovered from an event such as `APPLICATION_INITIALIZED` are
owned by a per-application runtime object. That object receives `tools` in its
constructor, records returned registration names, and unregisters them from its
named disposer. It does not retain the whole `Context`.

```python
name = self._tools.register(
    tool,
    namespace="plugin:my-plugin",
)
self._tool_names.append(name)
```

The disposer (`ctx.dispose(...)`) unregisters those names, so a plugin that
owns a shorter-lived dynamic resource removes it on unload or session close.

Tool registration is direct; registration is a fiber
effect — XCore's `current_fiber()` binds the cleanup, so the tool is removed
automatically when the plugin unloads:

```python
from XBotv2.core import Tool

class MyToolHandler:
    def __init__(self, capability) -> None:
        self._capability = capability

    async def invoke(self, value: str) -> str:
        return await self._capability.run(value)


handler = MyToolHandler(ctx.capability)
ctx.tools.register(Tool.from_function(handler.invoke, name="my-tool"))
```

Core supplies no arbitrary invocation dependency dictionary. Pass declared
services to a named handler or service. When a Tool needs the identity
of the final rewritten invocation, declare one keyword-only `ToolCall`
parameter; it is excluded from the model schema. Do not wrap sandbox, session,
or other services in a generic invocation context.

## Plugin Tree Entry

```yaml
- id: my-plugin
  name: my_package.my_plugin
  profiles: [agent]
  config:
    endpoint: https://example.invalid
```

Add durable workspace entries to `.xbot/plugins.yaml`; the loader imports
`<name>.plugin` first and otherwise tries `<name>`. Required service ownership
belongs in the plugin's `inject` declaration, not in tree configuration.

The bundled `xcore.yaml` contains complete entries and therefore requires
`name`. User configuration files are partial overlays: an existing entry can
be changed by `id` without repeating `name`, and omitted fields retain their
previous values. A new entry must provide both `id` and `name`.

Configuration has one precedence order:

1. bundled `xcore.yaml`;
2. `<data-dir>/config/plugins.yaml`;
3. `<workspace>/.xbot/plugins.yaml`;
4. session or direct-call overrides.

Only `config` is deep-merged. When present, `name`, `disabled`, `isolate`, and
`profiles` replace the earlier value. Invalid document shapes, unknown fields,
and values with the wrong type stop startup at the configuration boundary.
`--no-plugins` and the subagent composition select their allowed plugin set
before overlays are applied, so an overlay cannot add a capability that is not
part of that application profile.

## Built-in Plugins

### BrowserPlugin (`browser/`)

Provides live `web_search` and `web_fetch` research tools plus one lazily
started, isolated Chromium page for rendered interaction. Read-only operations
are separate from state-changing browser actions, which remain subject to the
normal permission policy. See [Browser plugin](browser.md).

### CompactPlugin (`compact/`)

Compacts a completed history prefix through the public `BEFORE_CONTEXT`
contract. The `compact` tool requests a manual compaction; current provider input
usage triggers it automatically, with a character fallback when usage is absent.
Recent complete user turns remain verbatim, the
auxiliary model call has no tools, and only a successful summary is committed as
one effective-history replacement. Resume observes the same summary and recent
tail; removed raw records are not retained in the conversation store. See
[Compact plugin](compact.md).

### TodolistPlugin (`todolist/`)

Provides one atomic `update_todos` Tool backed by a versioned snapshot in
`ctx.state.namespace("todolist")`. Each call supplies the complete
ordered checklist; invalid lists cannot partially change stored state. Its
normal Tool result confirms the update to the next model call; the plugin does
not inject repeated context.
See [TodoList plugin](todolist.md).

### GoalPlugin (`goal/`)

Maintains one durable session objective. `/goal` is the human control surface;
`create_goal`, `get_goal`, and `update_goal` are Agent Tools. Active goals
continue through continuation turns; ESC pauses them and `/goal resume`
reactivates them. Terminal context remains until resume, replacement, or clear.
See [Goal plugin](goal.md).

### SkillsPlugin (`skills/`)

Discovers SKILL.md files (agentskills.io format) and registers them as tools.

**Files:**
- `plugin.py`: SkillsPlugin and XCore dependency declarations
- `registry.py`: SkillRegistry — YAML frontmatter parsing, directory scanning
- `skill_tool.py`: `load_skill()` with `` !`cmd` `` shell injection preprocessing
- `permission_scope.py`: per-turn tool permission overrides

**Events:**
- `APPLICATION_INITIALIZED`: transactionally discover SKILL.md files from 6 paths and
  register each discovered skill once
- `BEFORE_USER_MESSAGE_ACCEPT`: detect `/skill-name` prefix, expand content
- Skill content enters context through the normal prompt-expansion or Tool-result
  path; the plugin does not add a repeated active-skill system message.
- `ON_TURN_END`: clear active skills and permission scopes
- `ctx.tools.guard(...)`: enforce active-skill tool restrictions in the shared
  monotonic guard pipeline

**Tools and prompt commands:**
- A model-invocable skill is registered as a namespaced Tool with its SKILL.md
  description in the provider schema.
- A user-invocable skill is separately registered as a prompt command.
  `/skill-name` is expanded before it enters history and never executes the
  skill Tool. There is no generic `skill` Tool.

**Features:**
- Repeated initialization on the same loaded plugin is idempotent; partial
  dynamic registration failure rolls back that discovery attempt.
- Shell injection: `` !`command` `` placeholders run only through the enabled
  session sandbox. There is no host subprocess fallback.
- Standard `allowed-tools` entries remain discovery metadata and never bypass
  the session permission policy. XBot-specific negative rules use the
  namespaced `xbotv2-disallowed-tools` field and register a monotonic Tool guard
  rather than a second permission system. Parameter patterns use the real
  `shell(command)` form, for example
  `shell(git *)`; compatibility aliases such as `Bash` are not provided.
  Patterns must be non-empty strings with balanced parameter parentheses;
  malformed permission metadata causes the skill to be ignored during
  discovery, and scope updates are atomic.
- `disable-model-invocation: true` keeps a skill out of the model tool list;
  explicit invocation remains available unless `user-invocable: false`. Both
  values must be YAML booleans.
- Provider-visible Skill name and description metadata is capped at two percent
  of the configured context window (estimated at four characters per token),
  with an absolute 8,000-character ceiling. Non-Skill Tool schemas are never
  removed by this budget.

### MCPPlugin (`mcp/`)

Connects to MCP (Model Context Protocol) servers and registers their tools.

**Files:**
- `plugin.py`: MCPPlugin and XCore dependency declarations
- `client.py`: MCPClient with StdioTransport and HttpTransport
- `tool.py`: MCP tool adapter returning `ToolResult`

**Events:**
- `APPLICATION_INITIALIZED`: connect to enabled MCP servers, validate tool definitions,
  and register each server transactionally
- `ON_SESSION_CLOSE`: unregister session tools and disconnect all servers

Initialization is idempotent within an open session. A registration failure
rolls back every tool and the connection for that server. Optional server
failures leave diagnostics degraded and allow startup to continue; a server
with `required: true` rolls back every server initialized by that hook call and
fails Agent startup. Session close resets the plugin so a later initialization
can reconnect and register a fresh tool set. Its XCore disposer remains the
final cleanup path for startup failures and application destruction.

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

**Configuration** (in a runtime `config.yaml` plugin entry):
```yaml
plugins:
  mcp:
    config:
      servers:
        github:
          type: local
          command: ["npx", "-y", "@modelcontextprotocol/server-github"]
          enabled: true
```

### TokenManagerPlugin (`token_manager/`)

Uses public model-request and model-response event listeners to expose the
latest provider-calibrated context estimate and provider usage. Engine owns cumulative
session accounting and Compact owns the automatic threshold; TokenManager does
not duplicate either policy. Its ephemeral observation resets on unload. See
[Token manager plugin](token_manager.md).

## Tool Namespace Convention

Tools use one canonical registered name. These are Tool identities, not slash
commands:

| Source | Name | Key |
|---|---|---|
| `builtin` | `core` | `shell` |
| `plugin` | plugin-name | `plugin:goal:create_goal` |
| `skills` | scope | `skills:global:find-skills` |
| `mcp` | server-name | `mcp:github:mcp__github__search` |

Plugins should register tools through `apply` (or another recorded plugin
capability); registration is a fiber effect, undone automatically on unload:
```python
ctx.tools.register(tool)
```

The plugin context deliberately does not expose `ToolRegistry` or
`ContextBuilder` directly. They are provided through the capability services
(`ctx.tools`, `ctx.commands`, `ctx.prompts`, `ctx.agents`) and every
registration so a failed setup can be rolled back atomically.

The built-in Skills, MCP, and token-manager plugins are the reference templates
for third-party plugins. When those plugins need behavior that the public API
cannot express, first verify that the gap is shared rather than plugin-local.
Shared gaps belong in the public API; plugin-local concerns stay inside the
plugin instead of receiving special runtime access or a new public wrapper.

- Compact demonstrates an auxiliary model call, transform event listener,
  structured request tool, and core-owned atomic persistence.
- Skills demonstrates setup tools, lifecycle listeners, runtime-discovered
  tools, per-turn state, diagnostics, and unload reset.
- MCP demonstrates external client ownership, degraded diagnostics, dynamic
  tools, session cleanup, and unload cleanup as a final safety net.
- Token Manager demonstrates a listener-only plugin with configuration,
  diagnostics, model-request inspection, public collector methods, and
  unload-time in-memory state reset.
  It reads `EventContext.messages` for history accounting and keeps its own
  diagnostics; event payloads are not a plugin persistence channel.
