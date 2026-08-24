"""Context: the central XCore object.

Composes the event bus, service store, plugin registry, lifecycle, middleware,
filters, and state service.  A root ``Context`` owns one EventBus, one
ServiceStore, one Registry, one StateService, and one root fiber; derived
contexts (``extend``/``select``/``isolate``/plugin fiber contexts) delegate to
the root and carry only their own filter chain and isolate labels.

Layering (design §2): ``context.py`` imports the leaf modules; nothing imports
``context.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from xcore.errors import InactiveEffectError, ServiceNotFoundError
from xcore.events import Disposer, EventBus, _UNSET
from xcore.plugin import (
    Fiber,
    FiberState,
    PluginHandle,
    Registry,
    RootFiber,
    _TARGET_CONVERGE,
    _TARGET_DISPOSED,
    _TARGET_PENDING,
    current_fiber,
)
from xcore.service import ServiceStore
from xcore.state import StateService

logger = logging.getLogger("xcore.context")


@dataclass
class _MiddlewareRecord:
    callback: Callable[..., Any]
    seq: int
    filters: tuple[Callable[[Any], bool], ...]


def _get_session_field(session: Any, field: str) -> Any:
    if isinstance(session, dict):
        return session.get(field)
    return getattr(session, field, None)


class Context:
    """The central object: events, services, plugins, lifecycle, state."""

    def __init__(
        self,
        *,
        name: str = "root",
        config: Any = None,
        parent: "Context | None" = None,
        data_dir: Path | str | None = None,
    ) -> None:
        self._name = name
        self._config = config
        self._parent = parent
        self._data_dir = Path(data_dir) if data_dir is not None else Path(".")
        self._children: list[Context] = []
        self._destroyed = False
        self._is_active = False
        if parent is None:
            self._root = self
            self._services = ServiceStore()
            self._bus = EventBus()
            self._registry = Registry(self)
            self._lifecycle_lock = asyncio.Lock()
            self._default_labels: dict[str, object] = {}
            self._middleware: list[_MiddlewareRecord] = []
            self._middleware_seq = 0
            self._state_service: StateService | None = None
            self._filters: list[Callable[[Any], bool]] = []
            self._isolate: dict[str, object] = {}
            self._fiber: RootFiber = RootFiber(self)
            self._install_internal_listener()
        else:
            self._root = parent._root
            self._services = parent._services
            self._bus = parent._bus
            self._registry = parent._registry
            self._lifecycle_lock = parent._lifecycle_lock
            self._default_labels = parent._default_labels
            self._middleware = parent._middleware
            self._middleware_seq = parent._middleware_seq
            self._state_service = None  # delegated via root
            self._filters = list(parent._filters)
            self._isolate = dict(parent._isolate)
            self._fiber = parent._fiber
            parent._children.append(self)

    def _install_internal_listener(self) -> None:
        """Handle ``internal/listener``: run ``ready`` immediately when active.

        Cordis parity: the interception receives the *registering* context as
        its first argument, so listeners can react to who registers what.
        """

        def handle(
            ctx: Any, name: str, listener: Any, options: dict[str, Any]
        ) -> Any:
            if name == "ready" and self._is_active:
                async def run() -> None:
                    result = listener()
                    if inspect.isawaitable(result):
                        await result

                task = asyncio.ensure_future(run())

                def cancel() -> bool:
                    task.cancel()
                    return True

                return cancel
            return None

        self._bus.register("internal/listener", handle, owner=self)

    # -- structure ----------------------------------------------------------

    @property
    def root(self) -> "Context":
        return self._root

    @property
    def parent(self) -> "Context | None":
        return self._parent

    @property
    def name(self) -> str:
        return self._name

    @property
    def config(self) -> Any:
        """This context's config.

        On a plugin fiber context this is the plugin's validated config
        (Cordis: ``ctx.config`` inside a plugin is the plugin's config);
        elsewhere it is the context's own ``config`` constructor value.
        """
        fiber = self._fiber
        if isinstance(fiber, Fiber) and fiber.ctx is self:
            return fiber.config
        return self._config

    @property
    def fiber(self) -> RootFiber | Fiber:
        return self._fiber

    @property
    def registry(self) -> Registry:
        return self._registry

    @property
    def is_active(self) -> bool:
        if self._parent is None:
            return self._is_active
        return self._root._is_active

    # -- service resolution -------------------------------------------------

    def _isolate_label(self, name: str) -> object:
        label = self._isolate.get(name)
        if label is not None:
            return label
        return self._default_labels.setdefault(name, object())

    def get(self, name: str, *, strict: bool = True) -> Any:
        """Resolve a service by name (``None`` when absent/inactive)."""
        if not isinstance(name, str) or not name:
            raise ValueError("service name must be a non-empty string")
        if name.startswith("_"):
            return None
        return self._services.get(self._isolate_label(name), name, strict=strict)

    def require(self, name: str) -> Any:
        """Resolve a service; raises :class:`ServiceNotFoundError` when absent."""
        value = self.get(name, strict=True)
        if value is None:
            raise ServiceNotFoundError(name)
        return value

    def has(self, name: str) -> bool:
        """Whether the service exists in this scope (regardless of strictness)."""
        if not isinstance(name, str) or not name:
            return False
        if name.startswith("_"):
            return False
        return self._services.has(self._isolate_label(name), name)

    def set(self, name: str, value: Any) -> Disposer:
        """Provide a service (owned by the current fiber).

        Cordis v3 semantics: a service may be provided only once per scope;
        pass ``None`` to release it.  Re-providing a non-None value raises
        :class:`ServiceConflictError`.  Returns a disposer that releases the
        service; the fiber unload also releases it automatically.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("service name must be a non-empty string")
        label = self._isolate_label(name)
        self._services.set(label, name, value, owner=self.fiber)
        # Only an active context has fibers waiting on dependencies; on a
        # fresh (not-yet-started) context there is nothing to refresh and no
        # event loop to schedule on.
        if self.is_active:
            asyncio.ensure_future(self._registry._refresh_dependents([name]))

        def release() -> bool:
            return self._services.unset(label, name, value)

        return self.fiber.effect(lambda: release, label=f"ctx.set({name!r})")

    def unset(self, name: str, value: Any = None) -> bool:
        """Release a service; pass ``value`` to verify the current value's identity."""
        if not isinstance(name, str) or not name:
            return False
        label = self._isolate_label(name)
        removed = self._services.unset(label, name, value)
        if removed:
            asyncio.ensure_future(self._registry._refresh_dependents([name]))
        return removed

    # -- attribute access ---------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name == "then":
            raise AttributeError(name)
        value = self.get(name, strict=True)
        if value is None:
            raise AttributeError(
                f"service {name!r} is not provided in this context"
            )
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            raise AttributeError(
                f"cannot set property {name!r} directly; use ctx.set({name!r}, ...)"
            )

    # -- events -------------------------------------------------------------

    def on(
        self,
        event: str,
        listener: Callable[..., Any],
        *,
        prepend: bool = False,
        global_: bool = False,
    ) -> Disposer:
        """Register a listener; returns a disposer (also removed on unload)."""
        options = {"prepend": prepend, "global_": global_}
        intercepted = self._bus._bail_sync(
            "internal/listener", self, event, listener, options
        )
        if intercepted is not None:
            return intercepted
        filters = tuple(self._filters)
        return self.fiber.effect(
            lambda: self._bus.register(
                event,
                listener,
                owner=self,
                filters=filters,
                global_=global_,
                prepend=prepend,
            ),
            label=f"ctx.on({event!r})",
        )

    def once(
        self,
        event: str,
        listener: Callable[..., Any],
        *,
        prepend: bool = False,
        global_: bool = False,
    ) -> Disposer:
        """Register a listener that fires at most once (concurrency-safe)."""
        fired = False

        def wrapper(*args: Any) -> Any:
            nonlocal fired
            if fired:
                return None
            fired = True
            self.off(event, wrapper)
            return listener(*args)

        return self.on(event, wrapper, prepend=prepend, global_=global_)

    def off(self, event: str, listener: Callable[..., Any]) -> bool:
        """Remove a listener by identity; returns whether it was removed."""
        return self._bus.unregister(event, listener)

    def before(
        self,
        event: str,
        listener: Callable[..., Any],
        *,
        append: bool = False,
    ) -> Disposer:
        """Sugar: ``on("before-" + event, ..., prepend=not append)`` (koishi)."""
        return self.on(f"before-{event}", listener, prepend=not append)

    async def emit(self, event: str, *args: Any) -> None:
        await self._bus.emit(event, *args)

    async def parallel(self, event: str, *args: Any) -> None:
        await self._bus.parallel(event, *args)

    async def serial(self, event: str, *args: Any) -> Any:
        return await self._bus.serial(event, *args)

    async def bail(self, event: str, *args: Any) -> Any:
        return await self._bus.bail(event, *args)

    async def chain(self, event: str, value: Any, *args: Any) -> Any:
        return await self._bus.chain(event, value, *args)

    async def waterfall(self, event: str, *args: Any, next: Any = _UNSET) -> Any:
        if next is _UNSET:
            raise TypeError("waterfall() requires the keyword argument next=")
        return await self._bus.waterfall(event, *args, next=next)

    # -- filters and scoping ------------------------------------------------

    def filter(
        self, predicate: Callable[[Any], bool], *, prepend: bool = False
    ) -> Disposer:
        """Register a filter affecting listeners registered from now on.

        Listeners snapshot this context's filter chain at registration time;
        a filter only applies to the session (first dispatch argument) when
        one is present (design §3.3).
        """
        if not callable(predicate):
            raise TypeError("filter() requires a callable predicate")
        if prepend:
            self._filters.insert(0, predicate)
        else:
            self._filters.append(predicate)

        def disposer() -> bool:
            try:
                self._filters.remove(predicate)
            except ValueError:
                return False
            return True

        return disposer

    def select(self, field: str, value: Any) -> "Context":
        """Return a child context whose listeners only see matching sessions.

        XCore extension (no ``ctx.select`` in Cordis; koishi builds these via
        session-selector filters, design §14.3).
        """
        child = self.extend()
        child._filters.append(
            lambda session: session is not None
            and _get_session_field(session, field) == value
        )
        return child

    def extend(self, *, fiber: RootFiber | Fiber | None = None) -> "Context":
        """Return a child context inheriting this one's scope (prototypal)."""
        child = object.__new__(Context)
        child._name = f"{self._name}/child"
        child._config = self._config
        child._parent = self
        child._data_dir = self._data_dir
        child._children = []
        child._destroyed = False
        child._is_active = False
        child._root = self._root
        child._services = self._services
        child._bus = self._bus
        child._registry = self._registry
        child._lifecycle_lock = self._lifecycle_lock
        child._default_labels = self._default_labels
        child._middleware = self._middleware
        child._middleware_seq = self._middleware_seq
        child._state_service = None
        child._filters = list(self._filters)
        child._isolate = dict(self._isolate)
        child._fiber = fiber if fiber is not None else self._fiber
        self._children.append(child)
        return child

    def isolate(self, name: str, label: object | None = None) -> "Context":
        """Return a child context with an independent scope for ``name``.

        The default label is a fresh object per call; pass the same ``label``
        to two ``isolate`` calls to join their scopes (Cordis fresh-Symbol
        semantics, design §5.3).
        """
        if not isinstance(name, str) or not name:
            raise ValueError("isolate() requires a service name")
        child = self.extend()
        child._isolate[name] = label if label is not None else object()
        return child

    # -- middleware ---------------------------------------------------------

    def middleware(
        self, callback: Callable[..., Any], *, prepend: bool = False
    ) -> Disposer:
        """Register a middleware ``(session, next)``; returns a disposer."""
        if not callable(callback):
            raise TypeError("middleware() requires a callable")
        filters = tuple(self._filters)
        root = self._root
        root._middleware_seq += 1
        record = _MiddlewareRecord(
            callback=callback,
            seq=root._middleware_seq if not prepend else -root._middleware_seq,
            filters=filters,
        )
        return self.fiber.effect(
            lambda: _register_middleware(root, record),
            label="ctx.middleware()",
        )

    async def run_middleware(self, session: Any) -> Any:
        """Run the middleware chain for a session (short-circuit aware)."""
        root = self._root
        records = [
            record
            for record in root._middleware
            if _middleware_passes(record, session)
        ]
        records.sort(key=lambda record: record.seq)
        iterator = iter(records)

        async def next_fn() -> Any:
            record = next(iterator, None)
            if record is None:
                return None
            result = record.callback(session, next_fn)
            if inspect.isawaitable(result):
                return await result
            return result

        return await next_fn()

    # -- plugins ------------------------------------------------------------

    def plugin(self, plugin: Any, config: Any = None) -> PluginHandle:
        """Mount a plugin; returns an awaitable/disposable handle."""
        return self._registry.plugin(plugin, config, parent_ctx=self)

    def inject(self, deps: Any, callback: Callable[..., Any]) -> PluginHandle:
        """Mount a dependency-gated callback (Cordis ``ctx.inject``)."""
        return self._registry.inject(deps, callback, parent_ctx=self)

    async def settle(self) -> None:
        """Drive the dependency graph to a stable state.

        This is a composition-boundary operation.  A plugin must finish its
        own ``apply`` callback before asking the graph to settle; otherwise it
        would wait for the very fiber that is currently loading.
        """
        if current_fiber() is not None:
            raise RuntimeError(
                "ctx.settle() cannot run inside plugin apply; finish the "
                "plugin lifecycle phase first"
            )
        await self._load_fixpoint()

    # -- effects and cleanup ------------------------------------------------

    def effect(
        self, execute: Callable[[], Any], *, label: str = "anonymous"
    ) -> Disposer:
        """Register an effect on the current fiber (design §3.4)."""
        return self.fiber.effect(execute, label=label)

    def dispose(self, callback: Callable[[], Any]) -> Disposer:
        """Register a cleanup callback for this context's fiber teardown.

        ``dispose()`` without a callback raises ``TypeError``: permanent
        teardown is ``destroy()`` (design §3.4, review D1).
        """
        if callback is None:
            raise TypeError(
                "dispose() requires a callback; use destroy() for teardown"
            )
        if not callable(callback):
            raise TypeError("dispose() requires a callable")
        return self.effect(lambda: callback, label="ctx.dispose()")

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Start the app: activate, load plugins (fixpoint), emit ``ready``."""
        if self._parent is not None:
            return await self._root.start()
        async with self._lifecycle_lock:
            if self._destroyed:
                raise RuntimeError("cannot start a destroyed root context")
            if self._is_active:
                logger.warning("start() called on an already-active root; no-op")
                return
            self._is_active = True
            await self._load_fixpoint()
            try:
                await self._bus.emit("ready")
            except BaseException:  # noqa: BLE001 - ready failures must not wedge start
                logger.exception("ready listeners failed; app continues")

    async def _load_fixpoint(self) -> None:
        """Iteratively load every loadable pending/failed fiber (review B1)."""
        while True:
            progressed = False
            for fiber in list(self._registry._all_fibers()):
                if fiber.state not in (FiberState.PENDING, FiberState.FAILED):
                    continue
                if not fiber._deps_satisfied():
                    continue
                await fiber.settle_to(_TARGET_CONVERGE)
                if fiber.state is FiberState.RUNNING:
                    progressed = True
            if not progressed:
                # A service notification may already be driving a transition
                # in the background.  Queue behind every fiber once, then
                # repeat if that transition made another dependency loadable.
                transitioning = [
                    fiber
                    for fiber in self._registry._all_fibers()
                    if fiber.state in (FiberState.LOADING, FiberState.UNLOADING)
                ]
                if not transitioning:
                    return
                for fiber in transitioning:
                    await fiber.settle_to(_TARGET_CONVERGE)

    async def stop(self) -> None:
        """Stop the app: unload fibers in reverse load order, emit ``dispose``.

        Never raises: disposer failures are logged (design §3.2, review B3).
        The ``dispose`` event fires *before* fibers unload so listeners are
        still registered (Cordis semantics).
        """
        if self._parent is not None:
            return await self._root.stop()
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        """stop() body; assumes the lifecycle lock is held."""
        if not self._is_active:
            logger.warning("stop() called on an inactive root; no-op")
            return
        self._is_active = False
        try:
            await self._bus.emit("dispose")
        except BaseException:  # noqa: BLE001
            logger.exception("dispose listeners failed; cleanup continues")
        fibers = [
            fiber
            for fiber in self._registry._all_fibers()
            if fiber._load_seq is not None
        ]
        fibers.sort(key=lambda fiber: fiber._load_seq, reverse=True)
        for fiber in fibers:
            await fiber.settle_to(_TARGET_PENDING)

    async def destroy(self) -> None:
        """Permanently tear down this context subtree (irreversible)."""
        if self._parent is not None:
            await self._destroy_child()
            return
        async with self._lifecycle_lock:
            if self._destroyed:
                return
            await self._stop_locked()
            for fiber in list(self._registry._all_fibers()):
                await fiber.settle_to(_TARGET_DISPOSED)
            errors = await self._fiber._run_disposers(list(self._fiber._disposers))
            self._fiber._disposers.clear()
            for child in list(self._children):
                await child._destroy_child()
            self._destroyed = True
            if errors:
                logger.error("root dispose errors: %s", errors)

    async def _destroy_child(self) -> None:
        if self._destroyed:
            return
        for fiber in list(self._registry._all_fibers()):
            if fiber.parent is self:
                await fiber.settle_to(_TARGET_DISPOSED)
        for child in list(self._children):
            await child._destroy_child()
        if self._parent is not None and self in self._parent._children:
            self._parent._children.remove(self)
        self._destroyed = True

    # -- state --------------------------------------------------------------

    @property
    def state(self) -> StateService:
        """The app-level recoverable state service (root singleton, "state")."""
        if self._parent is not None:
            return self._root.state
        if self._state_service is None:
            self._state_service = StateService(
                path=self._data_dir / "state.json"
            )
            self.set("state", self._state_service)
        return self._state_service

    def __repr__(self) -> str:
        return f"<Context {self._name!r} active={self.is_active}>"


def _register_middleware(root: Context, record: _MiddlewareRecord) -> Disposer:
    root._middleware.append(record)

    def disposer() -> bool:
        try:
            root._middleware.remove(record)
        except ValueError:
            return False
        return True

    return disposer


def _middleware_passes(record: _MiddlewareRecord, session: Any) -> bool:
    if session is None:
        return True
    return all(predicate(session) for predicate in record.filters)


__all__ = ["Context", "InactiveEffectError"]
