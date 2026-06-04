# XBotv2 Architecture Plan

## Context

Current XBot (Hermes) is an AI agent runtime with file-backed state, LangGraph ReAct loop, tool sandbox/permissions, and JSONL protocol TUI. After extensive use, several architectural limitations emerged:

- **6 hook stages only** (loop-only): no session lifecycle, turn boundaries, message interception, or error hooks
- **No plugin system**: DAG planning, compaction, skills, memory are hard-baked into `build_runtime_components()`
- **Planning deeply coupled to core**: `TaskStateStore` has ~10 plan-specific methods, plan tools hard-registered, DAG projection baked into `project_context()`
- **State bloat**: ~6 of ~24 state fields are DAG-specific; plugin concerns leak into core
- **Tests unreliable**: monolithic files, ContextVar leakage, module-level cache globals

XBotv2 is a **greenfield redesign** that shares no code with XBot but preserves its proven patterns (append-only logs, JSONL protocol, sandbox design, three-tier C/S architecture).

## Directory Structure

```
XBotv2/
  xbotv2/                      # Core package (NEVER imports from plugins)
    core/                      # Minimal ReAct engine
      engine.py                # Engine: linear ReAct loop
      state.py                 # Minimal state types (no DAG/plan/skills fields)
      context.py               # ContextBuilder with plugin fragment injection
      bootstrap.py             # Full bootstrap sequence
    hooks/                     # Complete hook lifecycle
      manager.py               # HookManager: register, execute, lifecycle stages
      types.py                 # HookStage enum (42 stages), HookContext dataclass
    plugin/                    # Plugin system
      loader.py                # PluginLoader: discover, resolve deps, load
      base.py                  # PluginBase abstract class
      manifest.py              # PluginManifest pydantic model
      store.py                 # PluginStore: per-plugin isolated K/V store
    tools/                     # Tool system (ported from XBot, extended)
      registry.py              # ToolRegistry (with plugin ownership tracking)
      sandbox.py               # SandboxPolicy (bubblewrap-based)
      permissions.py           # PermissionSystem (deny→allow→ask precedence)
      runtime.py               # Tool execution with hook integration
    protocol/                  # JSONL protocol (ported from XBot)
      server.py                # Stdio JSONL server
      frames.py                # ProtocolFrame, ProtocolEncoder
    tui/                       # TUI clients (ported from XBot)
      client.py                # Curses TUI client
      terminal.py              # Non-curses terminal client
    persistence/               # State persistence
      store.py                 # CoreStateStore (append-only JSONL)
      materializer.py          # Materialized view builder
    config/                    # Configuration
      loader.py                # YAML config loading
      models.py                # Pydantic config models
    llm/                       # LLM integration
      client.py                # Provider client factory
      mock.py                  # MockLLM for testing

  builtin_plugins/             # All optional plugins (core never imports from here)
    compact/                   # Context compaction
    planning/                  # DAG task/plan management
    skills/                    # Skill loading
    memory/                    # Long-term memory
    summary/                   # Summary artifacts
    mailbox/                   # Inter-agent messaging
    subagent/                  # Subagent management

  tests/
    core/                      # Core tests (NO plugins loaded)
    plugins/                   # Per-plugin test directories
    integration/               # Full integration tests

  docsv2/                      # New documentation
```

## Core Engine Design

### Minimal ReAct Loop (`xbotv2/core/engine.py`)

The engine runs a 3-node ReAct loop and contains NO references to plan, task, dag, skill, compact, memory, summary, or subagent concepts:

```python
class Engine:
    """Core ReAct loop. No plugin imports. No DAG, skills, or compaction logic."""

    def __init__(self, *, llm, tool_registry, hook_manager, state_store,
                 context_builder, sandbox_policy, permission_system,
                 checkpointer, config): ...

    async def run_turn(self, user_input: str) -> AsyncIterator[Event]:
        """Execute one turn through ReAct loop:
        1. on_turn_start hooks
        2. Loop: before_context → [context] → after_context
                 → before_agent → [llm call] → after_agent
                 → if tool_calls: before_tools → [execute] → after_tools → loop
                 → else: break
        3. on_turn_end hooks
        """
```

