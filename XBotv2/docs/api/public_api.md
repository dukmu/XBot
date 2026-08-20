# Public API

XBotv2 exposes shared contracts from `core`:

```python
from XBotv2.core import Events, Tool, ToolResult, Command, EventContext
```

Each plugin also owns a declaration surface through its package root. Package
roots may re-export only explicit `types`, `invariants`, `commands`, `events`,
`services`, or transitional `contracts` declarations:

```python
from XBotv2.core.jobs import Job, JobKind, JobStatus
from XBotv2.jobs import LIST_TASKS, TaskSnapshot
from XBotv2.llm import LlmCatalogPort, ProviderCatalog
```

Concrete services, registries, managers, routers, and plugin implementations
must be imported from their owning implementation module only by composition
or tests; they are not package-root API. The current symbol lists are checked
by `tests/core/test_public_api.py`.

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

The API is explicit and must be updated with behavior, docs, and tests whenever
the extension surface changes. Additive fields need defaults. Shape changes
need a migration note and a contract test that proves the intended behavior.
Plugin roots must export their own declarations, not shared core contracts or
runtime implementations.
