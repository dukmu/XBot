"""MCP tools translate one external result into canonical ToolOutcome values."""

from XBotv2.agentloop.api.contracts import TOOLS, HumanInput, InboxItem
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
import pytest
from XBotv2.core.api.domain import InboxTarget
from XBotv2.agentloop.api.outputs import ToolCompleted
from XBotv2.application.app import start_application
from XBotv2.core.api.paths import RuntimePaths
from XBotv2.core.api.tools import ToolFailed, ToolSucceeded
from XBotv2.llm.impl.mock import MockLLM
from XBotv2.mcp_plugin.impl.callbacks import _form_content, _sampling_text
from XBotv2.mcp_plugin.impl.tool import MCPTool
from XBotv2.permissions.api.models import PermissionPolicy, PermissionRule
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
