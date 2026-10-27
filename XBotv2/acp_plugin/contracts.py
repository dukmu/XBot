"""Public launch declarations owned by the ACP carrier."""

from __future__ import annotations

from dataclasses import dataclass
from XBotv2.core.api.providers import Provider


@dataclass(frozen=True, slots=True)
class ACPLaunch:
    provider_name: str | None
    no_plugins: bool
    selected_agent: str | None = None
    llm_override: Provider | None = None


__all__ = ["ACPLaunch"]
