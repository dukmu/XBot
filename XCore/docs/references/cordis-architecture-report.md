# Cordis Plugin Framework — Architecture Analysis

*A source-verified feature analysis of the Cordis/Koishi plugin framework, written to drive the design of a Python equivalent (not API-identical, but with equivalent core capabilities).*

**Version note (important).** The npm `cordis` package is currently at **4.0.0-rc.8** (master), but **Koishi 4.18.x (`@koishijs/core` 4.18.11) depends on `cordis ^3.18.1`** — i.e. *Koishi today runs Cordis v3* (last v3 release: 3.18.1). Cordis v4 is the in-development rewrite (same concepts, new names/mechanics). This report documents v3.18.1 as the primary reference (what Koishi actually uses), and flags v4 differences wherever they matter, because the v4 API is the "current" upstream code and the DeepSeek Harness fork (`@deepseek-ai/cordis` 4.0.1) is v4-based.

**Primary sources consulted (all read directly from source or official docs):**

- `cordis` v3.18.1 source (commit `4658414e`): `packages/core/src/{context,events,registry,scope,service,reflect,utils}.ts` — https://github.com/cordiverse/cordis
- `cordis` master (v4.0.0-rc.8) source: `packages/core/src/{context,events,fiber,registry,service,reflect,logger,utils}.ts`, plus `packages/loader`, `packages/group` — https://github.com/cordiverse/cordis/tree/master/packages/core
- `@deepseek-ai/cordis` 4.0.1 (DSH fork, ships full `src/`): https://github.com/deepseek-ai/deepseek-harness (vendor/cordis)
- Koishi monorepo master (`@koishijs/core` 4.18.11): `packages/core/src/{context,middleware,filter,schema,database,bot,session,permission,command}.ts`, `packages/loader`, `packages/koishi/src/worker` — https://github.com/koishijs/koishi
- Koishi docs: `guide/plugin/{service,lifecycle,schema,index,context}.md` and current English docs from https://github.com/koishijs/docs (`en-US/guide/plugin/*`, `en-US/guide/basic/events.md`, `en-US/guide/database/index.md`) — rendered at https://koishi.chat
- Minato (database) v3.7.0 npm source (`src/{model,database,selection,query,eval,driver,type}.ts`) and the v4-era rewrite repo https://github.com/cordiverse/minato (now `cordiverse/database`, published as `minato` 4.x / `@cordisjs/plugin-database`)
- Schemastery (config schema) 3.18.x source (`packages/core/src/index.ts`) — https://github.com/shigma/schemastery, https://www.npmjs.com/package/schemastery
- Satori (`@satorijs/core` 4.6.0) — the framework layer Koishi builds on top of Cordis (sessions, adapters, bots)

---

## 1. Core concepts: Context, Service, Plugin, schema, events, lifecycle

### 1.1 Context

A **Context** is the central object: every plugin, listener, middleware, command, and service is bound to one. In Cordis's own words, the app is "a container carrying various capabilities (database, adapters…), and the context is the interface to access them." It is explicitly compared to IoC: *"for developers familiar with IoC/DI: a service is an IoC-like implementation (but not implemented through DI)."*

Mechanics (v3, `context.ts`):

- The root context is created with `new Context(config?)` and is wrapped in a **Proxy** (`ReflectService.handler`). All property access on `ctx` goes through the proxy, which resolves *services* (see §2).
- Derived contexts are created with `ctx.extend(meta)` — a plain object whose prototype chain includes the parent context. Meta fields such as `filter`, `isolate`, `intercept` are **copy-on-write dictionaries chained via prototypes**, so a derived context sees parent values unless it shadows them. This is the trick that makes selectors/isolation cheap and compositional.
- `ctx.root` — the root context. `ctx.isolate(name)` and `ctx.intercept(name, config)` return derived contexts.
- Every plugin load creates a **new child context** (`scope.ctx = parent.extend({scope})`); this per-plugin context is what guarantees side-effect cleanup (see §4, §10).
- Koishi aliases `app = ctx.root` (deprecated but common) and defines selectors (`ctx.user(...)`, `ctx.guild(...)`, …) that return derived contexts carrying a `filter` predicate.

### 1.2 Service

A **Service** is a named capability registered on a context: `ctx.database`, `ctx.model`, `ctx.http`, `ctx.logger`, `ctx.assets`, … Koishi's docs classify services into three kinds:

1. **Built-in** services Koishi itself provides (`ctx.model`, `ctx.i18n`, `ctx.logger`…).
2. **Declared but not implemented** services that some plugin must provide (`ctx.database`, `ctx.assets`, `ctx.cache`) — naming convention: implementation plugins are prefixed with the service name (`database-mysql`, `assets-local`).
3. **Plugin-defined** services (`ctx.console`, `ctx.puppeteer`, `ctx.worker`).

A service is a subclass of `cordis.Service` (or in Koishi, `koishi.Service` which extends `satori.Service`): `class Console extends Service { constructor(ctx){ super(ctx, 'console', true) } }`. Because a Service subclass is itself a valid plugin, you load it with `ctx.plugin(Console)`. See §2 for the full mechanics.

### 1.3 Plugin

A **Plugin** is one of:

- a **function** `(ctx, config) => void` (the "apply" function),
- a **class** `new (ctx, config)` (constructor runs the plugin body),
- an **object** `{ apply(ctx, config), name?, Config?, inject?, reusable?, reactive?, fork? }`.

Loading a plugin = calling its body with a fresh context and the validated config: `ctx.plugin(plugin, config)` (v3 returns a `ForkScope`; v4 returns a `Fiber`). Plugins nest: a plugin body may call `ctx.plugin(otherPlugin)` — the child plugin gets its own child context and its own cleanup scope. See §4.

### 1.4 Schema

A **Schema** (schemastery) is a declarative config validator + default filler: `Schema.object({ foo: Schema.string().required(), bar: Schema.number().default(1) })`. Plugins attach it as `export const schema = ...` (or `Config`); Cordis applies it to the raw config before the plugin body runs (`resolveConfig`). See §7.

### 1.5 Events

Events are named, multi-listener dispatches with several execution modes (`emit`, `parallel`, `bail`, `serial`, `chain`, `waterfall`) and context filtering (§3). The lifecycle itself is expressed as events (`ready`, `dispose`, `fork`, plus `internal/*` events).

### 1.6 Lifecycle

Every plugin load and the app itself run inside a **scope** (v3: `EffectScope`/`MainScope`/`ForkScope`; v4: `Fiber`) — a state machine (`PENDING → LOADING → ACTIVE | FAILED → DISPOSED`) that owns all disposables created by that plugin. Lifecycle = the transition rules of this state machine + the `start()/stop()` app-level events (v3) or the fiber's `_reload/_unload` (v4). See §5.

### 1.7 Relationship: Context ↔ Service

