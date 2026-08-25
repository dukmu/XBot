# XBot API Reference

Primary source when a checkout is available: `XBotv2/docs/api/public_api.md`,
`XBotv2/docs/api/api_inventory.md`, and package `__init__.py` files. With a
pip/uv installation, use the version-matched bundled references and inspect
the installed package's `__init__.py`; import from package roots where the
symbol is documented as public.

## Core Symbols

```python
from XBotv2.core import Tool, ToolCall, ToolResult, Message, ClientEvent
from XBotv2.application import APPLICATION_INITIALIZED, ApplicationInitialized
from XBotv2.agentloop import EventContext, Events, LoopSettings
```

`Tool.from_function()` derives the provider schema from the signature and
docstring. `ToolResult` carries model content plus errors, artifacts, images,
and client events. `ToolCall` is the model call contract; it is not a session
identity object.

## Import Map

Import a cross-plugin contract from its owning package root. This keeps the
plugin independent of concrete built-in implementations.

| Need | Public package | Typical declarations |
|---|---|---|
| Tool and messages | `XBotv2.core` | `Tool`, `ToolCall`, `ToolResult`, `Message`, `ClientEvent`, artifact/path contracts |
| Loop hook | `XBotv2.agentloop` | `Events`, `EventContext`, loop settings/state contracts |
| Human command | `XBotv2.commands` | `Command`, `CommandResult`, parsing/error helpers, command operations |
| Session fact/operation | `XBotv2.session` | `SessionInfo`, lifecycle events, session operations/protocol contracts |
| Agent definition | `XBotv2.agents` | Agent catalog/declarations and Agent-owned events |
| Context contribution | `XBotv2.context_builder` | component contracts and context build events |
| Permission decision | `XBotv2.permissions` | permission request/decision contracts and events |
| Live approval channel | `XBotv2.permission_request` | `ApprovalPort` and permission request wire data |
| Application lifecycle | `XBotv2.application` | application initialization and typed lifecycle facts |

If a symbol is absent from the package root and public API inventory, treat it
as implementation detail. A plugin test may use an internal class to assemble a
small realistic harness, but production plugin code should not make a sibling
plugin implementation part of its contract.

## Package Boundaries

Before adding a symbol, check whether one already exists in the owner package:

- `XBotv2.application`: application-owned initialization and runtime events.
- `XBotv2.agents`: Agent definitions, Agent services, and Agent events.
- `XBotv2.context_builder`: context components and build events.
- `XBotv2.permissions`: permission ports and permission events.
- `XBotv2.session`: session contracts, services, and protocol models.
- `XBotv2.commands`: human command contracts.
- `XBotv2.core`: neutral contracts only.

If the feature is transport-facing, keep request/response models and routes in
the owning package's `protocol.py`; do not export a protocol model from core
just to make a plugin import easier.

## Public Tool Contract

```python
ctx.tools.register(
    Tool.from_function(my_tool, name="my-tool"),
    namespace="plugin:example",
)
```

Registration supports `model_visible`, `timeout_seconds`, and `namespace`.
There is no generic dependency dictionary. Bind plugin dependencies before
registration. A keyword-only `ToolCall` parameter is the only core-supplied
invocation metadata and is excluded from the provider schema.

`ToolResult` has four final statuses: `success`, `error`, `denied`, and
`cancelled`. Prefer:

```python
ToolResult.success("human/model-readable result", data={"stable": "json"})
ToolResult.failure("upstream_timeout", "Weather service timed out", retryable=True)
```

- `content` is the model-facing explanation.
- `data` is structured JSON-compatible data, not an arbitrary Python object.
- `artifacts` references content owned by the artifact service.
- `images` carries supported image content.
- `client_events` contains typed client-facing notifications.
- `turn_complete` is exceptional control behavior; do not set it simply because
  one Tool call finished.

The registered Tool name must be stable. `namespace` prevents collisions among
functional groups but does not replace ownership: the registering fiber still
owns automatic cleanup. `model_visible=False` keeps an operation out of the
provider Tool schema; it does not create a permission bypass.

## Human Command Contract

```python
from XBotv2.commands import Command, CommandResult

Command(
    name="sample",
    description="Describe the action",
    usage="/sample <value>",
    examples=("/sample demo",),
    parameters={"value": "Value to use"},
    handler=handler.run,
)
```

Command names use lowercase letters, digits, hyphens, and underscores. A
server command handler is `async (raw_args: str) -> CommandResult`. A prompt
command declares `kind="prompt"` and no handler because the client submits the
expanded prompt through the message boundary. Tool and command execution are
deliberately separate.

## Typed Events

Use `ctx.on(EVENT, handler)` for observers and the event's documented payload
type. Use `ctx.serial` only for an event whose contract explicitly supports a
short-circuit result. Do not return an ad-hoc dictionary from an observer or
use `EventContext` as a universal business payload.

Loop hooks use the `Events` declaration and the fields documented for that
phase. Required fields should be read directly so a malformed internal event
fails at the responsible boundary. A field that is contractually optional may
be checked explicitly; do not use `getattr` to make every historical payload
shape appear supported.

For plugin-owned business facts, define a dataclass payload and event constant
in the owning package. Observers use `emit`; a policy/selection contract may
use `serial` only when its owner documents the bail result. Client events are
transport-visible data and should not be reused as an internal service bus.

## Configuration and Tree

Plugin objects may expose an XCore `Config` schema. Tree entries provide `id`,
`name`, optional `profiles`, `disabled`, and `config`. XBot does not expose a
runtime reload contract: compose the complete tree before `Context.start()`
and let each mounted component own its registered effects. Schema
defaults are documentation/runtime validation concerns; do not assume a
service or a hidden config default exists until the mounted context provides it.

## Operation and Protocol Boundaries

Use a typed `Operation` when a request has one logical responder and crosses a
stable application boundary. Use an event when there can be multiple
observers. Use a direct service method inside one composed runtime when no
dispatch boundary is needed. Do not add an HTTP model to core, invoke a slash
command through a Tool, or dispatch an internal event merely to avoid passing a
typed dependency.

Transport routes, validation models, SSE payloads, and status codes live in the
owning package's `protocol.py`. A Tool or service should return its domain
result; the protocol adapter translates that result to the wire contract.
