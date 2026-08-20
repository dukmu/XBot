# Known Bugs and Verification Gaps

This file records defects noticed during unrelated work. Entries here are not
part of the current plugin-boundary refactor unless explicitly promoted into
scope.

## Stale tests depend on removed internal service paths

- `XBotv2/tests/core/test_todolist.py` still accesses
  `ctx.tools.registry`, although the public tools service no longer exposes a
  registry escape hatch.
- Several HTTP and fold-in integration tests still access
  `ctx.engine.tools.registry` or transport internals such as
  `app.state.manager` and `app.state.paths` instead of exercising public
  services and routes.
- These tests must be migrated to public XCore services or observable behavior.
  The removed internal attributes must not be restored for test compatibility.

## CLI FastAPI test double is incomplete

- The CLI `_FakeApp` used by tests does not implement
  `add_exception_handler`, which the real FastAPI application setup calls.
- The fixture should model the application interface actually consumed by the
  CLI startup path, or the test should use a real application instance.

