"""Public service Protocols for Agent catalogs and active runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from XBotv2.agents.contracts import AgentCreateOptions, AgentDefinition
from XBotv2.core.variables import RuntimeVariables
from XBotv2.agents.contracts import AgentSelection


class AgentCatalogPort(Protocol):
    def get(self, name: str) -> AgentDefinition | None: ...

    def definitions(self) -> tuple[AgentDefinition, ...]: ...

    def register(
        self,
        definition: AgentDefinition,
        *,
        overlay: bool = False,
    ) -> str: ...

    def register_markdown(
        self,
        directory: Path,
        *,
        variables: RuntimeVariables | None = None,
        overlay: bool = True,
        owner: str | None = None,
    ) -> tuple[str, ...]: ...

    def unregister_owned(
        self,
        owner: str | None = None,
        *,
        overlay: bool = True,
    ) -> list[str]: ...


class AgentRuntimePort(Protocol):
    async def create(self, options: AgentCreateOptions) -> object: ...

    def active_definition(self) -> AgentDefinition | None: ...

    def current_selection(self) -> AgentSelection: ...

    def runtime_config(
        self,
        definition: AgentDefinition | None = None,
    ) -> object: ...

    async def select(self, name: str) -> dict[str, object]: ...

    async def select_provider(
        self,
        name: str,
        model: str | None = None,
    ) -> dict[str, object]: ...

    async def select_effort(self, value: str) -> dict[str, object]: ...


__all__ = ["AgentCatalogPort", "AgentRuntimePort"]
