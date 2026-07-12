"""Integration tests for MCPPlugin — client, stdio transport, tool wrapping."""

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest


def _tool_definition(name, **values):
    return {
        "name": name,
        "inputSchema": {"type": "object", "properties": {}},
        **values,
    }


def _initialize_result(version="2024-11-05"):
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "test", "version": "1"},
    }

_EVERYTHING_SERVER = """\
import sys, json

def respond(id, result):
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":id,"result":result}) + "\\n")
    sys.stdout.flush()

def notify(method, params=None):
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","method":method,"params":params or {}}) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method", "")
    rid = req.get("id", 0)
    if method == "initialize":
        respond(rid, {"protocolVersion":"2024-11-05","serverInfo":{"name":"test","version":"1.0"},"capabilities":{"tools":{}}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        notify("notifications/tools/list_changed")
        respond(rid, {"tools":[
            {"name":"echo","description":"Echo back the message","inputSchema":{"type":"object","properties":{"message":{"type":"string"}},"required":["message"]}},
            {"name":"add","description":"Add two numbers","inputSchema":{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}},
        ]})
    elif method == "tools/call":
        params = req.get("params", {})
        tname = params.get("name","")
        args = params.get("arguments",{})
        if tname == "echo":
            respond(rid, {"content":[{"type":"text","text":args.get("message","")}]})
        elif tname == "add":
            respond(rid, {"content":[{"type":"text","text":str(args.get("a",0)+args.get("b",0))}]})
        else:
            respond(rid, {"content":[{"type":"text","text":"unknown"}],"isError":True})
    else:
        respond(rid, {})
"""


@pytest.fixture
def echo_server_script(tmp_path):
    script = tmp_path / "mcp_echo_server.py"
    script.write_text(_EVERYTHING_SERVER)
    return script


class TestMCPStdioTransport:
    @pytest.mark.asyncio
    async def test_connect_and_list_tools(self, echo_server_script):
        from builtin_plugins.mcp.client import MCPClient

        client = MCPClient()
        tools = await client.connect_and_list("test", {
            "type": "local",
            "command": ["python3", str(echo_server_script)],
        })

        tool_names = {t["name"] for t in tools}
        assert "echo" in tool_names
        assert "add" in tool_names
        assert len(tools) == 2

        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_call_echo_tool(self, echo_server_script):
        from builtin_plugins.mcp.client import MCPClient

        client = MCPClient()
        await client.connect_and_list("test", {
            "type": "local",
            "command": ["python3", str(echo_server_script)],
        })

        result = await client.call_tool("test", "echo", {"message": "hello world"})
        assert "hello world" in result.content
        assert result.is_error is False
        assert result.data["content"][0]["text"] == "hello world"

        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_call_add_tool(self, echo_server_script):
        from builtin_plugins.mcp.client import MCPClient

        client = MCPClient()
        await client.connect_and_list("test", {
            "type": "local",
            "command": ["python3", str(echo_server_script)],
        })

        result = await client.call_tool("test", "add", {"a": 3, "b": 5})
        assert "8" in result.content

        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_command_not_found_raises_error(self, echo_server_script):
        from builtin_plugins.mcp.client import MCPClient, MCPConnectionError

        client = MCPClient()
        with pytest.raises((MCPConnectionError, ConnectionResetError, OSError)):
            await client.connect_and_list("test", {
                "type": "local",
                "command": ["python3", "-c", "exit(1)"],
            })

    @pytest.mark.asyncio
    async def test_disconnect_all_cleans_up(self, echo_server_script):
        from builtin_plugins.mcp.client import MCPClient

        client = MCPClient()
        await client.connect_and_list("test", {
            "type": "local",
            "command": ["python3", str(echo_server_script)],
        })
        await client.disconnect_all()
        assert "test" not in client._transports

    @pytest.mark.asyncio
    async def test_duplicate_server_name_preserves_existing_connection(
        self, echo_server_script
    ):
        from builtin_plugins.mcp.client import MCPClient, MCPConnectionError

        client = MCPClient()
        config = {
            "type": "local",
            "command": ["python3", str(echo_server_script)],
        }
        await client.connect_and_list("test", config)
        original = client._transports["test"]

        with pytest.raises(MCPConnectionError, match="already connected"):
            await client.connect_and_list("test", config)

        assert client._transports["test"] is original
        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_invalid_tool_list_disconnects_before_commit(self):
        from builtin_plugins.mcp.client import MCPClient, MCPConnectionError

        client = MCPClient()
        transport = AsyncMock()
        transport.call.side_effect = [
            _initialize_result(),
            {"tools": [{"description": "no name"}]},
        ]
        client._create_transport = lambda config: transport

        with pytest.raises(MCPConnectionError, match="non-empty string name"):
            await client.connect_and_list("invalid", {})

        transport.disconnect.assert_awaited_once()
        assert client._transports == {}

    @pytest.mark.asyncio
    async def test_invalid_input_schema_disconnects_before_commit(self):
        from builtin_plugins.mcp.client import MCPClient, MCPConnectionError

        client = MCPClient()
        transport = AsyncMock()
        transport.call.side_effect = [
            _initialize_result(),
            {
                "tools": [
                    _tool_definition(
                        "invalid",
                        inputSchema={"type": "object", "required": "not-a-list"},
                    )
                ]
            },
        ]
        client._create_transport = lambda config: transport

        with pytest.raises(MCPConnectionError, match="invalid inputSchema"):
            await client.connect_and_list("invalid", {})

        transport.disconnect.assert_awaited_once()
        assert client._transports == {}

    @pytest.mark.asyncio
    async def test_connect_performs_required_handshake_before_tool_discovery(self):
        from builtin_plugins.mcp.client import MCPClient

        client = MCPClient()
        transport = AsyncMock()
        transport.call.side_effect = [
            _initialize_result(),
            {"tools": [_tool_definition("echo")]},
        ]
        client._create_transport = lambda config: transport

        tools = await client.connect_and_list("server", {})

        assert tools == [_tool_definition("echo")]
        assert transport.call.await_args_list == [
            call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "xbotv2", "version": "2"},
                },
            ),
            call("tools/list", {}),
        ]
        transport.notify.assert_awaited_once_with("notifications/initialized", {})

    @pytest.mark.asyncio
    async def test_unsupported_protocol_version_disconnects_before_commit(self):
        from builtin_plugins.mcp.client import MCPClient, MCPConnectionError

        client = MCPClient()
        transport = AsyncMock()
        transport.call.return_value = _initialize_result("unsupported")
        client._create_transport = lambda config: transport

        with pytest.raises(MCPConnectionError, match="unsupported protocol version"):
            await client.connect_and_list("server", {})

        transport.notify.assert_not_awaited()
        transport.disconnect.assert_awaited_once()
        assert client._transports == {}


