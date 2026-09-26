# `token_manager`

Provides a latest-request diagnostic from typed model lifecycle events. It
estimates request context from the actual `ModelRequest`, then records
provider-reported token counters and observed-context status for the matching
response. It does not own compaction or context policy.

- **Source:** `XBotv2/token_manager/`.
- **Profile:** Agent.
- **Requires:** `session`.
- **Provides:** `token_manager` service (the plugin instance), with
  `diagnostics()`.
- **Events:** `MODEL_REQUEST_READY` (`ModelRequestReady`) and
  `MODEL_RESPONSE_OBSERVED` (`ModelResponseObserved`).

The request snapshot includes turn count, message/tool counts, resolved model
selection, and `EstimatedContext(tokens, method)`. The response snapshot
includes typed `TokenCounters` and `ObservedContext`. An observed context may
be a provider measurement or `MeasurementUnavailable`; an estimate is not a
provider measurement. Diagnostics also calculate estimated or observed token
budget against the resolved context window and output reservation.

`diagnostics()` returns a JSON-compatible mapping with `status`, `mode`, and
`latest_request`; when a response was observed, that entry includes
`provider_usage`, `observed_context`, and `observed_budget` when measurable.
Exact nested serialization follows the typed models and is not a wire/API
stability guarantee.

The component is observe-only: it does not modify model requests, persist its
latest snapshot, or enforce budget policy. It clears the in-memory snapshot
when its context is disposed. For the user-facing cumulative usage snapshot,
see [usage](usage.md); for compaction behavior, see [compact](compact.md).
