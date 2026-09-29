# Runtime ownership closure

This note records the finite ownership audit of the main runtime path. It is an
implementation map and verification index, not a second specification.

| Area | Authoritative owner and production path | Evidence and conclusion |
| --- | --- | --- |
| Configuration and loading | `config` merges global, workspace, then session values; CLI values are the final in-memory overrides. `loader` discovers and constructs configured plugins. `application` owns launch and publishes launcher services into the XCore lifecycle. | `test_config_layers_apply_global_workspace_then_session`, `test_restricted_profiles_cannot_be_reintroduced_by_overlays`, optional-plugin failure and missing permission-guard startup tests. Optional capabilities may be skipped with a warning; a capability required by the selected Agent fails startup. No plugin name is special-cased by the runtime. |
| Agent launch identity | `SessionLaunch` owns session/thread/workspace identity and launch context. `AgentCreateOptions` owns selection and explicit overrides only. | New, resume, provider override, and child-Agent startup paths in `test_application_startup.py`. The former duplicate identity fields and callers were removed. |
| Canonical conversation | `core.messages` owns conversation variants; history commits records before the loop publishes completion. `session.records` is the persistence projection and session events are live projections, not alternate message stores. | `test_assistant_completion_is_committed_before_it_is_published`, `test_assistant_persistence_failure_prevents_completed_event`, recovery and input-routing tests. A failed append cannot produce a successful completion event. |
| Tool execution | `core.tools` owns calls, outcomes, directives, and `ToolExecution`; `agentloop.tool_runtime` executes through the registry and emits typed hook contexts. | Ordering, failure continuation, unchanged arguments, guard denial, timeout, and crash/non-replay tests. `ToolExecution` refers to canonical `ToolMessage`, while `ToolMessage` refers to the tool-owned outcome. The single `model_rebuild` in `core.messages` explicitly resolves that schema forward reference; the dependency remains intentional, because moving the envelope into the loop would make cross-package tool hooks depend on a loop implementation or require a third abstraction. |
| Loop lifecycle | `agentloop.events` owns synchronous in-process hook contexts and results. `agentloop.outputs` owns the closed stream of loop outputs. `agentloop.protocol` owns only tool-catalog HTTP models/routes. | Hook short-circuit, full durable turn, provider retry/partial-output failure, cancellation/recovery, and tool side-effect crash tests. Hook results are discriminated by Python type; unused string `kind` fields were removed. Mutable runtime state was removed from lifecycle hook values because consumers only require the durable history snapshot, stop reason, or error. |
| Live session events | `session.events` owns producer events; `session.contracts.SessionEvent` is the open frame contract; `session.protocol` only frames and serializes transport. `SessionEventStream` owns cursor/replay/close behavior. | Replay/follow, bounded slow-subscriber wakeups, expired/future cursors, and subscribe-after-close tests; server event contract and HTTP SSE tests. Unknown client event kinds remain explicit compatibility errors. `InputAcceptedEvent.target` and the transport `PendingInputData.target` retain the same two-value literals because the authoritative `InboxTarget` currently lives in `agentloop.contracts`, which imports session contracts; importing it back would create a real package cycle. No third shared-type package was added for two values. |
| Interactions and permissions | `interactions.models` owns requests, resolutions, and notices. `interactions.protocol` owns only HTTP response DTOs. The `ask_user` tool owns its two-or-more-options policy; generic interaction requests do not. Permission decisions remain a separate typed, fail-closed domain. | Ask-user schema/execution validation, generic-source request, exactly-once resolution, pending-before-publish HTTP, typed approval, unavailable approval, wrong resolution, and cancellation tests. Runtime publication accepts the open `SessionEvent` contract directly; its former private pass-through was removed. |
| Usage | Provider observation owns actual consumed usage; session stats aggregate that fact independently of whether a later history append succeeds. | Streaming usage and provider failure/recovery tests. Usage is deliberately not reconstructed from, or rolled back with, conversation history. |

There are three deliberately distinct event categories: synchronous in-process
hooks (`agentloop.events`), yielded loop outputs (`agentloop.outputs`), and live
session events (`session.events`). They do not share a registry or a generic
payload escape hatch.

Static review found no plugin imports from `core`, and no business-state
mutation through `setattr`, `__dict__`, `additional_kwargs`, or
`model_construct` in core/application/agentloop/session. `RuntimeVariables`
uses `object.__setattr__` only while constructing its immutable mapping. The
remaining dynamic attribute reads are diagnostic boundaries: logging reads
`LogRecord` extras and tool diagnostics read a guard's `__qualname__`; none of
these injects runtime semantics.

## Verification boundary

The focused suites cover startup failures and cleanup, provider retry and
partial-output failure, tool ordering/arguments/guards/crash recovery, hook
short-circuiting, cancellation and close, FIFO input routing, durable replay,
event cursor expiry, interaction ordering, and permission cancellation. ACP
adapter and focused HTTP interaction tests exercise the client boundaries.

Storage-plugin persistence internals, provider-branded implementations, WebUI,
and TUI presentation behavior are outside this audit. Their interfaces were
only updated mechanically where a domain type moved; this note makes no claim
about their independent implementation closure.
