"""Load workspace instructions and Agent definitions at application startup."""

from __future__ import annotations

from pathlib import Path

from xcore import Context

from XBotv2.agents import AgentCatalogPort
from XBotv2.context_builder import (
    CONTEXT_COMPONENTS_BUILT,
    ContextComponent,
    ContextComponentsBuilt,
)
from XBotv2.core.variables import RuntimeVariables


class WorkspaceInstructionsPlugin:
    """Contribute ``AGENTS.md`` and ``.agents/*.md`` from one workspace."""

    inject = ["variables", "workspace_root", "agent_catalog"]
    name = "workspace_instructions"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        self._instructions_path = Path(ctx.workspace_root) / "AGENTS.md"
        self._agents_dir = Path(ctx.workspace_root) / ".agents"
        self._variables: RuntimeVariables = ctx.variables
        self._catalog: AgentCatalogPort = ctx.agent_catalog

        self._register_workspace_agents()
        ctx.on(CONTEXT_COMPONENTS_BUILT, self._inject_workspace_instructions)
        ctx.dispose(self._clear_workspace_agents)

    def _register_workspace_agents(self) -> None:
        if self._agents_dir.is_dir():
            self._catalog.register_markdown(
                self._agents_dir,
                variables=self._variables,
                overlay=True,
                owner=self.name,
            )

    def _clear_workspace_agents(self) -> None:
        self._catalog.unregister_owned(self.name, overlay=True)

    def _inject_workspace_instructions(
        self,
        event: ContextComponentsBuilt,
    ) -> None:
        if not self._instructions_path.is_file():
            return
        text = self._variables.expand_markdown(
            self._instructions_path.read_text(encoding="utf-8").strip(),
            source="AGENTS.md",
        )
        if not text:
            return
        component = ContextComponent(
            role="system",
            source="workspace_instructions",
            content=text,
            plugin_name=self.name,
            stage="system_instructions",
            source_path="AGENTS.md",
        )
        before_sources = {
            "plugin_fragment",
            "memory",
            "runtime_state",
            "history",
        }
        index = next(
            (
                index
                for index, existing in enumerate(event.components)
                if existing.source in before_sources
            ),
            len(event.components),
        )
        event.components.insert(index, component)


plugin = WorkspaceInstructionsPlugin()
