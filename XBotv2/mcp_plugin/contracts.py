"""Public configuration and capability contracts for MCP."""

from pydantic import BaseModel, ConfigDict, Field

MCP_PLUGIN_ID = "mcp_plugin"


class MCPServerConfig(BaseModel):
    """One MCP transport declaration.

    Transport-specific options intentionally remain open: the MCP client owns
    the stdio/streamable-HTTP option set and validates it when opening the
    selected transport.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    required: bool = False


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


__all__ = ["MCPConfig", "MCP_PLUGIN_ID", "MCPServerConfig"]
