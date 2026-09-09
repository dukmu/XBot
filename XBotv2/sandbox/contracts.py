"""Public sandbox capability contracts."""

from typing import Protocol

from pydantic import JsonValue


class SandboxPort(Protocol):
    enabled: bool
    network: bool

    async def filesystem(
        self,
        operation: str,
        args: dict[str, JsonValue],
    ) -> str: ...

    def resolve_filesystem_args(
        self,
        operation: str,
        args: dict[str, JsonValue],
    ) -> dict[str, JsonValue]: ...


__all__ = ["SandboxPort"]