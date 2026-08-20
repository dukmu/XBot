"""Integration tests for MCPPlugin — client, stdio transport, tool wrapping."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
import xml.etree.ElementTree as ET

import pytest

from XBotv2.agentloop import EventContext


def _tool_definition(name, **values):
    return {
        "name": name,
        "inputSchema": {"type": "object", "properties": {}},
        **values,
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
        respond(rid, {"protocolVersion":"2024-11-05","serverInfo":{"name":"test","version":"1.0"},"capabilities":{"tools":{},"resources":{"subscribe":True},"prompts":{},"completions":{},"logging":{}}})
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
    elif method == "resources/list":
        respond(rid, {"resources":[{"uri":"memo://one","name":"Memo","mimeType":"text/plain"}]})
    elif method == "resources/templates/list":
        respond(rid, {"resourceTemplates":[{"uriTemplate":"memo://{name}","name":"Memo template"}]})
    elif method == "resources/read":
        uri = req.get("params", {}).get("uri", "")
        respond(rid, {"contents":[{"uri":uri,"mimeType":"text/plain","text":"memo content"}]})
    elif method in {"resources/subscribe", "resources/unsubscribe", "logging/setLevel", "ping"}:
        respond(rid, {})
    elif method == "prompts/list":
        respond(rid, {"prompts":[{"name":"review","description":"Review code","arguments":[]}]})
    elif method == "prompts/get":
        respond(rid, {"description":"Review code","messages":[{"role":"user","content":{"type":"text","text":"Review this"}}]})
    elif method == "completion/complete":
        respond(rid, {"completion":{"values":["one","two"],"total":2,"hasMore":False}})
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
        from XBotv2.mcp_plugin.mcp_client import MCPClient

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
        from XBotv2.mcp_plugin.mcp_client import MCPClient

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
        from XBotv2.mcp_plugin.mcp_client import MCPClient

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
        from XBotv2.mcp_plugin.mcp_client import MCPClient, MCPConnectionError

        client = MCPClient()
        with pytest.raises(MCPConnectionError, match="initialization failed"):
            await client.connect_and_list("test", {
                "type": "local",
                "command": ["python3", "-c", "exit(1)"],
            })

    @pytest.mark.asyncio
    async def test_disconnect_all_cleans_up(self, echo_server_script):
        from XBotv2.mcp_plugin.mcp_client import MCPClient

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
        from XBotv2.mcp_plugin.mcp_client import MCPClient, MCPConnectionError

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
    async def test_server_features_use_negotiated_protocol(self, echo_server_script):
        from XBotv2.mcp_plugin.mcp_client import MCPClient

        client = MCPClient()
        await client.connect_and_list("test", {
            "type": "local",
            "command": ["python3", str(echo_server_script)],
        })

        assert set(client.server_capabilities("test")) >= {
            "tools", "resources", "prompts", "completions", "logging",
        }
        resources = await client.list_resources("test")
        assert resources["resources"][0]["uri"] == "memo://one"
        assert resources["resourceTemplates"][0]["uriTemplate"] == "memo://{name}"
        read = await client.read_resource("test", "memo://one")
        assert read["contents"][0]["text"] == "memo content"
        prompts = await client.list_prompts("test")
        assert prompts[0]["name"] == "review"
        prompt = await client.get_prompt("test", "review")
        assert prompt["messages"][0]["content"]["text"] == "Review this"
        completion = await client.complete(
            "test",
            {"type": "ref/prompt", "name": "review"},
            {"name": "topic", "value": "o"},
        )
        assert completion["completion"]["values"] == ["one", "two"]
        assert await client.subscribe_resource("test", "memo://one") == {}
        assert await client.unsubscribe_resource("test", "memo://one") == {}
        assert await client.set_logging_level("test", "info") == {}
        assert await client.ping("test") == {}

        await client.disconnect_all()


def test_invalid_mcp_tool_schema_is_rejected():
    from XBotv2.mcp_plugin.mcp_client import MCPConnectionError, _validate_tool_list

    with pytest.raises(MCPConnectionError, match="invalid inputSchema"):
        _validate_tool_list({"tools": [_tool_definition(
            "invalid",
            inputSchema={"type": "object", "required": "not-a-list"},
        )]})


@pytest.mark.asyncio
async def test_mcp_client_callbacks_bridge_sampling_roots_and_form_elicitation(tmp_path):
    from XBotv2.mcp_plugin.callbacks import client_callbacks
    from mcp import types
    from XBotv2.core import SessionInfo
    from XBotv2.llm.mock import MockLLM
    requested = []
    llm = MockLLM(responses=[{"content": "Done"}])

    class FakeInteractions:
        async def request_user_input(self, question, **kwargs):
            requested.append((question, kwargs))
            return {"status": "answered", "answer": "focused"}

    callbacks = client_callbacks(
        llm,
        FakeInteractions(),
        SessionInfo(
            session_id="s",
            thread_id="t",
            workspace_root=str(tmp_path),
            provider="minimax",
        ),
    )

    sample = await callbacks["sampling_callback"](
        None,
        types.CreateMessageRequestParams(
            systemPrompt="Be concise",
            messages=[types.SamplingMessage(
                role="user",
                content=types.TextContent(type="text", text="Summarize"),
            )],
            maxTokens=100,
        ),
    )
    roots = await callbacks["list_roots_callback"](None)
    elicited = await callbacks["elicitation_callback"](
        None,
        types.ElicitRequestFormParams(
            message="Choose focus",
            requestedSchema={
                "type": "object",
                "properties": {"focus": {"type": "string"}},
                "required": ["focus"],
            },
        ),
    )

    sent = llm.get_call_messages(0)
    system = ET.fromstring(sent[0].content)
    assert system.tag == "mcp_sampling_system_prompt"
    assert system.attrib["source"] == "mcp_server"
    assert system.text.strip() == "Be concise"
    assert (sent[1].role, sent[1].content) == ("user", "Summarize")
    assert sample.content.text == "Done"
    assert sample.model == "minimax"
    assert str(roots.roots[0].uri) == tmp_path.resolve().as_uri()
    assert elicited.action == "accept"
    assert elicited.content == {"focus": "focused"}
    assert requested == [("Choose focus", {"source": "mcp_elicitation"})]


@pytest.mark.asyncio
async def test_mcp_plugin_unload_disconnects_external_resources():
    from XBotv2.mcp_plugin.plugin import MCPPlugin
    
    plugin = MCPPlugin()
    plugin._client.disconnect_all = AsyncMock()
    plugin._server_status["server"] = {"status": "ready"}

    await plugin._on_unload()

    plugin._client.disconnect_all.assert_awaited_once()
    assert plugin._server_status == {}


def _mcp_plugin(servers):
    from XBotv2.mcp_plugin.plugin import MCPPlugin
    from XBotv2.core import SessionInfo
    from plugin_harness import mount_plugin_standalone

    plugin = MCPPlugin()
    mount_plugin_standalone(plugin, {"servers": servers})
    plugin.ctx.set(
        "interactions",
        type("Interactions", (), {"request_user_input": AsyncMock()})(),
    )
    plugin.ctx.set(
        "session",
        SessionInfo(
            session_id="s",
            thread_id="t",
            workspace_root=str(plugin.ctx.workspace_root),
            provider="mock",
        ),
    )
    plugin._client.connect_and_list = AsyncMock()
    plugin._client.server_capabilities = lambda _server: {"tools": {}}
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
    tools = plugin.ctx.tools

    await plugin._on_session_init(EventContext())

    assert tools.registered_names() == ()
    assert plugin._server_tools == {}
    assert plugin._server_status["optional"]["status"] == "error"
    assert plugin._initialized is True
    plugin._client.disconnect.assert_awaited_once_with("optional")

    await plugin._on_session_init(EventContext())
    plugin._client.connect_and_list.assert_awaited_once()


@pytest.mark.asyncio
async def test_required_server_registration_failure_rolls_back_all_servers():
    plugin = _mcp_plugin({"ready": {}, "required": {"required": True}})
    plugin._client.connect_and_list.side_effect = [
        [_tool_definition("first")],
        [_tool_definition("duplicate"), _tool_definition("duplicate")],
    ]
    tools = plugin.ctx.tools

    with pytest.raises(ValueError, match="already registered"):
        await plugin._on_session_init(EventContext())

    assert tools.registered_names() == ()
    assert plugin._server_tools == {}
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
    tools = plugin.ctx.tools

    await plugin._on_session_init(EventContext())
    registered_name = "mcp:server:mcp__server__echo"
    assert registered_name in tools.registered_names()

    await plugin._on_session_close(EventContext())
    assert tools.registered_names() == ()
    assert plugin._server_tools == {}
    assert plugin._server_status == {}
    assert plugin._initialized is False

    await plugin._on_session_init(EventContext())
    assert registered_name in tools.registered_names()
    assert plugin._client.connect_and_list.await_count == 2


@pytest.mark.asyncio
async def test_negotiated_server_features_register_agent_bridges():
    plugin = _mcp_plugin({"server": {}})
    plugin._client.connect_and_list.return_value = []
    plugin._client.server_capabilities = lambda _server: {
        "resources": {}, "prompts": {}, "completions": {},
    }
    plugin._client.read_resource = AsyncMock(return_value={
        "contents": [{"uri": "memo://one", "text": "memo"}],
    })
    plugin._client.get_prompt = AsyncMock(return_value={
        "messages": [{"role": "user", "content": {"type": "text", "text": "review"}}],
    })
    plugin._client.complete = AsyncMock(return_value={
        "completion": {"values": ["one"]},
    })
    tools = plugin.ctx.tools

    await plugin._on_session_init(EventContext())

    assert plugin._server_status["server"] == {
        "status": "ready", "tools": 0, "bridges": 3,
    }
    assert len(plugin._server_tools["server"]) == 3
    resource = tools.resolve("mcp:server:mcp__server__protocol_resources")
    prompt = tools.resolve("mcp:server:mcp__server__protocol_prompts")
    completion = tools.resolve("mcp:server:mcp__server__protocol_complete")
    assert resource is not None and prompt is not None and completion is not None
    assert resource.parameters["properties"]["operation"]["enum"] == [
        "list", "read",
    ]

    read_result = await resource.ainvoke({
        "operation": "read", "uri": "memo://one",
    })
    prompt_result = await prompt.ainvoke({
        "operation": "get", "name": "review", "arguments": {"scope": "diff"},
    })
    completion_result = await completion.ainvoke({
        "reference_type": "prompt",
        "reference": "review",
        "argument": {"name": "scope", "value": "d"},
    })

    read_data = json.loads(read_result.content)
    prompt_data = json.loads(prompt_result.content)
    completion_data = json.loads(completion_result.content)
    assert read_data["contents"][0]["text"] == "memo"
    assert prompt_data["messages"][0]["content"]["text"] == "review"
    assert completion_data["completion"]["values"] == ["one"]
    plugin._client.read_resource.assert_awaited_once_with("server", "memo://one")
    plugin._client.get_prompt.assert_awaited_once_with(
        "server", "review", {"scope": "diff"},
    )


class TestMCPToolWrapper:
    @pytest.mark.asyncio
    async def test_mcp_tool_as_callable(self, echo_server_script):
        from XBotv2.mcp_plugin.mcp_client import MCPClient
        from XBotv2.mcp_plugin.tool import MCPTool

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
        from XBotv2.mcp_plugin.mcp_client import MCPClient
        from XBotv2.mcp_plugin.tool import MCPTool
        from XBotv2.core.tools import Tool

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
        from XBotv2.mcp_plugin.mcp_client import MCPCallResult
        from XBotv2.mcp_plugin.tool import MCPTool

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


class TestMCPNormalizeResult:
    def test_normalize_text_content(self):
        from XBotv2.mcp_plugin.mcp_client import _normalize_mcp_result

        result = {"content": [{"type": "text", "text": "hello"}]}
        assert _normalize_mcp_result(result) == "hello"

    def test_normalize_mixed_content(self):
        from XBotv2.mcp_plugin.mcp_client import _normalize_mcp_result

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
        from XBotv2.mcp_plugin.mcp_client import _normalize_mcp_result

        result = {"result": "ok"}
        normalized = _normalize_mcp_result(result)
        assert "ok" in normalized
