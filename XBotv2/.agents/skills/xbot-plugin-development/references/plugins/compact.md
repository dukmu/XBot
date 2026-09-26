# `compact`

Semantically replaces an old conversation prefix with a summary while retaining
recent turns. Compaction is an append-only history transition: earlier
trajectory records remain durable, while the current conversation surface is
replaced.

- **Import/profile:** `XBotv2.compact`, Agent profile.
- **Source:** `compact/contracts.py`, `service.py`, `plugin.py`, `protocol.py`,
  `tools.py`, and `commands.py`.
- **Injects:** `tools`, `commands`, `model`, `loop_state`, `usage`.
- **Events:** observes context-build, model-request-ready, model-request-error,
  model-response-observed, and turn-end stages; publishes typed compaction
  events through the application runtime-event path.
- **Tool:** `compact` requests one manual compaction before the next model call.
- **Command:** `/compact` compacts the current history immediately when
  invoked successfully.

## Configuration

```python
class CompactConfig(BaseModel):
    automatic: bool = True
    output_reservation: int | None = None
    trigger_ratio: float = 0.8
    keep_recent_turns: int = 4
    summary_max_chars: int = 8_000
    summary_output_tokens: int = 2_048
```

Fields are validated by the plugin's Pydantic `Config`. Automatic compaction
can run when the prepared request approaches the configured context threshold.
One provider-confirmed context-overflow retry may run compaction and rebuild the
request; other provider errors are not treated as overflow. The estimate and
reason are included in typed compaction metrics/events.

## Typed compaction contracts

`CompactionReason` is exactly `automatic | manual | context-overflow`.
`CompactionSelection` carries `expected_revision` and the selected
`source_ids`; `CompactionPlan` adds its ID, reason, summary message, and typed
metrics. This revision/identity check prevents committing a summary against a
history that changed after selection.

Runtime events include `CompactionStarted`, `CompactionCompleted`, and
`CompactionFailed`. A completion carries its typed summary and metrics and an
`automatic` flag for client presentation. These runtime observations do not
replace the persistence transaction or duplicate the history summary.

The durable surface replacement is recorded through the persistence/history
owner with a `compact:<reason>` operation. Compaction transaction markers and
the replacement are durable history records; a restart folds them to rebuild
the current surface. Earlier trajectory records remain available for audit and
replay. See [session-trace.md](../session-trace.md).

## Trigger behavior

- The `compact` Tool only requests manual compaction; it does not summarize
  synchronously inside its Tool handler. The request is consumed at the next
  context build.
- `/compact` runs against the current history and returns a `CommandResult`;
  invalid arguments or a history too short to compact are reported as command
  results.
- Automatic compaction uses the prepared request and active model selection.
- Context-overflow recovery is bounded and limited to a provider error
  explicitly classified as context overflow.

## Plugin guidance

- Use `CompactConfig`, `CompactionPlan`, and typed events from the package
  exports; do not construct a parallel proposal dictionary.
- Keep history selection and commit on the persistence/history API so revision
  checking and transaction recovery remain intact.
- Do not describe compaction as deleting transcript or trajectory history.
- The Tool, command, and automatic trigger have different timing; expose each
  according to its actual user intent.
