"""Public context component contracts owned by Context Builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

from XBotv2.core.messages import ConversationMessage

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


__all__ = [
    "BuiltContext",
    "ContextComponent",
    "FilePromptComponent",
    "HistoryComponent",
    "InlinePromptComponent",
    "PromptComponent",
    "PromptComponentRegistry",
    "PromptStage",
]