class TestMCPHttpTransport:
    @pytest.mark.asyncio
    async def test_call_and_notification_use_json_rpc_envelopes(self):
        import httpx

        from builtin_plugins.mcp.client import HttpTransport

        requests = []

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            if "id" not in body:
                return httpx.Response(202)
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}},
            )

        transport = HttpTransport("https://mcp.test")
        transport._client = httpx.AsyncClient(
            base_url="https://mcp.test",
            transport=httpx.MockTransport(handler),
        )

        assert await transport.call("test/call", {"value": 1}) == {"ok": True}
        await transport.notify("notifications/test", {})

        assert requests == [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "test/call",
                "params": {"value": 1},
            },
            {
                "jsonrpc": "2.0",
                "method": "notifications/test",
                "params": {},
            },
        ]
        await transport.disconnect()


@pytest.mark.asyncio
async def test_mcp_plugin_unload_disconnects_external_resources():
    from builtin_plugins.mcp.plugin import MCPPlugin
    from xbotv2.api import PluginManifest

    plugin = MCPPlugin(PluginManifest(name="mcp", version="1"), store=None)
    plugin._client.disconnect_all = AsyncMock()
    plugin._server_status["server"] = {"status": "ready"}

    await plugin.on_unload()

    plugin._client.disconnect_all.assert_awaited_once()
    assert plugin._server_status == {}


def _mcp_runtime():
    from xbotv2.plugin.loader import _RuntimePluginContext
    from xbotv2.tools.registry import ToolRegistry

    registry = ToolRegistry()
    owned_names: list[str] = []
    runtime = _RuntimePluginContext("mcp", registry, owned_names)
    return runtime, registry, owned_names


def _mcp_plugin(servers):
    from builtin_plugins.mcp.plugin import MCPPlugin
    from xbotv2.api import PluginManifest

    plugin = MCPPlugin(PluginManifest(name="mcp", version="1"), store=None)
    plugin._config = {"servers": servers}
    plugin._client.connect_and_list = AsyncMock()
    plugin._client.disconnect = AsyncMock(return_value=True)
    plugin._client.disconnect_all = AsyncMock()
    return plugin


@pytest.mark.asyncio
async def test_optional_server_registration_failure_rolls_back_that_server():
    plugin = _mcp_plugin({"optional": {}})
    plugin._client.connect_and_list.return_value = [
        _tool_definition("duplicate"),
        _tool_definition("duplicate"),
    ]
    runtime, registry, owned_names = _mcp_runtime()
    ctx = SimpleNamespace(plugin_runtime=runtime)

    await plugin._on_session_init(ctx)

    assert registry.registered_names() == []
    assert owned_names == []
    assert plugin._server_status["optional"]["status"] == "error"
    assert plugin._initialized is True
    plugin._client.disconnect.assert_awaited_once_with("optional")

    await plugin._on_session_init(ctx)
    plugin._client.connect_and_list.assert_awaited_once()


