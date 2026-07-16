"""Agent definition loading and model-facing subagent dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from xbotv2.api import (
    AgentDefinition,
    Command,
    CommandResult,
    HookAction,
    HookContext,
    HookDecision,
    HookStage,
    PluginBase,
    PluginSetupContext,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)

_FRONTMATTER = "---"
_FIELDS = {"description", "mode", "provider", "permissions", "tools", "hidden"}


class AgentsPlugin(PluginBase):
    """Register workspace Agent definitions and subagent tools."""

    def __init__(self, manifest, store) -> None:
        super().__init__(manifest, store)
        self._timeout_seconds = 600.0

    async def on_load(self, config: dict[str, Any]) -> None:
        self._timeout_seconds = float(config.get("timeout_seconds", 600.0))

    def setup(self, ctx: PluginSetupContext) -> None:
        for definition in _load_definitions(ctx.workspace_root / ".xbot" / "agents"):
            ctx.register_agent(definition)
        if ctx.agent_runtime is None:
            return

        runtime = ctx.agent_runtime

        async def task(
            agent: str,
            prompt: str,
            background: bool = False,
        ) -> ToolResult:
            """Delegate a focused task to a registered subagent.

            The child runs in a separate thread under the current session. Use a
            subagent when work can be delegated with a clear outcome and does not
            require continuous conversational clarification. The result contains
            only the child's final response, usage, and thread ID; its full history
            remains in the child thread. Background mode returns a task ID
            immediately and sends the final result through the mailbox when idle.

            Args:
                agent: Registered subagent name shown in the system instructions.
                prompt: Complete task, relevant context, constraints, and expected output.
                background: Return immediately and deliver completion asynchronously.
            """
            return await runtime.run(agent, prompt, background)

        async def list_agent_tasks(task_id: str | None = None) -> ToolResult:
            """List subagent tasks or inspect one task's complete final result.

            Args:
                task_id: Optional ID returned by task(background=true).
            """
            return await runtime.list_tasks(task_id)

        async def stop_agent_task(task_id: str) -> ToolResult:
            """Stop one running background subagent task.

            Args:
                task_id: Exact background subagent task ID to stop.
            """
            return await runtime.stop_task(task_id)

        ctx.register_tool(
            Tool.from_function(task, name="task"),
            options=ToolRegistrationOptions(
                sandbox_mode="host",
                namespace="plugin:agents",
                timeout_seconds=self._timeout_seconds,
            ),
        )
        for function in (list_agent_tasks, stop_agent_task):
            ctx.register_tool(
                Tool.from_function(function),
                options=ToolRegistrationOptions(
                    sandbox_mode="host",
                    namespace="plugin:agents",
                ),
            )
        ctx.register_command(Command(
            name="agent",
            description="Show the active Agent and registered Agent definitions.",
            handler=self._agent_command,
            usage="/agent [list|status]",
            examples=("/agent", "/agent list"),
        ))
        ctx.register_hook(HookStage.BEFORE_TOOL_CALL, self._allow_task)

    async def _agent_command(self, ctx: Any, raw_args: str) -> CommandResult:
        action = raw_args.strip() or "status"
        if action not in {"list", "status"}:
            return CommandResult("Usage: /agent [list|status]", status="error")
        definitions = self._definitions(ctx)
        active = str(getattr(ctx.engine.config, "agent_name", "XBotv2"))
        lines = [f"Active Agent: {active}"]
        lines.extend(
            f"{definition.name}  {definition.mode}  {definition.description}"
            for definition in definitions
            if not definition.hidden
        )
        return CommandResult(
            "\n".join(lines),
            data={
                "active": active,
                "agents": [
                    {
                        "name": definition.name,
                        "mode": definition.mode,
                        "description": definition.description,
                        "hidden": definition.hidden,
                    }
                    for definition in definitions
                ],
            },
        )

    @staticmethod
    def _definitions(ctx: Any) -> tuple[AgentDefinition, ...]:
        runtime = getattr(ctx.engine, "subagents", None)
        return runtime.definitions() if runtime is not None else ()

    async def _allow_task(self, ctx: HookContext) -> HookDecision | None:
        if ctx.tool_call is not None and ctx.tool_call.name in {
            "task", "list_agent_tasks", "stop_agent_task"
        }:
            return HookDecision(
                HookAction.ALLOW,
                "Subagent dispatch is controlled by child tool permissions",
            )
        return None


def _load_definitions(directory: Path) -> list[AgentDefinition]:
    if not directory.is_dir():
        return []
    return [_load_definition(path) for path in sorted(directory.glob("*.md"))]


def _load_definition(path: Path) -> AgentDefinition:
    text = path.read_text(encoding="utf-8")
    if not text.startswith(f"{_FRONTMATTER}\n"):
        raise ValueError(f"Agent definition requires YAML frontmatter: {path}")
    marker = text.find(f"\n{_FRONTMATTER}\n", len(_FRONTMATTER) + 1)
    if marker < 0:
        raise ValueError(f"Agent definition has unclosed frontmatter: {path}")
    metadata = yaml.safe_load(text[len(_FRONTMATTER) + 1:marker]) or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"Agent frontmatter must be a mapping: {path}")
    unknown = set(metadata) - _FIELDS
    if unknown:
        raise ValueError(
            f"Unknown Agent fields in {path}: {', '.join(sorted(unknown))}"
        )
    prompt = text[marker + len(_FRONTMATTER) + 2:].strip()
    tools = metadata.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise ValueError(f"Agent tools must be a list: {path}")
    permissions = metadata.get("permissions") or {}
    if not isinstance(permissions, dict):
        raise ValueError(f"Agent permissions must be a mapping: {path}")
    return AgentDefinition(
        name=path.stem,
        description=str(metadata.get("description") or ""),
        mode=str(metadata.get("mode") or "subagent"),
        prompt=prompt,
        provider=(str(metadata["provider"]) if metadata.get("provider") else None),
        permissions=permissions,
        tools=tuple(str(tool) for tool in tools) if tools is not None else None,
        hidden=bool(metadata.get("hidden", False)),
    )
