"""XBot runtime components: XCore plugins that provide core capabilities.

The XBot runtime is assembled on an XCore context as component packages
(design: ``XCore/docs/05-migration-plan.md``).  Each component is an XCore
object plugin whose ``apply(ctx)`` registers one or more services:

- :mod:`runtime` -- static runtime info (paths, session, config, variables).
- :mod:`tools`   -- tool registry, sandbox policy, permissions, jobs.
- :mod:`hooks`   -- the 41-stage hook manager (``ctx.hooks``).
- :mod:`core`    -- the engine component (builds :class:`Engine` from the
  context's services and provides it as ``ctx.engine``).

Plugins and the engine consume capabilities exclusively through these
services; there is no separate "core" beside XCore.
"""

from __future__ import annotations

from xbotv2.components.core import EngineComponent
from xbotv2.components.hooks import HooksComponent
from xbotv2.components.runtime import RuntimeComponent
from xbotv2.components.tools import ToolsComponent

__all__ = [
    "EngineComponent",
    "HooksComponent",
    "RuntimeComponent",
    "ToolsComponent",
]
