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

| Tree id | Import name | Profile | Required injected services | Provides / primary role |
|---|---|---|---|---|
| `config` | `config` | agent | `runtime_log`, `runtime_paths`, `session_launch` | `settings`; policy/config operations |
| `persistence` | `persistence` | agent | `loop_state`, `thread_persistence`, `runtime_log` | hydrates canonical thread persistence |
| `usage` | `usage` | agent | `state`, `loop_state`, `runtime_log` | `usage` snapshot and usage events |
| `agent-catalog` | `agents.catalog_provider` | agent | `data_root`, `variables`, `workspace_root` | `agent_catalog` |
| `session` | `session` | agent | `runtime_paths`, `session_launch`, `commands`, `artifacts` | `session`, `paths`, `thread_paths`, `loop_state`, `variables` |
| `jobs` | `jobs` | agent | `commands`, `engine` | `jobs`; shell/subagent lifecycle |
| `commands` | `commands` | agent | none | `commands`; human command registry |
| `llm` | `llm` | agent, server | `runtime_log` | `llm`, `model`; provider catalog |
| `tools` | `agentloop.tools` | agent | `runtime_log` | `tools`; standard Tool registry/executor |
| `agentloop` | `agentloop.runtime` | agent | loop-owned services | Agent ReAct loop factory/runtime |
| `agent-runtime` | `agents.runtime` | agent | catalog, loop factory, settings, llm/model, tools, artifacts, loop state, commands, launch facts, metadata, log | `agent_runtime`, `engine` |
| `llm-commands` | `llm.runtime_commands` | agent | agent runtime / commands | provider/model selection commands |
| `context_builder` | `context_builder` | agent | `runtime_log` | `context_builder`; context assembly |
| `prompts` | `prompts` | agent | `context_builder` | `prompts`; prompt fragment registry |
| `sandbox` | `sandbox` | agent | thread paths, session, tools, data/workspace roots, variables, commands, settings | `sandbox`; sandbox guard and context facts |
| `permission_request` | `permission_request` | agent | `client_events` | `approval`; live approval waiter |
| `permissions` | `permissions` | agent | session/launch, parent permissions, tools, approval, variables, commands, settings | `permissions`; permission guard and policy commands |
| `coretools` | `coretools` | agent | tools, session, artifacts, sandbox, jobs, workspace root | filesystem/Shell Tools and result-cache hook |
| `subagents` | `agents.subagent_tools` | agent | agent runtime, jobs, permissions, tools | subagent Tools |
| `goal` | `goal` | agent | tools, commands, engine, state | `goal`; objective Tools, `/goal`, status slot |
| `todolist` | `todolist` | agent | tools, state | `todolist`; `update_todos` Tool and snapshot operation |
| `skills` | `skills` | agent | tools, commands, sandbox, runtime paths | discovered skill Tools and prompt commands |
| `mcp_plugin` | `mcp_plugin` | agent | tools, model, interactions, session | configured MCP Tools/resources/prompts |
| `content_cache` | `content_cache` | agent | artifacts | current oversized-user-input provider projection |
| `compact` | `compact` | agent | tools, commands, model, loop state, usage | compaction Tool/command and history events |
| `browser` | `browser` | agent | tools, session, sandbox, artifacts | Web research and isolated browser Tools |
| `token_manager` | `token_manager` | agent | session | request/context observation diagnostics |
| `workspace_instructions` | `workspace_instructions` | agent | variables, workspace root | `AGENTS.md` context contribution |
| `interactions` | `interactions` | agent | tools, client events, session launch | `interactions`; `ask_user` and message delivery |
| `process.persistence` | `persistence.process` | server, acp | process launch services | process-level persistence host |
| `process.sessions` | `session.host` | server, acp | process launch services | `sessions`; session manager |
| `process.workspaces` | `workspaces` | server, acp | runtime log, sessions, state, workspace root | `workspaces`, `workspace_events`, `workspace_directories` |
| `acp` | `acp_plugin` | acp | `sessions`, `acp_launch`, `runtime_log` | ACP carrier (`acp_agent`) |
| `server` | `server` | server | `runtime_log` | FastAPI carrier (`server`) |
| `server.routes.core` | `server.routes` | server | `server`, `server_info` | health/provider/core routes |
| `server.routes.session` | `session.http` | server | `server`, `sessions`, `server_options`, `workspace_events` | session/workspace/thread HTTP routes |
| `server.routes.workspaces` | `workspaces.http` | server | `server`, `workspaces`, `workspace_directories`, `workspace_events` | workspace catalog and directory routes |
| `server.routes.jobs` | `jobs.http` | server | `server`, `sessions` | job listing/status routes |
| `server.routes.agents` | `agents.http` | server | `server`, `sessions` | Agent catalog and selection routes |
| `server.routes.llm` | `llm.http` | server | `server`, `llm`, `sessions` | provider/model catalog and selection routes |
| `server.routes.config` | `config.http` | server | `server`, `sessions` | settings and policy routes |

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

Use the constructors in `XBotv2.application.services` and
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
