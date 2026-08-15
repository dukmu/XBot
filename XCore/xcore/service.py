"""Service base class and the root service store.

Layering (design §2): ``service.py`` knows nothing about fibers or contexts --
the store is pure storage keyed by ``(isolate_label, name)`` and the
``Service`` base only holds a duck-typed ``ctx``.  Dependency rechecking is
wired by ``Context`` (which owns both the store and the registry).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from xcore.errors import ServiceConflictError

_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")


@dataclass
class Impl:
    """One service implementation record."""

    name: str
    value: Any
    owner: Any  # the owning fiber (duck-typed)


class ServiceStore:
    """Root service storage: ``(isolate_label, name) -> Impl``.

    A label is an arbitrary identity object; ``Context.isolate(name)`` gives a
    service its own label so different scopes can provide different
    implementations of the same name (Cordis isolation semantics).
    """

    def __init__(self) -> None:
        self._impls: dict[tuple[Any, str], Impl] = {}

    def set(self, label: Any, name: str, value: Any, owner: Any) -> None:
        """Provide a service in a scope. Raises on duplicate non-None provide."""
        if value is None:
            self.unset(label, name)
            return
        key = (label, name)
        existing = self._impls.get(key)
        if existing is not None and existing.value is not None:
            raise ServiceConflictError(name)
        self._impls[key] = Impl(name=name, value=value, owner=owner)

    def get(self, label: Any, name: str, *, strict: bool = True) -> Any:
        """Resolve a service. ``strict`` hides values whose owner is inactive."""
        impl = self._impls.get((label, name))
        if impl is None or impl.value is None:
            return None
        if strict:
            owner = impl.owner
            if owner is not None and not getattr(owner, "is_running", True):
                return None
        return impl.value

    def has(self, label: Any, name: str) -> bool:
        impl = self._impls.get((label, name))
        return impl is not None and impl.value is not None

    def unset(self, label: Any, name: str, value: Any = None) -> bool:
        """Remove a service; optionally verifies the current value's identity."""
        key = (label, name)
        impl = self._impls.get(key)
        if impl is None:
            return False
        if value is not None and impl.value is not value:
            return False
        del self._impls[key]
        return True

    def unset_by_owner(self, owner: Any) -> list[str]:
        """Release every service owned by a fiber; returns the released names."""
        removed: list[str] = []
        for key, impl in list(self._impls.items()):
            if impl.owner is owner:
                del self._impls[key]
                removed.append(impl.name)
        return removed

    def names_provided_by(self, owner: Any) -> list[str]:
        """Names currently provided by a fiber (for state-transition notify)."""
        return [
            impl.name for impl in self._impls.values() if impl.owner is owner
        ]


class Service:
    """Base class for services exposing a named API on ``ctx``.

    Subclasses call ``super().__init__(ctx, name=...)`` (or set ``name`` as a
    class attribute); the instance is registered immediately under the current
    fiber and unregistered automatically when that fiber unloads.  Cordis
    parity: constructing a ``Service`` *is* providing it.
    """

    name: str = ""

    def __init__(self, ctx: Any, *, name: str | None = None) -> None:
        self.ctx = ctx
        self.name = name or self._default_name()
        ctx.set(self.name, self)

    def _default_name(self) -> str:
        if self.name:
            return self.name
        class_name = type(self).__name__
        if class_name.endswith("Service"):
            class_name = class_name[: -len("Service")]
        return _SNAKE.sub("_", class_name).lower()


__all__ = ["Impl", "Service", "ServiceStore"]
