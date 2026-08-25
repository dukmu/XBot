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

from XBotv2.config import POLICY_CHANGED, PolicyChanged
from XBotv2.context_builder import BEFORE_CONTEXT_BUILD, ContextBuildRequest
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.sandbox.commands import build_sandbox_commands


class SandboxComponent:
    inject = [
        "thread_paths", "session", "tools", "data_root", "variables",
        "workspace_root", "commands", "settings",
    ]
    """Register the sandbox policy as ``ctx.sandbox`` and its guard."""

    name = "xbot.sandbox"

    def apply(self, ctx: Any, config: Any = None) -> None:
        policy = SandboxPolicy(
            (config or {}).get("sandbox"),
            data_root=ctx.data_root,
            workspace_root=ctx.workspace_root,
            session_root=ctx.thread_paths.state_dir,
            variables=ctx.variables,
        )
        ctx.set("sandbox", policy)
        ctx.tools.guard(policy.make_guard())
        for command in build_sandbox_commands(ctx.settings):
            ctx.commands.register(command)
        handlers = SandboxHandlers(policy)
        ctx.on(POLICY_CHANGED, handlers.update_policy)
        ctx.on(BEFORE_CONTEXT_BUILD, handlers.contribute_context, prepend=True)


class SandboxHandlers:
    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy

    async def update_policy(self, event: PolicyChanged) -> None:
        self._policy.replace_config(event.config.sandbox)

    async def contribute_context(self, event: ContextBuildRequest) -> None:
        event.sandbox_summary = self._policy.describe()


plugin = SandboxComponent()
