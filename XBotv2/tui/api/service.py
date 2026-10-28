"""Foreground terminal client capability."""

from typing import Protocol
from xcore.api.services import ServiceKey


class TerminalClient(Protocol):
    """Async foreground client provided by the selected client plugin."""

    async def run(self) -> None: ...

    def request_stop(self, reason: str) -> None: ...


TERMINAL_CLIENT = ServiceKey[TerminalClient]("terminal_client")
