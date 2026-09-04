# Session Trace and Persistence Schema

Read this page before inspecting or extending session storage. XBot has two
related views, both owned by the persistence layer:

- the **effective surface** is the conversation sent to the next Agent call;
- the **append-only trajectory** is the durable JSONL record of messages,
  surface replacements, and plugin-owned audit events.

Plugins do not create a second history file, append directly to
`messages.jsonl`, or reconstruct `Message` with a private serializer. Use the
typed `ThreadPersistence.history`, `ConversationHistory`, `InboxStore`, and
`ArtifactStore` contracts.

## Physical layout

Given `RuntimePaths(data_dir)`, `ThreadPaths` owns:

```text
<data_dir>/sessions/<session-id>/threads/<thread-id>/
├── thread.json                 # typed ThreadMetadata
├── state/
│   ├── messages.jsonl          # append-only trajectory; folded surface is derived
│   ├── inbox.json              # pending Agent inbox projection
│   ├── plugin_state/state.json # XCore StateService namespaces
│   └── artifacts/<kind>/...    # ArtifactStore-owned files
```

The session-level `threads.jsonl` is owned by `ThreadLifecycleStore`. Do not
infer paths from `data_dir`; obtain `ThreadPaths`/`SessionPaths` from the
declared `thread_paths` or `runtime_paths` service when the contract permits
it. The default data directory is `~/.xbot`, but `--data-dir` and
`XBOT_DATA_DIR` are authoritative.

## JSONL record kinds

Every trajectory record has `schema_version: 1` and a contiguous `position`.
Pydantic models in `XBotv2.persistence.models` are the codec:

| `record_type` | Model | Meaning | Changes effective surface? |
|---|---|---|---|
| omitted | `MessageRecord` | one accepted provider-neutral `Message` with typed `parts` | appends one node |
| `surface_replace` | `SurfaceReplaceRecord` | replaces a contiguous set of current node IDs for `undo`, `clear`, `regenerate`, or `compact` | yes |
| `event` | `TrajectoryEventRecord` | log-only plugin/runtime fact with JSON data | no |

`SurfaceReplaceRecord.transcript` is either `replace` or `preserve`. Compaction
uses an append-only surface replacement while retaining the shadowed raw
trajectory; it does not truncate the JSONL file. `ConversationHistory.page()`
reads the effective surface, while `page_transcript()` reads the explicit
human-history projection. Cursors are opaque and invalidated by a relevant
surface revision.

An ordinary message record contains `role`, `status`, `data`, discriminated
`parts`, `tool_call_id`, `input_id`, `name`, `additional_kwargs`,
`response_metadata`, `usage_metadata`, `artifact`, and `error`. The `parts`
union preserves text, reasoning, images, and Tool calls. Missing optional
metadata is represented by the model defaults; do not assume a legacy
`content` field exists in storage.

## Inbox and plugin state

`inbox.json` is an atomic `InboxSnapshot` with `items` containing
`message_id`, `content`, `target` (`next-turn` or `next-step`), `source`, typed
images/artifacts, and JSON metadata. It is reconciled against committed
`Message.input_id` values when a thread starts. A plugin that wants to enqueue
work uses the Agent inbox/service contract; it must not write this file.

XCore StateService persists one JSON object and exposes logical namespaces:

```python
store = ctx.state.namespace("my-plugin")
raw = await store.get("snapshot")
await store.set("snapshot", validated_snapshot.model_dump(mode="json"))
```

State values are not conversation records. Keep one versioned snapshot for
related plugin fields, and keep clients, tasks, waiters, `Path` objects, and
messages out of it. A missing key may mean the domain's initial state;
malformed existing state must fail loudly at the plugin boundary.

## Reading a trace without a live model

A provider is not required to inspect persistence. Use a disposable data root,
create a `ThreadPaths`, append typed `Message` values through
`ThreadPersistence.history`, then construct a new `ThreadPersistence` over the
same paths and assert the folded surface/trajectory. This proves the storage
contract without pretending that a model smoke test passed.

If `xbot once` fails because credentials or a provider endpoint are unavailable,
report the provider limitation separately. A non-zero provider exit code does
not prove that persistence is broken; inspect the startup error and the JSONL
records produced before the failure. Conversely, an empty or missing trace is
not evidence of a successful no-op turn.

## Safe extension points

- Observe `Events.STATE_CHANGED` or the typed session history events when a
  plugin needs a notification; let persistence remain the observer that writes.
- Use `PRE_COMPACT`/`POST_COMPACT` for compaction metadata, not a second summary
  file or direct surface mutation.
- Use `ArtifactStore` for large content and return an `ArtifactRef`; the model
  receives a session-relative reference and the original bytes remain owned by
  the artifact service.
- Use `UsageService` for cumulative token accounting. Auxiliary model calls
  may contribute usage without replacing the latest main-Agent context size.

Never edit or truncate `messages.jsonl` as a repair shortcut. If a fold fails,
return the validation error and repair the owning record at the persistence
boundary.
