"""EventBus: Cordis-style event dispatch for XCore.

One root-owned EventBus serves every context.  Hooks record their owning
context so they are removed with the owning fiber; filters are snapshotted at
registration time (see the design doc, §3.3/§4).

Dispatch modes (design §4.2):

- ``emit``      — serial, awaits every listener, ignores return values.
- ``parallel``  — concurrent; failures aggregate into ``ExceptionGroup``
                  (v4-style), ``CancelledError`` propagates immediately.
- ``serial``    — serial, stops at the first *bail* value, returns it.
- ``bail``      — same as ``serial`` in Python (both await; §14.2).
- ``chain``     — value pipeline: each listener receives the previous result.
- ``waterfall`` — around-middleware: listeners get ``(*args, next)``; calling
                  ``next()`` delegates, not calling it vetoes the chain.

``internal/*`` events dispatch *synchronously* (``_emit_sync``/``_bail_sync``)
because they fire from synchronous framework code (``on()`` interception,
fiber state transitions, service changes).  Their listeners must be plain
functions; failures are logged, never propagated (§4.3).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from xcore.api.events import Disposer, EventKey



logger = logging.getLogger("xcore.events")

_UNSET = object()


def is_bailed(value: Any) -> bool:
    """Cordis ``isBailed``: only ``None`` and ``False`` continue the chain."""
    return value is not None and value is not False


def _maybe_await(result: Any) -> Any:
    if inspect.isawaitable(result):
        return result
    return None


def _log_task_failure(task: asyncio.Task) -> None:
    """Log an async internal listener's failure without propagating it."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "internal listener failed error_type=%s",
            type(exc).__name__,
            exc_info=exc,
        )


@dataclass
class Hook:
    """One registered listener record."""

    event: EventKey | str
    callback: Callable[..., Any]
    seq: int
    filters: tuple[Callable[[Any], bool], ...]
    owner: Any  # the Context that registered this hook
    global_: bool = False
    descendants: bool = False
    once: bool = False
    disposed: bool = False
    on_remove: Callable[[], Any] | None = None


def _match_event(event: EventKey | str, pattern: tuple[str, ...]) -> bool:
    if pattern == ("*",):
        return True
    segments = event.split("/")
    if len(segments) != len(pattern):
        return False
    return all(p == "*" or p == segment for p, segment in zip(pattern, segments))


