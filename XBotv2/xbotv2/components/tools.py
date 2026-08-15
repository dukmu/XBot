"""Tools component: tool layer, commands, prompts, and jobs as services."""

from __future__ import annotations

from typing import Any

from xbotv2.plugin.bridge import AgentsService, CommandsService, PromptsService, ToolsService


class ToolsComponent:
    """Register the tool-layer capabilities as XCore services.

    Services: ``tools`` (plugin-facing :class:`ToolsService` wrapping the
    ToolRegistry), ``commands`` (CommandsService), ``prompts``
    (PromptsService over the ContextBuilder), ``sandbox`` (SandboxPolicy),
    ``permissions`` (PermissionSystem), ``job_registry`` (JobRegistry), and
    ``agents`` (plugin-facing :class:`AgentsService` wrapping the
    AgentRegistry).
    """

    def __init__(
        self,
        *,
        tool_registry: Any,
        context_builder: Any,
        sandbox_policy: Any,
        permissions: Any,
        job_registry: Any,
        agent_registry: Any,
    ) -> None:
        self._tool_registry = tool_registry
        self._context_builder = context_builder
        self._sandbox_policy = sandbox_policy
        self._permissions = permissions
        self._job_registry = job_registry
        self._agent_registry = agent_registry
        self.name = "xbot.tools"

    def apply(self, ctx: Any, config: Any = None) -> None:
        ctx.set("tools", ToolsService(self._tool_registry))
        ctx.set("commands", CommandsService())
        ctx.set("prompts", PromptsService(self._context_builder))
        ctx.set("sandbox", self._sandbox_policy)
        ctx.set("permissions", self._permissions)
        ctx.set("job_registry", self._job_registry)
        ctx.set("agents", AgentsService(self._agent_registry))
