# minisvc

This service accepts orders through a small HTTP adapter and a CLI bootstrap command.

Design notes from an older implementation:
- `MINISVC_READONLY=1` blocks all write paths.
- `create_order` retries failed SQLite writes twice before returning an error response.
- Audit events are stored in a durable audit table.

The code in this fixture is authoritative when documentation and implementation disagree.