- The Context is the **container + access interface**; the Service is a **value registered under a name** in the container's store.
- The context Proxy resolves `ctx.foo` → service value. Services hold a back-reference `service.ctx`.
- Services are *registered by plugins* (`ctx.set(name, value)` or by constructing a `Service` subclass), and *dereferenced by other plugins* — the dependency relationship is declared (`inject`), not wired by DI; Cordis resolves it dynamically at runtime and reacts to service (re)registration (see §2, "service referencing/injecting").
- The same service name can be **isolated** per context scope (`ctx.isolate(name)`), yielding multiple instances serving different context subtrees.

---

## 2. Service system

### 2.1 The service registry in v3 (`ctx.set` / `ctx.get` / `ctx.unset`)

Cordis v3 keeps services in a per-context **store** (a plain dict):

- `ctx[Context.store]`: `Dict<{ value, source }, symbol>` — maps a **per-name Symbol key** to the current value and the providing context.
- `ctx[Context.isolate]`: `Dict<symbol>` — maps each service **name** to a Symbol. All contexts that share the same Symbol for a name share the same instance; `ctx.isolate(name)` creates a derived context whose isolate dict shadows `name` with a **new Symbol**, splitting the instance.
- `ctx[Context.internal]`: name → `{type: 'service'|'accessor'|'alias'}` declarations (registered on the **root** context; the *declaration* is global even though the *value* is per-isolate).

Public API (mixed into every context via `reflect` mixins):

| Method | Semantics |
|---|---|
| `ctx.get(name)` | Resolve aliases, look up the store under the isolate Symbol, return value (or `undefined`). Wraps the value in a **traceable proxy** (see 2.3). |
| `ctx.set(name, value)` | Declare the service (if not yet declared) and set the value in the current context's isolate. Returns a **disposer** that resets it to `undefined` (auto-registered as a scope effect). |
| `ctx.provide(name, value?, builtin?)` | **Deprecated** alias of `set` with declaration semantics (declares on root, seeds value). |
| `ctx.alias(name, aliases)` | Declare alternate names resolving to `name`. |
| `ctx.accessor(name, {get, set})` | Declare a computed property instead of a stored value. |
| `ctx.mixin(source, mixins)` | Expose methods/accessors of a service directly on the context (`ctx.database.getUser(...)` sugar). |

`ctx.set` semantics (from `reflect.ts`):

1. `provide(name)` declares the name on root (no-op if already declared) and assigns the isolate Symbol.
2. **Override check**: if the old value and new value are both non-nullish → throw `service ${name} has been registered` (a service may only be *provided* once per isolate; setting to `undefined` releases it).
3. Emits `internal/before-service` (with a filtered this-context) **before** writing, then `internal/service` **after** writing. Both events are dispatched with a special `this` whose `[Context.filter]` matches only contexts sharing the same isolate Symbol for that name — this is how plugins waiting on a service are notified **only in the right scope** (see 2.5).
4. Registers `ctx.set(name, undefined)` as a scope effect, so the service disappears when the providing plugin unloads.
5. Unproxyable values (Map/Set/Date/Promise) trigger a warning (`internal/warning`).

### 2.2 Callable services and the Proxy (`ctx.foo(...)`)

Two things make `ctx.foo(...)` work:

1. **Context Proxy** (`ReflectService.handler`): when you read an unregistered property `ctx.foo`:
   - Resolve aliases (`resolveInject`).
   - If declared as an **accessor**, call its `get`.
   - If a **service**, `ctx.reflect.get(name)` → store lookup; returns `undefined` when not provided (no crash).
   - If **not declared at all**, it warns (unless inside a plugin that declared it via `inject`, or `$`/`_`-prefixed, or accessed from root): `property ${name} is not registered, declare it as inject to suppress this warning`, then returns `undefined`. `has` also reports declared props.
2. **Callable services** (`Service[invoke]`): a Service subclass that implements `[Service.invoke]` is wrapped by `createCallable` — a function whose prototype chain merges the service instance's prototype with `Function.prototype`. Calling `ctx.foo(...)` invokes the `invoke` body with the *traceable proxy* as `this`. Example: `ctx.logger('name')` returns a namespaced logger; `ctx.http(...)` performs a request.

### 2.3 Tracing / `ctx.caller` (hot-reload bookkeeping)

Every service value is wrapped in a **traceable proxy** (`getTraceable`/`createTraceable`) that:

