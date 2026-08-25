# XCore API Reference

Primary sources when a checkout is available: `XCore/docs/features/plugins.md`,
`services.md`, `events.md`, `lifecycle.md`, `schema.md`, and
`XCore/docs/features/api.md`. For pip/uv installations, use the version-matched
bundled references and inspect the installed `xcore` package. The installed
package is the executable contract for the selected runtime.

## Plugin Forms

XCore accepts a function, class, or object with `apply(ctx, config)`. XBot's
loader conventionally exports a module-level `plugin` object. Set `name`,
`inject`, and optional `Config` on the plugin object/class.

```python
class ExampleHandler:
    def __init__(self, tools):
        self._tools = tools

    async def run(self, event):
        return tuple(self._tools.registered_names())


class Example:
    name = "example"
    inject = ["tools"]

    def apply(self, ctx, config):
        handler = ExampleHandler(ctx.tools)
        ctx.on("example/run", handler.run)
```

`ctx.plugin(...)` returns a `PluginHandle`; load/dispose/restart follow the
fiber lifecycle. Effects from `on`, `set`, `plugin`, and related APIs belong to
the current fiber and are removed in reverse order on unload.

XBot resolves `plugin`, then `Plugin`, then a module/function plugin. It creates
a fresh instance for a module-level plugin object, so exported objects must have
a no-argument constructor. Put runtime dependencies in objects constructed by
`apply`, not in the plugin object's constructor.

`ctx.plugin()` registers a pending fiber. `await ctx.start()` activates the
root, repeatedly loads every dependency-ready fiber, then emits `ready`.
Register the complete XBot tree before that one start. During an active
Context, a newly provided service can activate waiting consumers and a removed
required service rolls consumers back to pending.

## Services and Injection

`ctx.set(name, value)` provides a service; `ctx.get(name)` reads it;
`ctx.require(name)` fails clearly when it is absent; `ctx.has(name)` checks
availability. Required `inject` entries keep a plugin pending until the service
is running. Optional entries do not gate activation and are appropriate only
when the plugin has an intentional, documented no-service mode; resolve that
choice once in `apply`. Use `ctx.isolate(...)`
when a child composition needs a separate service scope.

```python
class Producer:
    name = "producer"

    def apply(self, ctx, config=None) -> None:
        ctx.set("domain", DomainService())


class Consumer:
    name = "consumer"
    inject = ["domain"]

    def apply(self, ctx, config=None) -> None:
        handler = DomainHandler(ctx.domain)
        ctx.on("domain/request", handler.request)
```

The provider's service and the consumer's registrations are fiber effects.
XCore releases the service, settles dependents, and unwinds remaining effects.
Do not manually order tree entries or send a reload event for this lifecycle.

Optional injection is for a real mode such as telemetry-disabled operation:

```python
inject = {"required": ["tools"], "optional": ["telemetry"]}

def apply(self, ctx, config=None):
    telemetry = ctx.get("telemetry")
    handler = Handler(ctx.tools, telemetry or DisabledTelemetry())
```

The null object represents the documented no-telemetry mode. It is not valid
for required persistence, paths, tools, session, model, or permission services.

## Events

Use the appropriate primitive:

- `await ctx.emit(name, payload)` invokes all observers.
- `await ctx.parallel(name, payload)` invokes observers concurrently.
- `await ctx.serial(name, payload)` stops at the first non-`None`/non-`False` result.
- `await ctx.chain(...)` passes a transformed value through listeners.
- `await ctx.waterfall(...)` composes middleware around a `next` function.

`ctx.on` and `ctx.once` return disposers. Listener errors propagate through
normal dispatch except where the documented parallel aggregation applies.
Plugin listeners are removed with their fiber.

| Primitive | Choose it when |
|---|---|
| `emit` | every observer should run in order |
| `parallel` | observers are independent and failures should be collected |
| `serial` / `bail` | the contract defines the first meaningful answer |
| `chain` | each listener transforms one value |
| `waterfall` | middleware may wrap, delegate, or stop the next stage |

For `serial`, every value except `None` and `False` stops dispatch—including
`0`, an empty string, and an empty collection. Event filters inspect the first
argument. `internal/` events are XCore diagnostics/lifecycle hooks, not an
application event namespace.

## Schemas and State

