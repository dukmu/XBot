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
