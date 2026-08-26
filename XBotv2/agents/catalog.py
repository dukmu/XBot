"""Fiber-owned implementation of the Agent definition catalog."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from xcore import bound_effect, current_plugin_name

from XBotv2.agents.loader import load_definitions
from XBotv2.agents.contracts import AgentDefinition
from XBotv2.core.variables import RuntimeVariables


class AgentCatalog:
    """Store immutable definitions in base and workspace-overlay layers."""

    def __init__(self) -> None:
        self._base: dict[str, AgentDefinition] = {}
        self._base_owners: dict[str, str] = {}
        self._overlay: dict[str, AgentDefinition] = {}
        self._overlay_owners: dict[str, str] = {}

    def register(
        self,
        definition: AgentDefinition,
        *,
        overlay: bool = False,
    ) -> str:
        owner = current_plugin_name()
        layer = self._overlay if overlay else self._base
        owners = self._overlay_owners if overlay else self._base_owners
        if definition.name in layer:
            raise ValueError(f"Agent {definition.name!r} is already registered")
        layer[definition.name] = definition
        owners[definition.name] = owner
        bound_effect(partial(self._unregister, definition.name, owner=owner))
        return definition.name

    def register_markdown(
        self,
        directory: Path,
        *,
        variables: RuntimeVariables | None = None,
        overlay: bool = True,
        owner: str | None = None,
    ) -> tuple[str, ...]:
        bind_cleanup = owner is None
        owner = owner or current_plugin_name()
        names = tuple(
            self._register(definition, owner=owner, overlay=overlay)
            for definition in load_definitions(directory, variables)
        )
        if names and bind_cleanup:
            bound_effect(partial(self._unregister_many, names, owner))
        return names

    def unregister_owned(
        self,
        owner: str | None = None,
        *,
        overlay: bool = True,
    ) -> list[str]:
        owner = owner or current_plugin_name()
        layer = self._overlay if overlay else self._base
        owners = self._overlay_owners if overlay else self._base_owners
        removed = [name for name, registered_owner in owners.items() if registered_owner == owner]
        for name in removed:
            owners.pop(name, None)
            layer.pop(name, None)
        return removed

    def get(self, name: str) -> AgentDefinition | None:
        return self._overlay.get(name) or self._base.get(name)

    def definitions(self) -> tuple[AgentDefinition, ...]:
        merged = dict(self._base)
        merged.update(self._overlay)
        return tuple(merged.values())

    def _register(
        self,
        definition: AgentDefinition,
        *,
        owner: str,
        overlay: bool,
    ) -> str:
        layer = self._overlay if overlay else self._base
        owners = self._overlay_owners if overlay else self._base_owners
        if definition.name in layer:
            raise ValueError(f"Agent {definition.name!r} is already registered")
        layer[definition.name] = definition
        owners[definition.name] = owner
        return definition.name

    def _unregister(self, name: str, *, owner: str) -> bool:
        for layer, owners in (
            (self._overlay, self._overlay_owners),
            (self._base, self._base_owners),
        ):
            if owners.get(name) == owner:
                owners.pop(name, None)
                layer.pop(name, None)
                return True
        return False

    def _unregister_many(self, names: tuple[str, ...], owner: str) -> None:
        for name in names:
            self._unregister(name, owner=owner)


__all__ = ["AgentCatalog"]