Use `from xcore import S` for `Config` schemas (`S.object`, `S.string`,
`S.number`, `S.boolean`, `S.array`, `S.enum`, `optional`, `default`). Use
`ctx.state.namespace("plugin-name")` for recoverable key/value state; keep
runtime resources outside it. Read the XCore schema/state references when
validation or persistence is part of the plugin.

Prefer a strict schema for external configuration:

```python
Config = S.object({
    "endpoint": S.string(),
    "timeout": S.number().default(10),
    "enabled": S.boolean().default(True),
    "tags": S.array(S.string()).default([]),
}).strict()
```

Builders are immutable and defaults are deep-copied. Missing required keys and
invalid nested values fail before `apply`, with a path in
`SchemaValidationError`. XCore's object `.strict()` discards unknown keys; it
does not reject them. Keep configuration immutable after startup.

All StateService methods are async and writes are atomic. Namespace views share
one lock/cache and prefix keys logically. Values must be JSON-compatible. A
missing key may define a domain's initial state; malformed existing state should
fail loudly. Prefer one versioned snapshot for mutually consistent fields.

## Lifecycle and Cleanup

- `ctx.effect(factory)` executes the factory and records its disposer.
- `ctx.dispose(callback)` records cleanup for an acquired resource.
- apply failure rolls back effects already registered in that apply.
- `stop()` unloads fibers to pending and permits a later `start()`.
- `destroy()` permanently disposes the tree.

Register cleanup immediately after acquiring an external resource and test a
partial setup failure. `PluginHandle.restart()` is an XCore operation, but XBot
does not define a public runtime configuration/plugin reload workflow.

## What Context Is For

`Context` is the composition and effect-registration surface. It belongs in
`apply`, not as a generic service locator retained by domain objects. Resolve
declared dependencies once and pass them into named handlers/services. Avoid
`self.ctx`, runtime `getattr`, repeated `ctx.get`, and nested business closures;
they obscure ownership and turn missing required services into late errors.

## Complete Public Surface

XCore intentionally has a small top-level API. Plugin production code should
import these symbols from `xcore`, not private modules:

| Symbol | Role |
|---|---|
| `Context` | composition, services, events, plugins, effects, lifecycle, state, middleware |
| `Service` | framework service base whose construction provides the instance |
| `Registry` | plugin runtime/instance registry exposed as `ctx.registry` |
| `PluginDef` | normalized plugin definition |
| `PluginHandle` | awaitable lifecycle handle returned by `ctx.plugin` |
| `FiberState` | `pending`, `loading`, `running`, `failed`, `unloading`, `disposed` |
| `StateService` | atomic JSON state and namespace views |
| `EventBus` / `Disposer` | event engine and single-shot cleanup callable type |
| `S` | immutable configuration schema DSL |
| `XCoreError` | base XCore exception |
| `InactiveEffectError` | effect registered on an inactive/disposed owner |
| `ServiceNotFoundError` | `ctx.require` could not resolve a service |
| `ServiceConflictError` | duplicate service in one scope |
| `SchemaValidationError` | invalid schema value with a path |
| `current_fiber` | current applying fiber, otherwise `None` |
| `current_plugin_name` | current applying plugin name, otherwise `"unknown"` |
| `bound_effect` | capability-service helper binding cleanup during apply |

`current_fiber`, `current_plugin_name`, and `bound_effect` exist for framework
capability implementations such as a registry that must attribute a
registration to its caller. An ordinary plugin should use Context effects and
its returned disposers instead of inspecting the current fiber.

## All Plugin Shapes

Function plugin:

```python
def audit_plugin(ctx, config):
    handler = AuditHandler(ctx.audit_sink)
    ctx.on("audit/write", handler.write)

audit_plugin.name = "audit"
audit_plugin.inject = ["audit_sink"]
audit_plugin.Config = S.object({"level": S.string().default("info")}).strict()
```

Object plugin:

```python
class AuditPlugin:
    name = "audit"
    inject = ["audit_sink"]
    Config = S.object({"level": S.string().default("info")}).strict()

    def apply(self, ctx, config): ...

plugin = AuditPlugin()
```

Class plugin (construction is the plugin body):

```python
class AuditPlugin:
    name = "audit"
    inject = ["audit_sink"]

    def __init__(self, ctx, config): ...

plugin = AuditPlugin
```

XBot normally favors the object form with a no-argument plugin object and a
small `apply` composition method. The class-plugin form necessarily receives
Context in its constructor and is therefore easy to misuse as a long-lived
service locator; do not choose it for XBot business services.

