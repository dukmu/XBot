# Persistence ownership

`PERSISTENCE` is the process-owned physical backend. It provides one SQLite
worker with bounded asynchronous admission, owner-restricted SQL transactions,
KV state, rollout records and offsets, recovery and flush, session writer
claims and tracked I/O lifetimes, artifacts, and snapshot copy, stage, publish
and delete primitives. It does not own session facts, conversation record
schemas or domain projections. Callers explicitly invoke `ThreadRepository.ensure`
before their first persistent business mutation; the backend does not inject a
`before_write` callback.

A session owner holds the OS writer claim; its thread descendants share that
claim. Independent roots, including roots in the same process, cannot share it.
Draining rejects new work and waits for accepted I/O leases to actually exit.
The worker retains at most one SQLite connection, switching it when the path or
file identity changes. Transactions still commit per operation; failures roll
back and discard the connection. Session drain and prepared-copy publication
close the relevant connection before releasing ownership or renaming storage.
Process disposal closes the remaining connection on the worker thread.
Preparations are serialized. If a staging connection cannot close, its one
unpublished directory is retained and the next preparation or process drain
retries closing it before removing the directory.
Fork copies stream prefixes using pages of cuts from the frozen staging
database; the prepared copy reads an individual cut from that same snapshot.
Root `pre_stop` runs after children have stopped. Session claims are released
after their accepted work drains; the process worker closes during disposal. Archive page decoding does not hold the writer mutex, but
retains a tracked read lifetime.

For an append, the caller validates its projection in a rolled-back SQL
savepoint, then the backend appends and fsyncs the rollout record (and new
directory entries), then commits the SQL references, projection and offset.
A failure before append propagates without a committed record. A failure after
the durable append but before SQL confirmation raises `CommitUncertain`; retry
the original identity and recover the suffix. Recovery truncates only an
unfinished final line; malformed complete records and inconsistent confirmed
prefixes raise corruption errors. `flush` confirms the current indexed head.

The default admission budget is 64 operations and 16 MiB; replay defaults to
128 records and 1 MiB, a record to 8 MiB, and a KV value to 64 KiB. These are
current experimental configuration values, not hardware-independent
performance guarantees. General workspace filesystem operations remain owned
by `core.filesystem`, not by the storage backend.

Artifacts expose asynchronous `put`, `put_stream`, `adopt_file`, `read`,
`exists`, and `read_chunks`. File work uses a separate thread executor, never
the SQL worker. Writes, hashing and reads use at most 256 KiB storage chunks;
`read` remains the explicit whole-bytes convenience API. Cancellation waits
for accepted file work to finish before removing temporary files or releasing
the physical operation lease. `adopt_file` consumes a producer-owned spool,
hashes its original bytes, and durably publishes the immutable object by atomic
rename; a cross-filesystem source is copied in bounded chunks first.

`prepare_stream` is a publication context for one object. Normal exit retains
it; exceptional exit removes an object created by that scope. The domain owner
decides whether a durable-but-unconfirmed record must retain it. Publications
share a session lock across thread stores, reentrant only for the current task.
This lets an input hold several publication scopes until its
acceptance boundary without deadlocking on duplicate objects or differing order.
Active lock entries retire when the last holder or waiter exits.

`spool_file` owns temporary producer output under the session's `.spool`
directory. Its producer must stop before leaving the scope. Immutable object
files live under `objects`; spools are excluded from session snapshots. Reads
retain the existing physical operation lease through iterator closure; callers
must close partially consumed iterators. HTTP artifact responses do so even
when a send fails or a client disconnects. No SQL transaction spans a network
send. The server cancels requests and aborts transport buffers on shutdown, so
an unread response cannot prevent the process from exiting.

