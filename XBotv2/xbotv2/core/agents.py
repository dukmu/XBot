"""Ownership-aware registry for plugin-defined agents."""

from __future__ import annotations

from xbotv2.api.agents import AgentDefinition


class AgentRegistry:
    """Stores immutable Agent definitions under one plugin owner."""

    def __init__(self) -> None:
        self._definitions: dict[str, AgentDefinition] = {}
        self._owners: dict[str, str] = {}

    def register(self, definition: AgentDefinition, *, owner: str) -> str:
        if definition.name in self._definitions:
            raise ValueError(f"Agent {definition.name!r} is already registered")
        self._definitions[definition.name] = definition
        self._owners[definition.name] = owner
        return definition.name

    def unregister(self, name: str, *, owner: str) -> bool:
        if self._owners.get(name) != owner:
            return False
        self._owners.pop(name, None)
        self._definitions.pop(name, None)
        return True

    def get(self, name: str) -> AgentDefinition | None:
        return self._definitions.get(name)

    def definitions(self) -> tuple[AgentDefinition, ...]:
        return tuple(self._definitions.values())


__all__ = ["AgentRegistry"]
