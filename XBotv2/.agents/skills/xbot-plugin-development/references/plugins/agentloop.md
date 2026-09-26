# `agentloop`

Owns Agent turn sequencing, model/tool dispatch, typed loop events, and the
per-thread `LoopState`. It creates the Engine only after application launch
facts and required services have been composed.

- **Import/profile:** `XBotv2.agentloop`, Agent profile.
- **Source:** `agentloop/events.py`, `contracts.py`, `engine.py`, `factory.py`,
  `tool_runtime.py`, and `protocol.py`.
- **Provides:** `agent_loop_factory`, Tool registration/catalog services,
  `LoopState`, and the thread's runtime engine through session composition.
- **HTTP facet:** read-only Tool catalog at
  `GET /sessions/{session_id}/threads/{thread_id}/tools`.

## Event dispatch

Import `Events`, `EventPort`, and `SHORT_CIRCUIT_EVENTS` from
`XBotv2.agentloop`. Subscribe with `ctx.on(event_name, handler)` during plugin
composition. Events are passed as typed payload objects declared alongside the
producer; they are not a universal `EventContext` dictionary.

Observers are dispatched through event emission and return no replacement.
Only names in `SHORT_CIRCUIT_EVENTS` use serial dispatch and may return that
stage's documented result union. Current serial stages are input receipt and
acceptance, before/after context build, before model request, model request
failure, and before/after Tool call. Import the payload and result types from
the owner module and inspect its dispatcher before subscribing; the payloads
are stage-specific.

Other public event names include session start/resume/close, turn start/end,
error/stop, model response observation, Tool call/batch/message observations,
inbox changes, and state changes. The `Events` constants in source are the
authoritative vocabulary; do not use historical names such as
`BEFORE_USER_MESSAGE_ACCEPT`, `BEFORE_CONTEXT`, or `AFTER_TOOLS`.

## Runtime event model

`agentloop.protocol.LoopEvent` is the typed turn-output union. Its current
variants include turn start/end, assistant text and reasoning deltas, completed
assistant messages, tool-call argument deltas, completed Tool executions,
usage observations, and errors. Session HTTP/SSE frames add session/thread
identity, sequence, and session/turn scope. This client protocol is distinct
from internal XCore loop events.

Provider stream chunks and provider-specific usage/error behavior stay within
the provider implementation. Plugins should observe typed loop events at the
owner boundary rather than parse HTTP frames or provider chunks.

## Tool ownership

Register model-visible operations with `ctx.tools.register(...)`. The common
Tool pipeline applies argument validation, permission/sandbox guards, and
dispatch. Use the Tool registry/catalog operation to inspect currently enabled
Tools; the catalog is per thread and may change with runtime selection. A Tool
is not a human slash command: use `ctx.commands` for commands and do not create
a synthetic Tool call to bridge the two.

## State and lifecycle

`LoopState` owns the live `ConversationHistory`, runtime variables, thread
metadata state, turn count, resume fact, and inbox-facing state. Runtime handles
are not durable conversation records. Persist plugin snapshots with
`ctx.state.namespace(...)`; history mutation and durable replay belong to the
persistence/session owners.

Session lifecycle payloads carry `SessionRuntimeState`, whose identity is a
`SessionKey` and whose metadata is owned by thread metadata. They are not the
older `SessionInfo` shape and do not expose `event.session.paths`.

## Plugin guidance

- Import public declarations from `XBotv2.agentloop` and domain payloads from
  their owning packages.
- Observe a stage unless short-circuiting is a documented part of the feature.
- Return only the stage's declared result type from serial handlers.
- Use `Events.MODEL_RESPONSE_OBSERVED` for normalized usage observation; the
  `usage` plugin owns accumulation and persistence.
- Do not use a loop event to pass unrelated cross-plugin business data; define
  an event or operation in the owning package.
