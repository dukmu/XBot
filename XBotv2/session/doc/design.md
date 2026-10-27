# Session ownership

`session/api` owns identities, opening/closing/locating sessions and threads,
resource summaries, event records and stable record construction functions.
`SessionsPort.open` and `open_thread` return actual XCore `Context` objects.
Cross-owner callers locate the thread and require that owner's public service.
SessionManager does not forward Agent turns, tools, permissions, interactions,
provider selection or artifact reads.

A process has one root, each active session has one owner scope, and each
thread has a child owner scope. The main thread ID is `main`. Child IDs are
independent readable identities; Agent definition names remain references.
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
