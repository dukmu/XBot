"""MCPClient — JSON-RPC 2.0 over stdio and HTTP for Model Context Protocol.

Supports:
- tools/list: discover tools from a server
- tools/call: invoke a tool and return results
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("xbotv2.mcp")


class MCPConnectionError(RuntimeError):
    """Raised when an MCP server cannot be connected or called."""


class MCPClient:
    def __init__(self) -> None:
        self._transports: dict[str, MCPTransport] = {}

    async def connect_and_list(self, name: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        transport = self._create_transport(cfg)
        await transport.connect()
        result = await transport.call("tools/list", {})
        self._transports[name] = transport
        return result.get("tools", [])

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> str:
        transport = self._transports.get(server)
        if transport is None:
            raise MCPConnectionError(f"MCP server '{server}' not connected")
        result = await transport.call("tools/call", {"name": tool, "arguments": arguments})
        return _normalize_mcp_result(result)

    async def disconnect_all(self) -> None:
        for name, transport in list(self._transports.items()):
            try:
                await transport.disconnect()
            except Exception:
                logger.warning("MCP disconnect failed for %s", name, exc_info=True)
        self._transports.clear()

    def _create_transport(self, cfg: dict[str, Any]) -> MCPTransport:
        transport_type = cfg.get("type", "local")
        if transport_type == "remote":
            return HttpTransport(cfg["url"], cfg.get("headers", {}))
        return StdioTransport(cfg.get("command", []), cfg.get("env", {}))


class MCPTransport:
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


class StdioTransport(MCPTransport):
    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        if not command:
            raise MCPConnectionError("MCP stdio transport requires a command")
        self._command = command
        self._env = env or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def connect(self) -> None:
        import os
        proc_env = os.environ.copy()
        proc_env.update(self._env)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )
        except (OSError, FileNotFoundError) as exc:
            raise MCPConnectionError(f"MCP stdio failed to start {self._command!r}: {exc}") from exc
        # Send initialize (some servers require it)
        try:
            await self._send_json_rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "xbotv2"},
            })
        except Exception:
            pass  # Not all servers require initialize

    async def disconnect(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None:
            raise MCPConnectionError("MCP stdio transport not connected")
        return await self._send_json_rpc(method, params)

    async def _send_json_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        payload = json.dumps(request) + "\n"
        self._proc.stdin.write(payload.encode("utf-8"))  # type: ignore[union-attr]
        await self._proc.stdin.drain()  # type: ignore[union-attr]

        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=30)  # type: ignore[union-attr]
        except asyncio.TimeoutError:
            raise MCPConnectionError(f"MCP call '{method}' timed out")

        if not line:
            raise MCPConnectionError(f"MCP server closed connection during '{method}'")

        try:
            response = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MCPConnectionError(f"MCP invalid JSON response: {exc}") from exc

        if "error" in response:
            err = response["error"]
            raise MCPConnectionError(f"MCP error {err.get('code', '')}: {err.get('message', 'unknown')}")

        return response.get("result", {})


class HttpTransport(MCPTransport):
    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self._url = url.rstrip("/")
        self._headers = headers or {}
        self._request_id = 0

    async def connect(self) -> None:
        import httpx
        self._client = httpx.AsyncClient(base_url=self._url, headers=self._headers, timeout=30.0)

    async def disconnect(self) -> None:
        if hasattr(self, "_client"):
            await self._client.aclose()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        try:
            resp = await self._client.post("/", json=body)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise MCPConnectionError(f"MCP HTTP call failed: {exc}") from exc

        if "error" in data:
            err = data["error"]
            raise MCPConnectionError(f"MCP error {err.get('code', '')}: {err.get('message', 'unknown')}")
        return data.get("result", {})


def _normalize_mcp_result(result: dict[str, Any]) -> str:
    content = result.get("content", [])
    if not isinstance(content, list):
        return str(result)
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            texts.append(str(block))
        elif block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif block.get("type") == "image":
            texts.append(f"[image: {block.get('mimeType', 'unknown')}]")
        elif block.get("type") == "resource":
            uri = block.get("resource", {}).get("uri", "") if isinstance(block.get("resource"), dict) else block.get("uri", "")
            texts.append(f"[resource: {uri}]")
        else:
            texts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)
