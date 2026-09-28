# Persistence

This is the ownership summary. Typed ports, record schemas, replay rules, and
factory/hydration examples are maintained in the skill's
[persistence reference](../.agents/skills/xbot-plugin-development/references/plugins/persistence.md)
and [session trace reference](../.agents/skills/xbot-plugin-development/references/session-trace.md).

## Ownership

`ThreadPersistence` is the composition boundary for one thread. It exposes
typed history, inbox, metadata, lifecycle, and StateService views. The persistence
plugin creates this store; session restores its runtime projections and owns the
single AgentInbox. Without a store, session uses memory state and a transient
inbox. No application entry identifies this capability by its YAML mount ID.
ArtifactStore
belongs to the session plugin and remains available without conversation
persistence; it is not a port of ThreadPersistence. The physical layout is derived by `RuntimePaths → SessionPaths →
ThreadPaths`; plugins do not construct it themselves.

```text
<data-dir>/sessions/<session>/threads/<thread>/
├── thread.json
└── state/
    ├── messages.jsonl
    ├── inbox.jsonl
    ├── plugin_state/state.json
    └── artifacts/<kind>/<digest>...
```

## Append-only history

`messages.jsonl` contains typed trajectory records. Ordinary messages append
one record. Undo, clear, fork projections, and compact use a typed
surface-replacement record that names source nodes and replacement messages.
The old records remain readable and are folded into the effective surface on
replay. A failed transition is rejected before its record is appended.

Canonical message models reject empty message identities on
construction and decoding. History checks identity uniqueness before calling
its sink. The sink persists the supplied messages without replacing them or
returning a second canonical representation; memory advances only after the
write succeeds. A failed write does not reserve the attempted identities.

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

Startup acquires the claim before creating thread persistence files. The
launcher retains the claim through initialization and rollback, transferring
its release to the Context only after initialization succeeds. Failed startup
removes only its own newly created thread directory and empty session parents;
it must not delete sibling-thread files or release a live parent's claim.
Permanent disposal is `Context.destroy()` (`AgentApplication.close()` for the
public application port); XCore's restartable `Context.stop()` is not permanent
root-resource disposal.

Readers are not blocked: history, transcript, and trajectory reads take no lock,
which is why the torn-tail rule above exists. Ownership therefore guarantees
"one writer", not "one process may touch the directory": tooling that writes
`messages.jsonl` or `inbox.jsonl` directly bypasses it and is unsupported.
Plugin state is current-value KV storage, not a conversation trace.
`plugin_state/state.json` remains one JSON object, atomically replaced when
values change; cache changes become visible only after a successful save.
Namespaces share one service cache and lock, not independent file writers.
The same StateService also holds process workspace state; it must not inherit
conversation-specific trace semantics or keep an unbounded mutation history.

Metadata also remains an atomic snapshot. Growing conversation history belongs
in its trace, not in ever-growing StateService values. Per-plugin ownership and
cold-history reader costs still need review; reverting KV journaling does not
claim to resolve those costs.

Ownership uses `fcntl`, so it requires a POSIX platform; on a platform without
advisory locks the runtime refuses to start rather than run without it.

## History projections

In-memory history and persisted transcript projection use the same iterative
summary-source resolver. Transcript-preserving compaction stores ancestry edges
without copying the full transcript or flattening all original IDs each time.
Operations that actually replace transcript content resolve those edges when
locating the affected span. This does not solve cold-history paging by itself.

## Input recovery

`inbox.jsonl` appends the existing typed insertion, edit, retarget, removal,
consumption and discard events in a versioned envelope. Only insertion stores
the complete input; other records store the changed fields or identities.
Claims are transient and never enter this log. The writer validates each
transition, fsyncs its append, then advances the in-memory projection; it does
not rewrite or reread its known prefix for each input. A failed partial write
rolls back only that uncommitted append. A newly created empty file is removed
on failed first append. Readers ignore an incomplete final line; the next
writer drops that fragment before appending. Old `inbox.json` snapshots are
rejected, without a compatibility reader.

On resume, inbox reconciliation reads committed input identities from append-only
message records, not the current surface or transcript. Compaction and clear may
hide a message but do not undo its consumption. This startup query scans the
loaded trace once; it adds no persisted index or duplicate commit ledger.
Retiring those inputs appends consumption IDs to the inbox log; it does not
rewrite the remaining queue.

An accepted input message retains its inbox identity in its sole `id` field;
there is no duplicate `input_id` or `notice_id`. Old records carrying those
fields are rejected, not silently migrated. Live message events are
projected from accepted history on consumption, not manufactured from a claim
before input hooks and content externalization finish. Rejected inputs do not
become canonical conversation messages. Regenerate is a new input submission
with a new identity; it reuses content and artifact references, not the old
trajectory node's identity.

Input claims are runtime-only and belong to one loop turn, including claims
made at later step boundaries. At turn teardown, history-committed human inputs
and runtime notices are removed from the inbox; uncommitted claims become
pending again. This recovery also runs after cancellation and does not depend
on whether an inbox persistence sink is installed. It is not a cross-file
atomicity guarantee for process crashes.

If input handling raises after accepting only a batch prefix, teardown publishes consumption
for those committed inputs through the same event path as normal completion.
Online clients therefore see the canonical messages without needing a reconnect;
the unprocessed suffix remains pending rather than being reported as consumed.
Likewise, an input hook that rejects or completes handling of a prefix item
consumes only that processed prefix. It cannot discard later inputs in the same
claim before their own handlers run.

The inbox publishes consumption only after its sink and in-memory queue have
been updated. A failed sink write must not announce consumption; an observer
failure after commit does not undo it. A claim notification failure releases
that batch's transient claim markers so the input remains retryable.

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
