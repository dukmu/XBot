# Public API

XBotv2 exposes one supported Python extension surface:

```python
from xbotv2.api import PluginBase, Tool, ToolResult
```

Modules outside `xbotv2.api` are runtime implementation details for plugin
authors. The current symbol list is maintained in
[API inventory](api_inventory.md) and checked by
`tests/core/test_public_api.py`.

API v1 covers:

- tool definitions, calls, results, errors, artifacts, and client events;
- hook stages, contexts, and explicit guard decisions;
- plugin base class, manifest, setup/storage capabilities, and session information;
- canonical runtime and session paths.

The C/S wire API is separate. Request and response DTOs live in
`xbotv2.protocol.models`; the runtime currently serves HTTP JSON endpoints and
SSE streams. Wire models reject unknown fields where they are declared as
`WireModel`. A client sends `protocol_version` during `POST /hello`; an
unsupported version receives `unsupported_protocol` with HTTP 426. Plugin
manifests declare `api_version: "1"`.

The API is an explicit inventory that must be updated with behavior, docs, and
tests whenever the extension surface changes. Additive fields need defaults.
Shape changes need a migration note and a contract test that proves the
intended behavior. The public API must own its types and never re-export
runtime implementations.