@pytest.mark.asyncio
async def test_required_server_registration_failure_rolls_back_all_servers():
    plugin = _mcp_plugin({"ready": {}, "required": {"required": True}})
    plugin._client.connect_and_list.side_effect = [
        [_tool_definition("first")],
        [_tool_definition("duplicate"), _tool_definition("duplicate")],
    ]
    runtime, registry, owned_names = _mcp_runtime()
    ctx = SimpleNamespace(plugin_runtime=runtime)

    with pytest.raises(ValueError, match="already registered"):
        await plugin._on_session_init(ctx)

    assert registry.registered_names() == []
    assert owned_names == []
    assert plugin._initialized is False
    assert plugin._client.disconnect.await_args_list == [
        call("required"),
        call("ready"),
    ]
    plugin._client.disconnect_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_close_removes_tools_and_allows_reinitialization():
    plugin = _mcp_plugin({"server": {}})
    plugin._client.connect_and_list.return_value = [_tool_definition("echo")]
    runtime, registry, owned_names = _mcp_runtime()
    ctx = SimpleNamespace(plugin_runtime=runtime)

    await plugin._on_session_init(ctx)
    registered_name = "mcp:server:mcp__server__echo"
    assert registry.registered(registered_name)

    await plugin._on_session_close(ctx)
    assert registry.registered_names() == []
    assert owned_names == []
    assert plugin._server_status == {}
    assert plugin._initialized is False

    await plugin._on_session_init(ctx)
    assert registry.registered(registered_name)
    assert plugin._client.connect_and_list.await_count == 2


class TestMCPToolWrapper:
    @pytest.mark.asyncio
    async def test_mcp_tool_as_callable(self, echo_server_script):
        from builtin_plugins.mcp.client import MCPClient
        from builtin_plugins.mcp.tool import MCPTool

        client = MCPClient()
        tools = await client.connect_and_list("test", {
            "type": "local",
            "command": ["python3", str(echo_server_script)],
        })

        echo_def = next(t for t in tools if t["name"] == "echo")
        tool = MCPTool(client, "test", echo_def)
        assert tool.__doc__ == "Echo back the message"

        result = await tool(message="hi from mcp tool")
        assert "hi from mcp tool" in result.content

        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_mcp_tool_as_xbot_tool(self, echo_server_script):
        from builtin_plugins.mcp.client import MCPClient
        from builtin_plugins.mcp.tool import MCPTool
        from xbotv2.api.tools import Tool

        client = MCPClient()
        tools = await client.connect_and_list("test", {
            "type": "local",
            "command": ["python3", str(echo_server_script)],
        })

        add_def = next(t for t in tools if t["name"] == "add")
        mcp_tool = MCPTool(client, "test", add_def)
        xbot_tool = mcp_tool.as_tool("mcp__test__add")

        result = await xbot_tool.ainvoke({"a": 10, "b": 20})
        assert "30" in result.content
        assert xbot_tool.parameters == add_def["inputSchema"]
        assert xbot_tool.provider_schema()["function"]["parameters"] == add_def["inputSchema"]

        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_mcp_error_result_becomes_structured_tool_failure(self):
        from builtin_plugins.mcp.client import MCPCallResult
        from builtin_plugins.mcp.tool import MCPTool

        client = AsyncMock()
        client.call_tool.return_value = MCPCallResult(
            content="remote tool failed",
            is_error=True,
            data={"isError": True},
        )
        tool = MCPTool(client, "server", _tool_definition("failing"))

        result = await tool()

        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "mcp_tool_error"
        assert result.content == "remote tool failed"
        assert result.data == {"isError": True}


class TestMCPNormalizeResult:
    def test_normalize_text_content(self):
        from builtin_plugins.mcp.client import _normalize_mcp_result

        result = {"content": [{"type": "text", "text": "hello"}]}
        assert _normalize_mcp_result(result) == "hello"

    def test_normalize_mixed_content(self):
        from builtin_plugins.mcp.client import _normalize_mcp_result

        result = {"content": [
            {"type": "text", "text": "first"},
            {"type": "image", "mimeType": "image/png"},
            {"type": "text", "text": "third"},
        ]}
        normalized = _normalize_mcp_result(result)
        assert "first" in normalized
        assert "image" in normalized
        assert "third" in normalized

    def test_normalize_no_content(self):
        from builtin_plugins.mcp.client import _normalize_mcp_result

        result = {"result": "ok"}
        normalized = _normalize_mcp_result(result)
        assert "ok" in normalized
