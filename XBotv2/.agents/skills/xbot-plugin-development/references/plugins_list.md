# XBot Built-in Plugin Quick Reference

This is the first page to read when deciding whether a plugin already provides
the capability you need. It describes the bundled `XBotv2/xcore.yaml` tree as
of the installed XBot version. `id` is the tree identity used by overlays;
`name` is the import specifier. They are deliberately different in several
entries.

## Tree and process profiles

The Agent profile starts with the bundled entries, adds external entries from
the Python `plugin_dirs` argument, then applies (in order) the configured data
directory `plugins.yaml` overlay, the workspace `.xbot/plugins.yaml` overlay,
the session `config.yaml` overlay, and in-memory launch overrides. The client,
server, and ACP carrier profiles apply the bundled tree and the data-directory
overlay; they do not read the Agent workspace/session overlays. Later
overlays preserve omitted fields. `config` is recursively merged; `name`,
`profiles`, `disabled`, and `isolate` replace the previous value.

An entry with no `profiles` key belongs to the Agent profile only. Carrier
profiles are selected by `XBotv2.application.tree`: `load_agent_tree`,
`load_client_tree`, `load_server_tree`, and `load_acp_tree`. The `client`
carrier is the local client-plugin composition (client transport plus the
Textual TUI); it is not an Agent, server, or ACP process.

| Tree id | Import name | Profile | Dependency-gated integrations | Provides / primary role |
|---|---|---|---|---|
| `config` | `config` | agent, server | Agent: runtime paths and launch; server: `server`, `sessions` | Agent `settings`; policy/config HTTP routes |
| `client-transport` | `client_transport` | client | `client_launch` | `client_api` (`XBotClient`) |
| `textual-tui` | `tui` | client | `client_api`, `client_launch`, `commands` | `terminal_client`; Textual TUI and local command registration |
| `persistence` | `persistence` | agent, server, acp | Agent: loop state and thread persistence | process reader factory; Agent history/metadata hydration |
| `usage` | `usage` | agent | `state`, `loop_state`, `runtime_log` | `usage` snapshot and usage events |
| `agents` | `agents` | agent, server | Agent: runtime variables/catalog and loop dependencies; server: `server`, `sessions` | `agent_catalog`, `agent_runtime`, `engine`; Agent HTTP routes |
| `session` | `session` | agent, server, acp | Agent: launch, commands, artifacts; carrier: persistence/application factories; server: route dependencies | thread runtime, process `sessions`, Session HTTP routes |
| `jobs` | `jobs` | agent, server | Agent: `commands`, `engine`; server: `server`, `sessions` | `jobs`; task HTTP routes |
| `commands` | `commands` | agent, server, client | server route integration waits for `server`, `sessions` | human command registry; command HTTP routes |
| `llm` | `llm` | agent, server | runtime log; Agent commands wait for Agent runtime; HTTP waits for sessions | `llm`, `model`; provider catalog, commands, HTTP routes |
| `agentloop` | `agentloop` | agent, server | Agent launch/runtime log; server routes wait for sessions | Tool registry/executor and Agent loop factory; Tool HTTP routes |
| `context_builder` | `context_builder` | agent | `runtime_log`, `artifacts` | `context_builder`; typed context compilation and prompt component registry |
| `prompts` | `prompts` | agent | `context_builder` | `prompts`; prompt fragment registry |
| `sandbox` | `sandbox` | agent | thread paths, session, tools, data/workspace roots, variables, commands, settings | `sandbox`; sandbox guard and context facts |
| `permissions` | `permissions` | agent | session/launch, parent permissions, tools, client_events, interactions, variables, commands, settings, state | `permissions`, `approval`; guard, approval waiter, persistent grants and policy commands |
| `coretools` | `coretools` | agent | tools, session, artifacts, sandbox, jobs, workspace root; optional permissions | filesystem and Shell Tools; workspace hooks and Tools |
| `subagents` | `subagents` | agent | session, catalog, child applications, permissions, jobs, tools, persistence | subagent Tools and prompt catalog |
| `goal` | `goal` | agent | tools, loop state, commands, engine, model, state, usage; optional jobs, todolist | `goal`; objective Tools, `/goal`, status slot |
| `todolist` | `todolist` | agent, server | Agent: tools/state; server: sessions | Todo Tool/state and HTTP routes |
| `skills` | `skills` | agent | tools, commands, sandbox, runtime paths | discovered skill Tools and prompt commands |
| `mcp_plugin` | `mcp_plugin` | agent | tools, model, interactions, session, usage, loop state | configured MCP Tools/resources/prompts; public id in `mcp_plugin/contracts.py` |
| `content_cache` | `content_cache` | agent | artifacts | lossless externalization of oversized accepted user input and Tool output |
| `compact` | `compact` | agent | tools, commands, model, loop state, usage | compaction Tool/command and history events |
| `browser` | `browser` | agent | tools, session, sandbox, artifacts | Web research and isolated browser Tools |
| `token_manager` | `token_manager` | agent | session | request/context observation diagnostics |
| `workspace_instructions` | `workspace_instructions` | agent | variables, workspace root | `AGENTS.md` context contribution |
| `interactions` | `interactions` | agent | tools, client events, session launch | `interactions`; `ask_user` and message delivery |
| `caption` | `caption` | agent | tools, model, loop state, session, agent options, usage | `caption`; session title Tool and context contribution |
| `workspaces` | `workspaces` | server, acp | runtime log, sessions, state, workspace root; HTTP additionally waits for server | `workspaces`, events, directory browser, HTTP routes |
| `acp` | `acp_plugin` | acp | `sessions`, `acp_launch`, `runtime_log` | ACP carrier (`acp_agent`) |
| `server` | `server` | server | `runtime_log` | FastAPI carrier and its own health/core routes |

