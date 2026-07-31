# Edge Caching Draft

Recent work by Lin et al. (2023) shows that regional cache hints reduce tail latency. A complementary deployment study by Gomez and Patel (2020) found that cache invalidation drills improved recovery time.

For edge telemetry, Parker (2021) argues that low-cardinality counters should be retained alongside sampled traces. The evaluation protocol follows Singh (2022), which reports confidence intervals for repeated load tests.

The operational appendix cites Ortega (2020) for rollback drills, but the bibliography entry uses the correct title with an outdated key.

Two 2024 papers by Chen are cited separately: Chen (2024a) introduces adaptive prewarming, while Chen (2024b) reports an operations retrospective. The draft also indirectly cites Rao via Lin et al. (2023), but no standalone Rao bibliography entry is needed.

## References Cited in Text

- Lin et al. 2023: "Regional Cache Hints for Tail Latency"
- Gomez and Patel 2020: "Operational Drills for Cache Invalidation"
- Parker 2021: "Telemetry Counters at the Edge"
- Singh 2022: "Repeatable Load Test Intervals"
- Ortega 2020: "Rollback Drills for Edge Services"
- Chen 2024a: "Adaptive Cache Prewarming"
- Chen 2024b: "Edge Operations Retrospective"