An apply/function body may return a sync or async disposer. Configuration
validation or apply failure moves that fiber to `failed` without stopping
unrelated plugins. Awaiting its `PluginHandle` rethrows the original exception.
Effects already registered by the failed apply are rolled back in reverse
order.

## Registry and PluginHandle

```python
handle = ctx.plugin(plugin, {"option": 1})
await handle
handle.state
handle.error
handle.missing_dependencies
handle.name, handle.config, handle.uid
await handle.restart()
await handle.dispose()
```

Awaiting a pending handle with missing required services returns immediately;
inspect `state` and `missing_dependencies`. Awaiting a failed handle rethrows
the stored error. `dispose()` permanently removes that instance, while
`restart()` unloads and attempts to activate it with the same configuration.

`ctx.registry` supports `get`, `has`, `delete`, `keys`, `values`, `entries`,
iteration helpers, and length. Multiple mounts of the same callback share one
runtime definition but own independent fibers. `registry.delete(plugin)`
removes all instances of that plugin identity. A nested `ctx.plugin(child)` is
owned by its parent fiber and is recursively removed with it.

Fiber transitions are:

```text
pending -> loading -> running -> unloading -> pending
                  \-> failed                 \-> disposed
```

Dependency changes and restart may retry a failed fiber. A dependency cycle
leaves participants pending; startup itself does not hang. Each fiber
serializes in-flight transitions, and root lifecycle operations are also
serialized.

## Service Resolution and Isolation

```python
ctx.set("database", database)       # provide; returns disposer
ctx.database                        # required attribute-style access
ctx.get("database")                 # None when absent/inactive
ctx.require("database")             # ServiceNotFoundError when absent
ctx.has("database")                 # present regardless of strict running state
ctx.unset("database", database)     # identity-checked removal
```

One scope may provide a service name once; a second non-`None` value raises
`ServiceConflictError`. The providing fiber owns removal. Attribute assignment
such as `ctx.database = value` is invalid—use `set`.

`get(..., strict=True)` is the default and hides a service whose providing
fiber is not running. `strict=False` is a framework inspection escape hatch,
not a plugin dependency strategy; required `inject` provides the correct
activation guarantee.

The `Service` base provides itself when constructed:

```python
class DatabaseService(Service):
    name = "database"

    def __init__(self, ctx, path):
        self.path = path
        super().__init__(ctx)
```

This base stores Context for framework mechanics. In XBot plugins, prefer a
plain domain service with explicit fields plus `ctx.set("database", service)`
at composition, unless subclassing `Service` has a concrete benefit.

Isolation creates a distinct service scope:

```python
private = ctx.isolate("database")
shared_a = ctx.isolate("database", label)
shared_b = ctx.isolate("database", label)
```

Each omitted label is fresh; passing the same label joins scopes. Isolation is
bidirectional for that name: parent and isolated child do not see one another's
implementation. `ctx.extend()` inherits the current filters and isolation;
`ctx.select(field, value)` additionally filters later registrations.

Do not use Context members as service names: `name`, `config`, `root`, `parent`,
`fiber`, `registry`, `state`, lifecycle/event/plugin/effect methods, names
starting with `_`, and `then` are reserved.

## Effects and Disposers in Detail

```python
disposer = ctx.effect(lambda: register_and_return_disposer(), label="feature")
cleanup = ctx.dispose(resource.close)
```

The effect body is synchronous and runs immediately. It returns a callable
disposer or `None`; returning an awaitable or another value is an error. A
disposer may itself be async. Directly calling the returned disposer is
single-shot; synchronous cleanup runs immediately and async cleanup is
scheduled. Fiber unload awaits async cleanup and logs cleanup failures while
continuing the remaining teardown.

`on`, `once`, `set`, `middleware`, `plugin`, and XBot capability registries are
implemented as effects. Static registrations made during apply therefore need
no parallel manual list. Registrations made later from an event callback are
outside the apply fiber unless their capability explicitly attributes them;
their owner must retain and invoke the returned disposer.

`bound_effect(disposer)` is how an XBot capability registry attributes a call
made during plugin apply. It returns `False` outside apply or on an inactive
fiber, so a dynamic caller must still own cleanup explicitly.

## Filters, Selectors, and Middleware

```python
scoped = ctx.select("platform", "terminal")
scoped.on("message", handler)

ctx.filter(lambda session: session.user_id == "admin")
ctx.on("message", admin_handler)
```

