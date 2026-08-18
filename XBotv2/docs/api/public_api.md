# Public API

XBotv2 exposes one supported Python extension surface:

```python
from XBotv2.core import Events, Tool, ToolResult, Command, EventContext
```

Job data and lifecycle contracts come directly from core; the plugin package
exports only runtime implementations:

```python
from XBotv2.core.jobs import Job, JobKind, JobStatus
from XBotv2.jobs import JobRegistry
```

`XBotv2.jobs` does not re-export core models. The
current symbol list is maintained in [API inventory](api_inventory.md) and
checked by `tests/core/test_public_api.py`.

API v1 covers:

- event names, payloads, and short-circuit dispatch (`Events`, `EventContext`);
- tool definitions, calls, results, errors, artifacts, and client events;
- command, agent, and prompt contracts;
- canonical runtime and session paths.

The C/S wire API is separate. Request and response DTOs live in
`protocol.models`; the runtime currently serves HTTP JSON endpoints and
SSE streams. Wire models reject unknown fields where they are declared as
`WireModel`. A client sends `protocol_version` during `POST /hello`; an
unsupported version receives `unsupported_protocol` with HTTP 426. Plugin
manifests declare `api_version: "1"`.

The API is an explicit inventory that must be updated with behavior, docs, and
tests whenever the extension surface changes. Additive fields need defaults.
Shape changes need a migration note and a contract test that proves the
intended behavior. The public API must own its types; feature plugins must not
re-export core contracts as plugin APIs.
