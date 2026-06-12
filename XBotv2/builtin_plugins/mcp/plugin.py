"""MCPPlugin — connects MCP servers, registers tools in ToolRegistry via ON_SESSION_INIT hook."""

from __future__ import annotations

from typing import Any

from xbotv2.hooks.manager import HookManager
from xbotv2.hooks.types import HookContext, HookStage
from xbotv2.plugin.base import PluginBase
from xbotv2.plugin.manifest import PluginManifest
from xbotv2.plugin.store import PluginStore
from xbotv2.tools.types import XBotTool

from .client import MCPClient, MCPConnectionError
from .tool import MCPTool

import logging

logger = logging.getLogger("xbotv2.mcp")


class MCPPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._client = MCPClient()
        self._config: dict[str, Any] = {}

    async def on_load(self, config: dict[str, Any]) -> None:
        self._config = config

    def register_hooks(self, manager: HookManager) -> None:
        manager.register(HookStage.ON_SESSION_INIT, self._on_session_init)
        manager.register(HookStage.ON_SESSION_CLOSE, self._on_session_close)

    async def _on_session_init(self, ctx: HookContext) -> None:
        servers = self._config.get("servers", {})
        if not servers:
            return
        for server_name, server_cfg in servers.items():
            if not isinstance(server_cfg, dict):
                continue
            if not server_cfg.get("enabled", True):
                continue
            try:
                tools = await self._client.connect_and_list(server_name, server_cfg)
            except MCPConnectionError:
                logger.warning("MCP server %s unavailable, skipping", server_name)
                continue
            for tool_def in tools:
                mcp_tool = MCPTool(self._client, server_name, tool_def)
                tool_name = f"mcp__{server_name}__{tool_def['name']}"
                xbot_tool = XBotTool.from_function(mcp_tool, name=tool_name)
                try:
                    ctx.tools.register(xbot_tool, sandbox_mode="host", owner_plugin=self.manifest.name, namespace=f"mcp:{server_name}")
                except Exception:
                    logger.warning("MCP tool %s registration failed", tool_name)

    async def _on_session_close(self, ctx: HookContext) -> None:
        await self._client.disconnect_all()
