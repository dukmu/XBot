"""Public launch declarations owned by the ACP carrier."""

from __future__ import annotations

from dataclasses import dataclass

from XBotv2.core.providers import BaseProvider


@dataclass(frozen=True, slots=True)
class ACPLaunch:
    provider_name: str
    no_plugins: bool
    selected_agent: str | None = None
    llm_override: BaseProvider | None = None


__all__ = ["ACPLaunch"]
