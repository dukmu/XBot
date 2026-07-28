# Idempotency Policy

- Use item id as the idempotency key.
- Items listed in completed_ids in state.json must not be processed again.
- Reusing a completed result is allowed, but attempt_counts for skipped preexisting items must not increase.
- retry_ledger.csv must make skipped_preexisting rows explicit.
- Final results must merge partial results with newly processed results without duplicate item IDs.
