# Persistence

This is the ownership summary. Typed ports, record schemas, replay rules, and
factory/hydration examples are maintained in the skill's
[persistence reference](../.agents/skills/xbot-plugin-development/references/plugins/persistence.md)
and [session trace reference](../.agents/skills/xbot-plugin-development/references/session-trace.md).

## Ownership

`ThreadPersistence` is the composition boundary for one thread. It exposes
typed history, inbox, metadata, lifecycle, StateService, and ArtifactStore
ports. The physical layout is derived by `RuntimePaths → SessionPaths →
ThreadPaths`; plugins do not construct it themselves.

```text
<data-dir>/sessions/<session>/threads/<thread>/
├── thread.json
└── state/
    ├── messages.jsonl
    ├── inbox.json
    ├── plugin_state/state.json
    └── artifacts/<kind>/<digest>...
```

## Append-only history

`messages.jsonl` contains typed trajectory records. Ordinary messages append
one record. Undo, clear, fork projections, and compact use a typed
surface-replacement record that names source nodes and replacement messages.
The old records remain readable and are folded into the effective surface on
replay. A failed transition is rejected before its record is appended.

The conversation surface and the human transcript are separate projections.
HTTP history uses opaque cursors bound to the projection revision; clients must
not manufacture or compare cursor internals.

Durability boundaries differ by record kind. Message appends, surface
replacements, and `compaction/*` transaction markers are fsynced before the
call returns; ordinary telemetry events share a bounded-delay flush. A plugin
that needs a stronger boundary must own an explicit event rather than rely on
an adjacent telemetry write.

A plugin may bracket a multi-record commit with a `TrajectoryTransaction`
(start event, end event, correlation id field) and ask the history port for the
ids still open with `open_transactions`. Callers resolve an open bracket
explicitly; the store never rewrites or discards trajectory records.

Trajectory writes are serialized across processes by a POSIX advisory lock on
`messages.jsonl.lock` beside the trajectory. A writer owns that lock for the
whole read-modify-append critical section, so positions stay unique and
monotonic while several processes append; a reader takes the same lock in
shared mode and therefore never parses a partially written record. The lock is
released by the kernel when a writer exits, so a crashed process cannot lock a
session forever.

Contention is bounded: a writer that waits longer than the configured timeout
fails with a `TimeoutError` naming the lock file instead of interleaving writes.
The lock coordinates file mutations only. It does not make two runtimes share
one session's conversation semantics, and it does not coordinate the plugin
state or metadata files, which are replaced atomically by their own owner.

Locking uses `fcntl`, so multi-process coordination requires a POSIX platform.
On a platform without advisory locks, the first trajectory write fails with a
clear error rather than silently reverting to unsynchronized appends.

## Plugin state

StateService namespaces are the only plugin state protocol. A plugin writes one
typed snapshot in its namespace and does not create adjacent JSON files or put
runtime handles into that snapshot. Configuration overlays remain immutable
startup input.

## Artifacts

`ArtifactStore.put()` deduplicates payloads by digest and returns an
`ArtifactRef`. Persist the logical ID. Call `model_path()` only while building
a provider request. Tool-result and content-cache artifacts retain original
payloads; previews are projections, not replacements for stored data.