For the exact surface of one row, read the matching page under
[`plugins/`](plugins/README.md). A service listed here is a composition
capability, not a license to import that plugin's implementation module.

The row is intentionally a navigation aid, not a complete API contract. The
detail pages identify the package-root contracts and the owning source file;
the installed version's exports and tests remain authoritative.

## Application-injected services

These values are supplied by an application composition root before XCore
starts. They do not come from another plugin and are the usual answer when a
minimal integration test reports `FiberState.PENDING`:

| Service | Agent application value | Client carrier value | Server carrier value | ACP carrier value |
|---|---|---|---|---|
| `runtime_log` | boot's `RuntimeLog` | same | same | same |
| `runtime_paths` | `RuntimePaths` for the selected `data_dir` | same | process `RuntimePaths` | process `RuntimePaths` |
| `workspace_root` | selected workspace `Path` | not provided | server workspace `Path` | data root `Path` (carrier default) |
| `client_launch` | not provided | `ClientLaunch` for one client run | not provided | not provided |
| `session_launch` | `SessionLaunch` for one session/thread | not provided | not provided | not provided |
| `agent_options` | typed Agent launch facts | not provided | not provided | not provided |
| `thread_persistence` | `ThreadPersistence` when persistence is enabled | not provided | not provided | not provided |
| `thread_metadata` | in-memory metadata only when persistence is disabled | not provided | not provided | not provided |
| `artifacts` | thread `ArtifactStore` | not provided | not provided | not provided |
| `client_events` | `ClientEventRouter` | not provided | not provided | not provided |
| `child_applications` | child Agent application owner | not provided | not provided | not provided |
| `parent_permissions` | parent permission intersection | not provided | not provided | not provided |
| `plugin_overrides` | in-memory launch overlay list | not provided | not provided | not provided |
| `plugin_dirs` | external plugin import roots | not provided | not provided | not provided |
| `server_options` | not provided | not provided | `ServerOptions` | not provided |
| `agent_application_factory` | not provided | not provided | `create_agent_application` | `create_agent_application` |
| `acp_launch` | not provided | not provided | not provided | `ACPLaunch` |

Two further services are provided by plugins rather than by a composition root,
and both are easy to miss when reading the tree table:

- `agent_inbox` (`AgentInbox`) is provided by `persistence` when durability is
  active, and otherwise by `session` as a transient inbox. `agents` requires
  it, so it gates when the Agent engine can be constructed — the availability
  of the service, not plugin-tree order, decides the mount point.
- `variables` (`RuntimeVariables` for the active thread) is provided by
  `session`.

Use the contracts in `XBotv2.application` and constructors in the owning
application modules, together with
`XBotv2.core.paths` in a test harness. `Context(data_dir=...)` supplies the
XCore `state` service; the Agent test harness must still provide every other
declared dependency explicitly.

## Operations and event families

The protocol layer adapts these typed operations; a plugin should register the
operation with `ctx.on(operation.name, handler)` and return the operation's
declared result type:

| Owner | Operations / events worth extending |
|---|---|
| Tools | `agentloop` Tool registry, `LIST_TOOLS`, `before/tool-call`, `after/tool-call` |
| Commands | `commands/list`, `commands/execute`; `Command(kind="prompt")` is client prompt expansion |
| Context | `context/build` (`ContextBuildRequest`), `after/context-components-build` (`BuiltContext`) |
| Model | `before/model-request`, `model/request-ready`, `after/model-response`, `model/response-observed`, `model/request-error` |
| Session | `session/start`, `session/resume`, `session/close`, `session/history-changed` |
| Turn/input | `input/received`, `input/accepted`, `turn/start`, `turn/end`, `error`, `stop`, `stop/failure` |
| Tools/runtime | `agent/inbox/changed`, `tool/calls-observed`, `tool/batch-observed`, `tool/message-observed`, `state/changed` |
| Client/application | `client/event` via `ClientEventsPort`; application `runtime/event` carries typed runtime events |
| Compaction | `before/compact`, `after/compact` (compact-owned events) |
| Application | `session/init` (`ApplicationInitialized`), `application/status-slots/collect` |
| Workspace/session host | `session/resource-changed`, `session/resource-removed`, workspace catalog events |

Agent-loop hook events in the `SHORT_CIRCUIT_EVENTS` set use `ctx.serial` and
have a documented return contract; observers use `ctx.emit` and return
`None`. Event payload types belong to their producer packages and differ by
stage; consult those definitions instead of assuming a universal event
context. Define new cross-plugin facts in the owning package.

## State and file ownership

| Data | Canonical owner | Plugin rule |
|---|---|---|
| conversation surface and append-only trajectory | `ThreadPersistence.history` / `ConversationHistory` | never copy messages into plugin state |
| pending user inputs | `ThreadPersistence.inbox` / Agent inbox | use the inbox API; do not create a second queue |
| artifacts | `ArtifactStore` via `ctx.artifacts` | use typed artifact references, not hand-built paths |
| usage | `ctx.state.namespace("usage")` through `UsageService` | record deltas; do not recalculate from duplicated history |
| Todo/Goal/plugin data | the owning plugin's `ctx.state.namespace(name)` | one typed snapshot per related state |
| runtime waiters/clients/jobs | owning live service | never persist handles or Context objects |

The physical thread layout is exposed by `RuntimePaths`, `SessionPaths`, and
`ThreadPaths`; plugins do not join `data_dir`, `plugin_state`, or artifact
subdirectories themselves.
