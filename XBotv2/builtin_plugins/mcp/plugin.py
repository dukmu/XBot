"""MCPPlugin — connects MCP servers, registers tools in ToolRegistry via ON_SESSION_INIT hook."""

from __future__ import annotations

from typing import Any

from xbotv2.api import (
    HookContext,
    HookStage,
    PluginBase,
    PluginManifest,
    PluginSetupContext,
    PluginStore,
    XBotTool,
)

from .client import MCPClient, MCPConnectionError
from .tool import MCPTool

import logging

logger = logging.getLogger("xbotv2.mcp")


class MCPPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._client = MCPClient()
        self._config: dict[str, Any] = {}
        self._server_status: dict[str, dict[str, Any]] = {}

    async def on_load(self, config: dict[str, Any]) -> None:
        self._config = config

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.ON_SESSION_INIT, self._on_session_init)
        ctx.register_hook(HookStage.ON_SESSION_CLOSE, self._on_session_close)

    async def _on_session_init(self, ctx: HookContext) -> None:
        servers = self._config.get("servers", {})
        if not servers:
            return
        for server_name, server_cfg in servers.items():
            if not isinstance(server_cfg, dict):
                continue
            if not server_cfg.get("enabled", True):
                self._server_status[server_name] = {"status": "disabled"}
                continue
            try:
                tools = await self._client.connect_and_list(server_name, server_cfg)
            except MCPConnectionError as exc:
                self._server_status[server_name] = {
                    "status": "error",
                    "error": str(exc),
                }
                logger.error("MCP server %s unavailable: %s", server_name, exc)
                if server_cfg.get("required", False):
                    raise
                continue
            registered = 0
            for tool_def in tools:
                mcp_tool = MCPTool(self._client, server_name, tool_def)
                tool_name = f"mcp__{server_name}__{tool_def['name']}"
                xbot_tool = XBotTool.from_function(mcp_tool, name=tool_name)
                try:
                    ctx.tools.register(xbot_tool, sandbox_mode="host", owner_plugin=self.manifest.name, namespace=f"mcp:{server_name}")
                    registered += 1
                except Exception as exc:
                    logger.error("MCP tool %s registration failed: %s", tool_name, exc)
                    self._server_status[server_name] = {
                        "status": "error",
                        "error": f"Tool registration failed for {tool_name}: {exc}",
                    }
            self._server_status.setdefault(server_name, {
                "status": "ready",
                "tools": registered,
            })

    async def _on_session_close(self, ctx: HookContext) -> None:
        await self._client.disconnect_all()

    def diagnostics(self) -> dict[str, Any]:
        statuses = list(self._server_status.values())
        return {
            "status": "degraded" if any(s.get("status") == "error" for s in statuses) else "ready",
            "servers": dict(self._server_status),
        }
