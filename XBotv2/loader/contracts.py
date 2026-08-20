"""Public reload operation and composition input owned by Loader."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from XBotv2.core.operations import EmptyRequest, Operation


@dataclass(frozen=True, slots=True)
class ReloadPlan:
    config_path: Path
    variables: dict[str, object]


@dataclass(frozen=True, slots=True)
class Reloaded:
    reloaded: tuple[str, ...]
    errors: tuple[str, ...]
    provider: str
    model: str
    model_mode: str
    context_window: int


SOFT_RELOAD = "loader/soft-reload"


@dataclass(slots=True)
class SoftReload:
    scope: Literal["system", "agents"]
    config_path: Path | None = None
    variables: dict[str, object] = field(default_factory=dict)
    reloaded: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


RELOAD_PLUGINS = Operation(
    "loader/reload",
    EmptyRequest,
    Reloaded,
    exclusive=True,
)


__all__ = [
    "RELOAD_PLUGINS",
    "SOFT_RELOAD",
    "ReloadPlan",
    "Reloaded",
    "SoftReload",
]
