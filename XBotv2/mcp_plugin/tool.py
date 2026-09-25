"""MCPTool — wraps an MCP server tool as an Tool-compatible callable."""

from __future__ import annotations

from pydantic import JsonValue

from XBotv2.core import Tool, ToolError, ToolFailed, ToolOutput, ToolSucceeded, TextPart
from XBotv2.mcp_plugin.mcp_client import MCPClient


class MCPTool:
    def __init__(self, client: MCPClient, server: str, tool_def: dict[str, JsonValue]) -> None:
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

    async def __call__(self, **kwargs: JsonValue) -> ToolSucceeded | ToolFailed:
        result = await self._client.call_tool(self._server, self._name, dict(kwargs))
        if result.is_error:
            return ToolFailed(
                error=ToolError(code="mcp_tool_error", message=result.content),
                output=ToolOutput(),
            )
        return ToolSucceeded(output=ToolOutput(parts=(TextPart(text=result.content),)))