Record selections use the `records/<selection-hash>` artifact namespace.
`reference_records(stream_id, after_seq=0, through_seq=None, record_ids=None)`
captures a confirmed durable cut and registers only physical selection facts.
Sequence bounds mean `after_seq < seq <= through_seq`; omitting the upper bound
captures the current confirmed head. Explicit IDs retain caller order and
repetitions. Missing IDs fail without publishing a partial selection. Empty
selections read and materialize as empty files. Later source appends do not
change any existing selection. The ID hashes the selector, not its content;
`sha256` is empty; consumers can measure the streamed bytes independently.

`artifact_selections`, `artifact_record_ids`, and `artifact_import_sources`
are backend bookkeeping, excluded from business table declarations and fork
owner coordination. ID registration and reads use internal pages bounded by
both record count and the existing byte budget. The selector header publishes
only after its private ID rows are complete. Source payloads stay opaque.
`read_chunks` streams the selected original JSONL bytes; `read_records` decodes
one bounded canonical record at a time for the responsible business owner.

`pin(ref)` asynchronously materializes a selection in a process-specific cache
and yields its current absolute path. Construction uses bounded file chunks,
per-object build ownership and atomic publication. A short metadata lock never
covers a complete build or file deletion. Concurrent users share a complete
view. Failed/cancelled construction drains actual I/O before removing temporary
files. `model_path` requires an active materialization pin for record refs.

The process also owns one in-memory cache of immutable physical rollout record
bytes, shared by conversation archives and team streams. `record_cache_bytes`
defaults to 16 MiB as an initial measurement budget; zero disables caching.
The byte charge includes payload and conservative key/entry overhead. Records
larger than the budget, or records that cannot fit while entries are pinned,
are read normally without admission. Each read pins its bytes while decoding,
then returns a freshly decoded object. Decoded pages and in-flight file reads
are not included in the cache budget.

The cache keys a record by session, physical path, offset, length and record
identity. File device/inode, size, modification time and change time validate
the physical version. Known appends advance that version while retaining the
confirmed prefix; unexpected changes, replay and reset invalidate the affected
stream. Session drain drops its entries, staging cleanup drops its physical root,
and process drain clears the cache. `record_cache_stats` reports the budget,
resident and pinned bytes, entries, active pins, hits, misses, evictions and
bypasses; counters describe this process and are not restored across restarts.
The default budget is not a measured optimum. Concurrent external in-place
rewrites of an active rollout are outside the single-writer ownership contract.
A rewrite whose file identity, size and timestamps are unchanged cannot be
detected by this cache; arbitrary external corruption is therefore not promised
to become visible immediately. Controlled reset and recovery always invalidate.

The process config `artifact_cache_bytes` defaults to 64 MiB and bounds the
unpinned LRU. Its charge includes file bytes, encoded reference/path metadata,
and 2 KiB per entry; even empty files consume budget. Pinned views can exceed
this budget until their users finish. Releasing the last pin makes a view
eligible for eviction, and the persistent selector can rebuild it. Session
drain drops that session's entries; root close removes the process cache. Cache
paths and old pins are not restored or copied into a fork.

`resolve_model_path` is the shared pure mapping for permission path intent,
sandbox resolution and managed reads. It recognizes `artifact:<logical-id>`
and cache paths from an earlier process in the same data/session namespace.
`resolve_path` acquires the actual read pin while rebuilding the current path.
The original tool arguments stay unchanged.

`export_reference`/`import_reference` exchange selector metadata and, when the
canonical stream is not supplied separately, a verified frozen source prefix.
Packages publish atomically. Existing business-owned streams are verified and
never replaced or extended by an import. Backend-owned imported snapshots are
marked read-only: domain rollout/projector ownership is rejected. Later cuts
of the same foreign source can append a verified matching tail under the
source's ordinary operation barrier. Its available cut advances only after
file I/O and bounded index updates finish; failed/unconfirmed extension work
restores the old tail/index. Old fixed-prefix readers remain valid. Fork
copies these facts and prefixes into the target session, with no source
session path embedded in the reference.
