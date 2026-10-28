"""Public context component contracts owned by Context Builder."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import AsyncExitStack
from typing import Literal, Protocol, TypeAlias
from xcore.api.services import ServiceKey
from XBotv2.core.api.messages import ConversationMessage
from XBotv2.core.api.artifacts import ArtifactStorePort
from XBotv2.core.api.provider import ProviderMessage

PromptStage = Literal[
    "system_prefix",
    "system_instructions",
    "system_rules",
    "context_suffix",
]


class PromptComponentRegistry(Protocol):
    def register_component(self, owner: str, component: "PromptComponent") -> None: ...

    def unregister_owner(self, owner: str) -> None: ...


@dataclass(frozen=True, slots=True)
class InlinePromptComponent:
    stage: PromptStage
    source: str
    text: str


@dataclass(frozen=True, slots=True)
class FilePromptComponent:
    stage: PromptStage
    source: str
    logical_path: str
    text: str


@dataclass(frozen=True, slots=True)
class HistoryComponent:
    message: ConversationMessage


PromptComponent: TypeAlias = InlinePromptComponent | FilePromptComponent
ContextComponent: TypeAlias = PromptComponent | HistoryComponent


@dataclass(slots=True)
class BuiltContext:
    components: list[ContextComponent]


class ContextCompiler(Protocol):
    """Compile canonical context components for model requests."""
    async def pin_components(
        self, built_context: BuiltContext, resources: AsyncExitStack, *,
        artifacts: ArtifactStorePort | None = None,
    ) -> None: ...

    def messages_from_components(
        self, built_context: BuiltContext, *,
        artifacts: ArtifactStorePort | None = None,
    ) -> list[ProviderMessage]: ...


class ContextBuilderService(PromptComponentRegistry, ContextCompiler, Protocol):
    """The registered builder supports prompt registration and compilation."""


CONTEXT_BUILDER: ServiceKey[ContextBuilderService] = ServiceKey("context_builder")
