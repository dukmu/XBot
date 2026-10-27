"""MCP tools translate one external result into canonical ToolOutcome values."""

from XBotv2.agentloop.api.contracts import TOOLS, HumanInput, InboxItem
import asyncio
import os
import sys
from pathlib import Path
import pytest
from XBotv2.core.api.domain import InboxTarget
from XBotv2.agentloop.api.outputs import ToolCompleted
from XBotv2.application.app import start_application
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import ToolSucceeded
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.permissions.api.models import PermissionPolicy, PermissionRule


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
    tools = context.require(TOOLS)
    try:
        context.permissions.replace_policies((PermissionPolicy(rules=(
            PermissionRule(tool_pattern=".*", decision="allow"),
        )),))
        assert "mcp__local__echo" in {
            tool.name for tool in context.require(TOOLS).enabled()
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

    assert tools.resolve("mcp__local__echo", include_disabled=True) is None
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail(f"MCP child process {pid} survived application destruction")
