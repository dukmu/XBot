"""Public declarations for the model-facing Context Builder plugin."""

from XBotv2.context_builder.contracts import (
    BuiltContext,
    ContextComponent,
    FilePromptComponent,
    HistoryComponent,
    InlinePromptComponent,
    PromptComponent,
    PromptStage,
)
from XBotv2.context_builder.events import (
    BUILD_CONTEXT,
    CONTEXT_BUILD_INPUTS_READY,
    CONTEXT_COMPONENTS_BUILT,
    ContextBuildRequest,
)

__all__ = [
    "BUILD_CONTEXT",
    "CONTEXT_BUILD_INPUTS_READY",
    "CONTEXT_COMPONENTS_BUILT",
    "BuiltContext",
    "ContextBuildRequest",
    "ContextComponent",
    "FilePromptComponent",
    "HistoryComponent",
    "InlinePromptComponent",
    "PromptComponent",
    "PromptStage",
]
