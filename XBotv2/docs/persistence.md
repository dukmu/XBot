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

Usage and todo own their tables; permissions and goal own their state. Workspace
membership, archive status and ordering live in the process database. Fork
uses public owner events before publishing a staged copy. Artifact IDs are
logical references resolved against the active thread's artifact store.

The present SQL and KV storage does not make all collections bounded. Todo
active/completed pagination and quotas, workspace large-collection pagination,
streaming shell and artifact handling, fixed ranges and pins, and cache byte
budgets remain work to do. Product-scale 512/4096 stress tests and an end-to-end
MiniMax/TUI run remain outstanding, as do full core, integration, TUI and wheel
acceptance for this source revision.
