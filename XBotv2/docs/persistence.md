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

Both projections are folded incrementally and cached per trajectory path inside
the process, so a runtime reuses the parsed records of the current file version
and extends the folds in place when it appends. A file changed by another
process is detected by size and rebuilt on the next access. The cache is bounded
by a trajectory count and a total record budget, and evicts only entries no
operation is using.

A record becomes durable when its terminating newline is written. Readers ignore
a final fragment without that newline, because such a fragment is an append
still in flight; a writer removes the fragment before its first append and
never continues it.

Durability boundaries differ by record kind. Message appends, surface
replacements, and `compaction/*` transaction markers are fsynced before the
call returns; ordinary telemetry events share a bounded-delay flush. A plugin
that needs a stronger boundary must own an explicit event rather than rely on
an adjacent telemetry write.

A plugin may bracket a multi-record commit with a `TrajectoryTransaction`
(start event, end event, correlation id field) and ask the history port for the
ids still open with `open_transactions`. Callers resolve an open bracket
explicitly; the store never rewrites or discards trajectory records.

## Session ownership

Writers are exclusive per session: starting a runtime takes a POSIX advisory
lock on `<data-dir>/.locks/sessions/<session-id>.lock` for the lifetime of that
runtime, so a second runtime on the same session fails with the stable
`session_in_use` code instead of interleaving turns into one trajectory. The
stable lock inode is outside the session directory, so ownership alone does not
materialize a session and empty session files can be removed without unlinking
a lock that another process may be opening. Ownership is shared by every
runtime the process starts for that session (a main thread and its subagent
threads) and released when the last one closes; the kernel releases it if the
process dies, so a crashed runtime cannot lock a session forever.

Readers are not blocked: history, transcript, and trajectory reads take no lock,
which is why the torn-tail rule above exists. Ownership therefore guarantees
"one writer", not "one process may touch the directory": tooling that writes
`messages.jsonl` directly bypasses it and is unsupported, and plugin state,
metadata, and inbox files are replaced atomically by their own owner rather than
coordinated by this lock.

Ownership uses `fcntl`, so it requires a POSIX platform; on a platform without
advisory locks the runtime refuses to start rather than run without it.

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
