# Public API

XBotv2 exposes one supported Python extension surface:

```python
from xbotv2.api import PluginBase, ToolResult, XBotTool
```

Modules outside `xbotv2.api` are runtime implementation details. API v1 covers:

- tool definitions, calls, results, errors, artifacts, and client events;
- hook stages, contexts, and explicit guard decisions;
- plugin base class, manifest, registry, and session information;
- HTTP request/response DTOs and the versioned protocol frame.

Wire models reject unknown fields. A client must send `protocol_version` during
`POST /hello`; an unsupported version receives `unsupported_protocol` with HTTP
426. Plugin manifests declare `api_version: "1"`.

Breaking changes require a new protocol or plugin API major version. Additive
fields must have defaults and contract tests in `tests/core/test_public_api.py`.

