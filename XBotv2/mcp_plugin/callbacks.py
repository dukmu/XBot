"""Bridges MCP client requests to public XBot runtime capabilities."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp import types
from mcp.shared.context import RequestContext

from XBotv2.core.parts import ReasoningPart, TextPart
from XBotv2.core.provider import (
    ModelRequest,
    ProviderAssistant,
    ProviderMessage,
    ProviderSystem,
    ProviderUser,
)
from XBotv2.core import prompt_element
from XBotv2.core.tools import ToolCall
from XBotv2.core.domain import AuxiliaryRequest, RequestObservation, ResolvedModelSelection
from XBotv2.core.tokens import estimate_request_tokens
from XBotv2.interactions.contracts import InteractionsPort
from XBotv2.interactions.protocol import Answered
from XBotv2.llm import invoke_llm
from XBotv2.llm.contracts import ModelPort
from XBotv2.session.contracts import SessionPort
from XBotv2.usage import UsagePort

logger = logging.getLogger("xbotv2.mcp")


def client_callbacks(
    model: ModelPort,
    interactions: InteractionsPort,
    session: SessionPort,
    usage: UsagePort,
    model_selection: Callable[[], ResolvedModelSelection],
) -> dict[str, Any]:
    async def sample(request_context: Any, params: Any) -> Any:
        messages: list[ProviderMessage] = []
        if params.systemPrompt:
            messages.append(ProviderSystem(parts=(TextPart(text=prompt_element(
                    "mcp_sampling_system_prompt",
                    params.systemPrompt,
                    attributes={"source": "mcp_server"},
                )),)))
        for message in params.messages:
            text = _sampling_text(message.content)
            if text is None:
                return types.ErrorData(
                    code=-32602,
                    message="XBot sampling currently accepts text content only",
                )
            role = str(message.role)
            part = TextPart(text=text)
            if role == "user":
                messages.append(ProviderUser(parts=(part,)))
            elif role == "assistant":
                messages.append(ProviderAssistant(parts=(part,)))
            elif role == "system":
                messages.append(ProviderSystem(parts=(part,)))
            else:
                return types.ErrorData(
                    code=-32602,
                    message=f"XBot sampling does not support role {role!r}",
                )
        # The llm package owns the single-shot calling convention; MCP
        # sampling uses it instead of re-implementing the merge loop.
        request = tuple(messages)
        output_tokens = getattr(params, "maxTokens", None)
        selection = model_selection()
        if output_tokens is not None:
            selection = selection.model_copy(update={
                "generation": selection.generation.model_copy(update={
                    "max_output_tokens": max(1, int(output_tokens)),
                }),
            })
        model_request = ModelRequest(
            messages=request,
            tools=(),
            selection=selection,
        )
        try:
            aggregate = await invoke_llm(model, model_request)
        except RuntimeError as exc:
            return types.ErrorData(
                code=-32603,
                message=str(exc),
            )
        if any(isinstance(part, ToolCall) for part in aggregate.parts):
            return types.ErrorData(
                code=-32603,
                message="Unbound XBot sampling cannot execute tool calls",
            )
        await usage.record(
            RequestObservation(
                selection=selection,
                purpose=AuxiliaryRequest(
                    owner="mcp_sampling",
                    operation_id=str(request_context.request_id) or uuid4().hex,
                ),
                estimated_input_tokens=estimate_request_tokens(
                    model_request.messages,
                    model_request.tools,
                ),
                observed_context=aggregate.observed_context,
            ),
            aggregate.usage,
        )
        content = "".join(
            part.text
            for part in aggregate.parts
            if isinstance(part, (TextPart, ReasoningPart))
        )
        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text=content),
            model=selection.route.model,
            stopReason="endTurn",
        )

    async def elicit(request_context: RequestContext, params: Any) -> Any:
        tool_call_id = str(request_context.request_id)
        if not tool_call_id:
            logger.error("MCP elicitation request has no request id")
            return types.ElicitResult(action="cancel")
        question = params.message
        if isinstance(params, types.ElicitRequestURLParams):
            question = f"{question}\n{params.url}"
        result = await interactions.request_user_input(
            question,
            source="mcp_elicitation",
            tool_call_id=tool_call_id,
        )
        if not isinstance(result, Answered):
            return types.ElicitResult(action="cancel")
        answer = result.answer
        if isinstance(params, types.ElicitRequestURLParams):
            accepted = str(answer).strip().lower() in {"y", "yes", "accept", "ok"}
            return types.ElicitResult(action="accept" if accepted else "decline")
        content = _form_content(answer, params.requestedSchema)
        if content is None:
            return types.ElicitResult(action="decline")
        return types.ElicitResult(action="accept", content=content)

    async def roots(_request_context: Any) -> types.ListRootsResult:
        workspace = Path(session.workspace_root).resolve()
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
