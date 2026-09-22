"""Typed asynchronous client for the public XBot HTTP API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, AsyncIterator, Literal, TypeVar
from urllib.parse import quote

import httpx
from pydantic import JsonValue

from XBotv2.agents import (
    AgentListResponse,
    AgentSelectionRequest,
    AgentSelectionResponse,
)
from XBotv2.agentloop import ToolListResponse
from XBotv2.config import (
    PermissionDecision,
    PatchPluginConfig,
    PluginConfigCatalog,
    PluginConfigScope,
    SandboxKey,
    SandboxValue,
    SessionPolicyPatch,
    SessionPolicyResponse,
)
from XBotv2.interactions import (
    InteractionResponse,
    UserInputResponseRequest,
)
from XBotv2.jobs import JobListResponse, JobStopResponse
from XBotv2.llm import (
    EffortSelectionRequest,
    EffortSelectionResponse,
    ProviderCatalog,
    ProviderSelectionRequest,
    ProviderSelectionResponse,
)
from XBotv2.permissions import PermissionResponseRequest
from XBotv2.protocol import (
    ErrorResponse,
    HealthResponse,
    HelloRequest,
    HelloResponse,
    ServerEvent,
    WireModel,
)
from XBotv2.session import (
    AttachmentInput,
    CloseResponse,
    DeleteSessionResponse,
    ForkResponse,
    HistoryMutationResponse,
    ImageInput,
    InterruptResponse,
    MessageRequest,
    OpenSessionRequest,
    OpenSessionResponse,
    OpenThreadRequest,
    PendingInputListResponse,
    PendingInputUpdateRequest,
    RegenerateRequest,
    SessionListResponse,
    SessionMode,
    SessionSummary,
    ThreadListResponse,
    ThreadMessagesResponse,
    ThreadTrajectoryResponse,
    ThreadSummary,
    UndoRequest,
)
from XBotv2.commands import (
    CommandListResponse,
    CommandRequest,
    CommandResponse,
)
from XBotv2.workspaces import WorkspaceSnapshot
from XBotv2.protocol.sse import SseDecoder, decode_server_event
from XBotv2.protocol.version import PROTOCOL_VERSION

ResponseModel = TypeVar("ResponseModel", bound=WireModel)

# Hosts that name this machine. A request to one of them cannot need a proxy, and
# consulting the proxy settings for it is not merely useless: ``httpx`` parses
# every ``NO_PROXY`` entry as a URL at client construction, so an entry such as
# ``[::1]`` (its port parses as ``":1]"``) makes the client impossible to build.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def uses_proxy_environment(base_url: str, *, uds_path: str | None = None) -> bool:
    """Whether the process proxy settings may apply to this target.

    Local targets -- loopback and Unix sockets -- opt out; anything else keeps
    httpx's default, so a client behind a corporate proxy still works.
    """
    if uds_path is not None:
        return False
    try:
        host = httpx.URL(base_url).host
    except (httpx.InvalidURL, ValueError):
        return True
    return str(host).lower() not in _LOOPBACK_HOSTS


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
        trust_env: bool | None = None,
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
            trust_env=(
                uses_proxy_environment(base_url, uds_path=uds_path)
                if trust_env is None
                else trust_env
            ),
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

    async def list_providers(self) -> ProviderCatalog:
        return await self._request("GET", "/providers", ProviderCatalog)

    async def list_sessions(self) -> SessionListResponse:
        return await self._request("GET", "/sessions", SessionListResponse)

    async def list_workspaces(self) -> WorkspaceSnapshot:
        """The workspace catalogue the Web rail groups sessions by."""
        return await self._request("GET", "/workspaces", WorkspaceSnapshot)

    async def open_session(
        self,
        *,
        session_id: str | None = None,
        thread_id: str = "agent",
        workspace_root: str | None = None,
        mode: SessionMode = "new",
        agent: str | None = None,
        history_limit: int | None = None,
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
                history_limit=history_limit,
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

    async def list_plugin_config(
        self,
        session_id: str,
        thread_id: str,
        *,
        scope: PluginConfigScope = "workspace",
    ) -> PluginConfigCatalog:
        return await self._request(
            "GET",
            f"{thread_path(session_id, thread_id)}/plugin-config",
            PluginConfigCatalog,
            params={"scope": scope},
        )

    async def update_plugin_config(
        self,
        session_id: str,
        thread_id: str,
        plugin_id: str,
        patch: PatchPluginConfig,
    ) -> PluginConfigCatalog:
        return await self._request(
            "PATCH",
            f"{thread_path(session_id, thread_id)}/plugin-config/{_segment(plugin_id)}",
            PluginConfigCatalog,
            patch,
            params={"scope": patch.scope},
        )

    async def fork_session(self, session_id: str) -> ForkResponse:
        return await self._request(
            "POST", f"/sessions/{_segment(session_id)}/fork", ForkResponse
        )

    async def delete_session(self, session_id: str) -> DeleteSessionResponse:
        return await self._request(
            "DELETE", f"/sessions/{_segment(session_id)}", DeleteSessionResponse
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
        history_limit: int | None = None,
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
                history_limit=history_limit,
            ),
        )

    async def get_thread(
        self, session_id: str, thread_id: str
    ) -> ThreadSummary:
        return await self._request(
            "GET", thread_path(session_id, thread_id), ThreadSummary
        )

    async def close_thread(
        self, session_id: str, thread_id: str
    ) -> CloseResponse:
        return await self._request(
            "POST", f"{thread_path(session_id, thread_id)}/close", CloseResponse
        )

    async def list_commands(
        self, session_id: str, thread_id: str
    ) -> CommandListResponse:
        """What this thread can run, and what each command touches."""
        return await self._request(
            "GET",
            f"{thread_path(session_id, thread_id)}/commands",
            CommandListResponse,
        )

    async def run_command(
        self,
        session_id: str,
        thread_id: str,
        *,
        raw: str,
    ) -> CommandResponse:
        """Run one server command, given the line exactly as the user typed it."""
        return await self._request(
            "POST",
            f"{thread_path(session_id, thread_id)}/commands",
            CommandResponse,
            CommandRequest(raw=raw),
        )

    async def list_agents(
        self, session_id: str, thread_id: str
    ) -> AgentListResponse:
        return await self._request(
            "GET", f"{thread_path(session_id, thread_id)}/agents", AgentListResponse
        )

    async def select_agent(
        self, session_id: str, thread_id: str, name: str
    ) -> AgentSelectionResponse:
        return await self._request(
            "PUT",
            f"{thread_path(session_id, thread_id)}/agent",
            AgentSelectionResponse,
            AgentSelectionRequest(name=name),
        )

    async def select_effort(
        self, session_id: str, thread_id: str, effort: str
    ) -> EffortSelectionResponse:
        return await self._request(
            "PUT",
            f"{thread_path(session_id, thread_id)}/effort",
            EffortSelectionResponse,
            EffortSelectionRequest(effort=effort),
        )

    async def select_provider(
        self,
        session_id: str,
        thread_id: str,
        name: str,
        model: str | None = None,
    ) -> ProviderSelectionResponse:
        return await self._request(
            "PUT",
            f"{thread_path(session_id, thread_id)}/provider",
            ProviderSelectionResponse,
            ProviderSelectionRequest(name=name, model=model),
        )

    async def list_tools(
        self, session_id: str, thread_id: str
    ) -> ToolListResponse:
        return await self._request(
            "GET", f"{thread_path(session_id, thread_id)}/tools", ToolListResponse
        )

    async def list_messages(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ThreadMessagesResponse:
        return await self._request(
            "GET",
            f"{thread_path(session_id, thread_id)}/messages",
            ThreadMessagesResponse,
            params={
                key: value
                for key, value in {"cursor": cursor, "limit": limit}.items()
                if value is not None
            },
        )

    async def list_trajectory(
        self,
        session_id: str,
        thread_id: str,
        *,
        cursor: str | None = None,
        before: int | None = None,
        limit: int = 160,
    ) -> ThreadTrajectoryResponse:
        return await self._request(
            "GET",
            f"{thread_path(session_id, thread_id)}/trajectory",
            ThreadTrajectoryResponse,
            params={
                key: value
                for key, value in {
                    "cursor": cursor,
                    "before": before,
                    "limit": limit,
                }.items()
                if value is not None
            },
        )

    async def read_artifact(
        self,
        session_id: str,
        thread_id: str,
        artifact_id: str,
    ) -> bytes:
        response = await self._http.get(
            f"{thread_path(session_id, thread_id)}/artifacts/"
            f"{quote(artifact_id, safe='/')}"
        )
        await _raise_for_status(response)
        return response.content

    async def clear_history(
        self, session_id: str, thread_id: str
    ) -> HistoryMutationResponse:
        return await self._request(
            "POST",
            f"{thread_path(session_id, thread_id)}/history/clear",
            HistoryMutationResponse,
        )

    async def undo_history(
        self, session_id: str, thread_id: str, count: int = 1
    ) -> HistoryMutationResponse:
        return await self._request(
            "POST",
            f"{thread_path(session_id, thread_id)}/history/undo",
            HistoryMutationResponse,
            UndoRequest(count=count),
        )

    async def list_jobs(
        self, session_id: str, thread_id: str
    ) -> JobListResponse:
        return await self._request(
            "GET", f"{thread_path(session_id, thread_id)}/jobs", JobListResponse
        )

    async def stop_job(
        self, session_id: str, thread_id: str, job_id: str
    ) -> JobStopResponse:
        return await self._request(
            "POST",
            f"{thread_path(session_id, thread_id)}/jobs/{_segment(job_id)}/stop",
            JobStopResponse,
        )

    async def stop_all_jobs(
        self, session_id: str, thread_id: str
    ) -> JobStopResponse:
        return await self._request(
            "POST",
            f"{thread_path(session_id, thread_id)}/jobs/stop",
            JobStopResponse,
        )

    async def interrupt(
        self, session_id: str, thread_id: str
    ) -> InterruptResponse:
        return await self._request(
            "POST", f"{thread_path(session_id, thread_id)}/interrupt", InterruptResponse
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
            f"{thread_path(session_id, thread_id)}/interactions/permission-response",
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
        answer: JsonValue,
    ) -> InteractionResponse:
        return await self._request(
            "POST",
            f"{thread_path(session_id, thread_id)}/interactions/user-input",
            InteractionResponse,
            UserInputResponseRequest(request_id=request_id, answer=answer),
        )

    async def list_pending_inputs(
        self,
        session_id: str,
        thread_id: str,
    ) -> PendingInputListResponse:
        return await self._request(
            "GET",
            f"{thread_path(session_id, thread_id)}/queue",
            PendingInputListResponse,
        )

    async def update_pending_input(
        self,
        session_id: str,
        thread_id: str,
        message_id: str,
        *,
        action: Literal["edit", "remove", "steer"],
        content: str = "",
    ) -> PendingInputListResponse:
        return await self._request(
            "PATCH",
            f"{thread_path(session_id, thread_id)}/queue/{_segment(message_id)}",
            PendingInputListResponse,
            PendingInputUpdateRequest(action=action, content=content),
        )

    async def send_message(
        self,
        session_id: str,
        thread_id: str,
        content: str,
        *,
        request_id: str = "",
        delivery: Literal["queue", "steer"] = "steer",
        images: list[ImageInput] | None = None,
        attachments: list[AttachmentInput] | None = None,
    ) -> None:
        await self._request_no_content(
            "POST",
            f"{thread_path(session_id, thread_id)}/messages",
            MessageRequest(
                content=content,
                request_id=request_id,
                delivery=delivery,
                images=images or [],
                attachments=attachments or [],
            ),
        )

    async def regenerate_message(
        self,
        session_id: str,
        thread_id: str,
        *,
        request_id: str = "",
    ) -> None:
        await self._request_no_content(
            "POST",
            f"{thread_path(session_id, thread_id)}/history/regenerate",
            RegenerateRequest(request_id=request_id),
        )

    def stream_events(
        self,
        session_id: str,
        thread_id: str,
        *,
        after: int | None = None,
    ) -> AsyncIterator[ServerEvent]:
        return self._stream(
            "GET",
            f"{thread_path(session_id, thread_id)}/events",
            params={"after": after} if after is not None else None,
        )

    def stream_workspace_events(
        self,
        *,
        after: int | None = None,
    ) -> AsyncIterator[ServerEvent]:
        """Stream the process session/workspace catalog used by Web clients."""
        return self._stream(
            "GET",
            "/workspaces/events",
            params={"after": after} if after is not None else None,
        )

    async def _request(
        self,
        method: str,
        path: str,
        response_model: type[ResponseModel],
        payload: WireModel | None = None,
        *,
        params: Mapping[str, JsonValue] | None = None,
    ) -> ResponseModel:
        response = await self._http.request(
            method,
            path,
            json=payload.model_dump() if payload is not None else None,
            params=params,
        )
        await _raise_for_status(response)
        return response_model.model_validate(response.json())

    async def _request_no_content(
        self,
        method: str,
        path: str,
        payload: WireModel | None = None,
    ) -> None:
        response = await self._http.request(
            method,
            path,
            json=payload.model_dump() if payload is not None else None,
        )
        await _raise_for_status(response)

    async def _stream(
        self,
        method: str,
        path: str,
        payload: WireModel | None = None,
        *,
        params: Mapping[str, JsonValue] | None = None,
    ) -> AsyncIterator[ServerEvent]:
        async with self._http.stream(
            method,
            path,
            json=payload.model_dump() if payload is not None else None,
            params=params,
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(self._timeout, read=None),
        ) as response:
            await _raise_for_status(response)
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


def thread_path(session_id: str, thread_id: str) -> str:
    return f"/sessions/{_segment(session_id)}/threads/{_segment(thread_id)}"


def _segment(value: str) -> str:
    return quote(value, safe="")


async def _raise_for_status(response: httpx.Response) -> None:
    """Raise the typed client error for one non-success response.

    A streaming response has not read its body yet, so the error payload must
    be read first; otherwise httpx raises ``ResponseNotRead`` and the real
    status and error code are lost.
    """
    if response.is_success:
        return
    if not response.is_stream_consumed:
        await response.aread()
    try:
        error = ErrorResponse.model_validate(response.json())
    except (ValueError, TypeError):
        error = ErrorResponse(
            code=str(response.status_code),
            message=response.text or response.reason_phrase,
        )
    raise XBotClientError(response.status_code, error)


__all__ = ["XBotClient", "XBotClientError"]
