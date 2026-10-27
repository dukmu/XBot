"""Load workspace instructions and Agent definitions at application startup."""

from __future__ import annotations

from pathlib import Path
from pydantic import JsonValue

from xcore import Context

from XBotv2.context_builder import (
    CONTEXT_COMPONENTS_BUILT,
    BuiltContext,
    FilePromptComponent,
)
from XBotv2.core.variables import RuntimeVariables


class WorkspaceInstructionsPlugin:
    """Contribute ``AGENTS.md`` instructions from one workspace."""

    inject = ["variables", "workspace_root"]
    name = "workspace_instructions"

    def apply(
        self, ctx: Context, config: dict[str, JsonValue] | None = None
    ) -> None:
        self._instructions_path = Path(ctx.workspace_root) / "AGENTS.md"
        self._variables: RuntimeVariables = ctx.variables
        ctx.on(CONTEXT_COMPONENTS_BUILT, self._inject_workspace_instructions)

    def _inject_workspace_instructions(
        self,
        event: BuiltContext,
    ) -> None:
        if not self._instructions_path.is_file():
            return
        try:
            source_text = self._instructions_path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise UnicodeError(
                f"Workspace instructions at {self._instructions_path} must be UTF-8: {exc}"
            ) from exc
        except OSError as exc:
            raise OSError(
                f"Unable to read workspace instructions at {self._instructions_path}: {exc}"
            ) from exc
        text = self._variables.expand_markdown(
            source_text.strip(),
            source="AGENTS.md",
        )
        if not text:
            return
        component = FilePromptComponent(
            stage="system_instructions",
            source=self.name,
            logical_path="AGENTS.md",
            text=text,
        )
        event.components.append(component)


plugin = WorkspaceInstructionsPlugin()
