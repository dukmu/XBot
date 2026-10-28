# `persistence`

The actual process/session/thread boundary is documented in
[process persistence](process-persistence.md). Public contracts are declared in
`XBotv2/persistence/api/contracts.py`, with models in `api/models.py` and actual
atomic-file primitives in `api/files.py`. Implementations live under `impl/`.

`PERSISTENCE` owns reader/artifact/claim construction and physical fork/delete.
The session facet owns its one writer claim. Thread facets initialize stores;
agentloop owns working-state/inbox hydration and mutations. There is no factory
service or old persistence root export.

JSONL history distinguishes a durable human transcript from the model surface.
Compaction preserves the original transcript and replaces the model surface.
Clear/undo/regenerate update the visible transcript projection according to
those operations. HTTP uses the same transcript while active, closed or
resumed. Artifact IDs remain logical references; model-facing paths resolve
through the actual thread's ArtifactStore and RuntimeVariables.

The current format remains JSONL plus JSON metadata/inbox/plugin state. SQL and
bounded working sets are a subsequent change. Independent runtimes never share
writer ownership merely because they have the same PID. Owner pre_stop work
must finish before lock/store effects disappear; failed cleanup stays owned
and retryable.
