# Built-in owner quick reference

The bundled `XBotv2/xcore.yaml` is the declaration source. Each entry identifies
an implementation module; actual scope functions determine what is created.
Configuration order is bundled, global, workspace, session, explicit override.
Equal IDs merge config and preserve omitted fields; `optional`, `disabled`,
`profiles`, `name` and isolation declarations are explicit entry facts.

`loader/api/tree.py` constructs process/profile trees; `config/api/tree.py`
constructs fully expanded thread trees. The loader imports and mounts facets.
`no_plugins` removes optional entries. It does not name business capabilities.
The process profile selects a carrier while retaining shared process resources;
thread scopes use the Agent tree and their workspace/session overlays.

| Owner | Process / session resource | Thread capability |
|---|---|---|
| config | default files and HTTP config routes | SETTINGS |
| persistence | PERSISTENCE readers and session writer claim | THREAD_PERSISTENCE, artifacts |
| session | SESSIONS, SESSION_INFO owner | THREAD_INFO, THREAD_EVENTS, paths/variables |
| llm | provider directories and connections by config | LLM directory, independent MODEL |
| agents | definition infrastructure | AGENT_CATALOG and AGENT_RUNTIME selection |
| agentloop | business HTTP routes | AGENT_LOOP, LOOP_STATE, INBOX, TOOLS; internal Engine |
| commands | carrier command registry/routes | private COMMANDS registry |
| jobs | one registry for the process | authorized JOBS binding for this thread lifetime |
| subagents | session collaboration coordinator | delegation tools and owned children |
| interactions | — | CLIENT_EVENTS, INTERACTIONS, user input |
| permissions / sandbox | — | permission guard/approval and sandbox policy |
| coretools | — | standard filesystem and Shell tools |
| context_builder / prompts | — | context compilation and fragments |
| usage | — | usage snapshot and events |
| content_cache | — | lossless content externalization |
| skills / MCP / browser | — | optional standard tools/resources |
| compact / goal / todolist | business routes where declared | optional history/objective/checklist capabilities |
| workspace_instructions / token_manager / caption | — | optional instruction/observation/title features |
| workspaces | workspace catalog/events/routes | — |
| server | FastAPI and HTTP_LISTENER | — |
| acp_plugin | ACP_SERVER | — |
| client_transport / tui | CLIENT_API / TERMINAL_CLIENT | — |

`TerminalClient` and `TERMINAL_CLIENT` belong only to `tui/api/service.py`.
Packages keep declarations in `api/`, implementation in `impl/`, owner docs in
`doc/` and focused tests in `tests/`. Package roots export no facades. Consult
[the architecture](xbot-architecture.md) and each [owner page](plugins/README.md).

CLI supplies process paths/logging and the public loader constructor. The
session manager supplies only identity, explicit Agent choice facts and generic
scope configuration facts. Business facets initialize their own resources.
`SESSIONS.open` / `open_thread` return actual Contexts. Consumers locate the
thread then require its business API; there is no application service bag or
session method forwarding another owner's operations.

Scopes isolate even disabled facets' declared services. Required failures or
cycles prevent opening; optional failures dispose their facet. Close blocks
admissions and awaits owned work before removing effects and releasing locks.

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
| Client events | `ClientEventsPort` belongs to interactions; `runtime/event` belongs to agentloop |
| Compaction | `before/compact`, `after/compact` (compact-owned events) |
| Initialization/status | `THREAD_INITIALIZED` belongs to agentloop; status slots belong to interactions |
| Workspace/session | `session/resource-changed`, `session/resource-removed`, workspace catalog events |

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
| usage | `ctx.state.namespace("usage")` through `UsageService` | persist cumulative counters; derive only the latest turn observation from canonical assistant messages |
| Todo/Goal/plugin data | the owning plugin's `ctx.state.namespace(name)` | one typed snapshot per related state |
| runtime waiters/clients/jobs | owning live service | never persist handles or Context objects |

The physical thread layout is exposed by `RuntimePaths`, `SessionPaths`, and
`ThreadPaths`; plugins do not join `data_dir`, `plugin_state`, or artifact
subdirectories themselves.
