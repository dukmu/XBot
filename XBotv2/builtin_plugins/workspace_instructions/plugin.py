"""Dynamic workspace instruction loading."""

from __future__ import annotations

from xbotv2.api import (
    ContextComponent,
    HookContext,
    HookStage,
    PluginBase,
    PluginSetupContext,
)


class WorkspaceInstructionsPlugin(PluginBase):
    """Inject the current workspace AGENTS.md into each model request."""

    def setup(self, ctx: PluginSetupContext) -> None:
        path = ctx.workspace_root / "AGENTS.md"

        def inject_workspace_instructions(hook_ctx: HookContext) -> None:
            components = hook_ctx.context_components
            if components is None or not path.is_file():
                return
            text = ctx.variables.expand_markdown(
                path.read_text(encoding="utf-8").strip(),
                source="AGENTS.md",
            )
            if not text:
                return
            component = ContextComponent(
                role="system",
                source="plugin_fragment",
                content=text,
                plugin_name=self.manifest.name,
                stage="system_instructions",
                source_path="AGENTS.md",
            )
            index = next(
                (
                    index
                    for index, existing in enumerate(components)
                    if existing.source in {
                        "plugin_fragment",
                        "memory",
                        "runtime_state",
                        "history",
                    }
                ),
                len(components),
            )
            components.insert(index, component)

        ctx.register_hook(
            HookStage.AFTER_CONTEXT_COMPONENTS_BUILD,
            inject_workspace_instructions,
        )
