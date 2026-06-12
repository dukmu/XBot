"""MCPTool — wraps an MCP server tool as an XBotTool-compatible callable."""

from __future__ import annotations

from typing import Any


class MCPTool:
    def __init__(self, client: Any, server: str, tool_def: dict[str, Any]) -> None:
        self._client = client
        self._server = server
        self._name = tool_def["name"]
        self.__doc__ = tool_def.get("description", "")

    async def __call__(self, **kwargs: Any) -> str:
        return await self._client.call_tool(self._server, self._name, dict(kwargs))
