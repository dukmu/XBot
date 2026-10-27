# Content cache

The optional Agent-profile plugin requires `artifacts`. `PREPARE_INPUT` runs
before durable acceptance and pending-input edits. Oversized human text is
encoded in bounded UTF-8 chunks and published as an immutable artifact; the
canonical input record contains its logical reference and a head/tail preview.
The full original is absent from rollout, inbox and consumed-message records.
Agentloop fingerprints the original request before preparation, so same-ID
retries compare original content even after consumption, editing, recovery or
policy changes. Editing creates a real new body version; retrying the same
edit compares its original content hash.

The preparation event carries an `AsyncExitStack`. Content cache enters the
artifact publication scope there. Agentloop retains the object after a
confirmed append or `CommitUncertain`; pre-log failure or cancellation removes
a newly published, unreferenced object after actual file I/O drains. Concurrent
publications of the same physical object share its lock. Input artifact write
failures propagate and prevent acceptance.

`INPUT_ACCEPTED` separately externalizes a new large body introduced by prompt
expansion. `AFTER_TOOL_CALL` externalizes oversized successful or failed tool
text. Tool externalization failures retain the original tool result. The
message or outcome carries logical references; model-facing rendering resolves
them in the active thread. Agent-authored tool arguments are unaffected.

`api/models.py` declares `ContentCachePolicy` and `ExternalizedContent`.
Defaults are `threshold_chars: 48000`, `preview_chars: 12000`, and
`tail_chars: 2000`; preview must not exceed threshold, and tail must not exceed
preview. For example, set `{"threshold_chars": 1000, "preview_chars": 500,
"tail_chars": 100}`. A preview changes model context and prefix-cache inputs.
There is no separate plugin state, command or tool.

When this plugin is disabled, inputs retain their original text and are subject
to persistence's configured record byte budget (8 MiB by default); an oversized
record fails explicitly. Core has no implicit externalization policy.

Image and attachment uploads in `submit_message` still publish before inbox
acceptance. A conflicting request or later pre-log failure can leave an
unreferenced upload object. The preparation cleanup above covers long text,
not atomic publication of every uploaded media object.
