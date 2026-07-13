"""Bridges MCP client requests to public XBot runtime capabilities."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp import types

from xbotv2.api import HookContext, Message

logger = logging.getLogger("xbotv2.mcp")


def client_callbacks(ctx: HookContext) -> dict[str, Any]:
    async def sample(_request_context: Any, params: Any) -> Any:
        if ctx.invoke_model is None:
            return types.ErrorData(code=-32603, message="Model invocation unavailable")
        messages: list[Message] = []
        if params.systemPrompt:
            messages.append(Message(role="system", content=params.systemPrompt))
        for message in params.messages:
            text = _sampling_text(message.content)
            if text is None:
                return types.ErrorData(
                    code=-32602,
                    message="XBot sampling currently accepts text content only",
                )
            messages.append(Message(role=message.role, content=text))
        response = await ctx.invoke_model(messages)
        if response.tool_calls:
            return types.ErrorData(
                code=-32603,
                message="Unbound XBot sampling cannot execute tool calls",
            )
        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text=response.content),
            model=ctx.session.provider,
            stopReason="endTurn",
        )

    async def elicit(_request_context: Any, params: Any) -> Any:
        if ctx.request_user_input is None:
            return types.ElicitResult(action="cancel")
        question = params.message
        if isinstance(params, types.ElicitRequestURLParams):
            question = f"{question}\n{params.url}"
        result = await ctx.request_user_input(question, source="mcp_elicitation")
        if result.get("status") != "answered":
            return types.ElicitResult(action="cancel")
        answer = result.get("answer")
        if isinstance(params, types.ElicitRequestURLParams):
            accepted = str(answer).strip().lower() in {"y", "yes", "accept", "ok"}
            return types.ElicitResult(action="accept" if accepted else "decline")
        content = _form_content(answer, params.requestedSchema)
        if content is None:
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content=content)

    async def roots(_request_context: Any) -> types.ListRootsResult:
        workspace = Path(ctx.session.workspace_root).resolve()
        return types.ListRootsResult(roots=[
            types.Root(uri=workspace.as_uri(), name="workspace"),
        ])

    async def log_message(params: Any) -> None:
        logger.info("MCP server log [%s]: %s", params.level, params.data)

    return {
        "sampling_callback": sample,
        "elicitation_callback": elicit,
        "list_roots_callback": roots,
        "logging_callback": log_message,
    }


def _sampling_text(content: Any) -> str | None:
    blocks = content if isinstance(content, list) else [content]
    if not all(isinstance(block, types.TextContent) for block in blocks):
        return None
    return "\n".join(block.text for block in blocks)


def _form_content(answer: Any, schema: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(answer, dict):
        return answer
    if isinstance(answer, str):
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError:
            properties = list((schema.get("properties") or {}).keys())
            return {properties[0]: answer} if len(properties) == 1 else None
        return parsed if isinstance(parsed, dict) else None
    return None
