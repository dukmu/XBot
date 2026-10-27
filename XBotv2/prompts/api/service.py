"""Public prompt contribution contracts."""

from typing import Protocol

from xcore.api.services import ServiceKey

from XBotv2.context_builder.contracts import PromptComponent


class PromptsPort(Protocol):
    def add(self, component: PromptComponent) -> None: ...


PROMPTS = ServiceKey[PromptsPort]("prompts")
