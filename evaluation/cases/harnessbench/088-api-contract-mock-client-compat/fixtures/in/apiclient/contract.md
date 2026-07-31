# Mock API Contract

Base URL is supplied by the benchmark as `MOCK_API_BASE`.

## v1 users

`GET /v1/users?page=N`

Response:

```json
{"items":[{"id":"u1","full_name":"Ada Lovelace","email":"ada@example.com","plan":"pro"}],"next_page":2}
```

## v2 users

`GET /v2/users?cursor=TOKEN`

Response:

```json
{"data":[{"userId":"u1","profile":{"displayName":"Ada Lovelace","email":"ada@example.com"},"subscription":{"plan":"pro"}}],"nextCursor":"abc"}
```

`email` may be null or missing. Normalize it to `None`.

HTTP 429 should be retried locally with a short backoff. Error envelopes look like `{"error":{"code":"bad_cursor","message":"Bad cursor"}}` and should raise `ApiError(status, message)`.