class EventBus:
    """Root-owned event bus shared by every context."""

    def __init__(self, *, scope: Any = None) -> None:
        self._scope = scope
        self._exact: dict[tuple[object, EventKey | str], list[Hook]] = {}
        self._wildcards: dict[object, dict[tuple[str, ...], list[Hook]]] = {}
        self._seq = 0

    # -- registration -------------------------------------------------------

    def register(
        self,
        event: EventKey | str,
        callback: Callable[..., Any],
        *,
        owner: Any,
        filters: tuple[Callable[[Any], bool], ...] = (),
        global_: bool = False,
        once: bool = False,
        prepend: bool = False,
        descendants: bool = False,
        on_remove: Callable[[], Any] | None = None,
    ) -> Disposer:
        """Register a listener and return a disposer (idempotent)."""
        if not isinstance(event, (str, EventKey)) or not str(event):
            raise ValueError("event name must be a non-empty string")
        segments = event.split("/") if isinstance(event, str) else []
        if any("*" in segment and segment != "*" for segment in segments):
            raise ValueError(
                "wildcard '*' is only allowed as a full event segment "
                f"(got {event!r})"
            )
        self._seq += 1
        hook = Hook(
            event=event,
            callback=callback,
            seq=-self._seq if prepend else self._seq,
            filters=filters,
            owner=owner,
            global_=global_,
            descendants=descendants,
            once=once,
            on_remove=on_remove,
        )
        scope = owner._scope
        if self._scope is None:
            self._scope = owner.root
        if isinstance(event, str) and "*" in event:
            pattern = tuple(event.split("/"))
            self._wildcards.setdefault(scope, {}).setdefault(pattern, []).append(hook)
        else:
            self._exact.setdefault((scope, event), []).append(hook)
        logger.debug(
            "listener.registered event=%s owner=%s callback=%s once=%s global=%s",
            event,
            owner.fiber.name,
            _callback_name(callback),
            once,
            global_,
        )

        def disposer() -> bool:
            return self._remove_hook(hook)

        return disposer

    def unregister(self, event: EventKey | str, callback: Callable[..., Any], *, owner: Any = None) -> bool:
        """Remove one listener by callback identity; returns whether removed."""
        for hooks in self._collect_hook_lists(event, scope=owner._scope if owner is not None else None):
            for hook in hooks:
                if hook.callback is callback and not hook.disposed and (owner is None or hook.owner is owner):
                    return self._remove_hook(hook)
        return False

    def _remove_hook(self, hook: Hook) -> bool:
        if hook.disposed:
            return False
        hook.disposed = True
        scope = hook.owner._scope
        if isinstance(hook.event, str) and "*" in hook.event:
            pattern = tuple(hook.event.split("/"))
            patterns = self._wildcards[scope]
            hooks = patterns[pattern]
            hooks.remove(hook)
            if not hooks:
                del patterns[pattern]
            if not patterns:
                del self._wildcards[scope]
        else:
            key = (scope, hook.event)
            hooks = self._exact[key]
            hooks.remove(hook)
            if not hooks:
                del self._exact[key]
        logger.debug(
            "listener.unregistered event=%s owner=%s callback=%s",
            hook.event, hook.owner.fiber.name, _callback_name(hook.callback),
        )
        if hook.on_remove is not None:
            callback, hook.on_remove = hook.on_remove, None
            callback()
        return True

    def _collect_hook_lists(
        self, event: EventKey | str, *, scope: Any = None
    ) -> list[list[Hook]]:
        """Exact registration buckets, used for off and diagnostics."""
        if scope is not None:
            exact = self._exact.get((scope, event))
            lists = [exact] if exact is not None else []
            scopes = (scope,)
        else:
            lists = [hooks for (_, identity), hooks in self._exact.items() if identity == event]
            scopes = tuple(self._wildcards)
        if isinstance(event, str) and "*" in event:
            pattern = tuple(event.split("/"))
            for owner_scope in scopes:
                hooks = self._wildcards.get(owner_scope, {}).get(pattern)
                if hooks is not None:
                    lists.append(hooks)
        return lists

    def _matching_hooks(self, event: EventKey | str, scope: Any) -> list[Hook]:
        hooks = list(self._exact.get((scope, event), ()))
        if isinstance(event, str):
            for pattern, wildcard_hooks in self._wildcards.get(scope, {}).items():
                if _match_event(event, pattern):
                    hooks.extend(wildcard_hooks)
        return hooks

    # -- matching -----------------------------------------------------------

    def _match(self, event: EventKey | str, args: tuple[Any, ...], scope: Any = None) -> list[Hook]:
        """Collect live hooks for a dispatch, in registration order.

        Marks ``once`` hooks as consumed *synchronously* (no awaits here), so a
        concurrent dispatch cannot double-fire them.
        """
        scope = self._scope if scope is None else scope
        internal = isinstance(event, str) and event.startswith("internal/")
        if internal:
            scope = self._scope
        session = args[0] if args else None
        hooks: list[Hook] = []
        hooks.extend(self._matching_hooks(event, scope))
        ancestor = scope.parent if scope is not None else None
        seen = {scope}
        while ancestor is not None and not internal:
            ancestor_scope = ancestor._scope
            if ancestor_scope not in seen:
                hooks.extend(
                    hook for hook in self._matching_hooks(event, ancestor_scope)
                    if hook.descendants
                )
                seen.add(ancestor_scope)
            ancestor = ancestor.parent
        hooks.sort(key=lambda hook: hook.seq)
        result: list[Hook] = []
        for hook in hooks:
            if hook.disposed:
                continue
            if not hook.global_ and session is not None:
                if not all(predicate(session) for predicate in hook.filters):
                    continue
            if hook.once:
                self._remove_hook(hook)
            result.append(hook)
        return result

    def _candidate_count(self, event: EventKey | str) -> int:
        """Count root diagnostics candidates without evaluating user filters."""
        return sum(not hook.disposed for hook in self._matching_hooks(event, self._scope))

    @staticmethod
    async def _invoke(hook: Hook, args: tuple[Any, ...]) -> Any:
        started = time.perf_counter()
        owner = hook.owner.fiber.name
        callback = _callback_name(hook.callback)
        logger.debug(
            "event.handler.start event=%s owner=%s callback=%s",
            hook.event,
            owner,
            callback,
        )
        try:
            result = hook.callback(*args)
            if inspect.isawaitable(result):
                result = await result
            return result
        except asyncio.CancelledError:
            logger.debug(
                "event.handler.cancelled event=%s owner=%s callback=%s",
                hook.event,
                owner,
                callback,
            )
            raise
        except Exception as exc:
            logger.error(
                "event.handler.error event=%s owner=%s callback=%s error_type=%s",
                hook.event,
                owner,
                callback,
                type(exc).__name__,
                exc_info=exc,
            )
            raise
        finally:
            logger.debug(
                "event.handler.finish event=%s owner=%s callback=%s duration_ms=%.3f",
                hook.event,
                owner,
                callback,
                (time.perf_counter() - started) * 1000,
            )

    # -- public (async) dispatch -------------------------------------------

    def _announce(self, mode: str, event: EventKey | str, args: tuple[Any, ...]) -> None:
        """Emit ``internal/dispatch`` diagnostics for non-internal events."""
        if not (isinstance(event, str) and event.startswith("internal/")):
            logger.debug(
                "event.dispatch mode=%s event=%s candidates=%d args=%d",
                mode,
                event,
                self._candidate_count(event),
                len(args),
            )
            self._emit_sync("internal/dispatch", mode, event, args)

    async def emit(self, event: EventKey | str, *args: Any, scope: Any = None) -> None:
        """Serial dispatch; awaits every listener; propagates errors."""
        self._announce("emit", event, args)
        for hook in self._match(event, args, scope):
            await self._invoke(hook, args)

    async def parallel(self, event: EventKey | str, *args: Any, scope: Any = None) -> None:
        """Concurrent dispatch; aggregates failures into an ExceptionGroup."""
        self._announce("parallel", event, args)
        hooks = self._match(event, args, scope)
        results = await asyncio.gather(
            *(self._invoke(hook, args) for hook in hooks),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise ExceptionGroup(f"event {event!r} failed", errors)

    async def serial(self, event: EventKey | str, *args: Any, scope: Any = None) -> Any:
        """Serial dispatch; stops and returns at the first bail value."""
        self._announce("serial", event, args)
        for hook in self._match(event, args, scope):
            result = await self._invoke(hook, args)
            if is_bailed(result):
                return result
        return None

    async def bail(self, event: EventKey | str, *args: Any, scope: Any = None) -> Any:
        """Alias of :meth:`serial` (Python awaits both; design §14.2)."""
        return await self.serial(event, *args, scope=scope)

    async def chain(self, event: EventKey | str, value: Any, *args: Any, scope: Any = None) -> Any:
        """Value pipeline: each listener receives the previous listener's result."""
        self._announce("chain", event, args)
        for hook in self._match(event, (value, *args), scope):
            value = await self._invoke(hook, (value, *args))
        return value

    async def waterfall(self, event: EventKey | str, *args: Any, next: Any = _UNSET, scope: Any = None) -> Any:
        """Around-middleware dispatch; ``next`` is the innermost continuation.

        Each listener receives ``(*args, next_fn)``.  Calling ``await
        next_fn()`` delegates to the next listener (finally ``next``); not
        calling it vetoes the rest of the chain.  Returns the outermost
        listener's return value.
        """
        if next is _UNSET:
            raise TypeError(
                "waterfall() requires the keyword argument next="
            )
        if not callable(next):
            raise TypeError("waterfall() next= must be callable")
        self._announce("waterfall", event, args)
        hooks = self._match(event, args, scope)
        iterator = iter(hooks)

        async def run_next() -> Any:
            # NOTE: the parameter `next` shadows the builtin here; use the
            # iterator protocol explicitly (review C5).
            try:
                hook = iterator.__next__()
            except StopIteration:
                return await next()
            return await self._invoke(hook, (*args, run_next))

        return await run_next()

    # -- internal (synchronous) dispatch ------------------------------------

    def _emit_sync(self, event: EventKey | str, *args: Any) -> None:
        """Fire-and-forget synchronous dispatch for ``internal/*`` events.

        Listener failures are logged; they must never break framework cleanup
        (mirrors vendored ``emitPluginDisposed``).
        """
        for hook in self._match(event, args):
            try:
                result = hook.callback(*args)
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(result)
                    task.add_done_callback(_log_task_failure)
            except Exception as exc:  # noqa: BLE001 - internal bus must not raise
                logger.error(
                    "listener for %r failed error_type=%s",
                    event,
                    type(exc).__name__,
                    exc_info=True,
                )

    def _bail_sync(self, event: EventKey | str, *args: Any) -> Any:
        """Synchronous bail for ``internal/*`` interception (e.g. listener registration)."""
        for hook in self._match(event, args):
            try:
                result = hook.callback(*args)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "listener for %r failed error_type=%s",
                    event,
                    type(exc).__name__,
                    exc_info=True,
                )
                continue
            if is_bailed(result):
                return result
        return None

    # -- inspection ---------------------------------------------------------
    def listener_count(self, event: EventKey | str) -> int:
        """Number of live listeners for an event (diagnostics)."""
        count = 0
        for hooks in self._collect_hook_lists(event):
            count += sum(1 for hook in hooks if not hook.disposed)
        return count


def _callback_name(callback: Callable[..., Any]) -> str:
    return getattr(callback, "__qualname__", type(callback).__qualname__)


__all__ = ["Disposer", "EventBus", "is_bailed"]
