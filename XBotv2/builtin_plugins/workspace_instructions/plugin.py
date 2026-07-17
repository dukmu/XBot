"""Startup-only workspace instruction loading."""

from __future__ import annotations

from xbotv2.api import PluginBase, PluginSetupContext


class WorkspaceInstructionsPlugin(PluginBase):
    """Inject the workspace AGENTS.md as a source-tagged system fragment."""

    def setup(self, ctx: PluginSetupContext) -> None:
        path = ctx.workspace_root / "AGENTS.md"
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if text.strip():
                ctx.add_prompt_fragment(
                    "system_instructions",
                    text,
                    source="AGENTS.md",
                )
