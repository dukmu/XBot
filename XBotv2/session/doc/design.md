# Session ownership

`session/api` owns identities, opening/closing/locating sessions and threads,
resource summaries, event records and stable record construction functions.
`SessionsPort.open` and `open_thread` return actual XCore `Context` objects.
Cross-owner callers locate the thread and require that owner's public service.
SessionManager does not forward Agent turns, tools, permissions, interactions,
provider selection or artifact reads.

A process has one root, each active session has one owner scope, and each
thread has a child owner scope. The caller chooses the main thread ID; its
identity as the main thread comes from having no parent thread. Child IDs are
independent readable identities; Agent definition names remain references.
Thread IDs beginning with `__` are reserved for session-owned non-conversation streams.
A session owner holds one OS writer lock; child scopes share that owner.
Independent roots, including roots in the same PID, cannot share its claim.
Different sessions may run concurrently.

Concurrent opens of the same `(session_id, thread_id)` await one shared task;
canceling one caller does not cancel initialization. No global lock surrounds
long initialization. Scope state stores Contexts and ownership tasks/markers.
Configuration resolves through the config API. Feature facets initialize their
own services from identity and explicit choice facts. The permissions facet
locates a parent scope itself when intersecting parent permissions.

Closing retains the owner mapping until its close succeeds. Admissions reject
closing identities. A failed pre_stop leaves the owner locatable, its lock held
and its resources available for cleanup retry. Successful close drains work
before revoking effects and releasing persistence. Empty startup creates no
session; unwritten sessions are removed after successful shutdown. Failed
opening does not delete existing data or accepted input.

Agentloop owns the live history working set and history mutations. Persistence
owns physical records and locks. Stateless `project_message`,
`project_human_input` and `conversation_replay` are defined in `api/records.py`
for all clients, including TUI; there is no projection service facade.

Session owns `ThreadFacts`, `ThreadRepository` and the session/thread SQL
schema. Reads and default resolution do not materialize an empty session;
other owners explicitly call `ensure` before their first durable mutation.
Session coordinates fork staging and publication and emits the public fork
events; each participating owner handles its own state. An unhandled owner
prevents publication.

Fork first dispatches the session-level copy event once, including for a
session with no persisted threads. It then dispatches copy and finalization
events one thread at a time, with that thread's frozen stream cut and owner
origins. The durable fork boundary retires every input pending at that point;
rebuilding the projection applies the same boundary, without storing an
unbounded list of pending IDs in the event or record.

All thread replay windows and the workspace catalog window share the session
process owner's `replay_process_bytes` budget, initially 32 MiB. Each window
also retains at most 512 frames and 8 MiB of UTF-8 JSON serialized frame data,
including sequence and scope metadata. The shared pool evicts oldest retained
frames across windows. Subscribers hold only a cursor and a coalesced wakeup;
an expired cursor requires a new snapshot. The snapshot cursor is captured
before reading snapshot data so subscribing from it cannot silently miss a
concurrent event.

Set the process budget to zero to disable replay: published sequences still
advance, and unread cursors expire. A frame larger than either applicable
budget is not retained and clears its own prior window. Closing a scope
releases its window and frames. This process-scoped setting cannot be changed
by a thread override. The byte charge measures retained serialized event
frames; concurrent serialization, encoding, and transport buffers are
additional memory, so this is not a process RSS ceiling. The default is an
initial value to evaluate under real workloads.
