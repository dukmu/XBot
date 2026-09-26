# XBot API Reference

This is the version-matched public API map for the bundled XBot runtime. Import
cross-plugin contracts from the package that owns them. Package `__all__`
declarations and tests are the final authority; implementation modules not
listed as public are not plugin contracts.

## Import map

| Need | Public package | Examples |
|---|---|---|
| Tools, outcomes, messages, artifact/path values | `XBotv2.core` | `Tool`, `ToolCall`, `ToolOutcome`, `ToolOutput`, `ConversationMessage`, `ArtifactRef`, `RuntimeVariables` |
| Loop event names and tool/inbox contracts | `XBotv2.agentloop` | `Events`, `SHORT_CIRCUIT_EVENTS`, `ToolSelection`, `InboxItem`, `LoopState` |
| Human slash commands | `XBotv2.commands` | `Command`, `CommandResult`, `CommandDescription`, `split_command_args` |
| Sessions and threads | `XBotv2.session` | `SessionRuntimeState`, `SessionSummary`, `ThreadSummary`, session operations |
| Provider catalog and model binding | `XBotv2.llm` | `ModelPort`, `LlmConfig`, `ProviderCatalog`, selection operations |
| Provider-neutral model request | `XBotv2.core.provider` (used by `XBotv2.llm.ModelPort`) | `ModelRequest`, `ProviderMessage`, `ToolSchema` |
| Model stream values | `XBotv2.core` | `TextDelta`, `ReasoningDelta`, `ToolCallDelta`, `ModelCompleted`, `ModelFailed`, `ModelCancelled` |
| Usage | `XBotv2.core` / `XBotv2.usage` | `UsageSnapshot`, `UsageDelta`, `RequestObservation`, `UsagePort`, `UsageUpdated` |
| Permission policy and approval | `XBotv2.permissions` | `PermissionsPort`, `ApprovalPort`, `PermissionRequest`, `PermissionResponseRequest` |
| User input | `XBotv2.interactions` | `InteractionsPort`, `UserInputRequest`, `UserInputOption`, `UserInputResponseRequest` |
| Application lifecycle | `XBotv2.application` | `APPLICATION_INITIALIZED`, `ApplicationInitialized`, `RuntimeEvent` |

Protocol DTOs and routes remain in their semantic owner's `protocol.py`; a
transport model should not be moved into `core` for convenient imports.

## Tool contract

```python
from XBotv2.core import Tool, ToolCall, ToolOutcome, succeeded_text

async def lookup(place: str) -> ToolOutcome:
    """Look up a place."""
    return succeeded_text(f"Found {place}")

tool = Tool.from_function(lookup, name="lookup")
```

`Tool.from_function()` derives a JSON Schema from the callable signature and
docstring. A Tool may declare one keyword-only `ToolCall` parameter. Core omits
that parameter from the provider schema and supplies the final Tool call when
invoking it. Bind other dependencies before registration; there is no generic
dependency dictionary.

`ToolOutcome` is the union of `ToolSucceeded`, `ToolFailed`, `ToolDenied`, and
`ToolCancelled`. Success/failure output uses `ToolOutput` with typed content
parts, optional structured output, and artifact references. `ToolExecution`
wraps the completed `ToolMessage`, owner-produced events, and a turn directive.
Do not return an old `ToolResult` or attach ad-hoc `client_events` fields.

Register with `ctx.tools.register(...)`; the common pipeline handles the
declared guards and dispatch. Namespace and model visibility are registration
metadata, not an alternate executor or permission bypass.

## Human command contract

```python
from XBotv2.commands import Command, CommandResult, command_usage

async def greet(raw_args: str) -> CommandResult:
    if not raw_args.strip():
        return command_usage("/greet <name>")
    return CommandResult(f"Hello, {raw_args.strip()}!")

command = Command(
    name="greet",
    description="Greet one person",
    usage="/greet <name>",
    handler=greet,
)
```

`Command.kind` is `server`, `prompt`, or `client`. Server commands have an
async handler receiving raw arguments. Prompt commands have no handler and are
expanded/submitted by the client through the message boundary. Client commands
are local affordances; the Textual TUI uses this kind for its local commands.
The server command API accepts one raw line and resolves it against the dynamic
server catalog; it does not accept a kind field. See the [command reference](plugins/commands.md)
and [client runtime reference](client-runtime.md).

Command handlers return `CommandResult`, never a Tool outcome. Results include
status, message, and effects. Published `CommandDescription` entries include
name, slash form, kind, description, usage, examples, parameters, effects, and
exclusivity. The catalog content depends on loaded plugins and the active
thread; clients should discover it rather than hardcode server commands.

## Typed model stream

Providers implement `ModelPort.astream(request: ModelRequest)` and yield
`ModelStreamEvent` values. `TextDelta`, `ReasoningDelta`, and `ToolCallDelta`
carry incremental content; the stream ends with exactly one
`ModelCompleted(response)`, `ModelFailed(error)`, or `ModelCancelled(reason)`.
The terminal `ModelResponse` contains content parts, typed usage, observed
context, stop information, and provider extensions. Keep native provider
chunks and provider-specific retries/errors inside the adapter.

## Event and operation boundaries

Use `Events` from `XBotv2.agentloop` with typed payloads declared by the event
owner. `ctx.on` subscribes to an event; only names in
`SHORT_CIRCUIT_EVENTS` use serial dispatch and accept their stage's declared
result. Do not return ad-hoc dictionaries from observer handlers or model all
stages as one optional-field context.

Use a typed `Operation` where one responder serves a stable request boundary;
use an event when there may be multiple observers; use a direct service method
inside a composed runtime otherwise. HTTP/SSE routes and wire DTOs live with
the owning package.

## Configuration and state

Plugins may declare a Pydantic `Config` model. The complete plugin tree is
resolved before `Context.start()`; XBot does not expose a runtime plugin reload
contract. Treat plugin configuration as startup input.

Persist plugin-owned JSON-compatible values through
`ctx.state.namespace("plugin-name")`. Do not construct the physical state path,
write adjacent files, or persist runtime clients, waiters, or handles.
Persist logical artifact IDs and resolve model-facing absolute paths through
the active thread's `ArtifactStore`/`RuntimeVariables` when building requests.
