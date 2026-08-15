"""Exception hierarchy for XCore.

All framework-raised errors derive from :class:`XCoreError` so callers can
catch the whole family with one ``except XCoreError``.  Plugin *apply* failures
are not wrapped here: ``PluginHandle.await()`` re-raises the original exception
(Cordis parity), so application code sees its own error type unchanged.
"""

from __future__ import annotations


class XCoreError(Exception):
    """Base class for every error raised by the XCore framework."""


class InactiveEffectError(XCoreError):
    """Raised when an effect is created on an already-disposed fiber.

    Mirrors Cordis' ``INACTIVE_EFFECT``: a plugin must not register listeners,
    services, or disposers after its owning fiber has been torn down.
    """


class ServiceNotFoundError(XCoreError):
    """Raised by ``Context.require(name)`` when no service is resolvable."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"service not found: {name!r}")


class ServiceConflictError(XCoreError):
    """Raised when a service is provided twice in the same isolation scope.

    Cordis v3 semantics: a service may only be provided once per isolate;
    release it with ``set(name, None)`` (or ``unset``) before re-providing.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"service {name!r} has already been registered in this scope")


class SchemaValidationError(XCoreError):
    """Raised when a config value does not match its declared ``S`` schema.

    ``path`` is a dotted ``$.a.b[0]`` location pointing at the offending
    field.
    """

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"config validation failed at {path}: {message}")
