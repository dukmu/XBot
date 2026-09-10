"""Public context component contracts owned by Context Builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from XBotv2.core.messages import Message

PromptFragmentStage = Literal[
    "system_prefix",
    "system_instructions",
    "system_rules",
    "context_suffix",
]


class PromptFragmentRegistry(Protocol):
    def register_fragment(
        self,
        stage: PromptFragmentStage,
        plugin_name: str,
        text: str,
        *,
        source: str | None = None,
    ) -> None: ...

    def unregister_fragment(
        self,
        stage: PromptFragmentStage,
        plugin_name: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ContextComponent:
    """One source-tagged context section before escaped provider rendering."""

    role: str
    source: str
    content: str
    plugin_name: str | None = None
    stage: PromptFragmentStage | None = None
    source_path: str | None = None
    message: Message | None = None


__all__ = ["ContextComponent", "PromptFragmentStage", "PromptFragmentRegistry"]
