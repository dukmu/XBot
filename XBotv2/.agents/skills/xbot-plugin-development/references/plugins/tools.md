# Agent Tools

Agent Tools are model-visible operations registered through the common
`ctx.tools` service. The tool registry owns schema exposure and execution
metadata; the standard runtime owns validation, guards, dispatch, and
`ToolExecution` creation. Human slash commands use the separate `commands`
service.

## Define a Tool

```python
from XBotv2.core import Tool, ToolCall, ToolOutcome, succeeded_text

async def lookup(city: str, *, tool_call: ToolCall) -> ToolOutcome:
    """Look up a city."""
    return succeeded_text(f"Lookup complete for {city} ({tool_call.id}).")

tool = Tool.from_function(lookup, name="lookup")
```

`Tool.from_function()` derives the provider parameter schema from the callable
signature, annotations, defaults, and docstring. A Tool may declare at most one
`ToolCall` parameter, and it must be keyword-only. Core excludes it from the
model schema and supplies the final call during execution. Bind other service
dependencies before registering the Tool.

The handler returns `ToolOutcome`: `ToolSucceeded`, `ToolFailed`, `ToolDenied`,
or `ToolCancelled`. Success/failure carry a `ToolOutput`, which contains typed
text/image parts, optional structured data, and artifact references. Denied and
cancelled outcomes carry a reason. Tool output is a model-facing result; client
events, permission interaction requests, and command results have separate
producer-owned paths.

## Registration and metadata

```python
ctx.tools.register(
    Tool.from_function(lookup, name="lookup"),
    namespace="plugin:places",
    model_visible=True,
    timeout_seconds=15,
)
```

The active thread's Tool catalog is dynamic and exposed by
`GET /sessions/{session_id}/threads/{thread_id}/tools`. It reports the model
name, registered name, namespace, description, JSON Schema, and timeout for
currently enabled visible Tools. Plugins should not assume a hard-coded
catalog independent of configuration and runtime selection.

Tools may declare an owner-provided kind, sandbox-escape behavior, and grant
selectors on the `Tool` value. These are execution/authorization metadata; a
plugin must not infer categories or bypass checks from Tool names. The owning
fiber manages registration cleanup. Dynamic Tool installation should be
transactional and removed when the owning runtime is disposed.

## Execution boundaries

- Register every Tool through `ctx.tools`; do not create a second executor or
  construct synthetic Tool calls.
- Let the permission and sandbox guards evaluate registered calls. Do not add
  an approval UI or check permissions privately inside a Tool handler.
- Return typed outcomes; expected failures use `ToolFailed`/`ToolError` with a
  stable code, message, and retryability.
- Keep persisted values JSON-compatible and use the ArtifactStore for large or
  binary outputs. Persist logical artifact IDs, not temporary model paths.
- Use an owning typed interaction contract when a Tool needs a user answer.
  `ask_user` and `request_permission` are distinct built-in Tools with distinct
  outcomes and should not be recreated by plugins.

## Related interfaces

`ToolExecution` is the completed runtime result. It carries a `ToolMessage`,
owner-produced events, and a `TurnDirective` (`continue` or `complete`). The
Agent loop projects that result into its internal events and the client wire
stream. Tool providers should not construct HTTP/SSE DTOs.

For human commands, see [commands](commands.md). For streaming and UI projection,
see [client runtime](../client-runtime.md).
