"""Typed lifecycle events owned by context construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from XBotv2.config.contracts import UserContext
from XBotv2.core.domain import ResolvedRuntimeSelection
from XBotv2.core.variables import RuntimeVariables

if TYPE_CHECKING:
    from XBotv2.core.messages import ConversationMessage


BUILD_CONTEXT = "context/build"
CONTEXT_BUILD_INPUTS_READY = "context/inputs-ready"
CONTEXT_COMPONENTS_BUILT = "after/context-components-build"


@dataclass(slots=True)
class ContextBuildRequest:
    """Provider-neutral inputs for one context build."""

    history: tuple[ConversationMessage, ...]
    runtime_selection: ResolvedRuntimeSelection
    user_identity: UserContext
    memory: str
    sandbox_summary: str
    runtime_paths: RuntimeVariables
    turn: int


__all__ = [
    "BUILD_CONTEXT",
    "CONTEXT_BUILD_INPUTS_READY",
    "CONTEXT_COMPONENTS_BUILT",
    "ContextBuildRequest",
]
