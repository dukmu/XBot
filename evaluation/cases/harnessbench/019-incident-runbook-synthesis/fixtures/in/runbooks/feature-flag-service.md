# Runbook: feature-flag-service policy cache inconsistency

Symptoms:
- `PolicyVersionMismatch` in any service that consumes feature flags.
- Sporadic reservation failures even when inventory-api appears healthy.

Common causes:
- Flag cache worker restarts or network partitions.

Mitigation steps (use only if inventory-api runbook does not resolve):
1. Verify feature-flag-service cache lag > 30 seconds.
2. Restart feature-flag-service pods (requires approval for production).
3. Monitor `PolicyVersionMismatch` rate for 5 minutes.
4. If mismatch drops, continue monitoring; otherwise proceed to inventory-api rollback.

Stop condition: Mismatch rate exceeds 10% after restart.