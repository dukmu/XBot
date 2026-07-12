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
    RuntimePluginContext,
    ToolRegistrationOptions,
)

from .client import MCPClient
from .tool import MCPTool

import logging

logger = logging.getLogger("xbotv2.mcp")


class MCPPlugin(PluginBase):
    def __init__(self, manifest: PluginManifest, store: PluginStore) -> None:
        super().__init__(manifest, store)
        self._client = MCPClient()
        self._config: dict[str, Any] = {}
        self._server_status: dict[str, dict[str, Any]] = {}
        self._server_tools: dict[str, list[str]] = {}
        self._initialized = False

    async def on_load(self, config: dict[str, Any]) -> None:
        self._config = dict(config)

    async def on_unload(self) -> None:
        await self._client.disconnect_all()
        self._server_status.clear()
        self._server_tools.clear()
        self._initialized = False

    def setup(self, ctx: PluginSetupContext) -> None:
        ctx.register_hook(HookStage.ON_SESSION_INIT, self._on_session_init)
        ctx.register_hook(HookStage.ON_SESSION_CLOSE, self._on_session_close)

    async def _on_session_init(self, ctx: HookContext) -> None:
        if ctx.plugin_runtime is None:
            raise RuntimeError("MCPPlugin requires plugin runtime registration capability")
        if self._initialized:
            return
        servers = self._config.get("servers", {})
        if not servers:
            self._initialized = True
            return
        for server_name, server_cfg in servers.items():
            if not isinstance(server_cfg, dict):
                continue
            if not server_cfg.get("enabled", True):
                self._server_status[server_name] = {"status": "disabled"}
                continue
            try:
                tools = await self._client.connect_and_list(server_name, server_cfg)
                registered_names = self._register_server_tools(
                    ctx.plugin_runtime,
                    server_name,
                    tools,
                )
            except Exception as exc:
                await self._rollback_server(ctx.plugin_runtime, server_name)
                self._server_status[server_name] = {
                    "status": "error",
                    "error": str(exc),
                }
                logger.error("MCP server %s initialization failed: %s", server_name, exc)
                if server_cfg.get("required", False):
                    await self._rollback_all(ctx.plugin_runtime)
                    raise
                continue
            self._server_status[server_name] = {
                "status": "ready",
                "tools": len(registered_names),
            }
        self._initialized = True

    async def _on_session_close(self, ctx: HookContext) -> None:
        if ctx.plugin_runtime is None:
            raise RuntimeError("MCPPlugin requires plugin runtime registration capability")
        await self._rollback_all(ctx.plugin_runtime)
        self._server_status.clear()

    def _register_server_tools(
        self,
        runtime: RuntimePluginContext,
        server_name: str,
        tools: list[dict[str, Any]],
    ) -> list[str]:
        registered_names = self._server_tools.setdefault(server_name, [])
        for tool_def in tools:
            tool_name = f"mcp__{server_name}__{tool_def['name']}"
            mcp_tool = MCPTool(self._client, server_name, tool_def)
            registered_name = runtime.register_tool(
                mcp_tool.as_tool(tool_name),
                options=ToolRegistrationOptions(
                    sandbox_mode="host",
                    namespace=f"mcp:{server_name}",
                ),
            )
            registered_names.append(registered_name)
        return registered_names

    async def _rollback_server(
        self,
        runtime: RuntimePluginContext,
        server_name: str,
    ) -> None:
        for registered_name in reversed(self._server_tools.pop(server_name, [])):
            runtime.unregister_tool(registered_name)
        await self._client.disconnect(server_name)

    async def _rollback_all(self, runtime: RuntimePluginContext) -> None:
        for server_name in reversed(list(self._server_tools)):
            await self._rollback_server(runtime, server_name)
        await self._client.disconnect_all()
        self._initialized = False

    def diagnostics(self) -> dict[str, Any]:
        statuses = list(self._server_status.values())
        return {
            "status": "degraded" if any(s.get("status") == "error" for s in statuses) else "ready",
            "servers": dict(self._server_status),
        }
