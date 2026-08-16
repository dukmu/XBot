"""Agent definition loading and model-facing subagent job tools.

Subagents run as SUBAGENT jobs in the shared JobRegistry. This plugin only
implements the adapter: a JobRunner that spawns a child session through the
core AgentRuntime, and the typed model-facing tools. It never owns lifecycle
state; waiting, cancellation, output storage, and listing live in the registry.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import yaml

from xbotv2.api import (
    AgentDefinition,
    Job,
    JobContext,
    JobKind,
    JobNotFound,
    JobRegistry,
    JobRegistryClosed,
    JobResult,
    JobRunner,
    JobStatus,
    RuntimeVariables,
    Tool,
    ToolRegistrationOptions,
    ToolResult,
)
from xcore import S

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
_MAX_PROMPT_PREVIEW = 100
_MAX_SUMMARY = 256


class SubagentRunner:
    """Runs one SUBAGENT job through a spawned child session."""

    def __init__(
        self,
        *,
        runtime: Any,
        agent: str,
        prompt: str,
    ) -> None:
        self.runtime = runtime
        self.agent = agent
        self.prompt = prompt

    async def run(self, job: Job, ctx: JobContext) -> JobResult:
        session = await self.runtime.spawn(
            self.agent,
            self.prompt,
            parent_job_id=job.parent_job_id,
        )
        ctx.set_handle(session)
        result = await session.wait()
        output = ctx.outputs.create_text(result.final_response)
        ctx.primary_output = output
        return JobResult(
            summary=_preview(
                f"Subagent {self.agent} completed", _MAX_SUMMARY
            ),
            output_store=output,
            data={
                "agent": self.agent,
                "usage": dict(result.usage),
            },
        )

    async def cancel(self, job: Job) -> None:
        handle = job.runtime_handle
        if handle is not None:
            await handle.cancel()


class AgentsPlugin:
    """Register workspace Agent definitions and subagent job tools."""

    name = "agents"
    Config = S.object({
        "timeout_seconds": S.number().optional(),
    })

    def __init__(self) -> None:
        self._timeout_seconds = 600.0

    def apply(self, ctx, config=None) -> None:
        self.ctx = ctx
        self._timeout_seconds = float((config or {}).get("timeout_seconds", 600.0))
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
            ctx.agents.register(definition)
        if ctx.agent_runtime is None or ctx.job_registry is None:
            return

        runtime = ctx.agent_runtime
        registry: JobRegistry = ctx.job_registry

        async def spawn_subagent(
            agent: str,
            prompt: str,
            name: str | None = None,
        ) -> ToolResult:
            """Delegate a focused task to a registered subagent.

            The child runs asynchronously in a separate thread under the current
            session and returns a job ID immediately. Use ``wait_subagent`` when
            later work depends on completion and ``read_subagent`` to read the
            final response. The full history remains in the child thread; only
            the final response and usage are captured here. The child may spawn
            further subagents itself.

            Args:
                agent: Registered subagent name shown in the system instructions.
                prompt: Complete task, relevant context, constraints, and expected output.
                name: Optional short label for listing.
            """
            if registry.closing:
                return ToolResult.failure(
                    "session_closing", "Session is closing"
                )
            definition = runtime.definitions()
            known = {item.name for item in definition}
            if agent not in known:
                return ToolResult.failure(
                    "agent_not_found", f"Unknown subagent: {agent}"
                )
            if not prompt.strip():
                return ToolResult.failure(
                    "invalid_prompt", "Subagent prompt cannot be empty"
                )
            try:
                job = await registry.create(
                    kind=JobKind.SUBAGENT,
                    metadata={
                        "agent": agent,
                        "command": f"{agent}: {_preview(prompt, _MAX_PROMPT_PREVIEW)}",
                    },
                    name=name,
                )
            except JobRegistryClosed:
                return ToolResult.failure(
                    "session_closing", "Session is closing"
                )
            registry.start(
                job.id,
                SubagentRunner(runtime=runtime, agent=agent, prompt=prompt),
            )
            return ToolResult.success(
                f"Started {job.id}",
                data={"id": job.id, "status": job.status.value},
            )

        async def list_subagents(status: str | None = None) -> ToolResult:
            """List subagent jobs with lightweight metadata only.

            Returns IDs, names, statuses, and elapsed time — never prompts or
            responses. Use ``read_subagent`` to read a completed response.

            Args:
                status: Optional filter: pending, running, completed, failed, cancelled.
            """
            status_filter = _parse_status(status)
            summaries = registry.list(
                kind=JobKind.SUBAGENT, status=status_filter
            )
            return ToolResult.success(
                f"{len(summaries)} subagent job(s)",
                data={"subagents": [item.to_dict() for item in summaries]},
            )

        async def wait_subagent(
            ids: list[str] | None = None,
            mode: str = "all",
            timeout_ms: int | None = None,
        ) -> ToolResult:
            """Wait for subagent jobs to reach a terminal state.

            Returns only IDs and statuses, never responses; use
            ``read_subagent`` for the final text.

            Args:
                ids: Subagent job IDs to wait for. Omit to wait for any subagent
                    owned by this session.
                mode: ``all`` waits for every listed job; ``any`` returns on the first.
                timeout_ms: Optional maximum wait time in milliseconds.
            """
            if mode not in {"all", "any"}:
                return ToolResult.failure(
                    "invalid_mode", "mode must be 'all' or 'any'"
                )
            resolved = ids or [
                job.id for job in registry.all() if job.kind is JobKind.SUBAGENT
            ]
            if not resolved:
                return ToolResult.failure(
                    "subagent_not_found", "No subagent jobs to wait for"
                )
            try:
                result = await registry.wait(
                    resolved,
                    mode=mode,
                    timeout=(timeout_ms / 1000) if timeout_ms is not None else None,
                )
            except JobNotFound:
                return ToolResult.failure(
                    "subagent_not_found", "Unknown subagent job id"
                )
            return ToolResult.success(
                "Wait complete",
                data=result.to_dict(),
            )

        async def read_subagent(
            id: str,
            cursor: int | None = None,
            max_chars: int = 8000,
        ) -> ToolResult:
            """Read the final response from one subagent job.

            Continue reading by passing the returned ``next_cursor``. Reading
            never changes the job's status.

            Args:
                id: Subagent job ID returned by spawn_subagent.
                cursor: Character offset to start reading from.
                max_chars: Maximum characters to return (default 8000).
            """
            job = registry.get_or_none(id)
            if job is None or job.kind is not JobKind.SUBAGENT:
                return ToolResult.failure(
                    "subagent_not_found", f"Unknown subagent job: {id}"
                )
            store = job.result.output_store if job.result is not None else None
            if store is None:
                return ToolResult.success(
                    "No response captured yet",
                    data={"content": "", "next_cursor": None, "eof": False},
                )
            chunk = await store.read(cursor=cursor, max_bytes=max_chars)
            return ToolResult.success(
                chunk.data,
                data={
                    "content": chunk.data,
                    "next_cursor": chunk.next_cursor,
                    "eof": chunk.eof,
                    "truncated": chunk.truncated,
                },
            )

        async def cancel_subagent(id: str) -> ToolResult:
            """Cancel one subagent job (idempotent).

            Args:
                id: Subagent job ID returned by spawn_subagent.
            """
            job = registry.get_or_none(id)
            if job is None or job.kind is not JobKind.SUBAGENT:
                return ToolResult.failure(
                    "subagent_not_found", f"Unknown subagent job: {id}"
                )
            result = await registry.cancel(id)
            return ToolResult.success(
                f"Subagent {id} {result.status}",
                data=result.to_dict(),
            )

        ctx.tools.register(
            Tool.from_function(spawn_subagent, name="spawn_subagent"),
            options=ToolRegistrationOptions(
                sandbox_mode="host",
                namespace="plugin:agents",
                timeout_seconds=self._timeout_seconds,
            ),
        )
        for function in (
            list_subagents,
            wait_subagent,
            read_subagent,
            cancel_subagent,
        ):
            ctx.tools.register(
                Tool.from_function(function),
                options=ToolRegistrationOptions(
                    sandbox_mode="host",
                    namespace="plugin:agents",
                ),
            )


def _parse_status(value: str | None) -> JobStatus | None:
    if value is None:
        return None
    try:
        return JobStatus(value)
    except ValueError:
        return None


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


def _preview(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}[truncated]"


plugin = AgentsPlugin()