**Key properties:**

- No `mode` concept (chat/task) — that's a planning plugin concern
- No `current_plan_node_id` attribution — plugin hooks handle that
- No compaction thresholds — compact plugin's `before_context` hook checks those
- No skills summary injection — skills plugin's `before_agent` hook handles that

### Context Building (`xbotv2/core/context.py`)

The ContextBuilder assembles messages with pluggable fragment injection points:

```
[SystemMessage: system prefix (stable, memoized)]
[SystemMessage: plugin fragments at "system_instructions" stage]
[SystemMessage: runtime rules]
[SystemMessage: sandbox summary]
[... message history ...]
[SystemMessage: plugin fragments at "dag_suffix" stage]
[SystemMessage: current state (time, user, turn, mailbox — minimal)]
```

Plugins register fragments at named stages: `system_prefix`, `system_instructions`, `system_rules`, `dag_suffix`. Core renders them in order but never knows what they contain.

Cache invalidation uses explicit cache objects (constructor-injected), not module-level globals — this fixes the current test leakage problem.

## Hook System (42 Stages)

### Stage Definitions (`xbotv2/hooks/types.py`)

```python
class HookStage(Enum):
    # Session lifecycle
    ON_SESSION_INIT = "on_session_init"       # Before any state is created
    ON_SESSION_START = "on_session_start"     # New session begins
    ON_SESSION_RESUME = "on_session_resume"   # Session restored from checkpoint
    ON_SESSION_CLOSE = "on_session_close"     # Session shutting down

    # Turn lifecycle
    ON_TURN_START = "on_turn_start"           # User message received
    ON_TURN_END = "on_turn_end"               # Turn processing complete
    ON_STOP = "on_stop"                       # Turn stopped
    ON_STOP_FAILURE = "on_stop_failure"       # Turn stop or execution failed

    # User message intake
    BEFORE_USER_MESSAGE_ACCEPT = "before_user_message_accept"
    AFTER_USER_MESSAGE_ACCEPT = "after_user_message_accept"

    # Loop lifecycle (short-circuit on truthy return)
    BEFORE_CONTEXT = "before_context"         # Before context assembly
    PRE_COMPACT = "pre_compact"               # Before message-history replacement
    POST_COMPACT = "post_compact"             # After message-history replacement
    BEFORE_CONTEXT_BUILD = "before_context_build"  # Before ContextBuilder runs
    AFTER_CONTEXT = "after_context"           # After context assembly
    AFTER_CONTEXT_COMPONENTS_BUILD = "after_context_components_build"  # Source-tagged components built
    AFTER_CONTEXT_BUILD = "after_context_build"  # Final provider messages built
    BEFORE_AGENT = "before_agent"             # Before LLM call
    BEFORE_TOOL_SCHEMA_BIND = "before_tool_schema_bind"  # Before provider tool binding
    AFTER_TOOL_SCHEMA_BIND = "after_tool_schema_bind"  # Tools selected/bound for request
    BEFORE_MODEL_REQUEST = "before_model_request"  # Final provider request gate
    AFTER_MODEL_RESPONSE = "after_model_response"  # Raw provider response received
    ON_MODEL_REQUEST_ERROR = "on_model_request_error"  # Provider request failed
    AFTER_AGENT = "after_agent"               # After LLM call
    BEFORE_TOOLS = "before_tools"             # Before tool execution
    AFTER_TOOLS = "after_tools"               # After tool execution

    # Message lifecycle
    ON_USER_MESSAGE = "on_user_message"       # User input parsed
    ON_ASSISTANT_MESSAGE = "on_assistant_message"  # LLM response received
    ON_TOOL_MESSAGE = "on_tool_message"       # Tool result received

    # Tool call lifecycle
    ON_TOOL_CALLS_PARSED = "on_tool_calls_parsed"
    ON_PERMISSION_REQUEST = "on_permission_request"
    ON_PERMISSION_DENIED = "on_permission_denied"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    ON_TOOL_CALL_FAILURE = "on_tool_call_failure"
    POST_TOOL_BATCH = "post_tool_batch"
    ON_TOOL_DENIED = "on_tool_denied"
    ON_CLIENT_EVENT = "on_client_event"

    # Persistence lifecycle
    BEFORE_STATE_PERSIST = "before_state_persist"
    AFTER_STATE_PERSIST = "after_state_persist"

    # System events
    ON_ERROR = "on_error"                     # Error occurred
    ON_CONFIG_RELOAD = "on_config_reload"     # Config was reloaded
```

