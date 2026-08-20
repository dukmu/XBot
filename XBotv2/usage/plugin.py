"""Cumulative model-usage accounting, independent of the agent loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from XBotv2.application import APPLICATION_INITIALIZED, ApplicationInitialized
from XBotv2.agentloop import EventContext, Events
from XBotv2.core.filesystem.atomic import write_text_atomic


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

    def __init__(self, path: Path) -> None:
        self._path = path
        self._has_snapshot = path.exists()
        stored = self._read()
        if stored:
            self._usage = {
                key: int(stored.get(key) or 0)
                for key in _FIELDS
            }
        else:
            self._usage = _empty()

    def initialize(self, messages: list[Any]) -> None:
        """Rebuild a missing snapshot after optional state hydration settles."""
        if self._has_snapshot:
            return
        for message in messages:
            self.add(getattr(message, "usage_metadata", None), persist=False)
        self._persist()
        self._has_snapshot = True

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
        write_text_atomic(
            self._path,
            yaml.safe_dump(self._usage, allow_unicode=True, sort_keys=False),
        )

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        loaded = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError("Persisted usage must be a mapping")
        return loaded


class UsageComponent:
    inject = ["thread_paths", "loop_state"]
    name = "xbot.usage"

    def apply(self, ctx: Any, config: Any = None) -> None:
        service = UsageService(
            ctx.thread_paths.usage_file,
        )
        ctx.set("usage", service)

        async def initialize(_event: ApplicationInitialized) -> None:
            service.initialize(ctx.loop_state.messages)

        async def record(event: EventContext) -> None:
            usage = getattr(event.model_response, "usage_metadata", None)
            service.add(usage)

        ctx.on(Events.AFTER_MODEL_RESPONSE, record)
        ctx.on(APPLICATION_INITIALIZED, initialize, prepend=True)


plugin = UsageComponent()
