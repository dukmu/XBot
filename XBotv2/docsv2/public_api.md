# Public API

XBotv2 exposes one supported Python extension surface:

```python
from xbotv2.api import PluginBase, Tool, ToolResult
```

Modules outside `xbotv2.api` are runtime implementation details. API v1 covers:

- tool definitions, calls, results, errors, artifacts, and client events;
- hook stages, contexts, and explicit guard decisions;
- plugin base class, manifest, setup/storage capabilities, and session information;
- canonical runtime and session paths.

The C/S wire API is separate. Request and response DTOs live in
`xbotv2.protocol.models`; stream envelopes and `PROTOCOL_VERSION` live in
`xbotv2.protocol.frames`. Wire models reject unknown fields. A client sends
`protocol_version` during
`POST /hello`; an unsupported version receives `unsupported_protocol` with HTTP
426. Plugin manifests declare `api_version: "1"`.

Breaking changes require a new protocol or plugin API major version. Additive
fields must have defaults and contract tests in `tests/core/test_public_api.py`.
The public API must own its types and never re-export runtime implementations.
