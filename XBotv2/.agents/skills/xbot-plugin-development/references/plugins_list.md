# XBot Built-in Plugin Quick Reference

This is the first page to read when deciding whether a plugin already provides
the capability you need. It describes the bundled `XBotv2/xcore.yaml` tree as
of the installed XBot version. `id` is the tree identity used by overlays;
`name` is the import specifier. They are deliberately different in several
entries.

## Tree and process profiles

The Agent profile starts with the bundled entries, adds external entries from
the Python `plugin_dirs` argument, then applies (in order) the configured data
directory overlay, the workspace `.xbot/plugins.yaml` overlay, and session
overrides. The server and ACP carrier profiles apply the bundled tree and the
data-directory overlay; they do not read the Agent workspace overlay. Later
overlays preserve omitted fields. `config` is recursively merged; `name`,
`profiles`, `disabled`, and `isolate` replace the previous value.

| Tree id | Import name | Profile | Dependency-gated integrations | Provides / primary role |
|---|---|---|---|---|
| `config` | `config` | agent, server | Agent: runtime paths and launch; server: `server`, `sessions` | Agent `settings`; policy/config HTTP routes |
| `persistence` | `persistence` | agent, server, acp | Agent: loop state and thread persistence | process reader factory; Agent history/metadata hydration |
| `usage` | `usage` | agent | `state`, `loop_state`, `runtime_log` | `usage` snapshot and usage events |
| `agents` | `agents` | agent, server | Agent: runtime variables/catalog and loop dependencies; server: `server`, `sessions` | `agent_catalog`, `agent_runtime`, `engine`; Agent HTTP routes |
| `session` | `session` | agent, server, acp | Agent: launch, commands, artifacts; carrier: persistence/application factories; server: route dependencies | thread runtime, process `sessions`, Session HTTP routes |
| `jobs` | `jobs` | agent, server | Agent: `commands`, `engine`; server: `server`, `sessions` | `jobs`; task HTTP routes |
| `commands` | `commands` | agent, server | server route integration waits for `server`, `sessions` | human command registry; command HTTP routes |
| `llm` | `llm` | agent, server | runtime log; Agent commands wait for Agent runtime; HTTP waits for sessions | `llm`, `model`; provider catalog, commands, HTTP routes |
| `agentloop` | `agentloop` | agent, server | Agent launch/runtime log; server routes wait for sessions | Tool registry/executor and Agent loop factory; Tool HTTP routes |
| `context_builder` | `context_builder` | agent | `runtime_log` | `context_builder`; context assembly |
| `prompts` | `prompts` | agent | `context_builder` | `prompts`; prompt fragment registry |
| `sandbox` | `sandbox` | agent | thread paths, session, tools, data/workspace roots, variables, commands, settings | `sandbox`; sandbox guard and context facts |
| `permissions` | `permissions` | agent | session/launch, parent permissions, tools, client_events, interactions, variables, commands, settings, state | `permissions`, `approval`; guard, approval waiter, persistent grants and policy commands |
| `coretools` | `coretools` | agent | tools, session, artifacts, sandbox, jobs, workspace root | filesystem/Shell Tools and result-cache hook |
| `subagents` | `subagents` | agent | session, catalog, child applications, permissions, jobs, tools, prompts, persistence | subagent Tools and prompt catalog |
| `goal` | `goal` | agent | tools, commands, engine, state | `goal`; objective Tools, `/goal`, status slot |
| `todolist` | `todolist` | agent, server | Agent: tools/state; server: sessions | Todo Tool/state and HTTP routes |
| `skills` | `skills` | agent | tools, commands, sandbox, runtime paths | discovered skill Tools and prompt commands |
| `mcp_plugin` | `mcp_plugin` | agent | tools, model, interactions, session | configured MCP Tools/resources/prompts; public id in `mcp_plugin/contracts.py` |
| `content_cache` | `content_cache` | agent | artifacts | current oversized-user-input provider projection |
| `compact` | `compact` | agent | tools, commands, model, loop state, usage | compaction Tool/command and history events |
| `browser` | `browser` | agent | tools, session, sandbox, artifacts | Web research and isolated browser Tools |
| `token_manager` | `token_manager` | agent | session | request/context observation diagnostics |
| `workspace_instructions` | `workspace_instructions` | agent | variables, workspace root | `AGENTS.md` context contribution |
| `interactions` | `interactions` | agent | tools, client events, session launch | `interactions`; `ask_user` and message delivery |
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

| Service | Agent application value | Server carrier value | ACP carrier value |
|---|---|---|---|
| `runtime_log` | boot's `RuntimeLog` | same | same |
| `runtime_paths` | `RuntimePaths` for the selected `data_dir` | process `RuntimePaths` | process `RuntimePaths` |
| `workspace_root` | selected workspace `Path` | server workspace `Path` | data root `Path` (carrier default) |
| `session_launch` | `SessionLaunch` for one session/thread | not provided | not provided |
| `agent_options` | typed Agent launch facts | not provided | not provided |
| `thread_persistence` | `ThreadPersistence` when persistence is enabled | not provided | not provided |
| `thread_metadata` | in-memory metadata only when persistence is disabled | not provided | not provided |
| `artifacts` | thread `ArtifactStore` | not provided | not provided |
| `client_events` | `ClientEventRouter` | not provided | not provided |
| `child_applications` | child Agent application owner | not provided | not provided |
| `parent_permissions` | parent permission intersection | not provided | not provided |
| `server_options` | not provided | `ServerOptions` | not provided |
| `agent_application_factory` | not provided | `create_agent_application` | `create_agent_application` |
| `acp_launch` | not provided | not provided | `ACPLaunch` |

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
| Context | `context/build`, `after/context-components-build`, `after/context-build` |
| Model | `model/request-ready`, `after/model-response`, `before/model-request` |
| Session | `session/start`, `session/resume`, `session/close`, `session/history-changed` |
| Turn | `turn/start`, `turn/end`, `stop`, `stop/failure` |
| Tools/runtime | `before/tools`, `after/tools`, `tool/calls-parsed`, `tool/batch-done`, `agent/inbox/spliced` |
| Client | `client/event`, application `runtime/event` |
| Compaction | `before/compact`, `after/compact` |
| Application | `session/init` (`ApplicationInitialized`), `application/status-slots/collect` |
| Workspace/session host | `session/resource-changed`, `session/resource-removed`, workspace catalog events |

Agent-loop hook events in the `SHORT_CIRCUIT_EVENTS` set use `ctx.serial` and
have a documented return contract; observers use `ctx.emit` and return
`None`. See [xcore-api.md](xcore-api.md) for the complete event names and
`EventContext` fields. Do not invent an `EventContext` payload for a new
cross-plugin fact: define a typed contract in the owning package.

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