Hook failure semantics:

- Short-circuit guard stages propagate the first exception immediately.
- Normal observation stages log hook failures and continue.
- Critical lifecycle stages (`ON_SESSION_INIT`, `ON_SESSION_CLOSE`,
  `BEFORE_STATE_PERSIST`, `AFTER_STATE_PERSIST`) run every registered callback
  and then raise an `ExceptionGroup` if any callback failed.

### HookContext

Every hook receives a structured context:

```python
@dataclass
class HookContext:
    stage: HookStage
    state: dict[str, Any]              # Current agent state (messages, etc.)
    config: AgentConfig                # Current config snapshot
    tools: ToolRegistry                # For tool registration (init hooks only)
    plugin: PluginStore                # Per-plugin isolated K/V store (None for guard hooks)
    session: SessionInfo               # Session metadata
    emit: Callable[[Event], None]      # Emit system events
    # Stage-specific (populated by engine):
    user_input: str | None = None
    context_components: list[Any] | None = None
    context_messages: list[Any] | None = None
    agent_response: Any | None = None
    model_request: dict[str, Any] | None = None
    model_response: Any | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call: dict[str, Any] | None = None
    tool_results: list | None = None
    tool_result: Any | None = None
    stop_reason: str | None = None
    compact_reason: str | None = None
    permission_decision: str | None = None
    client_event: dict[str, Any] | None = None
    error: Exception | None = None
    short_circuit_result: Any | None = None
```

### Execution Semantics

- **LOOP/pre-request hooks** (before/after context/agent/tools and before_model_request): short-circuit on first truthy return — used by compact/plugin guards to replace messages, deny execution, or stop before provider calls
- **All other hooks** (session, turn, message, error, config): all registered functions always run; errors are logged, not propagated
- **ON_SESSION_INIT hooks** can register tools via `ctx.tools.register()`
- User intake, source-tagged context components, pre-bind tool filtering,
  provider request errors, compaction, permission, per-tool call and batch
  lifecycle hooks, client-directed events, stop/failure hooks, and persistence
  hooks expose the request surface needed by future token estimation, usage
  statistics, and token budget control plugins.

## Plugin System

### Plugin Manifest (`builtin_plugins/<name>/plugin.yaml`)

```yaml
name: planning
version: "1.0.0"
description: "DAG-based task planning"
depends_on: []

hooks:
  - stage: on_session_init
    handler: planning.hooks:on_init
  - stage: before_context
    handler: planning.hooks:inject_dag_context
  - stage: on_turn_end
    handler: planning.hooks:record_plan_events

tools:
  - handler: planning.tools:plan_add_nodes
  - handler: planning.tools:plan_next
  - handler: planning.tools:plan_update
  - handler: planning.tools:plan_autofill
  - handler: planning.tools:plan_node_history
  - handler: planning.tools:plan_remove_node

prompt_fragments:
  - stage: system_instructions
    file: prompts/instructions.md
  - stage: dag_suffix
    handler: planning.context:render_dag_state
```

### PluginBase (`xbotv2/plugin/base.py`)

