# Release Approval Policy

A release must be marked blocked when any hard blocker is present.

Hard blockers:

- Database migrations must have a tested reversal step or a signed database owner waiver.
- Canary error rate must not exceed 1.5 percent for checkout-api.
- Security approval is required for changes that add or expose customer-facing reason codes.
- Releases must not start during a freeze window unless an incident commander grants a written exception.
- If there was a related severity 1 or severity 2 incident in the last 14 days, SRE lead approval is required before release.

Allowed output:

- The package may recommend pending actions and approvals.
- The package must not claim that deploys, rollbacks, feature flag changes, pages, or other production actions were executed.
