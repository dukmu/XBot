"""Typed asynchronous client for the public XBot HTTP API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, AsyncIterator, Literal, TypeVar
from urllib.parse import quote

import httpx

from xbotv2.protocol.models import (
    AgentListResponse,
    AgentSelectionRequest,
    AgentSelectionResponse,
    CloseResponse,
    ErrorResponse,
    ForkResponse,
    HealthResponse,
    HelloRequest,
    HelloResponse,
    HistoryMutationResponse,
    InteractionResponse,
    InterruptResponse,
    MessageRequest,
    OpenSessionRequest,
    OpenSessionResponse,
    OpenThreadRequest,
    PermissionResponseRequest,
    PermissionDecision,
    ProviderListResponse,
    ProviderSelectionRequest,
    ProviderSelectionResponse,
    ServerEvent,
    SessionListResponse,
    SessionMode,
    SessionPolicyPatch,
    SessionPolicyResponse,
    SessionSummary,
    SandboxKey,
    SandboxValue,
    TaskListResponse,
    TaskStopResponse,
    ThreadListResponse,
    ThreadMessagesResponse,
    ThreadSummary,
    ToolListResponse,
    UndoRequest,
    UserInputResponseRequest,
    WireModel,
)
from xbotv2.protocol.sse import SseDecoder, decode_server_event
from xbotv2.protocol.version import PROTOCOL_VERSION

ResponseModel = TypeVar("ResponseModel", bound=WireModel)


class XBotClientError(RuntimeError):
    """Structured non-success response returned by the XBot server."""

    def __init__(self, status_code: int, error: ErrorResponse) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.status_code = status_code
        self.code = error.code
        self.message = error.message
        self.details = error.details
        self.retryable = error.retryable


class XBotClient:
    """Async client whose methods mirror the public OpenAPI resources."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4096",
        *,
        timeout: float = 30.0,
        uds_path: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if uds_path is not None and transport is not None:
            raise ValueError("uds_path and transport are mutually exclusive")
        if uds_path is not None:
            transport = httpx.AsyncHTTPTransport(uds=uds_path)
        self._timeout = timeout
        request_headers = {"Accept": "application/json", **dict(headers or {})}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=request_headers,
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> "XBotClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def health(self) -> HealthResponse:
        return await self._request("GET", "/health", HealthResponse)

    async def hello(
        self,
        *,
        client_name: str = "xbotv2-python",
        session_id: str | None = None,
        thread_id: str = "agent",
    ) -> HelloResponse:
        return await self._request(
            "POST",
            "/hello",
            HelloResponse,
            HelloRequest(
                client_name=client_name,
                protocol_version=PROTOCOL_VERSION,
                session_id=session_id,
                thread_id=thread_id,
            ),
        )

    async def list_providers(self) -> ProviderListResponse:
        return await self._request("GET", "/providers", ProviderListResponse)

    async def list_sessions(self) -> SessionListResponse:
        return await self._request("GET", "/sessions", SessionListResponse)

    async def open_session(
        self,
        *,
        session_id: str | None = None,
        thread_id: str = "agent",
        workspace_root: str | None = None,
        mode: SessionMode = "new",
        agent: str | None = None,
    ) -> OpenSessionResponse:
        return await self._request(
            "POST",
            "/sessions",
            OpenSessionResponse,
            OpenSessionRequest(
                session_id=session_id,
                thread_id=thread_id,
                workspace_root=workspace_root,
                mode=mode,
                agent=agent,
            ),
        )

    async def get_session(self, session_id: str) -> SessionSummary:
        return await self._request(
            "GET", f"/sessions/{_segment(session_id)}", SessionSummary
        )

    async def get_session_policy(self, session_id: str) -> SessionPolicyResponse:
        return await self._request(
            "GET",
            f"/sessions/{_segment(session_id)}/policy",
            SessionPolicyResponse,
        )

    async def update_session_policy(
        self,
        session_id: str,
        *,
        permissions: dict[str, PermissionDecision] | None = None,
        remove_permissions: list[str] | None = None,
        sandbox: dict[SandboxKey, SandboxValue] | None = None,
        remove_sandbox: list[SandboxKey] | None = None,
    ) -> SessionPolicyResponse:
        return await self._request(
            "PATCH",
            f"/sessions/{_segment(session_id)}/policy",
            SessionPolicyResponse,
            SessionPolicyPatch(
                permissions=permissions or {},
                remove_permissions=remove_permissions or [],
                sandbox=sandbox or {},
                remove_sandbox=remove_sandbox or [],
            ),
        )

    async def fork_session(self, session_id: str) -> ForkResponse:
        return await self._request(
            "POST", f"/sessions/{_segment(session_id)}/fork", ForkResponse
        )

    async def close_session(self, session_id: str) -> CloseResponse:
        return await self._request(
            "POST", f"/sessions/{_segment(session_id)}/close", CloseResponse
        )

    async def list_threads(self, session_id: str) -> ThreadListResponse:
        return await self._request(
            "GET", f"/sessions/{_segment(session_id)}/threads", ThreadListResponse
        )

    async def open_thread(
        self,
        session_id: str,
        *,
        thread_id: str,
        parent_thread_id: str = "agent",
        workspace_root: str | None = None,
        mode: SessionMode = "new",
        agent: str | None = None,
    ) -> OpenSessionResponse:
        return await self._request(
            "POST",
            f"/sessions/{_segment(session_id)}/threads",
            OpenSessionResponse,
            OpenThreadRequest(
                thread_id=thread_id,
                parent_thread_id=parent_thread_id,
                workspace_root=workspace_root,
                mode=mode,
                agent=agent,
            ),
        )

    async def get_thread(
        self, session_id: str, thread_id: str
    ) -> ThreadSummary:
        return await self._request(
            "GET", _thread_path(session_id, thread_id), ThreadSummary
        )

    async def close_thread(
        self, session_id: str, thread_id: str
    ) -> CloseResponse:
        return await self._request(
            "POST", f"{_thread_path(session_id, thread_id)}/close", CloseResponse
        )

    async def list_agents(
        self, session_id: str, thread_id: str
    ) -> AgentListResponse:
        return await self._request(
            "GET", f"{_thread_path(session_id, thread_id)}/agents", AgentListResponse
        )

    async def select_agent(
        self, session_id: str, thread_id: str, name: str
    ) -> AgentSelectionResponse:
        return await self._request(
            "PUT",
            f"{_thread_path(session_id, thread_id)}/agent",
            AgentSelectionResponse,
            AgentSelectionRequest(name=name),
        )

    async def select_provider(
        self, session_id: str, thread_id: str, name: str
    ) -> ProviderSelectionResponse:
        return await self._request(
            "PUT",
            f"{_thread_path(session_id, thread_id)}/provider",
            ProviderSelectionResponse,
            ProviderSelectionRequest(name=name),
        )

    async def list_tools(
        self, session_id: str, thread_id: str
    ) -> ToolListResponse:
        return await self._request(
            "GET", f"{_thread_path(session_id, thread_id)}/tools", ToolListResponse
        )

    async def list_messages(
        self, session_id: str, thread_id: str
    ) -> ThreadMessagesResponse:
        return await self._request(
            "GET",
            f"{_thread_path(session_id, thread_id)}/messages",
            ThreadMessagesResponse,
        )

    async def clear_history(
        self, session_id: str, thread_id: str
    ) -> HistoryMutationResponse:
        return await self._request(
            "POST",
            f"{_thread_path(session_id, thread_id)}/history/clear",
            HistoryMutationResponse,
        )

    async def undo_history(
        self, session_id: str, thread_id: str, count: int = 1
    ) -> HistoryMutationResponse:
        return await self._request(
            "POST",
            f"{_thread_path(session_id, thread_id)}/history/undo",
            HistoryMutationResponse,
            UndoRequest(count=count),
        )

    async def list_tasks(
        self, session_id: str, thread_id: str
    ) -> TaskListResponse:
        return await self._request(
            "GET", f"{_thread_path(session_id, thread_id)}/tasks", TaskListResponse
        )

    async def stop_task(
        self, session_id: str, thread_id: str, task_id: str
    ) -> TaskStopResponse:
        return await self._request(
            "POST",
            f"{_thread_path(session_id, thread_id)}/tasks/{_segment(task_id)}/stop",
            TaskStopResponse,
        )

    async def stop_all_tasks(
        self, session_id: str, thread_id: str
    ) -> TaskStopResponse:
        return await self._request(
            "POST",
            f"{_thread_path(session_id, thread_id)}/tasks/stop",
            TaskStopResponse,
        )

    async def interrupt(
        self, session_id: str, thread_id: str
    ) -> InterruptResponse:
        return await self._request(
            "POST", f"{_thread_path(session_id, thread_id)}/interrupt", InterruptResponse
        )

    async def respond_permission(
        self,
        session_id: str,
        thread_id: str,
        *,
        request_id: str,
        decision: Literal["allow", "deny"],
        scope: Literal["once", "session"] = "once",
    ) -> InteractionResponse:
        return await self._request(
            "POST",
            f"{_thread_path(session_id, thread_id)}/interactions/permission-response",
            InteractionResponse,
            PermissionResponseRequest(
                request_id=request_id,
                decision=decision,
                scope=scope,
            ),
        )

    async def respond_user_input(
        self,
        session_id: str,
        thread_id: str,
        *,
        request_id: str,
        answer: Any,
    ) -> InteractionResponse:
        return await self._request(
            "POST",
            f"{_thread_path(session_id, thread_id)}/interactions/user-input",
            InteractionResponse,
            UserInputResponseRequest(request_id=request_id, answer=answer),
        )

    def send_message(
        self,
        session_id: str,
        thread_id: str,
        content: str,
        *,
        request_id: str = "",
    ) -> AsyncIterator[ServerEvent]:
        return self._stream(
            "POST",
            f"{_thread_path(session_id, thread_id)}/messages",
            MessageRequest(content=content, request_id=request_id),
        )

    def stream_events(
        self, session_id: str, thread_id: str
    ) -> AsyncIterator[ServerEvent]:
        return self._stream(
            "GET", f"{_thread_path(session_id, thread_id)}/events"
        )

    async def _request(
        self,
        method: str,
        path: str,
        response_model: type[ResponseModel],
        payload: WireModel | None = None,
    ) -> ResponseModel:
        response = await self._http.request(
            method,
            path,
            json=payload.model_dump() if payload is not None else None,
        )
        _raise_for_status(response)
        return response_model.model_validate(response.json())

    async def _stream(
        self,
        method: str,
        path: str,
        payload: WireModel | None = None,
    ) -> AsyncIterator[ServerEvent]:
        async with self._http.stream(
            method,
            path,
            json=payload.model_dump() if payload is not None else None,
            timeout=httpx.Timeout(self._timeout, read=None),
        ) as response:
            _raise_for_status(response)
            decoder = SseDecoder()
            async for line in response.aiter_lines():
                message = decoder.feed(line)
                if message is None:
                    continue
                event = decode_server_event(message)
                yield event
                if event.type == "end":
                    return
            message = decoder.finish()
            if message is not None:
                yield decode_server_event(message)


def _thread_path(session_id: str, thread_id: str) -> str:
    return f"/sessions/{_segment(session_id)}/threads/{_segment(thread_id)}"


def _segment(value: str) -> str:
    return quote(value, safe="")


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        error = ErrorResponse.model_validate(response.json())
    except (ValueError, TypeError):
        error = ErrorResponse(
            code=str(response.status_code),
            message=response.text or response.reason_phrase,
        )
    raise XBotClientError(response.status_code, error)


__all__ = ["XBotClient", "XBotClientError"]
