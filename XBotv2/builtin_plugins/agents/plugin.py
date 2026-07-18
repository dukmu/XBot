"""Agent definition loading and model-facing subagent dispatch."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import yaml

from xbotv2.api import (
    AgentDefinition,
    PluginBase,
    PluginSetupContext,
    RuntimeVariables,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)

_FRONTMATTER = "---"
_FIELDS = {
    "description",
    "mode",
    "provider",
    "model",
    "temperature",
    "max_output_tokens",
    "context_window",
    "max_iterations",
    "steps",
    "permission",
    "permissions",
    "tools",
    "hidden",
}


class AgentsPlugin(PluginBase):
    """Register workspace Agent definitions and subagent tools."""

    def __init__(self, manifest, store) -> None:
        super().__init__(manifest, store)
        self._timeout_seconds = 600.0

    async def on_load(self, config: dict[str, Any]) -> None:
        self._timeout_seconds = float(config.get("timeout_seconds", 600.0))

    def setup(self, ctx: PluginSetupContext) -> None:
        definitions = {
            definition.name: definition
            for definition in _load_definitions(
                ctx.data_root / ".agents",
                ctx.variables,
            )
        }
        definitions.update({
            definition.name: definition
            for definition in _load_definitions(
                ctx.workspace_root / ".agents",
                ctx.variables,
            )
        })
        for definition in definitions.values():
            ctx.register_agent(definition)
        if ctx.agent_runtime is None:
            return

        runtime = ctx.agent_runtime

        async def task(
            agent: str,
            prompt: str,
            background: bool = False,
        ) -> ToolResult:
            """Delegate a focused task to a registered subagent.

            The child runs in a separate thread under the current session. Use a
            subagent when work can be delegated with a clear outcome and does not
            require continuous conversational clarification. The result contains
            only the child's final response, usage, and thread ID; its full history
            remains in the child thread. Background mode returns a task ID
            immediately and sends the final result through the mailbox when idle.

            Args:
                agent: Registered subagent name shown in the system instructions.
                prompt: Complete task, relevant context, constraints, and expected output.
                background: Return immediately and deliver completion asynchronously.
            """
            return await runtime.run(agent, prompt, background)

        async def list_agent_tasks(task_id: str | None = None) -> ToolResult:
            """List subagent tasks or inspect one task's complete final result.

            Args:
                task_id: Optional ID returned by task(background=true).
            """
            return await runtime.list_tasks(task_id)

        async def stop_agent_task(task_id: str) -> ToolResult:
            """Stop one running background subagent task.

            Args:
                task_id: Exact background subagent task ID to stop.
            """
            return await runtime.stop_task(task_id)

        ctx.register_tool(
            Tool.from_function(task, name="task"),
            options=ToolRegistrationOptions(
                sandbox_mode="host",
                namespace="plugin:agents",
                timeout_seconds=self._timeout_seconds,
            ),
        )
        for function in (list_agent_tasks, stop_agent_task):
            ctx.register_tool(
                Tool.from_function(function),
                options=ToolRegistrationOptions(
                    sandbox_mode="host",
                    namespace="plugin:agents",
                ),
            )
def _load_definitions(
    directory: Path,
    variables: RuntimeVariables | None = None,
) -> list[AgentDefinition]:
    if not directory.is_dir():
        return []
    return [
        _load_definition(path, variables)
        for path in sorted(directory.glob("*.md"))
    ]


def _load_definition(
    path: Path,
    variables: RuntimeVariables | None = None,
) -> AgentDefinition:
    variables = variables or RuntimeVariables()
    text = path.read_text(encoding="utf-8")
    if not text.startswith(f"{_FRONTMATTER}\n"):
        raise ValueError(f"Agent definition requires YAML frontmatter: {path}")
    marker = text.find(f"\n{_FRONTMATTER}\n", len(_FRONTMATTER) + 1)
    if marker < 0:
        raise ValueError(f"Agent definition has unclosed frontmatter: {path}")
    metadata = yaml.safe_load(text[len(_FRONTMATTER) + 1:marker]) or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"Agent frontmatter must be a mapping: {path}")
    unknown = set(metadata) - _FIELDS
    if unknown:
        raise ValueError(
            f"Unknown Agent fields in {path}: {', '.join(sorted(unknown))}"
        )
    prompt = variables.expand_markdown(
        text[marker + len(_FRONTMATTER) + 2:].strip(),
        source=str(path),
    )
    tools, disabled_tools, tool_permissions = _parse_tools(
        metadata.get("tools"), path
    )
    if "permission" in metadata and "permissions" in metadata:
        raise ValueError(f"Use either permission or permissions, not both: {path}")
    permissions = _parse_permissions(
        metadata.get("permission", metadata.get("permissions")), path
    )
    for decision, rules in tool_permissions.items():
        permissions.setdefault(decision, []).extend(rules)
    provider, model = _parse_model(metadata, path)
    return AgentDefinition(
        name=path.stem,
        description=str(metadata.get("description") or ""),
        mode=str(metadata.get("mode") or "all"),
        prompt=prompt,
        provider=provider,
        model=model,
        temperature=_optional_float(metadata, "temperature"),
        max_output_tokens=_optional_int(metadata, "max_output_tokens"),
        context_window=_optional_int(metadata, "context_window"),
        max_iterations=_optional_int(
            metadata, "max_iterations", alias="steps"
        ),
        permissions=permissions,
        tools=tools,
        disabled_tools=disabled_tools,
        hidden=bool(metadata.get("hidden", False)),
    )


def _parse_tools(
    value: Any,
    path: Path,
) -> tuple[tuple[str, ...] | None, tuple[str, ...], dict[str, list[dict[str, str]]]]:
    if value is None:
        return None, (), {}
    if isinstance(value, list):
        return tuple(str(tool) for tool in value), (), {}
    if isinstance(value, dict) and all(
        isinstance(enabled, bool) for enabled in value.values()
    ):
        disabled = tuple(str(tool) for tool, visible in value.items() if not visible)
        permissions: dict[str, list[dict[str, str]]] = {}
        for tool, visible in value.items():
            decision = "allow" if visible else "deny"
            permissions.setdefault(decision, []).append(
                {"tool": _tool_pattern(str(tool))}
            )
        return None, disabled, permissions
    raise ValueError(f"Agent tools must be a list or boolean mapping: {path}")


def _parse_permissions(value: Any, path: Path) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        if value not in {"allow", "ask", "deny"}:
            raise ValueError(f"Invalid Agent permission decision in {path}: {value}")
        return {value: [{"tool": ".*"}]}
    if not isinstance(value, dict):
        raise ValueError(f"Agent permissions must be a mapping or decision: {path}")
    if set(value).issubset({"allow", "ask", "deny"}) and all(
        isinstance(rules, list) for rules in value.values()
    ):
        return dict(value)

    normalized: dict[str, list[dict[str, str]]] = {}
    for tool, decision in value.items():
        if decision not in {"allow", "ask", "deny"}:
            raise ValueError(
                f"Permission for {tool!r} must be allow, ask, or deny: {path}"
            )
        normalized.setdefault(str(decision), []).append(
            {"tool": _tool_pattern(str(tool))}
        )
    return normalized


def _tool_pattern(value: str) -> str:
    return fnmatch.translate(value)


def _parse_model(
    metadata: dict[str, Any],
    path: Path,
) -> tuple[str | None, str | None]:
    provider = str(metadata["provider"]) if metadata.get("provider") else None
    model = str(metadata["model"]) if metadata.get("model") else None
    if model is None or "/" not in model:
        return provider, model
    model_provider, model_name = model.split("/", 1)
    if provider is not None and provider != model_provider:
        raise ValueError(
            f"Agent provider {provider!r} conflicts with model {model!r}: {path}"
        )
    return provider or model_provider, model_name


def _optional_float(metadata: dict[str, Any], name: str) -> float | None:
    value = metadata.get(name)
    return float(value) if value is not None else None


def _optional_int(
    metadata: dict[str, Any],
    name: str,
    *,
    alias: str | None = None,
) -> int | None:
    if alias and name in metadata and alias in metadata:
        raise ValueError(f"Use either {name} or {alias}, not both")
    value = metadata.get(name, metadata.get(alias) if alias else None)
    return int(value) if value is not None else None
