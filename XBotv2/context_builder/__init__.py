"""Public declarations for the model-facing Context Builder plugin."""

from XBotv2.context_builder.contracts import ContextComponent, PromptFragmentStage
from XBotv2.context_builder.events import (
    BEFORE_CONTEXT_BUILD,
    BUILD_CONTEXT,
    CONTEXT_BUILT,
    CONTEXT_COMPONENTS_BUILT,
    ContextBuildRequest,
    ContextBuilt,
    ContextComponentsBuilt,
)

__all__ = [
    "BEFORE_CONTEXT_BUILD",
    "BUILD_CONTEXT",
    "CONTEXT_BUILT",
    "CONTEXT_COMPONENTS_BUILT",
    "ContextBuildRequest",
    "ContextBuilt",
    "ContextComponent",
    "ContextComponentsBuilt",
    "PromptFragmentStage",
]
