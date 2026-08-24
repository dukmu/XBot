"""Load workspace instructions and Agent definitions at application startup."""

from __future__ import annotations

from pathlib import Path

from xcore import Context

from XBotv2.context_builder import (
    CONTEXT_COMPONENTS_BUILT,
    ContextComponent,
    ContextComponentsBuilt,
)
from XBotv2.core.variables import RuntimeVariables


class WorkspaceInstructionsPlugin:
    """Contribute ``AGENTS.md`` instructions from one workspace."""

    inject = ["variables", "workspace_root"]
    name = "workspace_instructions"

    def apply(self, ctx: Context, config: object | None = None) -> None:
        self._instructions_path = Path(ctx.workspace_root) / "AGENTS.md"
        self._variables: RuntimeVariables = ctx.variables
        ctx.on(CONTEXT_COMPONENTS_BUILT, self._inject_workspace_instructions)

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
