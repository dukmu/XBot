"""Cumulative model-usage accounting, independent of the agent loop."""

from __future__ import annotations

from typing import Any

from XBotv2.core.events import EventContext, Events


_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "requests",
    "context_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "prompt_cache_write_tokens",
)


def _empty() -> dict[str, int]:
    return {key: 0 for key in _FIELDS}


class UsageService:
    """Own cumulative usage and its plugin-local persisted state."""

    def __init__(self, state_store: Any, messages: list[Any]) -> None:
        self._state_store = state_store
        stored = state_store.get_plugin_state("usage")
        if stored:
            self._usage = {
                key: int(stored.get(key) or 0)
                for key in _FIELDS
            }
        else:
            self._usage = _empty()
            for message in messages:
                self.add(getattr(message, "usage_metadata", None), persist=False)
            self._persist()

    def snapshot(self) -> dict[str, int]:
        return dict(self._usage)

    def add(
        self,
        usage: dict[str, Any] | None,
        *,
        persist: bool = True,
    ) -> bool:
        if not isinstance(usage, dict):
            return False
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        if not input_tokens and not output_tokens:
            return False
        self._usage["input_tokens"] += input_tokens
        self._usage["output_tokens"] += output_tokens
        self._usage["total_tokens"] += int(
            usage.get("total_tokens") or input_tokens + output_tokens
        )
        self._usage["requests"] += int(usage.get("requests") or 1)
        self._usage["context_tokens"] = int(
            usage.get("context_tokens") or input_tokens
        )
        for key in _FIELDS[5:]:
            if usage.get(key) is not None:
                self._usage[key] += int(usage[key])
        if persist:
            self._persist()
        return True

    def _persist(self) -> None:
        self._state_store.set_plugin_state("usage", self._usage)


class UsageComponent:
    inject = ["state_store", "loop_state"]
    name = "xbot.usage"

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = UsageService(ctx.state_store, ctx.loop_state.messages)
        ctx.set("usage", service)

        async def record(event: EventContext) -> None:
            usage = getattr(event.model_response, "usage_metadata", None)
            service.add(usage)

        ctx.on(Events.AFTER_MODEL_RESPONSE, record)


plugin = UsageComponent()
