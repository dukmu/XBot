"""SkillsPlugin — discovers SKILL.md files, registers skill tool, injects context."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from XBotv2.core import (
    Command,
    EventContext,
    Events,
    prompt_container,
    prompt_element,
    Tool,
    ToolResult,
)
from XBotv2.core.tools import GuardDecision

from .permission_scope import SkillPermissionScope
from .registry import Skill, SkillRegistry
from .skill_tool import load_skill


class SkillsPlugin:
    inject = ['tools', 'commands', 'sandbox']
    name = "skills"

    def __init__(self) -> None:
        self._registry = SkillRegistry()
        self._permission_scope = SkillPermissionScope()
        self._active_skills: set[str] = set()
        self._skill_tools: list[str] = []
        self._skill_commands: list[str] = []
        self._model_skill_names: set[str] = set()
        self._metadata_budget_chars = 8_000
        self._initialized = False

    def apply(self, ctx, config=None) -> None:
        self.ctx = ctx
        ctx.dispose(self._cleanup_runtime)
        ctx.on(Events.SESSION_INIT, self._on_session_init)
        ctx.on(Events.BEFORE_USER_MESSAGE_ACCEPT, self._on_before_user_message)
        ctx.on(Events.BEFORE_TOOL_SCHEMA_BIND, self._on_before_tool_schema)
        ctx.on(Events.TURN_END, self._on_turn_end)
        ctx.tools.guard(self._guard_tool_scope)

    def _cleanup_runtime(self) -> None:
        """Unregister session-registered skill tools/commands and reset state."""
        for command_name in reversed(self._skill_commands):
            self.ctx.commands.unregister(command_name)
        self._skill_commands.clear()
        for registered_name in reversed(self._skill_tools):
            self.ctx.tools.unregister(registered_name)
        self._skill_tools.clear()
        self._registry = SkillRegistry()
        self._active_skills.clear()
        self._permission_scope.clear()
        self._model_skill_names.clear()
        self._initialized = False

    async def _on_session_init(self, ctx: EventContext) -> None:
        if self._initialized:
            return
        ws = getattr(ctx.session, "workspace_root", "") or str(Path.cwd())
        self._registry.discover(Path(ws))
        max_context = int(
            getattr(getattr(ctx, "config", None), "max_context_tokens", 0) or 0
        )
        if max_context > 0:
            self._metadata_budget_chars = min(
                8_000,
                int(max_context * 0.02 * 4),
            )
        try:
            for skill in self._registry.list_skills():
                if not skill.disable_model_invocation:
                    registered_name = self._register_skill_tool(skill)
                    self._skill_tools.append(registered_name)
                    self._model_skill_names.add(skill.name)
                if skill.user_invocable:
                    command_name = self.ctx.commands.register(Command(
                        name=skill.name,
                        kind="prompt",
                        description=skill.description,
                        usage=f"/{skill.name} [instructions]",
                    ))
                    self._skill_commands.append(command_name)
        except Exception:
            self._cleanup_runtime()
            self._registry = SkillRegistry()
            raise
        self._initialized = True

    def _register_skill_tool(self, skill: Skill) -> str:
        """Register one skill tool on the raw registry (tracked for cleanup)."""
        return self.ctx.tools.registry.register(
            self._skill_as_tool(skill),
            injected={"sandbox": self.ctx.sandbox},
            namespace=f"skills:{skill.scope}",
        )

    async def _on_before_tool_schema(self, ctx: EventContext):
        request = ctx.model_request or {}
        tools = list(request.get("tools") or [])
        if not tools or not self._model_skill_names:
            return None
        remaining = self._metadata_budget_chars
        selected = []
        for tool in tools:
            name = str(getattr(tool, "name", ""))
            if name not in self._model_skill_names:
                selected.append(tool)
                continue
            description = str(getattr(tool, "description", "") or "")
            size = len(name) + len(description)
            if size <= remaining:
                selected.append(tool)
                remaining -= size
                continue
            if remaining > len(name):
                selected.append(
                    replace(tool, description=description[: remaining - len(name)])
                )
                remaining = 0
        return {"tools": selected}

    def _skill_as_tool(self, skill: Skill) -> Tool:
        async def invoke(*, sandbox=None) -> ToolResult:
            content = await load_skill(
                skill.name,
                skill_registry=self._registry,
                sandbox=sandbox,
            )
            self._activate_skill(skill)
            return ToolResult.success(
                content,
                data={"name": skill.name, "scope": skill.scope},
            )

        invoke.__doc__ = (
            f"Load Skill instructions for this turn. {skill.description}"
        )
        return Tool.from_function(invoke, name=skill.name)

    def _activate_skill(self, skill: Skill) -> None:
        if skill.name in self._active_skills:
            return
        if skill.allowed_tools or skill.disallowed_tools:
            self._permission_scope.add(
                allowed=skill.allowed_tools,
                disallowed=skill.disallowed_tools,
            )
        self._active_skills.add(skill.name)


    async def _on_before_user_message(self, ctx: EventContext):
        """Expand /skill-name [instructions] with SKILL.md content."""
        text = (ctx.user_input or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split(None, 1)
        skill_name = parts[0][1:]  # strip leading /
        skill = self._registry.load_skill(skill_name)
        if skill is None:
            return
        if not skill.user_invocable:
            return {
                "event": {
                    "type": "error",
                    "data": {
                        "code": "skill_not_user_invocable",
                        "message": f"Skill '/{skill_name}' is not user-invocable.",
                    },
                },
                "turn_complete": True,
            }
        instructions = parts[1] if len(parts) > 1 else ""
        content = await load_skill(
            skill_name,
            arguments=instructions,
            skill_registry=self._registry,
            sandbox=self.ctx.sandbox,
        )
        self._activate_skill(skill)
        return {
            "user_input": prompt_container(
                "skill_invocation",
                [
                    prompt_element("skill_instructions", content),
                    prompt_element("user_arguments", instructions),
                ],
                attributes={
                    "name": skill.name,
                    "scope": skill.scope,
                    "source": skill.path,
                },
            )
        }

    async def _on_turn_end(self, ctx: EventContext) -> None:
        self._active_skills.clear()
        self._permission_scope.clear()

    async def _guard_tool_scope(self, tool_call: Any, _entry: Any) -> Any:
        if not self._active_skills:
            return
        tool_name = tool_call.name
        if not tool_name:
            return
        decision = self._permission_scope.check(tool_name, tool_call.args)
        if decision == "deny":
            return GuardDecision(
                "deny",
                f"Tool '{tool_name}' is denied by the active skill",
                source="skills",
            )
        return None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "skills": len(self._registry.list_skills()),
            "active_skills": len(self._active_skills),
        }


plugin = SkillsPlugin()