```python
class PluginBase(ABC):
    def __init__(self, manifest: PluginManifest, store: PluginStore): ...
    async def on_load(self, config: dict) -> None: ...       # Init plugin
    async def on_unload(self) -> None: ...                    # Cleanup
    def register_hooks(self, manager: HookManager) -> None: ...
    def register_tools(self, registry: ToolRegistry) -> None: ...
    def get_prompt_fragments(self) -> dict[str, str]: ...
```

### PluginLoader (`xbotv2/plugin/loader.py`)

```
discover() → resolve_order() → load() → register_all() → unload()/unload_all()
```

1. Scan `builtin_plugins/` and config-specified dirs for `plugin.yaml`
2. Topological sort by `depends_on`
3. Import plugin classes, instantiate with PluginStore
4. Call `on_load()` on each
5. Register hooks, tools, and prompt fragments with core components
6. On unload, call `on_unload()` and remove recorded hooks, tools, prompt fragments, and temporary import paths

### PluginStore (`xbotv2/plugin/store.py`)

Each plugin gets an isolated key-value namespace backed by `CoreStateStore`:

```python
class PluginStore:
    async def get(key, default=None) -> Any
    async def set(key, value) -> None
    async def delete(key) -> None
    async def all() -> dict
```

Core persists plugin states as opaque blobs in `plugin_states/<plugin_name>.yaml`. Core never reads or interprets plugin state.

## Core State (Minimal)

### state.yaml structure

```yaml
schema_version: 2
session_id: str
thread_id: str
personality_id: str
turn_count: int
event_count: int
status: active | error | interrupted | closed
mailbox_pending: int
pending_interactions: list[dict]   # Rebuilt from user/permission request events; cleared on response or close
plugin_states:          # Opaque — core never reads, only persists
  planning: {...}
  compact: {...}
artifacts_root: str
updated_at: str
```

Status is derived from ordered events. `turn_started` reactivates prior
`error` or `interrupted` sessions; `turn_finished` does not clear an
interruption raised during that same turn.

**No more:** `mode`, `plan`, `dag`, `phase`, `context_tree`, `summaries`, `goal`, `active_node`, `ready_nodes`, `running_nodes`, `blocked_nodes`, etc. Those are all plugin concerns.

## Bootstrap Sequence

### `bootstrap()` (`xbotv2/core/bootstrap.py`)

```
1. Load config: personality.yaml, provider.yaml, user.yaml
2. Create CoreStateStore
3. Create empty HookManager
4. Create empty ToolRegistry
5. Create ContextBuilder
6. Register core base tools: filesystem, shell, and interaction tools (always available)
   - legacy placeholder `ask` is not a core tool
   - `send_message` emits non-blocking `client_message` events
   - `ask_user` emits `user_input_required`, waits for a live `user.input` on the active protocol connection, and returns the answer, timeout, or cancellation as the tool result
   - filesystem tools return JSON metadata and support structured read/list/write operations
   - default `AFTER_TOOLS` hook caches oversized tool outputs under session artifacts before persistence/protocol emit
7. Register personality-declared hooks from `hooks:` config entries; invalid targets fail loudly
8. Create SandboxPolicy + PermissionSystem
9. Discover plugins from plugin dirs
10. Resolve plugin dependency order (topological sort)
11. Initialize plugins (on_load) with their config sections
12. Register plugin hooks → HookManager
13. Register plugin tools → ToolRegistry
14. Register plugin prompt fragments → ContextBuilder
15. Create LLM client
16. Run ON_SESSION_INIT hooks
17. Return fully-wired Engine
```

## How Each Current Feature Becomes a Plugin

### Planning Plugin (`builtin_plugins/planning/`)

- **What it moves**: DAG validator/scheduler (`planning.py`), plan store (`task_plan_store.py`), 6 plan tools, DAG context rendering (`state_context.py` plan sections), plan versioning
- **Hooks registered**: `on_session_init` (init plan state), `before_context` (inject DAG projection into context), `on_turn_end` (record plan node attribution)
- **Tools**: `plan_add_nodes`, `plan_next`, `plan_update`, `plan_autofill`, `plan_node_history`, `plan_remove_node`
- **State owned**: plan.yaml, versions/plans/, goal.md, task.yaml — all within plugin namespace
- **Without it**: agent runs linearly, no task mode, no DAG — works fine

