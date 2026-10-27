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
:mod:`XBotv2.sandbox.impl.tools`; this module only wires dependencies and
registers the resulting Tool when the session is interactive.
"""

from __future__ import annotations
from XBotv2.commands.api.contracts import COMMANDS
from XBotv2.config.api.contracts import SETTINGS

from XBotv2.agentloop.api.contracts import TOOLS
from xcore.api.context import Context
from XBotv2.config.api.events import POLICY_CHANGED, PolicyChanged
from XBotv2.context_builder.api.events import CONTEXT_BUILD_INPUTS_READY, ContextBuildRequest
from XBotv2.sandbox.impl.commands import build_sandbox_commands
from XBotv2.sandbox.api.models import SANDBOX, SandboxConfig
from XBotv2.permissions.api.models import APPROVAL
from XBotv2.sandbox.impl.policy import SandboxPolicy
from XBotv2.sandbox.impl.tools import RequestSandboxAccessTool, build_apply_sandbox_access


class SandboxComponent:
    inject = [
        "thread_paths", "thread_info", TOOLS, "data_root", "variables",
        "workspace_root", COMMANDS, SETTINGS, APPROVAL,
        "thread_info", "interactive",
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
        ctx.set(SANDBOX, policy)
        ctx.require(TOOLS).guard(policy.make_guard())
        for command in build_sandbox_commands(ctx.require(SETTINGS)):
            ctx.require(COMMANDS).register(command)
        handlers = SandboxHandlers(policy)
        ctx.on(POLICY_CHANGED, handlers.update_policy)
        ctx.on(CONTEXT_BUILD_INPUTS_READY, handlers.contribute_context)

        # Register the request tool only on the parent (non-subagent)
        # thread. Subagents must not mutate the host session's sandbox
        # policy — that decision belongs to the human in front of the
        # parent agent, not to a delegated worker. Non-interactive
        # sessions have no human to approve the rule, so the tool is
        # suppressed there as well.
        if ctx.interactive and not ctx.thread_info.is_subagent:
            tool = RequestSandboxAccessTool(
                ctx.require(APPROVAL),
                apply_decision=build_apply_sandbox_access(ctx.require(SETTINGS)),
            )
            ctx.require(TOOLS).register(tool.as_tool())


class SandboxHandlers:
    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy

    async def update_policy(self, event: PolicyChanged) -> None:
        self._policy.replace_config(
            SandboxConfig.model_validate(event.effective_sandbox)
        )

    async def contribute_context(self, event: ContextBuildRequest) -> None:
        event.sandbox_summary = self._policy.describe()


def mount_thread(ctx, config):
    SandboxComponent().apply(ctx, config)


mount_thread.inject = getattr(SandboxComponent, "inject", ())
mount_thread.provide = ('sandbox',)
mount_thread.Config = getattr(SandboxComponent, "Config", None)
