# Persistence and storage

The [persistence owner design](../persistence/doc/design.md) describes the
backend contract, append ordering, recovery and lifetimes. The
[session design](../session/doc/design.md) owns session and thread facts and
fork coordination; [agentloop](../agentloop/doc/design.md) owns conversation
records, projections and archive access.

The process database is `plugin-state.sqlite3`. Each persisted session has
`state.sqlite3`, per-thread `rollout.jsonl` records, and session-level
content-addressed objects. Paths are defined by `RuntimePaths`, `SessionPaths`
and `ThreadPaths`. Empty read-only startup does not create a session.

Todo owns its tables; usage, permissions and goal own their namespaced state. Workspace
membership, archive status and ordering live in the process database. Fork
uses public owner events before publishing a staged copy. Artifact IDs are
logical references resolved against the active thread's artifact store.

Todo, workspace, session and thread listings expose bounded pages. Shell output
and artifacts support streaming; record selections have fixed cuts and can be
pinned while their materialized paths are in use. Persistence owns a shared,
byte-budgeted cache of physical record bytes and a separate materialized-file
cache. See the owner design for their different accounting and lifetimes.

Configured limits and cache budgets are starting values, not measured universal
performance targets. Product-scale workload results and installed-wheel
acceptance must be assessed for the exact source revision under review; these
storage mechanisms alone do not establish those results.
