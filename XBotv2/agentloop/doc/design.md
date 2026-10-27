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

A tool may declare a keyword-only `ToolInvocation` parameter to receive the
canonical assistant message ID and original tool call ID for its invocation.
This parameter is omitted from the model-facing schema. A tool requiring it
fails if invoked outside an assistant-message execution scope.

The conversation archive exposes `latest_execution(session_id, thread_id)`
and `message(session_id, thread_id, message_id)`. An execution records its turn
identity, claimed input identities, status and, on successful completion, the
final assistant message identity if there is one. `running` in a cold archive
means that execution started but no terminal fact was recorded. It does not
imply the task is still alive or that an earlier assistant message is its reply.
The thread event stream publishes `ExecutionEnded` with the same record after
the owning task releases its turn lock, including pre-turn-start failures.

`latest_input` can filter by runtime input source and event and reports the
accepted input's current target and whether it is pending, removed, discarded,
or consumed. `execution_for_input` finds the latest execution that claimed an
input, even if other executions have followed it. Removal alone describes an
inbox operation; its meaning is determined by the caller.

The live loop exposes `execution_id` while it owns an execution; the archive
can retrieve its exact recorded state using `execution_for_turn`.

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

Media and attachment objects are published before inbox acceptance. If acceptance
fails, objects created by that input are removed while existing objects are
preserved. A committed or uncertain acceptance retains its objects for recovery;
retry uses the same input ID. Message requests use `media` entries containing
base64 `data`, `media_type`, and optional `name`; accepted messages retain
artifact references rather than encoded bytes. The TUI currently attaches image
files only, and provider adapters reject unsupported modalities.

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

After request hooks and any iteration-limit tool removal, the engine estimates
each candidate once. The request-ready event, token manager, request log, and
response observation, automatic compaction, and failure handling share that
estimate. An unchanged retry reuses it; a
replacement or rebuilt candidate receives its own estimate.

Canonical exchange preserves the original thread's relative stream identity.
Selection packages validate an already supplied canonical prefix, or import a
foreign frozen source without binding a business projector. Business owners
extract referenced nested artifacts; imported dependency metadata is backed by
owner facts after the original prefix, preserving all original sequence cuts.
Repeated export/import therefore retains full referenced originals while the
backend remains unaware of message formats.

The display history API returns `DisplayRecord` previews and field lengths, not complete `ConversationRecord` values. `display-messages/{message_id}/body` reads a bounded character page from the canonical field; `messages` remains the full-record export API. Tool argument fields resolve their assistant call through a rebuildable projection that stores identities only. One original record is still decoded in full for each field request; the API bounds client residency, not that server-side decode peak.

A text artifact referenced by a tool result is displayed through the same field-page route using an `artifact:` field. Its cursor and length are byte offsets; the owner authorizes the original artifact and reads a bounded byte range, preserving UTF-8 characters across page boundaries. Invalid UTF-8 displays replacement characters without altering the original bytes.
