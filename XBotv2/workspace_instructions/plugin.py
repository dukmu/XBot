"""Workspace instructions and workspace extension loading.

This plugin owns everything workspace-scoped:

* injecting ``<workspace>/AGENTS.md`` into each model request (context
  components);
* discovering ``<workspace>/.agents/*.md`` and registering them as workspace
  Agent overlays (workspace definitions win over data-root and built-ins);
* applying the workspace overlay ``<workspace>/.xbot/plugins.yaml`` to the
  plugin tree after load — overriding entries (config deep-merged, e.g.
  workspace hooks / workspace tools / permissions / disabled plugins) and
  mounting new workspace plugin ids.  The composition root does not merge
  workspace configuration; the workspace extension point is this plugin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from XBotv2.context_builder import (
    CONTEXT_COMPONENTS_BUILT,
    ContextComponent,
    ContextComponentsBuilt,
)
from XBotv2.loader import SOFT_RELOAD, SoftReload


class WorkspaceInstructionsPlugin:
    inject = {
        "required": ["session", "loader", "variables", "workspace_root"],
        "optional": ["agent_catalog"],
    }
    name = "workspace_instructions"

    """Inject the current workspace AGENTS.md into each model request."""

    async def apply(self, ctx, config=None) -> None:
        self.ctx = ctx
        self.workspace_root = Path(ctx.workspace_root)
        path = self.workspace_root / "AGENTS.md"
        overlay = self.workspace_root / ".xbot" / "plugins.yaml"
        disabled = False
        if overlay.is_file():
            patch = ctx.loader.patch_from_path(overlay)
            for entry in patch.entries:
                if entry.id == self.name and entry.disabled:
                    disabled = True
        if disabled:
            # The workspace overlay disables this plugin: skip AGENTS.md
            # injection and do not apply the rest of the patch.
            return

        self._register_workspace_agents(ctx)
        ctx.dispose(lambda: self._clear_workspace_agents(ctx))

        def inject_workspace_instructions(event: ContextComponentsBuilt) -> None:
            components = event.components
            if not path.is_file():
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
            CONTEXT_COMPONENTS_BUILT,
            inject_workspace_instructions,
        )
        ctx.on(SOFT_RELOAD, self._reload_workspace_agents)
        await self._apply_workspace_patch(ctx, overlay)

    async def _reload_workspace_agents(self, event: SoftReload) -> None:
        """Re-read workspace Agent definitions and the workspace overlay.

        Registration is overlay-scoped by this plugin under an explicit
        owner (event listeners run outside any plugin ``apply`` fiber), so
        re-registering replaces the previous set instead of raising.
        """
        self._register_workspace_agents(self.ctx)
        overlay = self.workspace_root / ".xbot" / "plugins.yaml"
        affected = await self._apply_workspace_patch(
            self.ctx,
            overlay,
        )
        if overlay.is_file() and self.name not in event.reloaded:
            event.reloaded.append(self.name)
        event.reloaded.extend(affected)

    def _register_workspace_agents(self, ctx: Any) -> None:
        """Discover and register workspace Agent definitions as overlays."""
        catalog = ctx.get("agent_catalog", strict=False)
        if catalog is None:
            return
        directory = self.workspace_root / ".agents"
        if not directory.is_dir():
            return
        catalog.unregister_owned(self.name, overlay=True)
        catalog.register_markdown(
            directory,
            variables=ctx.variables,
            overlay=True,
            owner=self.name,
        )

    def _clear_workspace_agents(self, ctx: Any) -> None:
        catalog = ctx.get("agent_catalog", strict=False)
        if catalog is None:
            return
        catalog.unregister_owned(self.name, overlay=True)

    async def _apply_workspace_patch(self, ctx: Any, overlay: Path) -> list[str]:
        """Apply ``<workspace>/.xbot/plugins.yaml`` as a tree patch."""
        if not overlay.is_file():
            return []
        loader = ctx.loader
        loader._patch_owner = self.name
        try:
            return await loader.apply_patch(loader.patch_from_path(overlay))
        finally:
            loader._patch_owner = None


plugin = WorkspaceInstructionsPlugin()
