"""Typed lifecycle events owned by context construction."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import AsyncExitStack
from xcore.api.events import EventKey
from XBotv2.context_builder.api.contracts import BuiltContext
from typing import TYPE_CHECKING
from XBotv2.core.api.domain import UserContext
from XBotv2.core.api.domain import ResolvedRuntimeSelection
from XBotv2.core.api.variables import RuntimeVariables

if TYPE_CHECKING:
    from XBotv2.core.api.messages import ConversationMessage


BUILD_CONTEXT: EventKey[ContextBuildRequest] = EventKey("context/build")
CONTEXT_BUILD_INPUTS_READY: EventKey[ContextBuildRequest] = EventKey("context/inputs-ready")
CONTEXT_COMPONENTS_BUILT: EventKey[BuiltContext] = EventKey("after/context-components-build")


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
    resources: AsyncExitStack
