# `usage`

Tracks cumulative token counters and the latest turn context observation for
one Agent thread. The public model is `UsageSnapshot`; provider-specific raw
usage metadata is normalized before it reaches this capability.

- **Import/profile:** `XBotv2.usage`, Agent profile.
- **Source:** `usage/contracts.py`, `plugin.py`.
- **Injects/provides:** `state`, `loop_state`, `runtime_log` → `usage`
  (`UsagePort`).
- **Events:** observes `ModelResponseObserved`; emits `RuntimeEvent` containing
  `UsageUpdated(snapshot=...)` after recording.
- **Persistence:** `ctx.state.namespace("usage")`, key `counters`, containing
  only cumulative `total_counters`.

## Canonical values

Import shared value types from `XBotv2.core`:

- `TokenCounters`: input, output, cache-read, cache-create, and prompt-cache
  write counters. `UsageSnapshot.total_counters` is their cumulative total.
- `UsageDelta`: one response's counter increment.
- `RequestObservation`: resolved model selection, request purpose, estimated
  input tokens, and observed context.
- `UsageSnapshot.latest_turn_observation`: the latest turn request's context
  reading. Auxiliary requests add to cumulative totals but do not replace it.
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

The service persists only cumulative counters. Per-turn observations already
belong to `AssistantMessage.exchange` in `messages.jsonl`; initialization,
resume, and inactive thread reads derive only the latest turn observation from
that canonical surface. Auxiliary observations are not retained after their
counters are accumulated. Recording is serialized, persists the updated
counters, and then publishes `UsageUpdated`. The owning application places the
resulting projection in thread summaries and open-thread responses.

## Extension guidance

- Read usage through the `UsagePort`; do not parse provider dictionaries in a
  plugin or create a competing usage state model.
- Treat missing context measurement as unavailable, not as zero or as the input
  counter.
- Do not infer per-delta usage events: the public update carries an authoritative
  cumulative snapshot.
