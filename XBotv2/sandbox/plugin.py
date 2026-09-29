"""Sandbox component: the sandboxed execution policy as an XCore service.

The sandbox policy decides read/write/execute access for tool calls against
the workspace, data root, and session root.  Backends (bwrap today) and the
filesystem helpers stay inside this plugin package.  The policy registers
itself as a monotonic execution guard on ``ctx.tools`` — enforcement-only:
it denies paths outside the policy and never requests approval (human
approval belongs to the permission layer).

The plugin also exposes ``request_sandbox_access`` as an agent-facing tool
(the human-facing ``/sandbox add`` is still the way to widen the policy by
hand).  The Tool implementation and its apply-side policy mutation live in
:mod:`XBotv2.sandbox.tools`; this module only wires dependencies and
registers the resulting Tool when the session is interactive.
"""

from __future__ import annotations

from xcore import Context

from XBotv2.config import POLICY_CHANGED, PolicyChanged
from XBotv2.context_builder import CONTEXT_BUILD_INPUTS_READY, ContextBuildRequest
from XBotv2.sandbox.commands import build_sandbox_commands
from XBotv2.sandbox.contracts import SandboxConfig
from XBotv2.sandbox.policy import SandboxPolicy
from XBotv2.sandbox.tools import (
    RequestSandboxAccessTool,
    build_apply_sandbox_access,
)


class SandboxComponent:
    inject = [
        "thread_paths", "session", "tools", "data_root", "variables",
        "workspace_root", "commands", "settings", "approval",
        "session_launch",
    ]
    """Register the sandbox policy as ``ctx.sandbox`` and its guard."""

    name = "xbot.sandbox"
    Config = SandboxConfig

    def apply(self, ctx: Context, config: SandboxConfig) -> None:
        policy = SandboxPolicy(
            config,
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
        ctx.on(CONTEXT_BUILD_INPUTS_READY, handlers.contribute_context)

        # Register the request tool only on the parent (non-subagent)
        # thread. Subagents must not mutate the host session's sandbox
        # policy — that decision belongs to the human in front of the
        # parent agent, not to a delegated worker. Non-interactive
        # sessions have no human to approve the rule, so the tool is
        # suppressed there as well.
        if ctx.session_launch.interactive and not ctx.session_launch.is_subagent:
            tool = RequestSandboxAccessTool(
                ctx.approval,
                apply_decision=build_apply_sandbox_access(ctx.settings),
            )
            ctx.tools.register(tool.as_tool())


class SandboxHandlers:
    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy

    async def update_policy(self, event: PolicyChanged) -> None:
        self._policy.replace_config(
            SandboxConfig.model_validate(event.effective_sandbox)
        )

    async def contribute_context(self, event: ContextBuildRequest) -> None:
        event.sandbox_summary = self._policy.describe()


plugin = SandboxComponent()
