"""Plugin system: plugin normalization, registry, fiber lifecycle.

Implements the design doc §6 (three plugin shapes, Registry keyed by callback
identity, Fiber state machine with per-fiber serialized transitions) and §3.4
(effect-based reversible registration).

Layering (design §2): ``plugin.py`` never imports ``context.py``.  Fibers reach
the service store / event bus / registry through duck-typed ``ctx`` accessors
(``ctx._services``, ``ctx._bus``, ``ctx.root``), which keeps the import graph
acyclic.
"""

from __future__ import annotations

import asyncio
import contextvars
import enum
import inspect
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from xcore.errors import InactiveEffectError
from xcore.schema import validate_config

logger = logging.getLogger("xcore.plugin")

_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")

#: The fiber whose ``apply`` callback is currently executing.  Set around the
#: plugin callback in :meth:`Fiber._load` so capability services can bind
#: fiber-scoped cleanup (registrations are undone on unload) without any
#: loader-side context tracking (Cordis tracks the same "current ctx").
_current_fiber: contextvars.ContextVar["Fiber | None"] = contextvars.ContextVar(
    "xcore.current_fiber", default=None
)


def current_fiber() -> "Fiber | None":
    """Return the fiber whose ``apply`` is currently executing (or ``None``).

    Outside a plugin ``apply`` (e.g. inside event listeners or app code) this
    returns ``None``: registrations made there are owned by the caller, which
    must undo them explicitly (typically through ``ctx.dispose``).
    """
    return _current_fiber.get()


def bound_effect(disposer: Callable[[], Any]) -> bool:
    """Bind ``disposer`` to the currently applying fiber's unload.

    Registration-time helper for capability services: during a plugin's
    ``apply``, the returned disposer runs when that plugin's fiber unloads.
    Outside ``apply`` (or when the fiber is already inactive) this is a
    no-op returning ``False`` — the caller owns the cleanup.
    """
    fiber = _current_fiber.get()
    if fiber is None:
        return False
    try:
        fiber.effect(lambda: disposer)
        return True
    except InactiveEffectError:
        return False


def current_plugin_name() -> str:
    """Name of the plugin whose ``apply`` is executing (``"unknown"`` outside)."""
    fiber = _current_fiber.get()
    runtime = getattr(fiber, "runtime", None)
    if runtime is not None:
        return runtime.definition.name
    return "unknown"


class FiberState(enum.Enum):
    """Lifecycle states of one plugin fiber (design §6.2)."""

    PENDING = "pending"
    LOADING = "loading"
    RUNNING = "running"
    FAILED = "failed"
    UNLOADING = "unloading"
    DISPOSED = "disposed"


# ---------------------------------------------------------------------------
# Effect machinery (shared by fibers and the root fiber)
# ---------------------------------------------------------------------------


async def _await_all(results: list[Any]) -> None:
    for result in results:
        try:
            await result
        except BaseException as exc:  # noqa: BLE001 - cleanup must not raise
            logger.error("async disposer failed: %s", exc, exc_info=exc)


class EffectOwner:
    """Disposer/effect machinery shared by :class:`Fiber` and the root fiber."""

    def __init__(self) -> None:
        self._disposers: list[Callable[[], Any]] = []

    def assert_active(self) -> None:
        raise NotImplementedError

    def _add_disposer(self, disposer: Callable[[], Any]) -> None:
        self._disposers.append(disposer)

    def effect(self, execute: Callable[[], Any], *, label: str = "anonymous") -> Callable[[], bool]:
        """Run ``execute`` now; its returned disposer is undone on unload.

        The returned disposer is single-shot: calling it runs the collected
        disposers (reverse order) and removes them from the fiber, so they
        will not run again on unload.  Sync disposers run immediately; async
        disposers are awaited during unload and scheduled on direct calls.
        """
        self.assert_active()
        collected: list[Callable[[], Any]] = []
        result = execute()
        if inspect.isawaitable(result):
            raise TypeError(
                "async effect bodies are not supported (design §14.10); "
                "return a disposer from a synchronous body"
            )
        if callable(result):
            collected.append(result)
        elif result is not None:
            raise TypeError("invalid effect result: expected a disposer or None")
        self._disposers.extend(collected)

        done = False

        def disposer() -> bool:
            nonlocal done
            if done:
                return False
            done = True
            for disposer_fn in reversed(collected):
                if disposer_fn in self._disposers:
                    self._disposers.remove(disposer_fn)
            pending = []
            for disposer_fn in reversed(collected):
                try:
                    result = disposer_fn()
                except BaseException as exc:  # noqa: BLE001
                    logger.error("disposer failed: %s", exc, exc_info=exc)
                    continue
                if inspect.isawaitable(result):
                    pending.append(result)
            if pending:
                asyncio.ensure_future(_await_all(pending))
            return True

        return disposer

    async def _run_disposers(
        self, disposers: list[Callable[[], Any]]
    ) -> list[BaseException]:
        """Run disposers in reverse registration order; never raises."""
        errors: list[BaseException] = []
        for disposer in reversed(disposers):
            try:
                result = disposer()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:  # noqa: BLE001 - cleanup must not raise
                errors.append(exc)
                logger.error("disposer failed: %s", exc, exc_info=exc)
        return errors


