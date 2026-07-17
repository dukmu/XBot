"""Public context component contracts for prompt-building plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from xbotv2.api.messages import Message

PromptFragmentStage = Literal[
    "system_prefix",
    "system_instructions",
    "system_rules",
    "context_suffix",
]


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


__all__ = ["ContextComponent", "PromptFragmentStage"]
