"""SkillsPlugin — discovers SKILL.md files, registers skill tool, injects context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xbotv2.api import (
    HookAction,
    HookContext,
    HookDecision,
    HookStage,
    Message,
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
        self._initialized = False

    async def on_unload(self) -> None:
        self._registry = SkillRegistry()
        self._active_skills.clear()
        self._permission_scope.clear()
        self._skill_tools.clear()
        self._initialized = False

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.ON_SESSION_INIT, self._on_session_init)
        ctx.register_hook(HookStage.BEFORE_USER_MESSAGE_ACCEPT, self._on_before_user_message)
        ctx.register_hook(HookStage.AFTER_CONTEXT, self._on_after_context)
        ctx.register_hook(HookStage.ON_TURN_END, self._on_turn_end)
        ctx.register_hook(HookStage.BEFORE_TOOL_CALL, self._on_before_tool)

        async def _load_skill(name: str, *, sandbox=None) -> ToolResult:
            skill = self._registry.load_skill(name)
            if skill is None:
                return ToolResult.failure(
                    "skill_not_found",
                    f"Skill '{name}' not found",
                )
            if skill.disable_model_invocation:
                return ToolResult.failure(
                    "skill_requires_explicit_invocation",
                    f"Skill '{name}' requires explicit /{name} invocation",
                )
            content = await load_skill(
                name, skill_registry=self._registry, sandbox=sandbox
            )
            self._activate_skill(skill)
            return ToolResult.success(
                content,
                data={"name": skill.name, "scope": skill.scope},
            )

        tool = Tool.from_function(_load_skill, name="skill")
        ctx.register_tool(
            tool,
            options=ToolRegistrationOptions(
                sandbox_mode="sandboxed",
                namespace="plugin:skills",
            ),
        )

    async def _on_session_init(self, ctx: HookContext) -> None:
        if ctx.plugin_runtime is None:
            raise RuntimeError("SkillsPlugin requires plugin runtime registration capability")
        if self._initialized:
            return
        ws = getattr(ctx.session, "workspace_root", "") or str(Path.cwd())
        self._registry.discover(Path(ws))
        try:
            for skill in self._registry.list_skills():
                if skill.disable_model_invocation:
                    continue
                registered_name = ctx.plugin_runtime.register_tool(
                    self._skill_as_tool(skill),
                    options=ToolRegistrationOptions(
                        sandbox_mode="sandboxed",
                        namespace=f"skills:{skill.scope}",
                    ),
                )
                self._skill_tools.append(registered_name)
        except Exception:
            self._rollback_skill_tools(ctx.plugin_runtime)
            self._registry = SkillRegistry()
            raise
        self._initialized = True

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
        instructions = parts[1] if len(parts) > 1 else ""
        content = await load_skill(
            skill_name, skill_registry=self._registry, sandbox=ctx.sandbox
        )
        expanded = f"## {skill_name}\n\n{content}"
        if instructions:
            expanded += f"\n\n## Instructions\n{instructions}"
        self._activate_skill(skill)
        return {"user_input": expanded}

    async def _on_after_context(self, ctx: HookContext) -> None:
        if not self._active_skills:
            return
        parts = ["## Active Skills"]
        for name in sorted(self._active_skills):
            parts.append(f"\n### {name}")
        parts.append("")
        content = "\n".join(parts)
        msgs = list(ctx.context_messages) if ctx.context_messages else []
        msgs.insert(1, Message(role="system", content=content))
        return {"context_messages": msgs}

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
