# `usage`

Tracks cumulative token counters and per-request context observations for one
Agent thread. The public model is `UsageSnapshot`; provider-specific raw usage
metadata is normalized before it reaches this capability.

- **Import/profile:** `XBotv2.usage`, Agent profile.
- **Source:** `usage/contracts.py`, `plugin.py`.
- **Injects/provides:** `state`, `loop_state`, `runtime_log` → `usage`
  (`UsagePort`).
- **Events:** observes `ModelResponseObserved`; emits `RuntimeEvent` containing
  `UsageUpdated(snapshot=...)` after recording.
- **Persistence:** `ctx.state.namespace("usage")`, key `snapshot`.

## Canonical values

Import shared value types from `XBotv2.core`:

- `TokenCounters`: input, output, cache-read, cache-create, and prompt-cache
  write counters. `UsageSnapshot.total_counters` is their cumulative total.
- `UsageDelta`: one response's counter increment.
- `RequestObservation`: resolved model selection, request purpose, estimated
  input tokens, and observed context.
- `UsageSnapshot.requests`: request observations; only a `TurnRequest` updates
  `latest_turn_observation`. Auxiliary requests add to cumulative totals but do
  not replace the latest turn's context reading.
- `ObservedContext`: either `ProviderMeasured(tokens=...)` or
  `MeasurementUnavailable(reason=...)`. Unlike the other values above, this one
  is not re-exported from `XBotv2.core`; import it from `XBotv2.core.domain`,
  as `XBotv2/token_manager/plugin.py` does.

The snapshot is a typed projection of provider observations, not a universal
tokenizer. `estimated_input_tokens` and provider-measured context have different
meanings; do not relabel cumulative input counters as the current context size.

## Service contract

```python
class UsagePort(Protocol):
    def snapshot(self) -> UsageSnapshot: ...
    async def record(
        self, observation: RequestObservation, usage: UsageDelta
    ) -> UsageSnapshot: ...
```

The service initializes from its persisted snapshot when present; otherwise it
reconstructs counters and request observations from assistant messages in the
current conversation surface. Recording is serialized, persisted, and then
published as `UsageUpdated`. The owning application places this snapshot in
thread summaries and open-thread responses.

## Extension guidance

- Read usage through the `UsagePort`; do not parse provider dictionaries in a
  plugin or create a competing usage state model.
- Treat missing context measurement as unavailable, not as zero or as the input
  counter.
- Do not infer per-delta usage events: the public update carries an authoritative
  cumulative snapshot.
