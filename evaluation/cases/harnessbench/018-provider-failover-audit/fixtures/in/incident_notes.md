# Incident Notes

- 2026-04-05 09:10: Anthropic us-east direct API returned several 529/timeout errors during alert bursts. Long-context research recovered well after retry, but low-latency alerts breached the 20s target.
- 2026-04-05 09:24: Gemini flash completed multimodal PDF triage fastest, but two strict JSON runs used `warning` instead of the allowed enum `warn`.
- 2026-04-05 10:00: OpenAI structured output runs stayed schema-valid. Cache diagnostics show `cacheRead` on repeated prefixes, while normalized `cacheWrite` remains zero.
- 2026-04-05 10:20: Existing gateway config routes `vision_pdf_triage` to OpenAI and `low_latency_alerts` to Anthropic. Both choices need review.
- Audit requirement: do not compare providers using one universal cache hit-rate threshold; use provider-specific expectations and task-specific success signals.
- 2026-04-06 14:00: Internal discussion suggested that Gemini's schema_enum_drift may only occur on specific versions, and upgrading to the latest gemini-3-pro revision might have fixed it; OpenAI's cacheWrite remains invisible but costs are still low.
  **To be audited**: This information is unconfirmed. You must verify against the traces.