### Compact Plugin (`builtin_plugins/compact/`)

- **What it moves**: `compaction.py` logic, `compact` tool, compaction thresholds
- **Hooks registered**: `before_context` (check thresholds, summarize old messages, short-circuit), `after_tools` (detect compact tool call flag)
- **Tools**: `compact`
- **Without it**: no automatic compaction — agent runs with full history

### Skills Plugin (`builtin_plugins/skills/`)

- **What it moves**: `skills.py` loading/resolution/rendering, `skill_load` tool
- **Hooks registered**: `on_config_reload` (rescan skill files)
- **Tools**: `skill_load`
- **Prompt fragments**: `system_instructions` stage — renders available skills section
- **Without it**: no skill system — agent works without skill context

### Memory Plugin (`builtin_plugins/memory/`)

- **What it moves**: Memory storage and search
- **Tools**: `memory_search`, `memory_list`, `memory_update`
- **Without it**: no persistent memory — agent works without

### Summary Plugin (`builtin_plugins/summary/`)

- **What it moves**: Summary artifact management
- **Tools**: `summary_add`, `summary_list`, `summary_read`
- **Without it**: no summary recording — agent works without

### Mailbox Plugin (`builtin_plugins/mailbox/`)

- **What it moves**: Mailbox send/read logic, mailbox event recording
- **Hooks registered**: `on_turn_start` (check pending mailbox)
- **Tools**: `mailbox_send`, `mailbox_read`
- **Without it**: no inter-agent messaging — agent works without

### Subagent Plugin (`builtin_plugins/subagent/`)

- **What it moves**: Subagent spawn/monitor logic
- **Tools**: `subagent_create`, `subagent_list`, `subagent_stop`, `subagent_wait`
- **Without it**: no subagents — agent works solo

## Critical Architecture Constraint

```
Plugins ──import──→ Core (xbotv2)
Core ──NEVER imports──→ Plugins (builtin_plugins)
```

Core defines interfaces (`HookStage`, `HookContext`, `PluginBase`, `ToolRegistry`). Plugins implement them. Bootstrap discovers and wires at runtime. This is dependency inversion.

## Protocol & TUI

The JSONL protocol is the best-isolated part of current XBot. XBotv2 copies the design directly:

- `ProtocolFrame` (identical Pydantic model: protocol_version, frame_id, seq, ts, direction, type, session_id, thread_id, request_id, payload)
- `ProtocolEncoder` — maps internal events to wire frames
- `ProtocolClient` — reads/writes JSONL via stdin/stdout subprocess
- interaction events (`client_message`, `permission_request`,
  `permission_denied`, `user_input_required`) stream through JSONL with request
  correlation and are rendered by TUI state
- unresolved `permission_request` and `user_input_required` events are
  materialized as `pending_interactions` in `state.yaml`
- client response commands (`user.input`, `permission.response`) append
  response events with the original request snapshot and clear matching pending
  interactions; live `ask_user` responses continue the current ReAct turn, while
  client disconnect records cancellation and stops the turn
- `provider: mock` — deterministic provider used for server subprocess smoke tests
- `CursesTuiClient` — background reader thread + curses event loop
- `TerminalClientSession` — async readline loop

**Boundary preserved**: TUI clients never import LangChain, LangGraph, or runtime modules.

## Testing Strategy

### Core Tests (`tests/core/`) — NO plugins loaded

- `test_engine.py` — ReAct loop with MockLLM, base tools only
- `test_hooks.py` — each HookStage tested independently with synthetic hooks
- `test_state.py` — CoreStateStore append-only behavior
- `test_context.py` — ContextBuilder fragment injection, cache isolation
- `test_tool_registry.py` — registration, filtering, plugin ownership
- `test_bootstrap.py` — bootstrap with zero plugins, bootstrap with test plugins
- `test_protocol.py` — frame serialization/deserialization, encoder, provider config, stdio server subprocess roundtrip, interaction event streaming
- `test_sandbox.py` — guard_tool_call, path resolution, denials, one-call approvals
- `test_permissions.py` — deny→allow→ask precedence
- `test_plugin_loader.py` — discovery, dependency resolution, cycle detection, atomic rollback/import-path cleanup

