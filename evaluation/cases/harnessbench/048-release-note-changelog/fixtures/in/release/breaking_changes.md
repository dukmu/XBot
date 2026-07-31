# Breaking Changes

- ISSUE-103: The usage endpoint response field `units` is renamed to `billable_units`.
- ISSUE-104: The legacy token query parameter is no longer accepted; clients must use the Authorization header.
- ISSUE-106: The rate limit reset field `retryAfter` is renamed to `retry_after_ms`.
- ISSUE-110: Webhook signature verification now uses the `X-Webhook-Signature` header instead of `X-Signature`.

Migration guidance:
- Update API clients to read `billable_units`.
- Remove token query parameters before deploying 2.4.0 client integrations.
- Update throttling clients to read `retry_after_ms` and treat it as milliseconds.
- Update webhook receivers to read `X-Webhook-Signature`; keep old `X-Signature` only for pre-2.4.0 clients.
