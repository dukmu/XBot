# Session

The process facet publishes `SESSIONS`; the thread facet publishes thread paths,
variables, state and `THREAD_EVENTS`. Identity seeds use `SESSION_INFO` and
`THREAD_INFO`. SessionManager owns opening, closing, identity and locating
actual Contexts. It does not construct Engine or forward other owners' methods.

See [process sessions](process-sessions.md) for the public API and lifecycle.
Use `session/api/contracts.py`, `api/records.py`, `api/snapshot.py` and
`api/protocol.py`; implementation files live under `session/impl/`.
