"""SkillsPlugin — discovers SKILL.md files, registers skill tool, injects context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookContext, HookStage
from xbotv2.llm.messages import Message
from xbotv2.plugin.base import PluginBase
from xbotv2.plugin.manifest import PluginManifest
from xbotv2.plugin.store import PluginStore
from xbotv2.tools.registry import ToolRegistry
from xbotv2.tools.types import XBotTool

from .permission_scope import SkillPermissionScope
from .registry import SkillRegistry
from .skill_tool import load_skill


class SkillsPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._registry = SkillRegistry()
        self._permission_scope = SkillPermissionScope()
        self._active_skills: dict[str, str] = {}

    async def on_load(self, config: dict[str, Any]) -> None:
        pass

    def register_hooks(self, manager: HookManager) -> None:
        manager.register(HookStage.ON_SESSION_INIT, self._on_session_init)
        manager.register(HookStage.BEFORE_USER_MESSAGE_ACCEPT, self._on_before_user_message)
        manager.register(HookStage.AFTER_CONTEXT, self._on_after_context)
        manager.register(HookStage.ON_TURN_END, self._on_turn_end)
        manager.register(HookStage.BEFORE_TOOL_CALL, self._on_before_tool)

    def register_tools(self, registry: ToolRegistry) -> None:
        plugin = self

        async def _load_skill(name: str) -> str:
            skill = plugin._registry.load_skill(name)
            if skill is None:
                return f"Error: skill '{name}' not found"
            content = await load_skill(name, skill_registry=plugin._registry)
            if skill.allowed_tools or skill.disallowed_tools:
                plugin._permission_scope.add(
                    allowed=skill.allowed_tools,
                    disallowed=skill.disallowed_tools,
                )
            plugin._active_skills[name] = name
            return content

        tool = XBotTool.from_function(_load_skill, name="skill")
        registry.register(tool, sandbox_mode="host", owner_plugin=self.manifest.name, namespace="plugin:skills")

    async def _on_session_init(self, ctx: HookContext) -> None:
        ws = getattr(ctx.session, "workspace_root", "") or str(Path.cwd())
        self._registry.discover(Path(ws))
        for s in self._registry.list_skills():
            content = s.content
            def _make_skill_invoke(c=content):
                def _skill_invoke(**kwargs):
                    return c
                return _skill_invoke
            fake = XBotTool.from_function(_make_skill_invoke(), name=s.name)
            ns = f"skills:{s.scope}"
            ctx.tools.register(fake, sandbox_mode="host", owner_plugin=self.manifest.name, namespace=ns)

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
        content = await load_skill(skill_name, skill_registry=self._registry)
        expanded = f"## {skill_name}\n\n{content}"
        if instructions:
            expanded += f"\n\n## Instructions\n{instructions}"
        if skill.allowed_tools or skill.disallowed_tools:
            self._permission_scope.add(allowed=skill.allowed_tools, disallowed=skill.disallowed_tools)
        self._active_skills[skill_name] = skill_name
        return {"user_input": expanded}

    async def _on_after_context(self, ctx: HookContext) -> None:
        if not self._active_skills:
            return
        parts = ["## Active Skills"]
        for name in self._active_skills:
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
        tool_name = ctx.tool_call.get("name", "") if ctx.tool_call else ""
        if not tool_name:
            return
        decision = self._permission_scope.check(tool_name)
        if decision == "deny":
            return {"deny_reason": f"Tool '{tool_name}' disallowed by active skill"}
        return None
