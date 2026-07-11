"""Integration tests for MCPPlugin — client, stdio transport, tool wrapping."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

_EVERYTHING_SERVER = """\
import sys, json

def respond(id, result):
    sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":id,"result":result}) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method", "")
    rid = req.get("id", 0)
    if method == "initialize":
        respond(rid, {"protocolVersion":"2024-11-05","serverInfo":{"name":"test","version":"1.0"},"capabilities":{"tools":{}}})
    elif method == "tools/list":
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
        assert "hello world" in result

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
        assert "8" in result

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
        xbot_tool = Tool.from_function(mcp_tool, name="mcp__test__add")

        result = await xbot_tool.ainvoke({"a": 10, "b": 20})
        assert "30" in result.content

        await client.disconnect_all()


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
