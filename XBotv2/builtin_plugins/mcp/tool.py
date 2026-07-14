"""MCPTool — wraps an MCP server tool as an Tool-compatible callable."""

from __future__ import annotations

from typing import Any

from xbotv2.api import Tool, ToolError, ToolResult


class MCPTool:
    def __init__(self, client: Any, server: str, tool_def: dict[str, Any]) -> None:
        self._client = client
        self._server = server
        self._name = tool_def["name"]
        self._description = str(tool_def.get("description", ""))
        self._parameters = dict(tool_def["inputSchema"])
        self.__doc__ = self._description

    def as_tool(self, registered_name: str) -> Tool:
        return Tool(
            name=registered_name,
            description=self._description,
            function=self,
            parameters=self._parameters,
        )

    async def __call__(self, **kwargs: Any) -> ToolResult:
        result = await self._client.call_tool(self._server, self._name, dict(kwargs))
        if result.is_error:
            return ToolResult(
                status="error",
                content=result.content,
                data=result.data,
                error=ToolError("mcp_tool_error", result.content),
            )
        return ToolResult.success(result.content, data=result.data)
