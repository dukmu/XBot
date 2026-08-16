"""Dynamic workspace instruction loading."""

from __future__ import annotations

from api import (
    ContextComponent,
    EventContext,
    Events,
)


class WorkspaceInstructionsPlugin:
    name = "workspace_instructions"

    """Inject the current workspace AGENTS.md into each model request."""

    def apply(self, ctx, config=None) -> None:
        self.ctx = ctx
        path = ctx.workspace_root / "AGENTS.md"

        def inject_workspace_instructions(hook_ctx: EventContext) -> None:
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
                source="workspace_instructions",
                content=text,
                plugin_name="workspace_instructions",
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

        ctx.on(
            Events.AFTER_CONTEXT_COMPONENTS_BUILD,
            inject_workspace_instructions,
        )


plugin = WorkspaceInstructionsPlugin()