### Plugin Tests (`tests/plugins/<name>/`) — loads ONLY that plugin

- `test_tools.py` — tool behavior in isolation
- `test_hooks.py` — hook integration with core engine
- `test_store.py` — plugin state persistence

### Integration Tests (`tests/integration/`)

- `test_engine_with_plugins.py` — engine with all plugins loaded
- `test_tui_protocol.py` — end-to-end protocol roundtrip

### Testing Principles

1. **No module-level state** — all caches, registries are constructor-injected objects
2. **No ContextVar leakage** — `autouse` fixtures reset after each test
3. **`temp_data_dir` only** — never real `data/sessions/`
4. **MockLLM** — deterministic, configurable response sequences, with recorded request messages for context assertions
5. **Each test creates its own engine** — no shared state between tests

## Implementation Phases

### Phase 1: Core Foundation

- Create `XBotv2/` directory structure
- Implement `xbotv2/hooks/` — HookStage, HookContext, HookManager
- Implement `xbotv2/core/state.py` — CoreStateStore with append-only JSONL
- Implement `xbotv2/tools/` — ToolRegistry, SandboxPolicy, PermissionSystem, tool-result cache hook
- Implement `xbotv2/core/context.py` — ContextBuilder with fragment injection
- Implement `xbotv2/core/engine.py` — minimal ReAct loop
- Implement `xbotv2/persistence/` — store + materializer
- Implement `xbotv2/llm/` — client + mock provider config
- Write all core tests → **core tests pass**

### Phase 2: Plugin System

- Implement `xbotv2/plugin/manifest.py` — PluginManifest model
- Implement `xbotv2/plugin/base.py` — PluginBase ABC
- Implement `xbotv2/plugin/store.py` — PluginStore
- Implement `xbotv2/plugin/loader.py` — PluginLoader
- Implement `xbotv2/core/bootstrap.py` — full bootstrap with plugin discovery
- Write plugin system tests → **plugin tests pass**

### Phase 3: Protocol & TUI

- Implement `xbotv2/protocol/` — port from XBot (ProvenFrame, ProtocolEncoder)
- Implement `xbotv2/tui/` — port from XBot (CursesTuiClient, TerminalClientSession)
- Implement `xbotv2/protocol/server.py` — JSONL stdio server
- Write protocol/server tests → **protocol tests pass**
- Curses TUI/client state and boundary coverage → **TUI tests pass**

### Phase 4: Plugin Migration (one at a time)

- Compact plugin → test independently
- Planning plugin → test independently
- Skills plugin → test independently
- Memory, Summary, Mailbox, Subagent → each tested independently

### Phase 5: Integration & Docs

- Integration tests with all plugins
- Write `docsv2/` — architecture, core, hooks, plugins, state, protocol, testing, migration
- Smoke test end-to-end

## Verification

1. **Core tests pass with zero plugins**: from repo root, `uv run pytest XBotv2/tests/core/ -q`
2. **Each plugin's tests pass independently**: from repo root, `uv run pytest XBotv2/tests/plugins/planning/ -q`
3. **Integration tests pass**: from repo root, `uv run pytest XBotv2/tests/integration/ -q`
4. **Smoke test**: Run engine with personality config, send messages, verify responses
5. **Plugin absence test**: Remove all plugins from config, verify engine still works (linear ReAct)
6. **Protocol server test**: Launch `python -m xbotv2 --mode server` with mock provider, verify JSONL roundtrip
7. **TUI test**: Launch non-curses TUI client against engine server, verify client wrapper roundtrip
8. **Curses TUI test**: Import and smoke-test curses client boundaries without loading runtime modules
