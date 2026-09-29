"""MCP tools translate one external result into canonical ToolOutcome values."""

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from XBotv2.agentloop import HumanInput, InboxItem, InboxTarget
from XBotv2.agentloop.protocol import ToolCompleted
from XBotv2.application.app import start_application
from XBotv2.core.paths import RuntimePaths
from XBotv2.core.tools import ToolFailed, ToolSucceeded
from XBotv2.llm.mock import MockLLM
from XBotv2.mcp_plugin.callbacks import _form_content, _sampling_text
from XBotv2.mcp_plugin.tool import MCPTool
from XBotv2.permissions import PermissionPolicy, PermissionRule
from mcp import types


@dataclass
class _Result:
    content: str
    is_error: bool = False


class _Client:
    def __init__(self, result):
        self.result = result
        self.call = None

    async def call_tool(self, server, name, arguments):
        self.call = (server, name, arguments)
        return self.result


@pytest.mark.asyncio
async def test_mcp_tool_success_is_canonical_tool_output():
    client = _Client(_Result("answer"))
    adapter = MCPTool(client, "docs", {
        "name": "search",
        "description": "Search docs",
        "inputSchema": {"type": "object", "properties": {}},
    })
    outcome = await adapter(query="x")
    assert isinstance(outcome, ToolSucceeded)
    assert outcome.output.parts[0].text == "answer"
    assert client.call == ("docs", "search", {"query": "x"})


@pytest.mark.asyncio
async def test_mcp_tool_error_is_not_reinterpreted_as_success():
    adapter = MCPTool(_Client(_Result("failed", is_error=True)), "docs", {
        "name": "search", "inputSchema": {"type": "object"},
    })
    outcome = await adapter()
    assert isinstance(outcome, ToolFailed)
    assert outcome.error.code == "mcp_tool_error"
    assert outcome.error.message == "failed"


def test_sampling_accepts_only_text_blocks():
    text = types.TextContent(type="text", text="hello")
    assert _sampling_text([text, text]) == "hello\nhello"
    assert _sampling_text(types.ImageContent(type="image", data="x", mimeType="image/png")) is None


def test_elicitation_form_content_requires_an_object_shape():
    schema = {"properties": {"name": {"type": "string"}}}
    assert _form_content("Alice", schema) == {"name": "Alice"}
    assert _form_content('{"name":"Bob"}', schema) == {"name": "Bob"}
    assert _form_content("plain", {"properties": {"a": {}, "b": {}}}) is None


@pytest.mark.asyncio
async def test_stdio_server_tool_lifecycle_through_application_factory(tmp_path: Path):
    server_script = tmp_path / "mcp_server.py"
    pid_file = tmp_path / "mcp_server.pid"
    server_script.write_text(
        """\
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

Path(os.environ["XBOT_MCP_TEST_PID"]).write_text(str(os.getpid()), encoding="utf-8")
server = FastMCP("xbot-test")

@server.tool()
def echo(text: str) -> str:
    \"\"\"Echo one string.\"\"\"
    return f"echo:{text}"

server.run("stdio")
""",
        encoding="utf-8",
    )
    provider = MockLLM(responses=[
        {"content": "MCP test"},
        {"tool_calls": [{
            "id": "echo-1",
            "name": "mcp__local__echo",
            "args": {"text": "production-path"},
        }]},
        {"content": "done"},
    ])
    context = await start_application(
        paths=RuntimePaths.from_data_dir(tmp_path / "data"),
        session_id="mcp-stdio",
        thread_id="main",
        workspace_root=tmp_path,
        llm_override=provider,
        extra_plugins=[{
            "id": "mcp_plugin",
            "config": {"servers": {"local": {
                "required": True,
                "command": [sys.executable, str(server_script)],
                "env": {"XBOT_MCP_TEST_PID": str(pid_file)},
            }}},
        }],
    )
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        context.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern=".*", decision="allow"),
        )),))
        assert "mcp__local__echo" in {
            tool.name for tool in context.tools.enabled()
        }
        events = [event async for event in context.engine.run_turn(InboxItem(
            target=InboxTarget.NEXT_TURN,
            input=HumanInput(content="Call the local echo tool"),
        ))]
        completed = [event for event in events if isinstance(event, ToolCompleted)]
        assert len(completed) == 1
        outcome = completed[0].execution.message.outcome
        assert isinstance(outcome, ToolSucceeded)
        assert outcome.output.parts[0].text == "echo:production-path"
    finally:
        await context.destroy()

    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail(f"MCP child process {pid} survived application destruction")
