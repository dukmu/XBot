"""XCore: a Python, Cordis-like, plugin-centric runtime core.

Provides recoverable state, pluginization, an event system, a lifecycle, and
a service system -- the stable surface that plugin authors and the migration
layer (Step 3) depend on.  The public symbol list is fixed and validated by
``tests/test_public_api.py`` against ``docs/features/api.md``.

Zero third-party dependencies (stdlib only), Python 3.11+.
"""

from xcore.context import Context
from xcore.errors import (
    InactiveEffectError,
    SchemaValidationError,
    ServiceConflictError,
    ServiceNotFoundError,
    XCoreError,
)
from xcore.events import Disposer, EventBus
from xcore.plugin import (
    FiberState,
    PluginDef,
    PluginHandle,
    Registry,
)
from xcore.schema import S
from xcore.service import Service
from xcore.state import StateService

__version__ = "0.1.0"

__all__ = [
    "Context",
    "Disposer",
    "EventBus",
    "FiberState",
    "InactiveEffectError",
    "PluginDef",
    "PluginHandle",
    "Registry",
    "S",
    "SchemaValidationError",
    "Service",
    "ServiceConflictError",
    "ServiceNotFoundError",
    "StateService",
    "XCoreError",
    "__version__",
]
