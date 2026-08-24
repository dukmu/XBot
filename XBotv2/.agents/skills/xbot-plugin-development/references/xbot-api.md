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

## Typed Events

Use `ctx.on(EVENT, handler)` for observers and the event's documented payload
type. Use `ctx.serial` only for an event whose contract explicitly supports a
short-circuit result. Do not return an ad-hoc dictionary from an observer or
use `EventContext` as a universal business payload.

## Configuration and Tree

Plugin objects may expose an XCore `Config` schema. Tree entries provide `id`,
`name`, optional `profiles`, `disabled`, and `config`. XBot does not expose a
runtime reload contract: compose the complete tree before `Context.start()`
and let each mounted component own its registered effects. Schema
defaults are documentation/runtime validation concerns; do not assume a
service or a hidden config default exists until the mounted context provides it.
