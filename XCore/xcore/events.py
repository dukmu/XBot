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
from dataclasses import dataclass
from typing import Any, Callable



logger = logging.getLogger("xcore.events")

#: A disposer is a plain callable that undoes a registration.  Returns
#: ``True`` when the registration was still live (Cordis parity).
Disposer = Callable[[], bool]

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
        logger.error("internal listener failed: %s", exc, exc_info=exc)


@dataclass
class Hook:
    """One registered listener record."""

    event: str
    callback: Callable[..., Any]
    seq: int
    filters: tuple[Callable[[Any], bool], ...]
    owner: Any  # the Context that registered this hook
    global_: bool = False
    once: bool = False
    disposed: bool = False


def _match_event(event: str, pattern: tuple[str, ...]) -> bool:
    if pattern == ("*",):
        return True
    segments = event.split("/")
    if len(segments) != len(pattern):
        return False
    return all(p == "*" or p == segment for p, segment in zip(pattern, segments))


class EventBus:
    """Root-owned event bus shared by every context."""

    def __init__(self) -> None:
        self._exact: dict[str, list[Hook]] = {}
        self._wildcards: list[tuple[tuple[str, ...], list[Hook]]] = []
        self._seq = 0

    # -- registration -------------------------------------------------------

    def register(
        self,
        event: str,
        callback: Callable[..., Any],
        *,
        owner: Any,
        filters: tuple[Callable[[Any], bool], ...] = (),
        global_: bool = False,
        once: bool = False,
        prepend: bool = False,
    ) -> Disposer:
        """Register a listener and return a disposer (idempotent)."""
        if not isinstance(event, str) or not event:
            raise ValueError("event name must be a non-empty string")
        segments = event.split("/")
        if any("*" in segment and segment != "*" for segment in segments):
            raise ValueError(
                "wildcard '*' is only allowed as a full event segment "
                f"(got {event!r})"
            )
        if prepend:
            self._seq -= 1
        else:
            self._seq += 1
        hook = Hook(
            event=event,
            callback=callback,
            seq=self._seq,
            filters=filters,
            owner=owner,
            global_=global_,
            once=once,
        )
        if "*" in event:
            pattern = tuple(event.split("/"))
            for entry in self._wildcards:
                if entry[0] == pattern:
                    entry[1].append(hook)
                    break
            else:
                self._wildcards.append((pattern, [hook]))
        else:
            self._exact.setdefault(event, []).append(hook)

        def disposer() -> bool:
            return self.unregister(event, callback)

        return disposer

    def unregister(self, event: str, callback: Callable[..., Any]) -> bool:
        """Remove one listener by callback identity; returns whether removed."""
        for hooks in self._collect_hook_lists(event):
            for index, hook in enumerate(hooks):
                if hook.callback is callback and not hook.disposed:
                    hook.disposed = True
                    del hooks[index]
                    return True
        return False

    def _collect_hook_lists(self, event: str) -> list[list[Hook]]:
        lists: list[list[Hook]] = []
        if event in self._exact:
            lists.append(self._exact[event])
        if "*" in event:
            pattern = tuple(event.split("/"))
            for entry in self._wildcards:
                if entry[0] == pattern:
                    lists.append(entry[1])
        return lists

    # -- matching -----------------------------------------------------------

    def _match(self, event: str, args: tuple[Any, ...]) -> list[Hook]:
        """Collect live hooks for a dispatch, in registration order.

        Marks ``once`` hooks as consumed *synchronously* (no awaits here), so a
        concurrent dispatch cannot double-fire them.
        """
        session = args[0] if args else None
        hooks: list[Hook] = []
        exact = self._exact.get(event)
        if exact:
            hooks.extend(exact)
        for pattern, wildcard_hooks in self._wildcards:
            if _match_event(event, pattern):
                hooks.extend(wildcard_hooks)
        hooks.sort(key=lambda hook: hook.seq)
        result: list[Hook] = []
        for hook in hooks:
            if hook.disposed:
                continue
            if not hook.global_ and session is not None:
                if not all(predicate(session) for predicate in hook.filters):
                    continue
            if hook.once:
                hook.disposed = True
            result.append(hook)
        return result

    @staticmethod
    async def _invoke(hook: Hook, args: tuple[Any, ...]) -> Any:
        result = hook.callback(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    # -- public (async) dispatch -------------------------------------------

    def _announce(self, mode: str, event: str, args: tuple[Any, ...]) -> None:
        """Emit ``internal/dispatch`` diagnostics for non-internal events."""
        if not event.startswith("internal/"):
            self._emit_sync("internal/dispatch", mode, event, args)

    async def emit(self, event: str, *args: Any) -> None:
        """Serial dispatch; awaits every listener; propagates errors."""
        self._announce("emit", event, args)
        for hook in self._match(event, args):
            await self._invoke(hook, args)

    async def parallel(self, event: str, *args: Any) -> None:
        """Concurrent dispatch; aggregates failures into an ExceptionGroup."""
        self._announce("parallel", event, args)
        hooks = self._match(event, args)
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

    async def serial(self, event: str, *args: Any) -> Any:
        """Serial dispatch; stops and returns at the first bail value."""
        self._announce("serial", event, args)
        for hook in self._match(event, args):
            result = await self._invoke(hook, args)
            if is_bailed(result):
                return result
        return None

    async def bail(self, event: str, *args: Any) -> Any:
        """Alias of :meth:`serial` (Python awaits both; design §14.2)."""
        return await self.serial(event, *args)

    async def chain(self, event: str, value: Any, *args: Any) -> Any:
        """Value pipeline: each listener receives the previous listener's result."""
        self._announce("chain", event, args)
        for hook in self._match(event, (value, *args)):
            value = await self._invoke(hook, (value, *args))
        return value

    async def waterfall(self, event: str, *args: Any, next: Any = _UNSET) -> Any:
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
        hooks = self._match(event, args)
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

    def _emit_sync(self, event: str, *args: Any) -> None:
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
                    "listener for %r failed: %s", event, exc, exc_info=True
                )

    def _bail_sync(self, event: str, *args: Any) -> Any:
        """Synchronous bail for ``internal/*`` interception (e.g. listener registration)."""
        for hook in self._match(event, args):
            try:
                result = hook.callback(*args)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "listener for %r failed: %s", event, exc, exc_info=True
                )
                continue
            if is_bailed(result):
                return result
        return None

    # -- inspection ---------------------------------------------------------

    def hooks_for(self, event: str) -> list[Hook]:
        """Live hooks for an event in registration order (no session filtering).

        Used by bridge layers (e.g. XBotv2's HookManager) that gather
        listeners while keeping their own invocation contract.  ``once``
        hooks are *not* consumed here -- consumption happens only in the
        async dispatch primitives.
        """
        hooks: list[Hook] = []
        exact = self._exact.get(event)
        if exact:
            hooks.extend(exact)
        for pattern, wildcard_hooks in self._wildcards:
            if _match_event(event, pattern):
                hooks.extend(wildcard_hooks)
        hooks.sort(key=lambda hook: hook.seq)
        return [hook for hook in hooks if not hook.disposed]

    def listener_count(self, event: str) -> int:
        """Number of live listeners for an event (diagnostics)."""
        count = 0
        for hooks in self._collect_hook_lists(event):
            count += sum(1 for hook in hooks if not hook.disposed)
        return count


__all__ = ["Disposer", "EventBus", "is_bailed"]
