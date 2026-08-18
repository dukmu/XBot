"""Sandbox component: the sandboxed execution policy as an XCore service.

The sandbox policy decides read/write/execute access for tool calls against
the workspace, data root, and session root.  Backends (bwrap today) and the
filesystem helpers stay inside this plugin package.  The policy registers
itself as a monotonic execution guard on ``ctx.tools`` — enforcement-only:
it denies paths outside the policy and never requests approval (human
approval belongs to the permission layer).
"""

from __future__ import annotations

from typing import Any

from XBotv2.core.events import Events
from XBotv2.sandbox.policy import SandboxPolicy


class SandboxComponent:
    inject = [
        "storage", "session", "tools", "data_root", "variables",
        "workspace_root",
    ]
    """Register the sandbox policy as ``ctx.sandbox`` and its guard."""

    name = "xbot.sandbox"

    def apply(self, ctx: Any, config: Any = None) -> None:
        policy = SandboxPolicy(
            (config or {}).get("sandbox"),
            data_root=ctx.data_root,
            workspace_root=ctx.workspace_root,
            session_root=ctx.storage.root,
            variables=ctx.variables,
        )
        ctx.set("sandbox", policy)
        ctx.tools.guard(policy.make_guard())

        async def contribute_context(event: Any) -> None:
            if event.context_kwargs is not None:
                event.context_kwargs["sandbox_summary"] = policy.describe()

        ctx.on(Events.BEFORE_CONTEXT_BUILD, contribute_context, prepend=True)


plugin = SandboxComponent()
