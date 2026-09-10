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
