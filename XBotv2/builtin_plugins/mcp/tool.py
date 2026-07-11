"""MCPTool — wraps an MCP server tool as an Tool-compatible callable."""

from __future__ import annotations

from typing import Any

from xbotv2.api import ToolResult


class MCPTool:
    def __init__(self, client: Any, server: str, tool_def: dict[str, Any]) -> None:
        self._client = client
        self._server = server
        self._name = tool_def["name"]
        self.__doc__ = tool_def.get("description", "")

    async def __call__(self, **kwargs: Any) -> ToolResult:
        content = await self._client.call_tool(self._server, self._name, dict(kwargs))
        return ToolResult.success(content)
