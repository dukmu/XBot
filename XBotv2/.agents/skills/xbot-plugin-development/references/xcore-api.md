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
class Example:
    name = "example"
    inject = {"required": ["tools"], "optional": ["metrics"]}

    def apply(self, ctx, config):
        metrics = ctx.get("metrics", strict=False)
        ctx.dispose(self.close)
```

`ctx.plugin(...)` returns a `PluginHandle`; load/dispose/restart follow the
fiber lifecycle. Effects from `on`, `set`, `plugin`, and related APIs belong to
the current fiber and are removed in reverse order on unload.

## Services and Injection

`ctx.set(name, value)` provides a service; `ctx.get(name)` reads it;
`ctx.require(name)` fails clearly when it is absent; `ctx.has(name)` checks
availability. Required `inject` entries keep a plugin pending until the service
is running. Optional entries do not gate activation. Use `ctx.isolate(...)`
when a child composition needs a separate service scope.

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

## Schemas and State

Use `from xcore import S` for `Config` schemas (`S.object`, `S.string`,
`S.number`, `S.boolean`, `S.array`, `S.enum`, `optional`, `default`). Use
`ctx.state.namespace("plugin-name")` for recoverable key/value state; keep
runtime resources outside it. Read the XCore schema/state references when
validation or persistence is part of the plugin.
