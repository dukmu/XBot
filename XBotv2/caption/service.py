"""Session captioning service: auto-title and the agent-facing tool."""

from __future__ import annotations

import logging
from uuid import uuid4

from xcore import Context
from XBotv2.agentloop.events import BeforeContextBuild

from XBotv2.caption.contracts import (
    CaptionConfig,
    CaptionRequest,
    CaptionResult,
    CaptionTitleError,
)
from XBotv2.core.messages import HumanInputMessage
from XBotv2.core.parts import TextPart
from XBotv2.core.provider import ModelRequest, ProviderMessage, ProviderSystem, ProviderUser
from XBotv2.core.metadata import ThreadMetadataState
from XBotv2.core.domain import AuxiliaryRequest, RequestObservation
from XBotv2.core.tokens import estimate_request_tokens
from XBotv2.llm import invoke_llm
from XBotv2.llm.contracts import ModelPort
from XBotv2.usage import UsagePort

logger = logging.getLogger("xbotv2.caption")

_SYSTEM = (
    "You derive one short, human-readable title for a chat session. "
    "Return only the title text on a single line: no quotes, no explanation, "
    "and no reasoning or thinking output."
)


def caption_request(
    request: CaptionRequest,
    max_chars: int,
) -> tuple[ProviderMessage, ...]:
    latest = " ".join(
        "".join(part.text for part in message.parts if isinstance(part, TextPart))
        for message in request.messages
        if isinstance(message, HumanInputMessage)
    ).strip()
    preview = latest or "a new conversation"
    if len(preview) > 2400:
        preview = preview[:2400] + "…"
    user = (
        "Title this conversation in at most "
        f"{max_chars} characters. Current title: {request.current_title!r}. "
        f"Conversation: {preview!r}"
    )
    return (
        ProviderSystem(parts=(TextPart(text=_SYSTEM),)),
        ProviderUser(parts=(TextPart(text=user),)),
    )


def _fallback_title(request: CaptionRequest, max_chars: int) -> str:
    """A deterministic caption when the auxiliary model returns no text.

    Thinking models routinely spend a small output budget on reasoning and
    emit no content, which would leave sessions untitled. The first human
    message is the best signal the session already holds.
    """
    latest = " ".join(
        "".join(part.text for part in message.parts if isinstance(part, TextPart))
        for message in request.messages
        if isinstance(message, HumanInputMessage)
    ).strip()
    if not latest:
        return ""
    title = " ".join(latest.split())
    if len(title) > max_chars:
        title = title[: max_chars - 1].rstrip() + "…"
    return title


def _clean_title(raw: str, max_chars: int) -> str:
    title = " ".join(raw.split()).strip(" \n\"'")
    title = title.replace("\n", " ").strip()
    if len(title) > max_chars:
        title = title[: max_chars - 1].rstrip() + "…"
    return title


class CaptionService:
    """Owns the caption lifecycle for one session runtime."""

    def __init__(
        self,
        *,
        events: Context,
        model: ModelPort,
        state: ThreadMetadataState,
        usage: UsagePort,
        session_id: str,
        config: CaptionConfig,
        is_subagent: bool = False,
    ) -> None:
        self._events = events
        self.model = model
        self.state = state
        self._usage = usage
        self._session_id = session_id
        self.config = config
        # Subagent threads are named by their parent session, never here.
        self._is_subagent = is_subagent
        self._captioned = False
        self._last_attempted_turn: int | None = None

    @property
    def title(self) -> str:
        return self.state.value.title

    def _needs_caption(self, event: BeforeContextBuild) -> bool:
        if self._captioned or self._is_subagent:
            return False
        if self._last_attempted_turn == event.request.turn:
            return False
        # A session starts with its id as the provisional title. Keep retrying
        # after a transient provider failure until a title is actually applied.
        if self.state.value.title != self._session_id:
            return False
        return any(
            isinstance(message, HumanInputMessage)
            for message in event.request.history
        )

    async def _on_before_context(self, event: BeforeContextBuild) -> None:
        """Try one independent caption request per turn until one succeeds."""
        if not self.config.auto or not self._needs_caption(event):
            return
        self._last_attempted_turn = event.request.turn
        caption = CaptionRequest(
            messages=tuple(event.request.history),
            current_title=self.title,
        )
        request = caption_request(
            caption,
            self.config.max_chars,
        )
        selection = self.state.value.runtime_selection.model
        selection = selection.model_copy(update={
            "generation": selection.generation.model_copy(update={
                "max_output_tokens": self.config.output_tokens,
            }),
        })
        model_request = ModelRequest(
            messages=request,
            tools=(),
            selection=selection,
        )
        try:
            response = await invoke_llm(self.model, model_request)
        except Exception:  # noqa: BLE001 — a caption must never break the turn
            logger.exception("caption.request.failed")
            return
        await self._usage.record(
            RequestObservation(
                selection=selection,
                purpose=AuxiliaryRequest(
                    owner="caption",
                    operation_id=uuid4().hex,
                ),
                estimated_input_tokens=estimate_request_tokens(
                    model_request.messages,
                    model_request.tools,
                ),
                observed_context=response.observed_context,
            ),
            response.usage,
        )
        text = "".join(
            part.text for part in response.parts if isinstance(part, TextPart)
        )
        title = _clean_title(text, self.config.max_chars)
        if not title:
            title = _fallback_title(
                caption,
                self.config.max_chars,
            )
        if title:
            # Only a successfully applied title disables future attempts; a
            # transient provider failure leaves _captioned False so the next
            # turn retries instead of permanently disabling auto-title.
            await self._apply_title(CaptionResult(title=title))

    async def _apply_title(self, result: CaptionResult) -> CaptionResult:
        await self.state.replace_title(result.title)
        self._captioned = True
        logger.info("caption.applied")
        return result

    async def caption_get(self) -> CaptionResult:
        return CaptionResult(title=self.title)

    async def caption_set(self, title: str) -> CaptionResult:
        cleaned = _clean_title(title, self.config.max_chars)
        if not cleaned:
            raise CaptionTitleError("Caption title must contain visible text.")
        return await self._apply_title(CaptionResult(title=cleaned))
