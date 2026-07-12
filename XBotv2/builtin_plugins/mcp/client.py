"""MCPClient — JSON-RPC 2.0 over stdio and HTTP for Model Context Protocol.

Supports:
- tools/list: discover tools from a server
- tools/call: invoke a tool and return results
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

logger = logging.getLogger("xbotv2.mcp")
MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPConnectionError(RuntimeError):
    """Raised when an MCP server cannot be connected or called."""


@dataclass(frozen=True, slots=True)
class MCPCallResult:
    content: str
    is_error: bool
    data: dict[str, Any]


class MCPClient:
    def __init__(self) -> None:
        self._transports: dict[str, MCPTransport] = {}

    async def connect_and_list(self, name: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        if name in self._transports:
            raise MCPConnectionError(f"MCP server '{name}' is already connected")
        transport = self._create_transport(cfg)
        try:
            await transport.connect()
            initialize_result = await transport.call(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "xbotv2", "version": "2"},
                },
            )
            _validate_initialize_result(initialize_result)
            await transport.notify("notifications/initialized", {})
            result = await transport.call("tools/list", {})
            tools = _validate_tool_list(result)
        except BaseException:
            await transport.disconnect()
            raise
        self._transports[name] = transport
        return tools

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> MCPCallResult:
        transport = self._transports.get(server)
        if transport is None:
            raise MCPConnectionError(f"MCP server '{server}' not connected")
        result = await transport.call("tools/call", {"name": tool, "arguments": arguments})
        return MCPCallResult(
            content=_normalize_mcp_result(result),
            is_error=bool(result.get("isError", False)),
            data=result,
        )

    async def disconnect_all(self) -> None:
        for name in list(self._transports):
            await self.disconnect(name)

    async def disconnect(self, name: str) -> bool:
        transport = self._transports.pop(name, None)
        if transport is None:
            return False
        try:
            await transport.disconnect()
        except Exception:
            logger.warning("MCP disconnect failed for %s", name, exc_info=True)
        return True

    def _create_transport(self, cfg: dict[str, Any]) -> MCPTransport:
        transport_type = cfg.get("type", "local")
        if transport_type == "remote":
            return HttpTransport(cfg["url"], cfg.get("headers", {}))
        return StdioTransport(cfg.get("command", []), cfg.get("env", {}))


class MCPTransport:
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...
    async def notify(self, method: str, params: dict[str, Any]) -> None: ...


class StdioTransport(MCPTransport):
    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        if not command:
            raise MCPConnectionError("MCP stdio transport requires a command")
        self._command = command
        self._env = env or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._io_lock = asyncio.Lock()

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
            raise MCPConnectionError(
                f"MCP stdio failed to start {self._command!r}: {exc}"
            ) from exc

    async def disconnect(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
            try:
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.2)
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None:
            raise MCPConnectionError("MCP stdio transport not connected")
        return await self._send_json_rpc(method, params)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if self._proc is None:
            raise MCPConnectionError("MCP stdio transport not connected")
        notification = {"jsonrpc": "2.0", "method": method, "params": params}
        async with self._io_lock:
            await self._write_message(notification)

    async def _send_json_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._io_lock:
            self._request_id += 1
            request_id = self._request_id
            await self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            return await self._read_response(request_id, method)

    async def _write_message(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message) + "\n"
        self._proc.stdin.write(payload.encode("utf-8"))  # type: ignore[union-attr]
        await self._proc.stdin.drain()  # type: ignore[union-attr]

    async def _read_response(self, request_id: int, method: str) -> dict[str, Any]:
        while True:
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(),  # type: ignore[union-attr]
                    timeout=30,
                )
            except asyncio.TimeoutError as exc:
                raise MCPConnectionError(f"MCP call '{method}' timed out") from exc
            if not line:
                raise MCPConnectionError(
                    f"MCP server closed connection during '{method}'"
                )
            try:
                response = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise MCPConnectionError(f"MCP invalid JSON response: {exc}") from exc
            if not isinstance(response, dict):
                raise MCPConnectionError("MCP JSON-RPC response must be an object")
            _validate_json_rpc_version(response)
            if "id" not in response:
                continue
            if response["id"] != request_id:
                raise MCPConnectionError(
                    f"MCP response id {response['id']!r} does not match request {request_id}"
                )
            if "error" in response:
                err = response["error"]
                if not isinstance(err, dict):
                    raise MCPConnectionError(f"MCP error: {err}")
                raise MCPConnectionError(
                    f"MCP error {err.get('code', '')}: "
                    f"{err.get('message', 'unknown')}"
                )
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise MCPConnectionError("MCP JSON-RPC result must be an object")
            return result


class HttpTransport(MCPTransport):
    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self._url = url.rstrip("/")
        self._headers = headers or {}
        self._request_id = 0

    async def connect(self) -> None:
        import httpx

        self._client = httpx.AsyncClient(
            base_url=self._url,
            headers=self._headers,
            timeout=30.0,
        )

    async def disconnect(self) -> None:
        if hasattr(self, "_client"):
            await self._client.aclose()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        data = await self._post(body)
        if data.get("id") != request_id:
            raise MCPConnectionError("MCP HTTP response id does not match request")
        if "error" in data:
            err = data["error"]
            if not isinstance(err, dict):
                raise MCPConnectionError(f"MCP error: {err}")
            raise MCPConnectionError(
                f"MCP error {err.get('code', '')}: "
                f"{err.get('message', 'unknown')}"
            )
        result = data.get("result", {})
        if not isinstance(result, dict):
            raise MCPConnectionError("MCP JSON-RPC result must be an object")
        return result

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": params})

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        try:
            resp = await self._client.post("/", json=body)
            resp.raise_for_status()
            if not resp.content:
                return {}
            data = resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise MCPConnectionError(f"MCP HTTP call failed: {exc}") from exc
        if not isinstance(data, dict):
            raise MCPConnectionError("MCP HTTP JSON-RPC response must be an object")
        if data:
            _validate_json_rpc_version(data)
        return data


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
            resource = block.get("resource")
            uri = (
                resource.get("uri", "")
                if isinstance(resource, dict)
                else block.get("uri", "")
            )
            texts.append(f"[resource: {uri}]")
        else:
            texts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)


def _validate_tool_list(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        raise MCPConnectionError("MCP tools/list result must be an object")
    tools = result.get("tools", [])
    if not isinstance(tools, list):
        raise MCPConnectionError("MCP tools/list 'tools' must be a list")
    for index, tool in enumerate(tools):
        if (
            not isinstance(tool, dict)
            or not isinstance(tool.get("name"), str)
            or not tool["name"]
        ):
            raise MCPConnectionError(
                f"MCP tools/list entry {index} must have a non-empty string name"
            )
        input_schema = tool.get("inputSchema")
        if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
            raise MCPConnectionError(
                f"MCP tools/list entry {index} must have an object inputSchema"
            )
        try:
            Draft202012Validator.check_schema(input_schema)
        except SchemaError as exc:
            raise MCPConnectionError(
                f"MCP tools/list entry {index} has invalid inputSchema: {exc.message}"
            ) from exc
    return tools


def _validate_initialize_result(result: Any) -> None:
    if not isinstance(result, dict):
        raise MCPConnectionError("MCP initialize result must be an object")
    version = result.get("protocolVersion")
    if version != MCP_PROTOCOL_VERSION:
        raise MCPConnectionError(
            f"MCP server selected unsupported protocol version {version!r}"
        )
    if not isinstance(result.get("capabilities"), dict):
        raise MCPConnectionError("MCP initialize result must contain capabilities")
    if not isinstance(result.get("serverInfo"), dict):
        raise MCPConnectionError("MCP initialize result must contain serverInfo")


def _validate_json_rpc_version(message: dict[str, Any]) -> None:
    if message.get("jsonrpc") != "2.0":
        raise MCPConnectionError("MCP message must declare JSON-RPC 2.0")
