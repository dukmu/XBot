# Agent loop and tools

The loop owns the live thread working set, inbox, turn execution, model/tool
sequencing and history mutations. Public `AGENT_LOOP` is the actual execution
service: submit, interrupt, status, pending-input and history control, and typed
operation invocation. Its Engine, task and lock stay in `impl/`; there is no
factory, second runner protocol or application container.

`mount_thread` creates tools, restores its state/inbox from the rollout and projections,
then constructs Engine once model selection, guards and owner services are
available. Agents owns definition parsing/binding and selection. The loader
only discovers and mounts facets. Business HTTP routes locate an actual thread
Context through the session API and invoke this owner service.

`TOOLS`, `INBOX`, `AGENT_LOOP` and `LOOP_STATE` are the canonical service keys.
Hook declarations/payloads live in `api/events.py`, public execution/input/tool
contracts in `api/contracts.py`, execution values in `api/models.py`, outputs
in `api/outputs.py` and tool wire values in `api/protocol.py`.

Accepted input and tool results commit before completion events. Turn count is
a lifetime counter through edits and compaction. The default iteration limit
is `None`; only an explicitly finite value imposes a cap. Configuring a model
does not reset an explicit cap or turn an unlimited loop into a finite one.

History has one mutable working-set owner. Agentloop owns the typed rollout
codec, history, inbox, turn and surface projections, archive service, HTTP and
import/export; persistence supplies physical records and SQL. The durable human
transcript and model surface belong to agentloop; compaction changes the latter while retaining
original human transcript text. HTTP reads the same canonical transcript for
active, closed and resumed threads. Clear/undo/regenerate still change its
visible projection according to their operations. No alternate active-only
content source is maintained in session management.

Tools follow registry resolution, original-argument validation, standard guards
and execution. Plugin registrations belong to actual fibers; late callbacks
pass an owner. No slash command is transformed into a synthetic ToolCall.
Synchronous tool workers run outside the event loop and remain awaited through
cancellation until their actual I/O finishes, including timeout paths.

pre_stop rejects new work, cancels and awaits active turns/provider streams and
owned tools before effects disappear. Other owners drain commands, interactions,
children and jobs in that same phase. Persistence/locks are released afterward.
Rollout records are append-only and indexed by SQLite; archive reads page the
projection. This does not establish a bound on all live working sets.

Accepted inputs are prepared through the optional `PREPARE_INPUT` event before
writing their canonical record. The event owns a short publication-resource
scope, with no backend business callback. Original request fingerprints and
current content hashes remain small metadata in accepted/edit records. Retries
check the original request before preparing another body; edits compare the
current original content hash. A cancelled append whose I/O completed reports
`CommitUncertain`, retaining the referenced object for same-ID recovery.

The rebuildable `conversation_artifacts` projection indexes references and
metadata from accepted inputs, edits, committed projections, messages and
replacement versions. Archive authorization uses that thread's indexed
references, including pending and archived versions, without scanning all
messages per download. Fork and canonical export/import preserve those refs;
recovery rebuilds the index from owner records. Downloads stream physical
chunks and close readers on completion, send failure or disconnect.

Uploaded media/attachment objects are still written before inbox acceptance;
failed/conflicting uploads can leave unreferenced objects. Message requests use `media` entries containing base64 `data`, `media_type`, and optional `name`; accepted messages retain complete artifact references rather than the encoded bytes. The TUI currently attaches image files only, and provider adapters reject unsupported modalities. Long-text
preparation cleanup does not claim atomicity for that separate upload path.

`archive_records(after_seq=0, through_seq=None, record_ids=None)` is an ordinary
registered read tool for the current thread's canonical JSONL. It returns a
small description and a managed record-selection artifact. Bounds freeze at
the confirmed head; explicit IDs preserve caller order. It reads no other
thread through a permission shortcut. `ConversationArchive.record_selection`
owns selection/authorization and records `artifact_referenced` facts so the
same metadata index authorizes downloads after restart or fork. A closed
session uses a short actual writer claim when this metadata must be committed.
The backend handles raw records and physical selection facts only.

Each request candidate owns an `AsyncExitStack`: preparation pins attachments
before any provider-facing path is emitted, and the same scope survives model
return until that response's tool batch ends. Reusing an unchanged request
through `RetryRequest` retains its scope. History changes or a rejected/rebuilt
candidate close it before the replacement is prepared. Completed iterations
release their own pins rather than accumulating them for the whole turn.
Auxiliary compaction requests hold their separate component pins through their
model invocation. Runtime resources never enter `ModelRequest` or rollout.

Canonical exchange preserves the original thread's relative stream identity.
Selection packages validate an already supplied canonical prefix, or import a
foreign frozen source without binding a business projector. Business owners
extract referenced nested artifacts; imported dependency metadata is backed by
owner facts after the original prefix, preserving all original sequence cuts.
Repeated export/import therefore retains full referenced originals while the
backend remains unaware of message formats.
