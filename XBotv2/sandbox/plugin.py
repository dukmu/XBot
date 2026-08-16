"""Sandbox component: the sandboxed execution policy as an XCore service.

The sandbox policy decides read/write/execute access for tool calls against
the workspace, data root, and session root.  Backends (bwrap today) and the
filesystem helpers stay inside this plugin package.
"""

from __future__ import annotations

from typing import Any

from XBotv2.sandbox.policy import SandboxPolicy


class SandboxComponent:
    inject = ['state_store', 'session']
    """Register the sandbox policy as ``ctx.sandbox``."""

    name = "xbot.sandbox"

    def apply(self, ctx: Any, config: Any = None) -> None:
        runtime_config = ctx.runtime
        ctx.set(
            "sandbox",
            SandboxPolicy(
                runtime_config.sandbox,
                data_root=ctx.data_root,
                workspace_root=ctx.workspace_root,
                session_root=ctx.state_store.root,
                variables=ctx.variables,
            ),
        )


plugin = SandboxComponent()
