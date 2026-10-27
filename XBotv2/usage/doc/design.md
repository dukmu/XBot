# Usage accounting

Records model usage per thread, publishes `UsageUpdated`, and exposes `UsagePort` via the `USAGE` service key. It depends on thread state, loop state and runtime logging. It has no plugin-specific configuration. Counters persist in the usage state namespace; the latest turn observation is reconstructed from history during initialization. Consumers call `snapshot()` or await `record(observation, usage)`. It contributes no tools, commands, or prompt text.
