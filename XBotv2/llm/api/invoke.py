"""Execute a complete auxiliary model request."""
from __future__ import annotations

from XBotv2.core.api.provider import ModelRequest
from XBotv2.core.api.providers import ProviderFailure
from XBotv2.core.api.stream import (
    ModelCancelled, ModelCompleted, ModelFailed, ModelResponse,
    ReasoningDelta, TextDelta, ToolCallDelta,
)
from XBotv2.llm.api.contracts import ModelPort


async def invoke_llm(
    llm: ModelPort,
    request: ModelRequest,
) -> ModelResponse:
    """Run one unbound auxiliary model call and return the merged response.

    This is the single-shot calling convention over any ``ModelPort`` for
    auxiliary requests that never enter the conversation (compaction
    summaries, session captions). The loop's own streaming remains the
    port's ``astream``; providers implement nothing extra for this.
    """
    aggregate: ModelResponse | None = None
    terminal_seen = False
    async for event in llm.astream(request):
        if terminal_seen:
            raise RuntimeError(
                "Model stream produced an event after its terminal event"
            )
        if isinstance(event, ModelCompleted):
            aggregate = event.response
            terminal_seen = True
        elif isinstance(event, ModelFailed):
            raise ProviderFailure(event.error)
        elif isinstance(event, ModelCancelled):
            raise RuntimeError(event.reason)
        elif not isinstance(event, (TextDelta, ReasoningDelta, ToolCallDelta)):
            raise TypeError(f"Unsupported model stream event: {event!r}")
    if aggregate is None:
        raise RuntimeError("Auxiliary model call produced no response")
    return aggregate
