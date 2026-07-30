# Rollback Runbook for checkout-api refund exception v2

Available steps:

1. Set feature flag refund-exception-v2 to 0 percent after approval.
2. Redeploy previous checkout-api image after release manager approval.
3. Verify checkout API p95 latency and error rate.

Missing item:

- There is no tested reversal step for CH-DB-1 and no signed database owner waiver in this package.