class RootFiber(EffectOwner):
    """The root context's fiber: always running, owns root-level effects."""

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ctx = ctx
        self.uid = 0
        self.name = "root"
        self.state = FiberState.RUNNING

    @property
    def is_running(self) -> bool:
        return True

    def assert_active(self) -> None:
        if getattr(self.ctx, "_destroyed", False):
            raise InactiveEffectError("cannot create effect on a destroyed root context")


# ---------------------------------------------------------------------------
# Plugin normalization
# ---------------------------------------------------------------------------


@dataclass
class PluginDef:
    """Normalized plugin definition (design §6.1)."""

    name: str
    key: Any  # registry identity: function / class / object instance
    callback: Callable[..., Any]  # function / class / bound apply
    config_schema: Any  # S schema | plain dict | None
    inject: dict[str, bool]  # service name -> required
    provided: list[str] = field(default_factory=list)


def _resolve_inject(inject: Any) -> dict[str, bool]:
    """Normalize an inject declaration into ``{name: required}`` (copied).

    Accepts ``["a", "b"]`` (all required), koishi's
    ``{"required": [...], "optional": [...]}``, or a ``{name: config}`` map
    (values ignored in v1; intercept merging is a future item, §14.8).
    """
    result: dict[str, bool] = {}
    if inject is None:
        return result
    if isinstance(inject, (list, tuple)):
        for name in inject:
            result[str(name)] = True
        return result
    if isinstance(inject, dict):
        if "required" in inject or "optional" in inject:
            for name in inject.get("required") or []:
                result[str(name)] = True
            for name in inject.get("optional") or []:
                result.setdefault(str(name), False)
        else:
            for name in inject:
                result[str(name)] = True
        return result
    raise TypeError("inject must be a list or a dict")


def _snake_case(name: str) -> str:
    return _SNAKE.sub("_", name).lower()


def _as_name_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(name) for name in value]
    raise TypeError("provide must be a string or a list of strings")


def resolve_plugin(plugin: Any) -> PluginDef | None:
    """Normalize a function / class / ``{apply}`` object into a PluginDef.

    Returns ``None`` for unsupported shapes.  The ``key`` is the function
    object, the class, or the object instance -- *not* a bound method, because
    bound methods are recreated on every attribute access and cannot serve as
    stable registry keys.
    """
    if inspect.isfunction(plugin):
        name = getattr(plugin, "name", None) or plugin.__name__
        return PluginDef(
            name=name,
            key=plugin,
            callback=plugin,
            config_schema=getattr(plugin, "Config", None),
            inject=_resolve_inject(getattr(plugin, "inject", None)),
            provided=_as_name_list(getattr(plugin, "provide", None)),
        )
    if inspect.isclass(plugin):
        name = getattr(plugin, "name", None) or _snake_case(plugin.__name__)
        return PluginDef(
            name=name,
            key=plugin,
            callback=plugin,
            config_schema=getattr(plugin, "Config", None),
            inject=_resolve_inject(getattr(plugin, "inject", None)),
            provided=_as_name_list(getattr(plugin, "provide", None)),
        )
    apply = getattr(plugin, "apply", None)
    if callable(apply):
        name = getattr(plugin, "name", None) or _snake_case(type(plugin).__name__)
        return PluginDef(
            name=name,
            key=plugin,
            callback=apply,
            config_schema=getattr(plugin, "Config", None),
            inject=_resolve_inject(getattr(plugin, "inject", None)),
            provided=_as_name_list(getattr(plugin, "provide", None)),
        )
    return None


def _key_of(plugin: Any) -> Any:
    definition = resolve_plugin(plugin)
    return definition.key if definition is not None else None


