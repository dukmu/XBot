"""XBot runtime components: XCore plugins that provide core capabilities.

The XBot runtime is assembled on an XCore context as component packages
(design: ``XCore/docs/05-migration-plan.md``).  Each component is an XCore
object plugin whose ``apply(ctx)`` registers one or more services:

- :mod:`runtime` -- static runtime info (paths, session, config, variables).
- :mod:`tools`   -- tool registry, sandbox policy, permissions, jobs, and the
  plugin-facing capability services (``ctx.tools`` / ``ctx.commands`` /
  ``ctx.prompts`` / ``ctx.agents``).
- :mod:`coretools` -- base tools and the tool-result cache event listener.
- :mod:`core`    -- the engine component (builds :class:`Engine` from the
  context's services and provides it as ``ctx.engine``).

Plugins and the engine consume capabilities exclusively through these
services and the runtime events defined in :mod:`xbotv2.api.events`; there is
no separate "core" beside XCore.
"""

from __future__ import annotations

from xbotv2.plugins.core import EngineComponent
from xbotv2.plugins.coretools import CoreToolsComponent
from xbotv2.plugins.runtime import RuntimeComponent
from xbotv2.plugins.tools import ToolsComponent

__all__ = [
    "CoreToolsComponent",
    "EngineComponent",
    "RuntimeComponent",
    "ToolsComponent",
]
