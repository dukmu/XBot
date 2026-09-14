"""Session captioning service: auto-title and the agent-facing tool."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from xcore import Context
from XBotv2.agentloop.events import EventContext

from XBotv2.caption.contracts import CaptionConfig
from XBotv2.compact.summary import invoke_llm
from XBotv2.core import Message
from XBotv2.core.metadata import ThreadMetadataState
from XBotv2.llm.contracts import ModelPort

logger = logging.getLogger("xbotv2.caption")

_SYSTEM = (
    "You derive one short, human-readable title for a chat session. "
    "Return only the title text, no quotes, no explanation, on a single line."
)


def caption_request(
    conversation: Sequence[Message],
    max_chars: int,
) -> list[Message]:
    latest = " ".join(
        str(message.content)
        for message in conversation
        if message.role == "user"
    ).strip()
    preview = latest or "a new conversation"
    if len(preview) > 2400:
        preview = preview[:2400] + "…"
    user = (
        "Title this conversation in at most "
        f"{max_chars} characters: {preview!r}"
    )
    return [
        Message(role="system", content=_SYSTEM),
        Message(role="user", content=user),
    ]


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
        session_id: str,
        thread_id: str,
        config: CaptionConfig,
        is_subagent: bool = False,
    ) -> None:
        self._events = events
        self.model = model
        self.state = state
        self._session_id = session_id
        self._thread_id = thread_id
        self.config = config
        # Subagent threads are named by their parent session, never here.
        self._is_subagent = is_subagent
        self._captioned = False

    @property
    def title(self) -> str:
        return self.state.value.title

    def caption_available(self) -> bool:
        return self.config.allow_access and not self.state.value.parent_thread_id

    def _is_first_turn(self, ctx: EventContext) -> bool:
        if self._captioned or self._is_subagent:
            return False
        if self.state.value.title:
            return False
        turn_count = ctx.session.turn_count if getattr(ctx.session, "turn_count", None) is not None else 0
        if int(turn_count or 0) > 1:
            return False
        return any(message.role == "user" for message in ctx.messages)

    async def _on_before_context(self, ctx: EventContext) -> None:
        """Fire one independent caption request on the first user message."""
        if not self.config.auto or not self._is_first_turn(ctx):
            return
        self._captioned = True
        request = caption_request(ctx.messages, self.config.max_chars)
        try:
            response = await invoke_llm(
                self.model,
                request,
                output_tokens=self.config.output_tokens,
            )
        except Exception:  # noqa: BLE001 — a caption must never break the turn
            logger.exception("caption.request.failed")
            return
        title = _clean_title(response.content or "", self.config.max_chars)
        if title:
            self._apply_title(title)

    def _apply_title(self, title: str) -> None:
        if not title.strip():
            raise ValueError("Session title must be non-empty")
        if len(title) > 200:
            raise ValueError("Session title must not exceed 200 characters")
        self.state.update(title=title)
        self._captioned = True
        logger.info("caption.applied title=%s", title)

    async def caption_get(self) -> dict[str, str]:
        return {
            "title": self.title,
            "session_id": self._session_id,
            "thread_id": self._thread_id,
        }

    async def caption_set(self, title: str) -> str:
        cleaned = _clean_title(title, self.config.max_chars)
        self._apply_title(cleaned)
        return cleaned
