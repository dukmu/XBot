# Persistence resources

The process `PERSISTENCE` service owns construction of durable readers,
artifacts, archive queries and ownership claims. The session facet acquires one
writer claim; thread facets construct their stores. Public ports are in
`persistence/api/contracts.py`, models in `api/models.py`, actual atomic file
primitives in `api/files.py`; implementation lives in `persistence/impl/`.

The same process ID never shares independent writer claims. Child scopes share
their actual session owner's claim. Drain completes before its disposer
releases the lock. Archive reads do not open an Agent or acquire a writer.

Storage remains JSONL plus JSON metadata/inbox/plugin state. This batch does
not implement SQLite or bounded working sets. Generic workspace file operations
remain tools/sandbox concerns; their remaining core.filesystem imports are
reported by audits.
