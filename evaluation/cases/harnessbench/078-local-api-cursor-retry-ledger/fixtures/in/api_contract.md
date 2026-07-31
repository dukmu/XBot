Endpoints:
- GET /datasets?cursor=START
- GET /jobs?cursor=START
- GET /artifacts?cursor=START
- GET /checkpoint?stream=artifacts

Each list response is {"items": [...], "next_cursor": "..." or null}.
Retry HTTP 429 and 503 after a short local backoff. If the body contains
{"error":"cursor_expired","checkpoint":"artifacts-restart"}, call /checkpoint?stream=artifacts
and use the returned cursor to continue the artifacts stream.
