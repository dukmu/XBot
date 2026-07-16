"""SkillsPlugin — discovers SKILL.md files, registers skill tool, injects context."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from xbotv2.api import (
    Command,
    HookAction,
    HookContext,
    HookDecision,
    HookStage,
    PluginBase,
    PluginManifest,
    PluginSetupContext,
    PluginStore,
    RuntimePluginContext,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)

from .permission_scope import SkillPermissionScope
from .registry import Skill, SkillRegistry
from .skill_tool import load_skill


class SkillsPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._registry = SkillRegistry()
        self._permission_scope = SkillPermissionScope()
        self._active_skills: set[str] = set()
        self._skill_tools: list[str] = []
        self._skill_commands: list[str] = []
        self._model_skill_names: set[str] = set()
        self._metadata_budget_chars = 8_000
        self._initialized = False

    async def on_unload(self) -> None:
        self._registry = SkillRegistry()
        self._active_skills.clear()
        self._permission_scope.clear()
        self._skill_tools.clear()
        self._skill_commands.clear()
        self._model_skill_names.clear()
        self._initialized = False

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.ON_SESSION_INIT, self._on_session_init)
        ctx.register_hook(HookStage.BEFORE_USER_MESSAGE_ACCEPT, self._on_before_user_message)
        ctx.register_hook(HookStage.BEFORE_TOOL_SCHEMA_BIND, self._on_before_tool_schema)
        ctx.register_hook(HookStage.ON_TURN_END, self._on_turn_end)
        ctx.register_hook(HookStage.BEFORE_TOOL_CALL, self._on_before_tool)

    async def _on_session_init(self, ctx: HookContext) -> None:
        if ctx.plugin_runtime is None:
            raise RuntimeError("SkillsPlugin requires plugin runtime registration capability")
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
                    registered_name = ctx.plugin_runtime.register_tool(
                        self._skill_as_tool(skill),
                        options=ToolRegistrationOptions(
                            sandbox_mode="sandboxed",
                            namespace=f"skills:{skill.scope}",
                        ),
                    )
                    self._skill_tools.append(registered_name)
                    self._model_skill_names.add(skill.name)
                if skill.user_invocable:
                    command_name = ctx.plugin_runtime.register_command(Command(
                        name=skill.name,
                        kind="prompt",
                        description=skill.description,
                        usage=f"/{skill.name} [instructions]",
                    ))
                    self._skill_commands.append(command_name)
        except Exception:
            self._rollback_skill_tools(ctx.plugin_runtime)
            self._registry = SkillRegistry()
            raise
        self._initialized = True

    async def _on_before_tool_schema(self, ctx: HookContext):
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

        invoke.__doc__ = skill.description
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

    def _rollback_skill_tools(self, runtime: RuntimePluginContext) -> None:
        for command_name in reversed(self._skill_commands):
            runtime.unregister_command(command_name)
        self._skill_commands.clear()
        for registered_name in reversed(self._skill_tools):
            runtime.unregister_tool(registered_name)
        self._skill_tools.clear()
        self._initialized = False

    async def _on_before_user_message(self, ctx: HookContext):
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
            sandbox=ctx.sandbox,
        )
        self._activate_skill(skill)
        return {"user_input": content}

    async def _on_turn_end(self, ctx: HookContext) -> None:
        self._active_skills.clear()
        self._permission_scope.clear()

    async def _on_before_tool(self, ctx: HookContext) -> None:
        if not self._active_skills:
            return
        tool_name = ctx.tool_call.name if ctx.tool_call else ""
        if not tool_name:
            return
        decision = self._permission_scope.check(tool_name, ctx.tool_call.args)
        if decision == "allow":
            return HookDecision(
                HookAction.ALLOW,
                f"Tool '{tool_name}' pre-approved by active skill",
            )
        if decision == "deny":
            return HookDecision(
                HookAction.DENY,
                f"Tool '{tool_name}' not permitted by active skill",
            )
        return None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "skills": len(self._registry.list_skills()),
            "active_skills": len(self._active_skills),
        }
