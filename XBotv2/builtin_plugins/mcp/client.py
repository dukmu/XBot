"""MCP client backed by the official Model Context Protocol SDK."""

from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

logger = logging.getLogger("xbotv2.mcp")


class MCPConnectionError(RuntimeError):
    """Raised when an MCP server cannot be connected or called."""


@dataclass(frozen=True, slots=True)
class MCPCallResult:
    content: str
    is_error: bool
    data: dict[str, Any]


@dataclass(slots=True)
class _Connection:
    stack: AsyncExitStack
    session: ClientSession
    initialize_result: types.InitializeResult


class MCPClient:
    def __init__(self) -> None:
        self._transports: dict[str, _Connection] = {}

    async def connect_and_list(
        self,
        name: str,
        cfg: dict[str, Any],
        *,
        callbacks: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if name in self._transports:
            raise MCPConnectionError(f"MCP server '{name}' is already connected")

        stack = AsyncExitStack()
        try:
            read, write = await self._open_transport(stack, cfg)
            timeout = timedelta(seconds=float(cfg.get("timeout", 30)))
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timeout,
                    **(callbacks or {}),
                )
            )
            initialize_result = await session.initialize()
            connection = _Connection(stack, session, initialize_result)
            tools = await self._list_tools(connection)
        except BaseException as exc:
            await stack.aclose()
            if isinstance(exc, MCPConnectionError):
                raise
            if isinstance(exc, Exception):
                raise MCPConnectionError(
                    f"MCP server '{name}' initialization failed: {exc}"
                ) from exc
            raise

        self._transports[name] = connection
        return tools

    async def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> MCPCallResult:
        result = await self._connection(server).session.call_tool(tool, arguments)
        data = _dump(result)
        return MCPCallResult(
            content=_normalize_mcp_result(data),
            is_error=bool(result.isError),
            data=data,
        )

    def server_capabilities(self, server: str) -> dict[str, Any]:
        return _dump(self._connection(server).initialize_result.capabilities)

    async def list_resources(self, server: str) -> dict[str, Any]:
        session = self._connection(server).session
        resources = await _collect_pages(session.list_resources, "resources")
        templates = await _collect_pages(
            session.list_resource_templates,
            "resourceTemplates",
        )
        return {"resources": resources, "resourceTemplates": templates}

    async def read_resource(self, server: str, uri: str) -> dict[str, Any]:
        result = await self._connection(server).session.read_resource(AnyUrl(uri))
        return _dump(result)

    async def subscribe_resource(self, server: str, uri: str) -> dict[str, Any]:
        result = await self._connection(server).session.subscribe_resource(AnyUrl(uri))
        return _dump(result)

    async def unsubscribe_resource(self, server: str, uri: str) -> dict[str, Any]:
        result = await self._connection(server).session.unsubscribe_resource(AnyUrl(uri))
        return _dump(result)

    async def list_prompts(self, server: str) -> list[dict[str, Any]]:
        return await _collect_pages(
            self._connection(server).session.list_prompts,
            "prompts",
        )

    async def get_prompt(
        self,
        server: str,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        result = await self._connection(server).session.get_prompt(name, arguments)
        return _dump(result)

    async def complete(
        self,
        server: str,
        reference: dict[str, Any],
        argument: dict[str, str],
        context_arguments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if reference.get("type") == "ref/resource":
            ref = types.ResourceTemplateReference.model_validate(reference)
        else:
            ref = types.PromptReference.model_validate(reference)
        result = await self._connection(server).session.complete(
            ref,
            argument,
            context_arguments,
        )
        return _dump(result)

    async def set_logging_level(self, server: str, level: str) -> dict[str, Any]:
        result = await self._connection(server).session.set_logging_level(level)  # type: ignore[arg-type]
        return _dump(result)

    async def ping(self, server: str) -> dict[str, Any]:
        return _dump(await self._connection(server).session.send_ping())

    async def disconnect_all(self) -> None:
        for name in list(self._transports):
            await self.disconnect(name)

    async def disconnect(self, name: str) -> bool:
        connection = self._transports.pop(name, None)
        if connection is None:
            return False
        try:
            await connection.stack.aclose()
        except Exception:
            logger.warning("MCP disconnect failed for %s", name, exc_info=True)
        return True

    async def _open_transport(
        self,
        stack: AsyncExitStack,
        cfg: dict[str, Any],
    ) -> tuple[Any, Any]:
        if cfg.get("type", "local") == "remote":
            client = await stack.enter_async_context(httpx.AsyncClient(
                headers=dict(cfg.get("headers") or {}),
                timeout=float(cfg.get("timeout", 30)),
            ))
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(
                    str(cfg["url"]),
                    http_client=client,
                    terminate_on_close=bool(cfg.get("terminate_on_close", True)),
                )
            )
            return read, write

        command = list(cfg.get("command") or [])
        if not command:
            raise MCPConnectionError("MCP stdio transport requires a command")
        params = StdioServerParameters(
            command=command[0],
            args=command[1:],
            env=dict(cfg.get("env") or {}) or None,
            cwd=cfg.get("cwd"),
        )
        return await stack.enter_async_context(stdio_client(params))

    async def _list_tools(self, connection: _Connection) -> list[dict[str, Any]]:
        if connection.initialize_result.capabilities.tools is None:
            return []
        tools = await _collect_pages(connection.session.list_tools, "tools")
        return _validate_tool_list({"tools": tools})

    def _connection(self, server: str) -> _Connection:
        connection = self._transports.get(server)
        if connection is None:
            raise MCPConnectionError(f"MCP server '{server}' not connected")
        return connection


async def _collect_pages(method: Any, field: str) -> list[dict[str, Any]]:
    cursor = None
    values: list[dict[str, Any]] = []
    while True:
        result = await method(cursor=cursor)
        values.extend(_dump(item) for item in getattr(result, field))
        cursor = result.nextCursor
        if not cursor:
            return values


def _dump(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json", by_alias=True, exclude_none=True)


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
            uri = resource.get("uri", "") if isinstance(resource, dict) else ""
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
        if not isinstance(tool, dict) or not tool.get("name"):
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
