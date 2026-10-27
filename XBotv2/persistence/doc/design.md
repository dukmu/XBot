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
share a session/object lock across thread stores, and active lock entries retire
when the last holder or waiter exits. This is not a multi-object transaction.

`spool_file` owns temporary producer output under the session's `.spool`
directory. Its producer must stop before leaving the scope. Immutable object
files live under `objects`; spools are excluded from session snapshots. Reads
retain the existing physical operation lease through iterator closure; callers
must close partially consumed iterators. HTTP artifact responses do so even
when a send fails or a client disconnects. No SQL transaction spans a network
send. The server cancels requests and aborts transport buffers on shutdown, so
an unread response cannot prevent the process from exiting.
