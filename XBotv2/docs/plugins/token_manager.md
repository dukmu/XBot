# Token Manager Plugin

`token_manager` is a small observe-only view of the latest
model request. Engine persistence remains authoritative for session usage;
Compact owns the only automatic context threshold.

## Behavior

At `BEFORE_MODEL_REQUEST`, after context externalization and Tool selection, the
plugin records the runtime context window, provider-calibrated context estimate,
raw estimate, estimate source, message count, Tool count, and utilization.

At `AFTER_MODEL_RESPONSE`, it attaches the provider's input, output, total,
context, and cache token fields to the same observation. Diagnostics expose only
this latest request and reset on unload.

The plugin does not maintain another cumulative counter, define a 32K fallback,
enforce a threshold, trigger compaction, or persist state. It has no
configuration.

## Context Semantics

- `context_tokens`: input tokens in the latest main-Agent provider request,
  including provider-reported cache read/creation tokens where required.
- `input_tokens`: full-rate input, excluding cache read and cache creation
  tokens reported separately by the provider.
- `output_tokens`: provider-reported output, including reasoning when the
  provider includes it there.
- `total_tokens`: complete processed input plus output, including separately
  reported cache read and cache creation tokens. This matches Inspect AI's
  `ModelUsage` accounting.
- Engine accumulates these fields and the separate cache counters for the
  session.
- auxiliary calls contribute to cumulative usage but do not overwrite the
  main-Agent `context_tokens`.

When a previous main request has provider usage, estimation applies only the
provider-neutral size difference between that request and the next one. This
reuses the measured stable system instructions and Tool schemas. The first
request uses a conservative UTF-8-aware estimate.
