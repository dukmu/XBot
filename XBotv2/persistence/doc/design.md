# Persistence ownership

`persistence/api` declares durable thread stores, artifact models, ownership
claims and the process `PERSISTENCE` capability. Public atomic-file primitives
are defined in `api/files.py`. Implementations own JSONL history, metadata,
inbox/plugin state, artifacts, session locking and archive reads. Session
records are declared in `session/api/records.py`.

The process creates shared resource definitions once. The session facet claims
one OS writer lock; thread facets initialize their own stores and artifacts.
Only actual session-owner descendants share a lock. The same PID does not
allow independent runtimes to share ownership. Close drains started work before
releasing the writer claim. Physical fork/delete operations go through the
persistence resource capability.

This batch keeps the existing JSONL/JSON format. SQLite migration and bounded
history/storage working sets remain separate work. Provider directory/client
caches are also not currently bounded. General filesystem operations used by
tools, sandbox and permissions still live in `core.filesystem`; architecture
and contract audits report those three remaining imports. This does not make
persistence the executor for ordinary workspace operations.
