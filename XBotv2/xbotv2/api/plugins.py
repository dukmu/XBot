"""Stable contracts for XBotv2 plugins (migrated to the XCore model).

A plugin is a :class:`PluginBase` subclass whose lifecycle is hosted by an
XCore context: the loader calls ``on_load(config)``, then mounts the plugin
through ``ctx.plugin(adapter, config)`` which runs ``apply(ctx)`` as the
plugin body.  ``apply`` receives the XCore context with XBotv2 capabilities
registered as services (``ctx.tools`` / ``ctx.commands`` / ``ctx.prompts`` /
``ctx.agents`` / ``ctx.state`` / ``ctx.jobs`` / ``ctx.variables`` / ...).
Anything registered through those services is undone automatically when the
plugin is unloaded (fiber effects); ``on_unload`` runs as a disposer.

Per-plugin persisted state is ``ctx.state.namespace(manifest.name)`` -- an
async ``get/set/delete/all/clear`` store that satisfies :class:`PluginStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xbotv2.api.commands import Command
from xbotv2.api.hooks import HookStage
from xbotv2.api.context import PromptFragmentStage


class PluginConfigError(ValueError):
    """Raised when configured plugin values do not match the manifest schema."""

    def __init__(self, plugin_name: str, path: tuple[str | int, ...], message: str) -> None:
        self.plugin_name = plugin_name
        self.path = path
        self.validation_message = message
        location = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in path
        )
        super().__init__(f"Plugin {plugin_name!r} config at {location}: {message}")


class HookDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    handler: str

    @field_validator("stage")
    @classmethod
    def _validate_stage(cls, value: str) -> str:
        HookStage(value)
        return value


class ToolDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handler: str
    sandbox_mode: Literal["host", "sandboxed"] = "host"


@dataclass(frozen=True, slots=True)
class ToolRegistrationOptions:
    """Public setup-time options for registering one plugin tool."""

    sandbox_mode: Literal["host", "sandboxed"] = "host"
    namespace: str | None = None
    model_visible: bool = True
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.sandbox_mode not in {"host", "sandboxed"}:
            raise ValueError("sandbox_mode must be 'host' or 'sandboxed'")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


class PromptFragmentDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: PromptFragmentStage
    file: str | None = Field(default=None, min_length=1)
    handler: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_source(self) -> "PromptFragmentDeclaration":
        if (self.file is None) == (self.handler is None):
            raise ValueError("prompt fragment requires exactly one of file or handler")
        return self


class PluginManifest(BaseModel):
    """Validated contents of a plugin's ``plugin.yaml`` file."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    version: str
    description: str = ""
    api_version: Literal["1"] = "1"
    depends_on: list[str] = Field(default_factory=list)
    hooks: list[HookDeclaration] = Field(default_factory=list)
    tools: list[ToolDeclaration] = Field(default_factory=list)
    prompt_fragments: list[PromptFragmentDeclaration] = Field(default_factory=list)
    config_schema: dict[str, Any] | None = None
    plugin_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _validate_config_schema(self) -> "PluginManifest":
        if self.config_schema is not None:
            try:
                Draft202012Validator.check_schema(self.config_schema)
            except SchemaError as exc:
                raise ValueError(f"config_schema is invalid: {exc.message}") from exc
        return self

    def validate_config(self, config: dict[str, Any]) -> None:
        """Validate configured values without applying schema defaults."""
        if self.config_schema is None:
            return
        validator = Draft202012Validator(self.config_schema)
        errors = sorted(
            validator.iter_errors(config),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if not errors:
            return
        error = errors[0]
        raise PluginConfigError(
            self.name,
            tuple(error.absolute_path),
            error.message,
        )


class PluginStore(Protocol):
    """Per-plugin persisted key-value state (``ctx.state.namespace(name)``)."""

    async def get(self, key: str, default: Any = None) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def all(self) -> dict[str, Any]: ...
    async def clear(self) -> None: ...


class RuntimePluginContext(Protocol):
    """Capabilities available to a plugin hook at runtime (``ctx.plugin_runtime``)."""

    def register_tool(
        self,
        tool: Any,
        options: ToolRegistrationOptions | None = None,
    ) -> str: ...
    def unregister_tool(self, registered_name: str) -> bool: ...
    def register_command(self, command: Command) -> str: ...
    def unregister_command(self, name: str) -> bool: ...


class PluginBase:
    """Base class for XBotv2 plugins hosted on an XCore context.

    Lifecycle:

    1. ``on_load(config)`` -- validate configuration and initialize external
       resources (loader calls this before mounting).
    2. ``apply(ctx)`` -- the plugin body: register hooks, tools, commands,
       prompt fragments, agents through ``ctx`` services.  May be async.
       Everything registered is undone automatically on unload.
    3. ``on_unload()`` -- optional cleanup for resources the loader cannot
       track (registered as a disposer by the bridge).
    """

    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        # Bound by the plugin adapter before ``apply`` runs.
        self.ctx: Any = None
        self.store: PluginStore | None = None

    async def on_load(self, config: dict[str, Any]) -> None:
        """Validate configuration and initialize external resources."""

    async def on_unload(self) -> None:
        """Release resources created by ``on_load``."""

    def apply(self, ctx: Any) -> None:
        """Register the plugin's extensions on the XCore context."""

    async def status_slots(self) -> dict[str, str]:
        """Return compact, human-facing runtime status values."""
        return {}

    def diagnostics(self) -> dict[str, Any]:
        return {"status": "ready"}


__all__ = [
    "HookDeclaration",
    "PluginBase",
    "PluginConfigError",
    "PluginManifest",
    "PluginStore",
    "PromptFragmentDeclaration",
    "RuntimePluginContext",
    "ToolDeclaration",
    "ToolRegistrationOptions",
]