def _child_fiber_disposer(fiber: "Fiber") -> Callable[[], Any]:
    def disposer() -> Any:
        return fiber.settle_to(_TARGET_DISPOSED)

    return disposer


class _InjectPlugin:
    """Adapter turning ``ctx.inject(deps, callback)`` into an object plugin."""

    def __init__(self, deps: Any, callback: Callable[..., Any]) -> None:
        self.inject = deps
        self._callback = callback
        self.name = getattr(callback, "__name__", "inject") or "inject"

    def apply(self, ctx: Any, config: Any) -> Any:
        return self._callback(ctx)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class _Runtime:
    """Shared per-plugin record; one per registry key (design §6.2)."""

    definition: PluginDef
    fibers: list["Fiber"] = field(default_factory=list)


class Registry:
    """Plugin registry (``ctx.registry``): map-like over plugin runtimes."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._runtimes: dict[Any, _Runtime] = {}
        self._counter = 0
        self._load_counter = 0

    # -- plugin mounting ----------------------------------------------------

    def plugin(
        self, plugin: Any, config: Any = None, parent_ctx: Any = None
    ) -> "PluginHandle":
        """Mount a plugin; returns a handle (awaitable, disposable)."""
        definition = resolve_plugin(plugin)
        if definition is None:
            raise TypeError(
                "invalid plugin, expect function, class, or object with an "
                f"'apply' method, received {type(plugin).__name__}"
            )
        parent = parent_ctx if parent_ctx is not None else self.ctx
        parent.fiber.assert_active()
        runtime = self._runtimes.get(definition.key)
        if runtime is None:
            runtime = _Runtime(definition=definition)
            self._runtimes[definition.key] = runtime
        self._counter += 1
        fiber = Fiber(
            parent=parent,
            definition=definition,
            raw_config=config,
            runtime=runtime,
            uid=self._counter,
        )
        runtime.fibers.append(fiber)
        # Child fiber cleanup is an effect of the parent fiber: unloading the
        # parent recursively unloads plugins it mounted (Cordis §10.3).
        parent.fiber._add_disposer(_child_fiber_disposer(fiber))
        if parent.root.is_active:
            asyncio.ensure_future(fiber.settle_to(_TARGET_CONVERGE))
        return PluginHandle(fiber)

    def inject(
        self, deps: Any, callback: Callable[..., Any], parent_ctx: Any = None
    ) -> "PluginHandle":
        """Shorthand: mount ``{inject: deps, apply: callback}`` (Cordis parity)."""
        return self.plugin(_InjectPlugin(deps, callback), None, parent_ctx)

    # -- inspection ---------------------------------------------------------

    def get(self, plugin: Any) -> _Runtime | None:
        key = _key_of(plugin)
        return self._runtimes.get(key) if key is not None else None

    def has(self, plugin: Any) -> bool:
        key = _key_of(plugin)
        return key is not None and key in self._runtimes

    def delete(self, plugin: Any) -> bool:
        """Remove a runtime and dispose every one of its fibers."""
        key = _key_of(plugin)
        runtime = self._runtimes.pop(key, None) if key is not None else None
        if runtime is None:
            return False
        for fiber in list(runtime.fibers):
            asyncio.ensure_future(fiber.settle_to(_TARGET_DISPOSED))
        return True

    def keys(self):
        return self._runtimes.keys()

    def values(self):
        return self._runtimes.values()

    def entries(self):
        return self._runtimes.items()

    def forEach(self, callback: Callable[[Any, Any], None]) -> None:
        for key, runtime in self._runtimes.items():
            callback(runtime, key)

    def __len__(self) -> int:
        return len(self._runtimes)

    # -- dependency refresh -------------------------------------------------

    def _all_fibers(self) -> list["Fiber"]:
        return [
            fiber
            for runtime in self._runtimes.values()
            for fiber in runtime.fibers
        ]

    async def _refresh_dependents(
        self, names: list[str], *, await_settle: bool = False
    ) -> None:
        """Re-evaluate every fiber that injects one of ``names``.

        Wakes pending/failed fibers whose required services appeared and
        unloads running fibers whose required services disappeared (A1 fix:
        also called from state transitions, not only set/unset).
        """
        affected = [
            fiber
            for fiber in self._all_fibers()
            if any(name in fiber.inject for name in names)
        ]
        if not affected:
            return
        tasks = [
            asyncio.ensure_future(fiber.settle_to(_TARGET_CONVERGE))
            for fiber in affected
        ]
        if not await_settle:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.error("dependent refresh failed: %s", result, exc_info=result)

    def _notify_provided(self, fiber: "Fiber") -> None:
        """Wake dependents when a fiber becomes RUNNING (A1 fix)."""
        names = self.ctx._services.names_provided_by(fiber)
        if names:
            asyncio.ensure_future(self._refresh_dependents(names))


# ---------------------------------------------------------------------------
# Fiber
# ---------------------------------------------------------------------------

_TARGET_CONVERGE = "converge"
_TARGET_PENDING = "pending"
_TARGET_DISPOSED = "disposed"


class Fiber(EffectOwner):
    """Runtime instance of one plugin application (design §6.2/§6.3).

    A fiber owns the plugin's context (``ctx``), its validated config, all
    effects registered during ``apply``, and the lifecycle transitions between
    :class:`FiberState` values.  Transitions are serialized per fiber by an
    ``asyncio.Lock``; a disposed fiber cannot restart.
    """

    def __init__(
        self,
        *,
        parent: Any,
        definition: PluginDef,
        raw_config: Any,
        runtime: _Runtime,
        uid: int,
    ) -> None:
        super().__init__()
        self.uid: int | None = uid
        self.parent = parent
        self.runtime = runtime
        self.definition = definition
        self.inject: dict[str, bool] = definition.inject
        self._raw_config = raw_config
        self.config: Any = None
        self.state = FiberState.PENDING
        self._error: BaseException | None = None
        self._instance: Any = None
        self._load_seq: int | None = None
        self._settle_lock = asyncio.Lock()
        self._settle_task: asyncio.Task | None = None
        self.ctx = parent.extend(fiber=self)

    # -- identity -----------------------------------------------------------

    @property
    def name(self) -> str:
        return self.runtime.definition.name

    @property
    def is_running(self) -> bool:
        return self.state is FiberState.RUNNING

    def assert_active(self) -> None:
        """Raise :class:`InactiveEffectError` once the fiber is disposed."""
        if self.uid is None:
            raise InactiveEffectError(
                f"cannot create effect on inactive fiber {self.name!r}"
            )

    # -- dependency evaluation ----------------------------------------------

    def _deps_satisfied(self) -> bool:
        for name, required in self.inject.items():
            if not required:
                continue
            if self.ctx.get(name, strict=True) is None:
                return False
        return True

    def _app_active(self) -> bool:
        return bool(getattr(self.ctx.root, "is_active", True))

    # -- state machine ------------------------------------------------------

    def _set_state(self, new_state: FiberState) -> None:
        old_state = self.state
        if old_state is new_state:
            return
        self.state = new_state
        self.ctx._bus._emit_sync("internal/status", self, old_state)

    def _next_action(self, target: str) -> str | None:
        if self.state is FiberState.DISPOSED:
            return None
        if target == _TARGET_CONVERGE:
            if self.state in (FiberState.PENDING, FiberState.FAILED):
                if self._deps_satisfied() and self._app_active():
                    return "load"
                return None
            if self.state is FiberState.RUNNING and not self._deps_satisfied():
                return "unload_pending"
            return None
        if target == _TARGET_PENDING:
            if self.state in (
                FiberState.RUNNING,
                FiberState.LOADING,
                FiberState.FAILED,
            ):
                return "unload_pending"
            return None
        if target == _TARGET_DISPOSED:
            if self.state in (
                FiberState.PENDING,
                FiberState.LOADING,
                FiberState.RUNNING,
                FiberState.FAILED,
                FiberState.UNLOADING,
            ):
                return "unload_disposed"
            return None
        raise ValueError(f"unknown transition target {target!r}")

    async def settle_to(self, target: str) -> None:
        """Drive the fiber to ``target``, awaiting every transition.

        Transitions are serialized by a per-fiber lock; concurrent callers
        queue behind the one holding it.  Load failures are captured into
        ``_error`` (FAILED state) and never raised here -- callers observe
        them through :meth:`await_fiber` / ``PluginHandle.await``.
        """
        async with self._settle_lock:
            self._settle_task = asyncio.current_task()
            try:
                while True:
                    action = self._next_action(target)
                    if action is None:
                        return
                    if action == "load":
                        await self._load()
                        # A load failure lands the fiber in FAILED; stop the
                        # settle pass there. Retry happens only via restart()
                        # or a later dependency change (design A6), never in
                        # an unbounded loop.
                        if self.state is FiberState.FAILED:
                            return
                    elif action == "unload_pending":
                        await self._unload(permanent=False)
                    else:
                        await self._unload(permanent=True)
            finally:
                self._settle_task = None

    async def _load(self) -> None:
        self._set_state(FiberState.LOADING)
        try:
            config = validate_config(
                self.runtime.definition.config_schema, self._raw_config
            )
            self.config = config
            self._error = None
            token = _current_fiber.set(self)
            try:
                result = self._execute_callback()
                if inspect.isawaitable(result):
                    result = await result
            finally:
                _current_fiber.reset(token)
            if callable(result):
                self._disposers.append(result)
            self.ctx.registry._load_counter += 1
            self._load_seq = self.ctx.registry._load_counter
            self._set_state(FiberState.RUNNING)
            self.ctx.registry._notify_provided(self)
        except BaseException as exc:  # noqa: BLE001 - plugin failure is isolated
            self._error = exc
            logger.error("plugin %r failed: %s", self.name, exc, exc_info=exc)
            self.ctx._bus._emit_sync("internal/error", self, exc)
            # Roll back partial effects registered before the failure (Cordis).
            await self._run_disposers(list(self._disposers))
            self._disposers.clear()
            self._set_state(FiberState.FAILED)

    def _execute_callback(self) -> Any:
        callback = self.runtime.definition.callback
        if inspect.isclass(callback):
            instance = callback(self.ctx, self.config)
            self._instance = instance
            return None  # class instances are not disposers
        return callback(self.ctx, self.config)

    async def _unload(self, *, permanent: bool) -> None:
        if self.state is FiberState.DISPOSED:
            return
        if permanent:
            self.uid = None
        self._set_state(FiberState.UNLOADING)
        # B5: release provided services first and let dependents settle before
        # this fiber's own cleanup runs.
        removed = self.ctx._services.unset_by_owner(self)
        if removed:
            await self.ctx.registry._refresh_dependents(removed, await_settle=True)
        errors = await self._run_disposers(list(self._disposers))
        self._disposers.clear()
        if errors:
            self.ctx._bus._emit_sync("internal/error", self, errors)
        if permanent:
            self._set_state(FiberState.DISPOSED)
            if self in self.runtime.fibers:
                self.runtime.fibers.remove(self)
            if not self.runtime.fibers:
                self.ctx.registry._runtimes.pop(self.runtime.definition.key, None)
        else:
            self._set_state(FiberState.PENDING)

    # -- public waiting -----------------------------------------------------

    async def await_fiber(self) -> "Fiber":
        """Wait for in-flight transitions; re-raise the stored failure, if any.

        Deterministic: if no transition is in flight but the fiber still needs
        to converge (e.g. it was mounted while active and its background load
        has not started yet), drives the converge itself.  A fiber stuck
        pending on missing dependencies resolves immediately (Cordis parity).
        """
        while True:
            task = self._settle_task
            if task is not None and task is not asyncio.current_task():
                try:
                    await asyncio.shield(task)
                except BaseException:
                    pass
                continue
            if self.state is FiberState.FAILED:
                break
            if self._next_action(_TARGET_CONVERGE) is not None:
                await self.settle_to(_TARGET_CONVERGE)
                continue
            break
        if self._error is not None:
            raise self._error
        return self


# ---------------------------------------------------------------------------
# PluginHandle
# ---------------------------------------------------------------------------


class PluginHandle:
    """Python view of a fiber returned by ``ctx.plugin()`` (design §6.2).

    Awaitable (settles when loading finishes; re-raises startup errors),
    disposable (permanent unload), and restartable.
    """

    def __init__(self, fiber: Fiber) -> None:
        self._fiber = fiber

    @property
    def state(self) -> FiberState:
        return self._fiber.state

    @property
    def name(self) -> str:
        return self._fiber.name

    @property
    def config(self) -> Any:
        return self._fiber.config

    @property
    def uid(self) -> int | None:
        return self._fiber.uid

    async def dispose(self) -> None:
        """Permanently unload this plugin instance."""
        await self._fiber.settle_to(_TARGET_DISPOSED)

    async def restart(self) -> None:
        """Unload and reload with the current config."""
        await self._fiber.settle_to(_TARGET_PENDING)
        await self._fiber.settle_to(_TARGET_CONVERGE)

    def __await__(self):
        return self._fiber.await_fiber().__await__()

    def __repr__(self) -> str:
        return f"<PluginHandle {self.name!r} state={self.state.value}>"


__all__ = [
    "EffectOwner",
    "Fiber",
    "FiberState",
    "PluginDef",
    "PluginHandle",
    "Registry",
    "RootFiber",
    "resolve_plugin",
]
