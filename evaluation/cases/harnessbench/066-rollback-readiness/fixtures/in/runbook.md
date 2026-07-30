# Payments rollback runbook

1. Incident commander declares rollback for PAY-2026-0417.
2. Release manager scales traffic weight for payments-api back to previous stable version.
3. Disable card_bin_cache_v3 if cache error rate exceeds threshold.
4. Monitor p95 latency and payment error rate for 20 minutes.
5. Database migration rollback requires explicit database owner approval before any schema reversal.

Known gaps:
- payment_routing_v2 does not currently have a kill switch command.
- Estimated full rollback is 18 minutes due to cache warmup.
- Do not execute database rollback without database owner approval.