Filters are snapshotted when a listener or middleware is registered. Removing
or adding a filter later does not rewrite earlier registrations. They inspect
the first dispatch argument; events without arguments are not filtered.
`global_=True` bypasses filters and should only be used for genuinely global
facts.

Middleware is an around-chain independent from named events:

```python
async def timing(session, next_fn):
    started = clock()
    try:
        return await next_fn()
    finally:
        metrics.observe(clock() - started)

ctx.middleware(timing, prepend=False)
result = await ctx.run_middleware(session)
```

Each middleware has signature `async (session, next_fn) -> Any`. Not calling
`next_fn` short-circuits later middleware. Registrations are globally ordered,
filter-aware, fiber-owned, and snapshotted into a fresh chain for each run.
Exceptions propagate to the caller.

Use `waterfall` for a named around-event contract and `middleware` for the
Context's generic session middleware chain; do not combine them accidentally.

## Event Naming and Internal Events

Event names are non-empty `/`-separated strings. Exact listeners are normal;
`foo/*` matches one hierarchy pattern and `*` matches all events. `*` must be a
whole segment. Exact and wildcard listeners retain global registration order.

`ctx.once` marks itself fired before awaiting the listener, so concurrent
dispatch still invokes it at most once. `ctx.before("save", handler)` registers
`before-save`, prepended by default.

Framework internal events are synchronous diagnostic hooks; their listeners
must be synchronous and failures are logged rather than propagated:

| Event | Arguments | Meaning |
|---|---|---|
| `internal/status` | `(fiber, old_state)` | fiber state transition |
| `internal/service` | `(name, value)` | service provide/remove/change |
| `internal/dispatch` | `(mode, name, args)` | non-internal dispatch trace |
| `internal/listener` | `(ctx, name, listener, options)` | listener registration interception |
| `internal/error` | `(fiber, error)` | plugin/effect failure notice |

Do not use `internal/` for plugin business events.

## Root Lifecycle Guarantees

```python
await ctx.start()
await ctx.stop()
await ctx.start()
await ctx.destroy()
```

- `start` activates, loads to a dependency fixpoint, then emits `ready`.
- registering a `ready` listener after activation schedules it for the next
  event-loop turn.
- `stop` emits `dispose` before listener fibers unload, unloads in reverse load
  order, logs disposer failures, and never raises.
- state content survives `stop` then `start`.
- repeated start/stop is a logged no-op.
- `destroy` is permanent; a later start raises `RuntimeError`.
- an async apply in flight is allowed to finish before its serialized unload,
  so late effects are still collected and removed.

The `dispose` event is an escape hatch for a side effect XCore cannot otherwise
track. Prefer an apply return disposer or `ctx.dispose` for resources with one
clear owner.

## Schema Rules and Errors

Schema constructors are `S.any`, `string`, `number`, `boolean`, `array`,
`object`, `union`, `enum`, and `const`. Modifiers are `default`, `optional`,
`description`, and object-only `strict`. Important rules:

- `number` rejects booleans;
- object unknown keys are preserved unless `.strict()` discards them;
- optional missing keys remain absent rather than being inserted as `None`;
- defaults are deep-copied and never shared;
- union branches are tried in order and errors are aggregated;
- nested failures include a path such as `$.servers[0].url`.

An `S` plugin `Config` is validated with defaults before apply. A plain dict
Config only shallow-merges defaults and is intentionally loose; avoid it for a
new external plugin. `Config = None` performs no validation.

## State Guarantees and Limits

```python
state = ctx.state.namespace("example")
value = await state.get("snapshot", default=None)
await state.set("snapshot", {"version": 1, "items": []})
await state.delete("snapshot")
keys = await state.keys()
copy = await state.all()
await state.clear()
```

The root Context uses `<data_dir>/state.json`; XBot supplies its configured
state service/data directory. `set` and `delete` return only after atomic
write-and-replace. All namespaces over one state file share an asyncio lock and
cache, so individual writes do not lose unrelated keys. A plugin-level
`get`-then-`set` sequence is not a transaction; prefer one snapshot or an owner
service that serializes compound updates. Invalid JSON on disk raises
`RuntimeError`; unsupported Python values raise `TypeError`.

Namespace prefixes are logical ownership, not separate files. StateService is
a small JSON KV contract, not a general database transaction system: when a
domain update must change several fields atomically, serialize them as one
validated snapshot value.