- redirects the special `ctx` property to the *accessing* context (so `this.ctx` inside a service method is the caller's context, not the provider's — wait, precisely: it's the context that was used to obtain the service);
- when a method is called on a traced service, the caller context is attached to the receiver, so the method can read `this[Context.current]` (Koishi) or `this.caller` (older docs) to learn **which plugin context invoked it**, and register cleanup (`this[Context.current]?.on('dispose', ...)`) that undoes the call's side effects when that plugin is hot-reloaded. The docs stress: for **async** methods, capture the caller reference *before* any `await`, because it may be reassigned by a later call.

This is the key mechanism that makes *hot reload* safe: side effects caused by plugin A on service B are removed when A unloads.

### 2.4 Selectors and service isolation (`ctx.select`?)

**There is no `ctx.select` API in Cordis or Koishi.** The "selectors" in Koishi docs are:

- **Session selectors** (context level): `ctx.user('112233')`, `ctx.self(...)`, `ctx.guild(...)`, `ctx.channel(...)`, `ctx.platform(...)`, `ctx.private()` — each returns a derived context whose `filter(session)` predicate matches the session. Chainable (`app.platform('discord').user('112233')`). Composable with `ctx.union/intersect/exclude/any/never` (set algebra on contexts or raw `(session) => boolean` predicates).
- **Service isolation** (cordis core): `ctx.isolate(name)` — creates a context where `name` maps to a fresh Symbol, so a service provided in that subtree is a *separate instance* invisible outside it (and vice versa). Used in configs via the `$isolate` loader key. In v4 the same primitive exists (`ctx.isolate`) plus `ctx.intercept(name, config)` which attaches *per-scope config* to a service (merged via `Service[resolveConfig]`).
- **Event-level filtering**: when dispatching with a leading "thisArg" (e.g. a `Session`), Cordis calls `thisArg[Context.filter](hook.ctx)` for each hook and only invokes hooks whose owning context matches. Koishi wires `Session.prototype[Context.filter] = (ctx) => ctx.filter(this)`, which is how session events only reach listeners registered on matching selector contexts.

### 2.5 Service referencing / injecting (`inject`, `using`)

Plugins declare dependencies so Cordis can gate loading and react to (re)registration:

```ts
export const name = 'dialogue'
export const inject = { required: ['database'], optional: ['assets'] }
// array form: export const inject = ['database']        (all required)
// legacy name: export const using = ...
export function apply(ctx: Context) {
  ctx.database.get('dialogue', {})   // safe: only runs when database is truthy
}
```

`Inject.resolve` normalizes to `{ name: { required: boolean } }`. Semantics (official docs):

- Until every **required** service's value is truthy, the plugin body **does not load** (the scope stays PENDING).
- When a required service's value *changes*, the plugin is **rolled back** (effects disposed) and, if the new value is still truthy, **reloaded**.
- **Optional** deps don't gate loading; check `if (ctx.assets)` at runtime.

Implementation: `EffectScope.ready` = every required dep `get(name)` truthy; `start()` only runs when ready. Two global event handlers in `Lifecycle` drive it: `internal/before-service` resets affected scopes (rollback), `internal/service` re-`start()`s them; both filter by isolate so only the right scope reacts. Sub-dependencies use `ctx.inject(deps, callback)` = `ctx.plugin({ inject, apply: callback })`. In v4 this becomes `ctx.inject(deps, cb)` / the `inject` plugin property, plus `@Inject(name, config?)` decorators and per-service **intercept config** (`ctx.intercept(name, config)`; services read it via `Service[resolveConfig]` — merged from the intercept chain with `Config.merge` or shallow assign).

### 2.6 v4 differences (service system)

- `ctx.set/get` still exist but the store is replaced by `ctx.reflect` (`provide`, `accessor`, `mixin`, `get`, `set`) with a cleaner `Impl` model: `{ name, fiber, value, check }` per isolate key; `provide()` returns a disposer; service availability is **per-fiber** and `internal/get` / `internal/set` are **waterfall** events so any plugin can intercept service reads/writes (e.g. the DSH host intercepts `ctx.get` for cross-process services).
- Services are registered by the `Service` constructor itself: `self.ctx.reflect.provide(name, self, this[Service.check])` — no separate `set` needed; unregistration is automatic with the fiber.
- `ctx.accessor`/`ctx.mixin` remain, driven by the same reflect service.

---

## 3. Event system

### 3.1 Registration

- `ctx.on(name, listener, options?)` → returns a **disposer** `() => boolean` (call it to unregister). Options: `{ prepend?: boolean, global?: boolean }`; a bare boolean is shorthand for `prepend`.
- `ctx.once(name, listener, options?)` → same, listener auto-removes before running (implemented as a wrapper that disposes itself).
- `ctx.off(name, listener)` exists in the documented type surface but is **not implemented in cordis v3 core** (the lifecycle mixin list is `['on','once','parallel','emit','serial','bail','start','stop']`); the canonical unregister is the returned disposer. Koishi docs still mention `off` as the EventEmitter-style API.
- Every listener is registered as an **effect of the current plugin scope/fiber** — it is automatically removed when the owning plugin is disposed (this is the hot-reload safety net).
- Registration is interceptable: `ctx.on` first runs `bail(ctx, 'internal/listener', name, listener, options)`; the framework's own handler special-cases the `ready`, `dispose`, and `fork` "events" (see §5).

### 3.2 Naming conventions

- **kebab-case** (`guild-member-added`), never snake_case.
- **Namespaces with `/`** for related events: `dialogue/before-search`, `dialogue/search`, `internal/error`, `app/ready`, `loader/config-update`.
- **Paired before/after events**: `xxx` and `before-xxx`. `ctx.before('xxx', cb, append?)` is sugar for `ctx.on('before-xxx', cb, !append)` (note the **inverted** default: `before` listeners default to *prepend*).
- **No wildcards.** Event names are exact-match strings; `foo/*` is not supported. The universal interception point is the special event `internal/event` (v3) / `internal/dispatch` (v4): **every non-internal dispatch first emits `internal/event` with `(mode, name, args, thisArg)`**, so a listener can observe/rewrite/filter all events. Listener registration itself is interceptable via `internal/listener`.

### 3.3 Payload conventions

- Any arguments; a leading argument that is an **object or function** is treated as the dispatch **`this`** (and the filter subject): `ctx.emit(session, 'custom-event', arg1, ...)`. This is how session events filter by selector (see 2.4).
- Return values are ignored by `emit`/`parallel` but meaningful for `bail`/`serial`/`chain`/`waterfall`.

### 3.4 Dispatch modes and ordering (v3 `events.ts`)

The heart is `dispatch(type, args)`: a generator that resolves the `thisArg`, emits `internal/event`, then **filters hooks** (`filterHooks`: keep `hook.global` or hooks whose owning ctx passes `thisArg[Context.filter]`) and yields each `callback.bind(thisArg)(...args)` **in registration order** (`prepend` inserts at the front).

| Method | Mode | Behavior |
|---|---|---|
| `ctx.emit(name, ...args)` | sync | Runs **all** listeners synchronously in order; returned promises are fire-and-forget. |
| `ctx.parallel(...)` | async | Runs all listeners, **concurrently** (v3: `Promise.all` over the invoked callbacks — callbacks start in order; **first rejection rejects** the whole dispatch; v4: `Promise.allSettled` and throws an **`AggregateError`** of all rejections). |
| `ctx.bail(...)` | sync | Runs listeners in order until one returns a **bail value** = anything except `null`, `false`, `undefined` (`isBailed`); returns that value. |
| `ctx.serial(...)` | async | Same as bail, but awaits each listener before the next. |
| `ctx.chain(...)` *(koishi)* | sync | Runs listeners in order; each listener's return value becomes the **first argument** of the next; returns the final value. |
| `ctx.waterfall(...)` *(koishi, async; v4 core)* | async | Same as chain but awaited; **v4 core version**: the *last dispatch argument is treated as the innermost `next`* and listeners are composed around it (see below). |

**Interception / middleware-style events (v4 `waterfall`):** `ctx.waterfall('internal/update', config, noSave, next)` — each listener receives the original args plus a `next` continuation; calling `next()` proceeds to the next listener (finally the built-in `inner`), *not* calling it **vetoes** the operation. This is exactly middleware composition and is how `internal/get`, `internal/set`, `internal/update`, `internal/config`, `internal/listener` are extensible. In v3 the closest analogues are `chain`/`waterfall` in koishi and the `internal/*` interception events.

Ordering guarantees: listeners run in registration order; `prepend` puts a listener ahead of all previously registered ones (LIFO for prepended chains); `serial`/`bail` preserve that order with early termination; `parallel` starts all in order but completion order is unspecified.

---

## 4. Plugin system

### 4.1 `ctx.plugin(plugin, config?)`

v3: `ctx.plugin(plugin, config?)` → returns a **`ForkScope`** (a promise-like? no — it's an object with `.dispose()`, `.ctx`, `.config`, `.status`; *not* awaitable in v3 — you observe readiness via `ready`/status events). v4: returns a **`Fiber` & PromiseLike<Fiber>** — awaiting it settles when loading finishes (rejects on config/startup errors).

Flow (`registry.ts` v3):

1. `resolve(plugin, assert)` — a **function** resolves to itself; an **object with `apply`** resolves to `apply`; `null` is the special root marker; otherwise throws `invalid plugin, expect function or object with an "apply" method`.
2. `this.ctx.scope.assertActive()` — can't load plugins after the scope is disposed.
3. **Config resolution**: `resolveConfig(plugin, config)` applies `plugin.Config || plugin.schema` (unless `schema === false`) to the raw config; throws on validation failure. On failure it emits `internal/error`, records the error, and *still creates* the fork in a **FAILED** state (config = `null`).
4. **Dedup**: look up `registry.get(plugin)` (keyed by the resolved callback). If a runtime exists: if it is **not reusable** → warn `duplicate plugin detected` and **reuse** it (return `runtime.fork(...)` — the body does not run twice). If **reusable** → create a new fork of the same runtime.
5. Otherwise create a `MainScope` (the runtime: one per plugin *function*), register it, and fork it once.

### 4.2 Plugin shapes

```ts
// function plugin (the most common)
export const name = 'my-plugin'
export const schema = Schema.object({ ... })
export const inject = ['database']
export function apply(ctx, config) { ... }

// object plugin
export default { name, schema, inject, apply(ctx, config) { ... } }

// class plugin
export default class MyPlugin {
  static reusable = true
  constructor(ctx, config) { ... }   // body runs in constructor
}
```

Additional plugin metadata: `name` (display; function name/class name default; object `apply` without name defaults to `undefined`), `Config`/`schema`, `inject`/`using`, `reusable` (see below), `reactive` (config reactivity for the web console), `fork` (class plugin hook), `intercept` (v4), `provide` (v4, services this plugin provides). Koishi adds `filter?: boolean` on object plugins (whether the plugin participates in context filtering) and package.json `koishi.service.{required,optional,implements}` declarations for the marketplace UI.

### 4.3 Lifecycle states and failure

`ScopeStatus`: `PENDING → LOADING → ACTIVE | FAILED → DISPOSED`.

- **PENDING**: plugin registered but not ready (missing required services, or awaiting start).
- **LOADING**: `start()` called — the apply body is running (`scope.ensure(async () => scope.value = apply(ctx, config))`); `status` reflects pending tasks.
- **ACTIVE**: body finished without throwing and all required services present.
- **FAILED**: the body (or config validation, or a service-related reset) threw; `scope.error` holds the error.
- **DISPOSED**: `fork.dispose()`/`scope.dispose()` called; `uid = null`.

**What happens on throw**: inside `ensure()`, the task's rejection is caught → `context.emit(ctx, 'internal/error', reason)` → `scope.cancel(reason)` → status FAILED and `reset()` **disposes every effect the plugin created before throwing** (partial side effects are rolled back). The app keeps running; only that plugin is out. The loader (koishi) logs the error; historically it also disables the plugin in the config file so a crash-loop doesn't repeat.

### 4.4 Reusable plugins and `fork`

Non-reusable plugins take effect once per application (second `plugin()` call reuses the same instance). `reusable = true` (or a class `fork` method, or a `fork` event listener) lets one plugin definition run **multiple instances** with different configs:

```ts
ctx.plugin(reply, { input: '天王盖地虎', output: '宝塔镇河妖' })
ctx.plugin(reply, { input: '宫廷玉液酒', output: '一百八一杯' })
```

The runtime (`MainScope`) runs once (outer scope); each `plugin()` call creates a `ForkScope` (inner scope) whose body is the reusable part. The `fork` lifecycle event fires for each fork, with `(ctx, config)` of that instance — used for shared state across instances (`let count = 0; ctx.on('fork', ctx => { count++; ctx.on('dispose', () => count--) })`).

### 4.5 `ctx.registry` and unload

`ctx.registry` is a Map-like of plugin **runtime** records: `get/has/set/delete/keys/values/entries/forEach`, plus `counter` (unique uid allocator) and `plugin/using/inject` (v3) or `plugin/inject` (v4).

- `fork.dispose()` — unload **one instance** (one ForkScope / Fiber): runs its disposers (LIFO), removes it from the runtime, and when the last fork of a runtime is gone, the runtime record is deleted.
- `ctx.registry.delete(plugin)` — remove the runtime and **all** its forks at once (used for reusable plugins).
- v3 koishi docs also mention `ctx.dispose(plugin?)` (unload the current plugin) — legacy; the current API is `fork.dispose()` / `registry.delete()`.

### 4.6 Loading plugins from disk (loader)

Cordis core only loads plugins already in memory. Config-driven loading lives in `@cordisjs/plugin-loader` (v4) / `@koishijs/loader` (koishi):

- A config file (`koishi.yml`/`.ts`) declares a **plugin tree**: `plugins: { admin: {}, 'group:1': { $isolate: [...], database-mysql: {}, github: {} } }`.
- `$`-prefixed keys are **special** (selectors/filters/isolate/intercept — `$platform`, `$channel`, `$user`, `$or`, `$not`, `$isolate`, …) and are translated into derived contexts before the plugin is loaded (`app.platform('onebot').channel('123','456').plugin('repeater', {...})`).
- `~`-prefixed keys are comments. Entries can be disabled; failed plugins are unloaded and (koishi) marked disabled in the written-back config.
- The loader **persists config updates**: when a plugin calls `ctx.fiber.update(config)` (v4) or the web console edits config, `internal/update` is emitted; the loader simplifies the config via the schema (`Config.simplify`) and writes it back to the file, then restarts the plugin (see §5).
- Hot module replacement (`@cordisjs/plugin-hmr`), plugin groups (`@cordisjs/plugin-group`), and config-file `include` (`@cordisjs/plugin-include`) extend this.

---

## 5. Lifecycle

### 5.1 App lifecycle (v3): `start()` / `stop()` / `dispose()`

The v3 `Lifecycle` (mixed into `ctx` as `ctx.start/stop` and aliased `ctx.events`/`ctx.lifecycle`):

- `ctx.start()` — sets `isActive = true`; drains the `ready` hook queue: each `ready` listener is run via `scope.ensure(...)` (async, error-tracked); then `flush()` awaits all pending scope tasks. (Koishi: `await app.start()` after the loader has `app.plugin(...)`-ed the whole plugin tree — `ready` fires after every plugin has been applied.)
- `ctx.stop()` — sets `isActive = false`; `ctx.scope.reset()` disposes the **root scope's** disposables (the root's own effects). Plugin forks are disposed through their own runtimes; in Koishi the loader/stop path tears down the plugin tree.
- `ready` event: fired at start; **if a plugin registers a `ready` listener while the app is already active, it runs immediately** (that's the `internal/listener` special case: `if (name === 'ready') { if (!this.lifecycle.isActive) return; this.scope.ensure(async () => listener()); return () => false }`).
- `dispose` event: fired on plugin unload / app stop — the manual escape hatch for side effects Cordis cannot see (see §10).

### 5.2 Plugin lifecycle (v3 scope machine)

Each plugin = one `MainScope` (runtime, outer) + one `ForkScope` per `plugin()` call (inner):

- `ForkScope` constructor: registers itself on the runtime, emits `internal/fork`, then `init(error)`: if config is falsy (validation failed) → `cancel(error)` (FAILED); else `start()`.
- `start()`: no-op unless `ready` (all required services truthy) and not already active and not disposed; then sets `isActive`, and (non-reusable or the runtime) runs `apply` via `ensure()` (LOADING while the task runs; `_getStatus()` computes `LOADING` when `tasks.size > 0`).
- `reset()`: disposes all disposables (except `Context.static`-marked ones, which survive — used for built-in framework listeners), async, errors routed to `internal/error`.
- `restart()`: `reset(); error=null; hasError=false; status=PENDING; start()` — used by config hot-update.
- `update(config, forced?)`: re-resolves config (schema), computes `checkUpdate` against the current config and the plugin's registered **acceptors** (`ctx.accept(keys?, cb, {passive, immediate})` / `ctx.decline(keys)`), emits `internal/before-update` / `internal/update`, and restarts only if needed. This is the mechanism behind hot-reloading config edits without a process restart.
- `dispose()` (fork): `uid = null`, `reset()`, emit `internal/fork`, unlink from runtime; when the last fork goes, `parent.registry.delete(plugin)`.

### 5.3 v4 fiber machine

v4 replaces scope with `Fiber` and states `PENDING, LOADING, ACTIVE, FAILED, DISPOSED, UNLOADING`:

- The **root** fiber is created in the `Context` constructor: `uid = 0`, immediately `ACTIVE`, `dispose = () => this.restart()` — i.e. there is **no `start()`/`stop()`**; the app is "started" by construction and plugins begin as soon as their injections are satisfiable.
- Each plugin gets a fiber owned by the parent fiber's effect list (`parent.fiber.effect(() => {...})`); the disposer clears `uid`, emits `internal/plugin`, removes from the runtime, and **awaits `inertia`** (in-flight reload/unload work).
- `_refresh()` computes an **epoch string** from the uids of the fiber's injected services; any service change → `_setEpoch` → `_unload()` (dispose all effects) or `_reload()` (re-run the plugin body) with `inertia` chaining; `fiber.await()` waits until inertia settles and **rethrows the stored error** if the plugin failed.
- `fiber.update(config, noSave)` revalidates config and runs the `internal/update` **waterfall** (vetoable), then `restart()`.
- Effects: `ctx.effect(execute, label)` — `execute` may return a disposer, a promise of one, an (async) iterable of disposers, or an async disposer (`then`-able); errors in disposers are logged via `ctx.logger.error`, never thrown (the "effect" contract: dispose must not crash the unload loop). Effects are LIFO-disposed in reverse registration order.

### 5.4 Restart / state recovery

- v3: `stop()` disposes root effects; a subsequent `start()` re-fires `ready` for plugins that register it while active (immediately), and the loader re-`plugin()`s the tree from config — services are re-provided by their plugins as they load, and dependent plugins (via `inject`) reload automatically when their deps reappear.
- v4: "restart" is the fundamental operation: `fiber.restart()` = epoch `INACTIVE` → `_unload` → `_refresh` → `_reload`; the root fiber's `dispose()` literally restarts the whole app. **In-memory state is not preserved** across a plugin reload — that is the *designed* contract: a plugin's mutable state lives in its body closure and is recreated on reload; anything that must survive lives in the database or the config file (see §6).
- Koishi's `await app.start()`/`app.stop()` are the user-facing app lifecycle; the loader owns the plugin tree and its persistence.

---

## 6. State & recoverability

**Premise correction:** there is **no built-in "state service" (`ctx.state` as a persisted key-value store) in Cordis or Koishi.**

- In Cordis v3, `ctx.state` is a **deprecated getter alias for `ctx.scope`** (the plugin's EffectScope).
- In older Koishi docs, `ctx.state` was the per-plugin **info object**: `{ id, parent, config, using, schema, plugin, children, disposables }` — the plugin's runtime bookkeeping, in-memory only.
- Koishi *does* have `ctx.set`/`ctx.get`, but those are the **in-memory service registry**, not persistence.

Persistence in the Koishi stack is achieved by two real mechanisms:

### 6.1 The database (Minato) — the sanctioned persistence layer

Koishi embeds Minato (`minato` 3.7): `ctx.plugin(minato.Database)` provides the `database` service, and koishi itself calls `ctx.model.extend('user', {...}, {autoInc: true})` etc. API surface:

- **Model definition**: `ctx.model.extend('schedule', { id: 'unsigned(8)', time: 'timestamp', assignee: 'string(255)' }, { primary: 'id', autoInc: true, unique: [...], indexes: [...], foreign: {...} })`. Fields are strings (`'string(255)'`), shorthand types, or objects (`{ type: 'string', length: 255, initial: 100, nullable, deprecated, expr }`) with `initial` defaults; relations (`oneToOne/oneToMany/manyToOne/manyToMany`) supported.
- **CRUD**: `database.get(table, query, fields?)`, `create`, `set(table, query, update)`, `upsert(table, rows, key?)`, `remove` — with **query expressions** (`{ id: { $gt: 2, $lte: 5 } }`, `$or`, `$and`, `$in`, …) and **eval expressions** (`$.add(row.count, 1)`, `$concat`, field refs `$: 'field'`). `set/upsert/remove` return `{ matched, inserted }`.
- **Selections**: `database.select('user').where(...).project(...).orderBy(...).limit(...)` returns a lazy `Selection` (`.execute()`, `.page()`, `.count()`, streaming via `.execute()` generator) — SQL-like power over any driver.
- **Observed objects**: `session.observeUser(fields)` returns a proxy-wrapped row; mutations are tracked and flushed with `user.$update()` at the end of the middleware chain — the "change tracking + auto-commit" pattern koishi uses for `session.user`/`session.channel`.
- **Drivers** (memory, sqlite, mysql, postgres, mongo) implement a `Driver` interface; minato handles type conversion (`Type`), query compilation, and migrations (`Model.migrations` callbacks).

### 6.2 Config-file persistence (loader)

Plugin config is stored in the config file; runtime edits (web console or `ctx.fiber.update(config)`) flow through `internal/update` → the loader **simplifies** the config via the plugin schema (`Config.simplify` — removes fields equal to defaults) and **writes it back** to disk, then restarts the plugin. So "configuration state" survives restarts by construction.

### 6.3 Recoverability patterns

- **Per-plugin in-memory state** lives in the plugin body's closure; it is *recreated* on every load — hot reload is designed around this (all effects must be re-derivable from `(ctx, config)`).
- **Shared state across reusable instances** uses the `fork` event pattern (count example in §4.4).
- **Cross-restart state** belongs in the database (a plugin can `ctx.model.extend('my_table', ...)` for its own tables) or in config.
- **Service availability recovery** is automatic: a plugin whose `inject` deps are missing sits PENDING and starts the moment the services appear; when a dep disappears it is rolled back, and re-appears when the dep returns.

For a Python port, the natural "state service" is a thin table-backed KV (`ctx.database` + a `key`/`value` table with JSON values) or a dedicated `StateService` plugin — nothing like that exists in the JS framework, so the Python design is free to add it without contradicting the original.

---

## 7. Configuration & schema (schemastery)

### 7.1 Declaring schemas

```ts
import { Schema } from 'koishi'   // re-export of schemastery

export interface Config { foo: string; bar?: number }
export const schema = Schema.object({
  foo: Schema.string().required(),
  bar: Schema.number().default(1),
})
export function apply(ctx: Context, config: Config) { ... }
```

Schema values are **fluent, immutable builders**: every modifier returns a new Schema (`Schema(this)` copies). Modifiers: `required()`, `default(v)`, `hidden()`, `loose()`, `role(text, extra)` (UI role), `link()`, `comment()`, `description()`, `disabled()`, `collapse()`, `deprecated()`, `experimental()`, `pattern(re)` (string), `max/min/step` (number/string length), `set(key, schema)` / `push(schema)` (dict/list), `i18n(messages)`.

Type constructors: `any`, `never`, `const(v)`, `string`, `number`, `natural` (≥0 integer), `percent`, `boolean`, `date`, `regExp(flags?)`, `arrayBuffer(encoding?)`, `bitset(bitmap)`, `function`, `is(ctor)`, `array(inner)`, `dict(inner, sKey?)`, `tuple([...])`, `object({...})`, `union([...])`, `intersect([...])`, `transform(inner, cb, preserve?)`, `lazy(() => schema)`. `Schema.from(value)` infers a schema from a primitive/constructor/schema. Each schema carries UI `meta` (default, required, role, description, badges…) consumed by the web console to render forms.

### 7.2 Validation + defaults semantics (`Schema.resolve`)

- **Nullish input**: if `required` → throw `ValidationError('missing required value')`; else fill with `clone(default)` (deep-cloned so defaults are never shared/mutated), descending through `intersect` to find the first non-null default.
- **Type checks** per constructor with precise messages (`expected string but got 5`); `string` also checks `pattern` and length min/max; `number` checks min/max/step (with float-safe `isMultipleOf`); `array/dict/object/tuple` recurse and report the **path** in the error (`$.a.b[0]`), and `object`/`dict` **merge unknown keys** unless `strict` (unknown keys are kept by default; `strict` drops them). `union` tries branches in order, collecting errors. `intersect` merges objects.
- **`transform`** runs a converter after inner validation: `Schema.transform(Schema.string(), (value, options) => new Date(value), true)` — `preserve: true` means the *output* type differs from the input (`date`, `regExp`, `arrayBuffer` are built this way).
- **`loose`**: on validation failure, fall back to the default instead of throwing. **`autofix` option**: drop invalid keys.
- Error type: `Schema.ValidationError` (TypeError subclass) with `path`; schemastery also exposes a Standard Schema v1 adapter (`schema['~standard'].validate(value)` → `{value}` or `{issues}`) which is exactly what **cordis v4** uses (`resolveConfig` in `fiber.ts` calls `runtime.Config['~standard'].validate`, wrapping issues into a `ValidationError`; async validation is rejected with a clear error).

### 7.3 Plugin config wiring

- `resolveConfig(plugin, config)` (v3 utils): `schema = plugin.Config || plugin.schema`; `if (schema && plugin.schema !== false) config = schema(config)`; returns `config ?? {}`. Applied in `registry.plugin()` **before** creating the fork; validation failure → `internal/error` + FAILED fork (never crashes the app).
- `plugin.Config` may also be a **transform** function (a schema *producer*), and koishi's `Context.Config` static is a `Schema.intersect([...])` whose parts are attached via `defineProperty(Context.Config, 'Basic', Schema.object({...}))` and `Context.Config.list.push(...)` — the "extensible config" pattern (the `SchemaService` in koishi, `ctx.schema.extend(name, schema, order?)`, lets plugins contribute to shared config schemas).
- v4 intercept config: services declare `declare [Service.config]: T` (phantom type) and read merged per-scope config via `Service[resolveConfig](base, head)`, merging intercept chain entries (`ctx.intercept(name, config)`) with `Config.merge` or shallow assign.

---

## 8. Middleware / filters / interception chains

### 8.1 Cordis core: no middleware primitive

Cordis itself has no "middleware" concept — the primitives are **events with modes** (`bail`/`serial`/`waterfall`) and **context filters**. The middleware-style *next()* composition exists in v4's `waterfall` (each listener receives `next`; not calling it vetoes) and in the internal events (`internal/get`, `internal/set`, `internal/update`, `internal/config` all take a trailing `next`). This is the pattern a Python port should generalize.

### 8.2 Koishi middleware (`ctx.middleware`)

Koishi's `Processor` (service `$processor`) implements a **koa/Express-style** middleware stack for messages:

- `ctx.middleware((session, next) => Awaitable<void | Fragment>, prepend?)` registers into `$processor._hooks` (a plain hook list, same registration/disposal machinery as events). Returns a disposer.
- On the `message` event: `filterHooks(this._hooks, session)` selects only middlewares whose context matches the session, binds them to the session, and runs them as a queue:
  - `next()` advances to the next middleware; `next(callback)` **pushes a temporary middleware onto the current queue** (used by `session.prompt()`).
  - Not calling `next()` **stops the chain** (a middleware that handles the message fully just returns).
  - `Next.MAX_DEPTH = 64` guard against runaway stacks.
  - Errors: `SessionError` → rendered as an i18n message; any other error → logged via `ctx.logger('session').warn(...)` and the chain continues to the finally block.
  - The built-in `attach` middleware is registered first (`this.middleware(this.attach.bind(this), true)`): database attachment (`before-attach-channel`/`attach-channel`/`before-attach-user`/`attach-user` events with field collection), shortcut matchers (`ctx.match`), then `next()`.
  - After the chain: `middleware` event, then `session.user?.$update()` / `session.channel?.$update()` flush observed DB rows.
- The chain's return value (a Fragment) is sent as the reply (`if (result) await session.send(result)`).

### 8.3 Filters

- `ctx.filter(session)` — the context's predicate (selector-derived); `Session.prototype[Context.filter]` bridges it into event dispatch.
- `FilterService` (`ctx.$filter`) builds derived contexts: `any()`, `never()`, `union(other)`, `intersect(other)`, `exclude(other)`, `user(...)`, `self(...)`, `guild(...)`, `channel(...)`, `platform(...)`, `private()` — each `ctx.extend({filter: ...})`.
- Plugin contexts get filters too: koishi wires `internal/runtime` so a plugin's ctx.filter matches if **any** of its forks' contexts match (`runtime.children.some(p => p.ctx.filter(session))`).
- `ctx.before('xxx', cb, append?)` — the `before-` event sugar with inverted default ordering (see §3.2).

---

## 9. Concurrency & error handling

### 9.1 Async emission

| Mode | Concurrency | Ordering | Error behavior |
|---|---|---|---|
| `emit` | none (sync) | registration order | exceptions propagate to the caller synchronously; returned promises unhandled (fire-and-forget) |
| `parallel` | all at once | start order = registration order | v3: `Promise.all` → **first rejection** rejects; v4: `Promise.allSettled` → throws **`AggregateError`** with *all* rejected reasons |
| `serial` | one at a time | strict order | stops at first bail value; a rejection propagates (unwinding the loop) |
| `bail` | none (sync) | strict order | stops at first bail value; exceptions propagate |
| `chain`/`waterfall` | sequential | strict order | value threading; rejections propagate |

### 9.2 Error channels

- **`internal/error` event** — the universal error sink. Cordis v3 registers default listeners for `internal/info|error|warning` that `console.info(...)` only **when no other listeners exist** (`if (this._hooks['internal/error'].length > 1) return`) — i.e. the framework logs errors by default, and any plugin that registers its own `internal/error` listener takes over. Koishi replaces the console with its `Logger` service (`ctx.logger(name)` with levels error/warn/info/debug, colored exporters, per-namespace filtering) and adapters report failures via `internal/error`.
- **Scope/fiber failures**: any task run through `scope.ensure()` that rejects → `internal/error` + `scope.cancel(reason)` → FAILED + full effect rollback (§4.3). Plugin startup errors therefore never crash the process.
- **Effect disposal errors**: in v4, disposer failures are caught and logged (`ctx.logger.error`), never thrown out of the unload path (the unload loop must always complete).
- **Middleware errors**: caught per-message, logged, session continues (or SessionError → user-facing text).
- **Process-level**: koishi's worker registers `process.on('uncaughtException', e => { new Logger('app').error(e); process.exit(1) })` and `process.on('unhandledRejection', e => new Logger('app').warn(e))`.
- Note: there is **no `app.on('error')`** in the core event map; the koishi idiom is `ctx.on('internal/error')` / logger, or the process handlers above.

### 9.3 Cancellation semantics

There is no true cancellation/abort of a running plugin body in v3 — `reset()` disposes *effects* but an in-flight async `apply` keeps running to completion (its later effect registrations are ignored because the scope is inactive/`uid === null`; `assertActive()` throws `INACTIVE_EFFECT` for new registrations). v4 tracks `inertia` and *awaits* in-flight unload/reload chains so dispose doesn't race the plugin body. This is an important semantic to replicate in Python (asyncio tasks + cancellation discipline).

---

## 10. Dispose & resource cleanup

### 10.1 The disposer model

Everything a plugin registers returns (or internally registers) a **disposer** — a plain callable that undoes the registration:

- `ctx.on/once` → disposer (unregister listener)
- `ctx.middleware` → disposer; `ctx.command` → disposer (unregister command); `ctx.plugin` → `ForkScope`/`Fiber` with `.dispose()` (unload subtree)
- `ctx.set` → disposer (reset service to `undefined`)
- `ctx.effect(callback)` / `ctx.collect(label, callback)` → disposer (v3); `ctx.fiber.effect(execute, label)` (v4)
- `ctx.on('dispose', cb)` — manual cleanup for side effects Cordis can't track (`server.close()`)

### 10.2 How cleanup runs

- **v3**: all disposers live in the scope's `disposables` array. `reset()`/`dispose()` takes them out (`splice(0)`) and calls each **asynchronously** (`(async () => dispose())()`), routing rejections to `internal/error`; order is registration order in v3 (v4 disposes **LIFO**: `disposables.splice(0).reverse()`). `Context.static`-marked disposables (built-in framework listeners) survive scope resets.
- **v4**: `Fiber.effect()` returns an idempotent, epoch-guarded disposer; nested effects are tracked in an `EffectMeta` tree (for diagnostics: `fiber.getEffects()`); `_unload()` runs `Promise.all` over the disposers *with composed stack traces* so errors point at the registering plugin.
- **`Symbol.dispose` / `Symbol.asyncDispose` are NOT used** by Cordis (verified by grep) — disposers are plain functions (optionally then-able in v4). A Python port can freely use context managers/`try/finally`/`ExitStack`, which map naturally.

### 10.3 Cleanup guarantees

- Plugin unload is **recursive**: disposing a plugin disposes its event listeners, middlewares, commands, *and child plugins* (each child is an effect of the parent scope).
- Service unregistration is automatic: services provided by a plugin vanish when it unloads (`provide` returns a disposer deleting the store entry; dependent plugins roll back via `internal/before-service`).
- Cleanup is **best-effort and isolated**: a throwing disposer is logged, not propagated (the rest still run).
- The `dispose` **event** fires before/around the disposer sweep (`this.scope.disposables` handling in the v3 `internal/listener` special case), giving plugins a last chance to release resources (close servers, flush buffers).

---

## Python port mapping

The following table proposes Python idioms for each Cordis concept. The goal is *equivalent capabilities*, not API identity.

| Cordis concept | Python equivalent | Notes |
|---|---|---|
| `Context` (proxy + derived contexts) | `Context` class; derived contexts via `__getattr__` delegation to a parent + copy-on-write dicts (`filter`, `isolate`, `intercept`), or simply `dataclass` fields with `parent` refs | No Proxy needed: Python's `__getattr__`/`__setattr__` already intercept property access; `__getattr__` resolves services, `__setattr__` routes to the registry (mirroring `ReflectService.handler`) |
| Service registry (`ctx.set/get`, store + isolate Symbols) | `dict[name, dict[scope_key, value]]`; `set(name, value)` returns a disposer; `get(name)` returns value; `scope_key` = tuple of isolate tokens (Symbols → `object()` or strings) | Override rule ("one provider per scope") maps directly; emit `before/after service change` events to wake waiting plugins |
| Callable services (`ctx.logger(...)`, `ctx.http(...)`) | `Service` subclasses implementing `__call__`; registry stores the instance; `ctx.foo(...)` = `ctx.foo.__call__(...)` | Python callables are first-class; no prototype merging needed |
| Tracing / `ctx.caller` | `contextvars.ContextVar` holding the current *calling* context; service methods read it at entry and **snapshot it before any `await`** | Exactly replicates the documented async caveat; `contextvars` is the right tool |
| Selectors & filters | Derived contexts with `filter: Callable[[Session], bool]`; `__and__/__or__/__sub__` operators for intersect/union/exclude; event dispatch filters listeners by `this_arg.filter(hook.ctx)` | No `ctx.select` in the original — don't invent one; name it `isolate`/filters |
| `inject` / `using` (required/optional deps) | Plugin attribute `inject: list[str] | {"required": [...], "optional": [...]}`; the plugin **coroutine** is only awaited when all required deps are truthy; service change → cancel task + re-await | This is "async dependency gating"; implement with an `asyncio.Condition`/event per service name |
| Event bus (`on/once`, modes) | `EventBus` with `name -> list[Hook(ctx, callback, prepend, global)]`; `emit` (sync loop), `parallel` (`asyncio.gather(..., return_exceptions=True)` + `AggregateError` like v4), `bail`/`serial` (ordered, `is_bailed` = value not in (None, False)), `waterfall` (next-composition) | `prepend` = insert(0); `once` wraps; registration is a scope effect (see below); expose an `internal/dispatch`-style hook for interception |
| `internal/event` / `internal/dispatch` interception | Reserved event names (`"internal/*"`); the bus emits `internal/dispatch(mode, name, args, this_arg)` before user events | Replicates "wildcard"-style interception without wildcards |
| `before-` event sugar | `ctx.before(name, cb, append=False)` → `on("before-" + name, cb, prepend=not append)` | |
| Plugin shapes | `apply(ctx, config)` coroutine; object plugin `Plugin(apply=..., name=..., schema=..., inject=...)`; class plugin = class with `__init__(ctx, config)` | In Python there's no function/class duality issue; normalize to one shape (async callable) |
| Plugin lifecycle state machine | `enum ScopeStatus: PENDING/LOADING/ACTIVE/FAILED/DISPOSED`; plugin = `Task`-wrapped coroutine; FAILED = exception captured, effects rolled back, app continues | Must implement "partial effects rolled back on throw": use a per-plugin `ExitStack` (sync) + `AsyncExitStack` (async resources), entered before running the body |
| Effects / disposers | `ctx.effect(cb)` returns disposer; per-plugin `AsyncExitStack`; unload = LIFO `aclose()`; disposer errors logged, never thrown | `contextlib.AsyncExitStack` is the idiomatic equivalent of the fiber disposables list; `@asynccontextmanager` for `ctx.on('dispose')`-style blocks |
| Registry (`ctx.registry`) | `Registry`: `dict[callback, Runtime(fibers=[...], name, config_schema)]`; `plugin()` dedups non-reusable plugins; `delete()` disposes all fibers; `counter` for uids | Reusable plugins = multiple fibers per runtime; `fork` event on each new fiber |
| Config schema (schemastery) | Either pydantic models (validation + defaults + nested) or a fluent `Schema` builder (`s.object({"foo": s.string().required()})`); `ValidationError` with dotted path; `default` deep-copied; `union` tries branches; `transform` = validator+converter; `loose` fallback | Pydantic v2 gives required/default/nested/union for free; a fluent wrapper keeps the declarative "schema as data" style and eases config-file round-tripping (`simplify`) |
| `start()`/`stop()` (app) vs plugin lifecycle | `async app.start()` → run root `ready` hooks + drain tasks; `async app.stop()` → dispose root effects; plugin lifecycle purely dependency-driven (like v4) | Decide one model: v4-style "everything starts on demand" is simpler in asyncio |
| `ready` / `dispose` / `fork` events | Plain events on the bus; `ready` runs immediately if already started; `dispose` fires during scope teardown; `fork` fires per reusable instance | |
| Middleware chain (`next`) | Koishi-style: `async def middleware(session, next)`; queue with `next(cb)` pushing temporary middlewares, depth guard; or v4-style waterfall with trailing `next` param | asyncio makes this trivial; use `await next()` |
| Concurrency & error handling | `parallel` → `gather(return_exceptions=True)` + `AggregateError` (match v4); serial → sequential awaits with bail; `internal/error` event + `Logger`; per-plugin FAILED rollback; `unhandledRejection`-equivalent: `loop.set_exception_handler` | Expose an error channel (e.g. `ctx.logger.error` / `ctx.on("internal/error")`) so failures are observable but non-fatal |
| Persistence (Minato) | Async ORM layer: `Model` registry (`model.extend(table, fields, {primary, autoInc})`), query/eval expression DSL (`$gt`, `$or`, `$.add`), selection chain, drivers behind a `Driver` protocol; observed rows with deferred flush | Python: sqlite3/aiosqlite + `dataclasses` + an expression interpreter; or map to SQLAlchemy Core; keep the "type-driven, driver-independent" core |
| `ctx.state` / "recoverable state" | **Do not replicate as-is** (it's a deprecated alias in JS). Add a first-class `StateService`: table-backed KV (`ctx.database` table `state(key TEXT PRIMARY KEY, value JSON)`) with `get/set/delete/update`, plus per-plugin in-memory `ctx.store` dict scoped to the plugin fiber | This is the one place a Python port can improve on the original; make it optional (a plugin), not core |
| Plugin loading from config | Loader service: parse a YAML/TOML config into a plugin tree (`$`-prefixed keys → derived contexts/filters); hot-reload via `internal/update` → revalidate config → restart fiber → write back simplified config | Python: `importlib` + `importlib.util.spec_from_file_location` for dynamic module loading |

### Design implications for the Python implementation (summary)

1. **One async event loop, one `Context` tree**: root `Context` on `asyncio`; derived contexts are cheap (delegation + copy-on-write dicts).
2. **Scope/fiber is the backbone**: every plugin, service, and listener is owned by a scope; teardown = `AsyncExitStack.aclose()` (LIFO, error-isolated). Implement `effect()`/disposers first — everything else (events, services, plugins) is built on it.
3. **Dependency gating is async**: plugin bodies are coroutines awaited when their `inject` set is satisfied; service changes cancel+re-await (v4 semantics).
4. **Dispatch modes map directly**: `emit`/`bail` sync; `parallel`/`serial`/`waterfall` async; `AggregateError` aggregation for parallel (v4 behavior is the better spec).
5. **Interception without wildcards**: reserve `internal/*` events (`dispatch`, `listener`, `get`, `set`, `update`) with `next`-style composition for extensibility.
6. **Errors are events, not exceptions**: an `internal/error` channel + logger; plugin failure → FAILED + rollback; process stays alive.
7. **Persistence is opt-in**: model registry + DB service + (optionally) a KV `state` service; in-memory plugin state is intentionally non-persistent (recreated on load) — document this contract clearly.

*All statements above were verified against the referenced sources; file paths for the key sources are listed at the top of this document. The analysis reflects cordis 3.18.1 (Koishi's runtime dependency), cordis 4.0.0-rc.8 master, schemastery 3.18.x, minato 3.7, and koishi 4.18.x.*
