"""ACP Agent implementation backed by the XBot session runtime."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from acp import (
    PROTOCOL_VERSION,
    RequestError,
    update_agent_message_text,
)
from acp.schema import (
    AgentCapabilities,
    AllowedOutcome,
    AvailableCommand,
    AvailableCommandsUpdate,
    ClientCapabilities,
    CloseSessionResponse,
    ConfigOptionUpdate,
    DeniedOutcome,
    ElicitationFormSessionMode,
    ElicitationSchema,
    ElicitationStringPropertySchema,
    ForkSessionResponse,
    HttpMcpServer,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    McpServerStdio,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    RequestPermissionResponse,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionForkCapabilities,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SseMcpServer,
    ToolCallProgress,
    Usage,
)

from XBotv2.main import __version__
from XBotv2.agentloop.protocol import LoopError, LoopTurnEnded
from XBotv2.acp_plugin.events import ACPEventMapper, replay_history
from XBotv2.session import SessionEventFrame, conversation_replay
from XBotv2.agents import LIST_AGENTS, SELECT_AGENT, SelectAgent
from XBotv2.commands import (
    EXECUTE_COMMAND,
    LIST_COMMANDS,
    CommandCatalog,
    ExecuteCommand,
)
from pydantic import JsonValue

from XBotv2.core import EmptyRequest
from XBotv2.core.domain import TokenCounters, TurnId, TurnScope
from XBotv2.interactions.contracts import InteractionRequest, InteractionResolution
from XBotv2.interactions.protocol import (
    Answered,
    InputCancelled,
    InputTimedOut,
    UserInputRequest,
)
from XBotv2.permissions.contracts import (
    Allowed,
    Denied,
    NamedPermission,
    PermissionRequest,
    ToolPermission,
)
from XBotv2.core.errors import OperationError
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.llm import LIST_PROVIDERS, SELECT_PROVIDER, SelectProvider
from XBotv2.mcp_plugin import MCP_PLUGIN_ID
from XBotv2.session.contracts import (
    ImageInput,
    OpenSession,
    SendMessage,
    SessionEventSubscription,
    SessionNotFound,
    ThreadSummary,
    ThreadNotActive,
)
from XBotv2.session.contracts import SessionsPort
from XBotv2.session.protocol import AgentConfiguredEvent, MessagePublishedEvent

_MCP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(slots=True)
class ActivePrompt:
    request_id: str
    mapper: ACPEventMapper
    completed: asyncio.Event
    turn_id: TurnId | None = None
    failure: Exception | None = None


class XBotACPAgent:
    """Expose XBot as a stable ACP v1 Agent."""

    def __init__(
        self,
        *,
        sessions: SessionsPort,
        provider_name: str | None,
        no_plugins: bool = False,
        selected_agent: str | None = None,
        llm_override: Any | None = None,
        runtime_log: RuntimeLog = DEFAULT_RUNTIME_LOG,
    ) -> None:
        self.sessions = sessions
        self.provider_name = provider_name
        self.no_plugins = no_plugins
        self.selected_agent = selected_agent
        self.llm_override = llm_override
        self._log = runtime_log.bind("acp")
        self.connection: Any | None = None
        self.client_capabilities: ClientCapabilities | None = None
        self._commands_announced: set[str] = set()
        self._event_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_prompts: dict[str, ActivePrompt] = {}

    def on_connect(self, connection: Any) -> None:
        self.connection = connection

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **_: Any,
    ) -> InitializeResponse:
        del client_info
        del protocol_version
        self.client_capabilities = client_capabilities
        self._log.info(
            "acp.initialized",
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=client_capabilities is not None,
        )
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=True,
                    audio=False,
                    embedded_context=True,
                ),
                mcp_capabilities=McpCapabilities(
                    http=not self.no_plugins,
                    sse=False,
                ),
                session_capabilities=SessionCapabilities(
                    list=SessionListCapabilities(),
                    fork=SessionForkCapabilities(),
                    resume=SessionResumeCapabilities(),
                    close=SessionCloseCapabilities(),
                ),
            ),
            agent_info=Implementation(
                name="xbot",
                title="XBot",
                version=__version__,
            ),
            auth_methods=[],
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **_: Any,
    ) -> NewSessionResponse:
        self._reject_additional_directories(additional_directories)
        workspace = _workspace(cwd)
        opened = await self.sessions.open(OpenSession(
            session_id=None,
            thread_id="agent",
            provider_name=self.provider_name,
            workspace_root=workspace,
            selected_agent=self.selected_agent,
            mode="new",
            no_plugins=self.no_plugins,
            plugin_configs=self._mcp_plugin_config(
                workspace, None, mcp_servers
            ),
            model_override=self.llm_override,
        ))
        await self._prepare_session(opened.key.session_id, opened.event_cursor)
        self._log.info(
            "acp.session.created",
            session_id=opened.key.session_id,
            workspace_root=workspace,
        )
        return NewSessionResponse(
            session_id=opened.key.session_id,
            config_options=await self._config_options(opened.key.session_id),
        )

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **_: Any,
    ) -> ResumeSessionResponse:
        self._reject_additional_directories(additional_directories)
        await self._open_existing(session_id, cwd, mcp_servers)
        self._log.info("acp.session.resumed", session_id=session_id)
        return ResumeSessionResponse(
            config_options=await self._config_options(session_id)
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **_: Any,
    ) -> LoadSessionResponse:
        self._reject_additional_directories(additional_directories)
        await self._open_existing(session_id, cwd, mcp_servers)
        await self._replay_history(session_id)
        self._log.info("acp.session.loaded", session_id=session_id)
        return LoadSessionResponse(
            config_options=await self._config_options(session_id)
        )

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **_: Any,
    ) -> ListSessionsResponse:
        if cursor:
            return ListSessionsResponse(sessions=[])
        sessions: list[SessionInfo] = []
        for snapshot in await self.sessions.list_sessions():
            workspace = snapshot.workspace_root
            if not workspace or (
                cwd and Path(workspace).resolve() != Path(cwd).resolve()
            ):
                continue
            sessions.append(SessionInfo(
                session_id=snapshot.session_id,
                cwd=workspace,
                title=snapshot.title,
            ))
        self._log.debug("acp.sessions.listed", sessions=len(sessions))
        return ListSessionsResponse(sessions=sessions)

    async def close_session(
        self, session_id: str, **_: Any
    ) -> CloseSessionResponse:
        await self.sessions.close_session(session_id)
        task_entry = self._event_tasks.pop(session_id, None)
        if task_entry is not None:
            await asyncio.gather(task_entry, return_exceptions=True)
        self._commands_announced.discard(session_id)
        self._log.info("acp.session.closed", session_id=session_id)
        return CloseSessionResponse()

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **_: Any,
    ) -> ForkSessionResponse:
        self._reject_additional_directories(additional_directories)
        workspace = _workspace(cwd)
        snapshot = await self.sessions.session_summary(session_id)
        stored_workspace = snapshot.workspace_root
        if stored_workspace and Path(stored_workspace).resolve() != Path(workspace):
            raise RequestError.invalid_params({
                "sessionId": session_id,
                "cwd": cwd,
                "expectedCwd": stored_workspace,
            })

        try:
            forked_id = await self.sessions.fork_session(session_id)
        except OperationError as exc:
            raise RequestError.invalid_params({
                "sessionId": session_id,
                "reason": str(exc),
            }) from exc

        await self._open_existing(forked_id, workspace, mcp_servers)
        self._log.info(
            "acp.session.forked",
            session_id=session_id,
            forked_session_id=forked_id,
        )
        return ForkSessionResponse(
            session_id=forked_id,
            config_options=await self._config_options(forked_id),
        )

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **_: Any,
    ) -> PromptResponse:
        started = time.perf_counter()
        summary = await self._thread(session_id)
        content, images = _prompt_content(prompt)
        command = await self._slash_command(session_id, content)
        if command is not None:
            self._log.info(
                "acp.command.started",
                session_id=session_id,
                command=command[0],
                argument_chars=len(command[1]),
            )
            await self._run_command(session_id, *command)
            self._log.info(
                "acp.command.finished",
                session_id=session_id,
                command=command[0],
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            return PromptResponse(stop_reason="end_turn")

        if session_id not in self._commands_announced:
            await self._announce_commands(session_id)
            self._commands_announced.add(session_id)

        if session_id in self._active_prompts:
            raise RequestError.invalid_request({
                "sessionId": session_id,
                "reason": "a prompt is already running",
            })
        request_id = f"acp:{session_id}"
        prompt = ActivePrompt(
            request_id,
            ACPEventMapper(context_size=summary.context_window),
            asyncio.Event(),
        )
        self._active_prompts[session_id] = prompt
        try:
            await self.sessions.send_message(SendMessage(
                session_id=session_id,
                thread_id="agent",
                content=content,
                request_id=request_id,
                images=tuple(images),
            ))
            await prompt.completed.wait()
        finally:
            self._active_prompts.pop(session_id, None)
        if prompt.failure is not None:
            raise prompt.failure
        if prompt.mapper.error is not None:
            self._log.error(
                "acp.prompt.failed",
                session_id=session_id,
                error_type="mapped_runtime_error",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            raise RequestError.internal_error(
                prompt.mapper.error.model_dump(mode="json")
            )
        self._log.info(
            "acp.prompt.finished",
            session_id=session_id,
            content_chars=len(content),
            images=len(images),
            stop_reason=prompt.mapper.stop_reason,
            usage=prompt.mapper.usage,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return PromptResponse(
            stop_reason=prompt.mapper.stop_reason,
            usage=_usage(prompt.mapper.usage),
        )

    async def cancel(self, session_id: str, **_: Any) -> None:
        result = await self.sessions.interrupt(session_id, "agent")
        self._log.info(
            "acp.session.cancelled",
            session_id=session_id,
            cancelled=result.cancelled,
        )

    async def set_session_mode(
        self, session_id: str, mode_id: str, **_: Any
    ) -> None:
        del session_id, mode_id
        raise RequestError.method_not_found("session/set_mode")

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **_: Any,
    ) -> SetSessionConfigOptionResponse:
        if not isinstance(value, str):
            raise RequestError.invalid_params({
                "configId": config_id,
                "value": value,
            })
        try:
            if config_id == "agent":
                await self.sessions.dispatch(
                    session_id,
                    "agent",
                    SELECT_AGENT,
                    SelectAgent(value),
                )
            elif config_id == "provider":
                await self.sessions.dispatch(
                    session_id,
                    "agent",
                    SELECT_PROVIDER,
                    SelectProvider(value),
                )
            else:
                raise RequestError.invalid_params({"configId": config_id})
        except OperationError as exc:
            raise RequestError.invalid_params({
                "configId": config_id,
                "value": value,
                "reason": str(exc),
            }) from exc
        options = await self._config_options(session_id)
        await self._update(
            session_id,
            ConfigOptionUpdate(
                session_update="config_option_update",
                config_options=options,
            ),
        )
        self._log.info(
            "acp.config.updated",
            session_id=session_id,
            config_id=config_id,
            value=value,
        )
        return SetSessionConfigOptionResponse(config_options=options)

    async def authenticate(self, method_id: str, **_: Any) -> None:
        raise RequestError.method_not_found(f"authenticate:{method_id}")

    async def ext_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(
        self, method: str, params: dict[str, Any]
    ) -> None:
        del method, params

    async def close(self) -> None:
        for prompt in self._active_prompts.values():
            prompt.failure = RuntimeError("ACP event delivery stopped")
            prompt.completed.set()
        for task in self._event_tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(
            *self._event_tasks.values(),
            return_exceptions=True,
        )
        self._event_tasks.clear()

    async def _open_existing(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[Any] | None = None,
    ) -> None:
        try:
            snapshot = await self.sessions.session_summary(session_id)
        except SessionNotFound as exc:
            raise RequestError.resource_not_found(session_id) from exc
        stored_workspace = snapshot.workspace_root
        workspace = _workspace(cwd)
        if stored_workspace and Path(stored_workspace).resolve() != Path(workspace):
            raise RequestError.invalid_params({
                "sessionId": session_id,
                "cwd": cwd,
                "expectedCwd": stored_workspace,
            })
        try:
            opened = await self.sessions.open(OpenSession(
                session_id=session_id,
                thread_id="agent",
                provider_name=self.provider_name,
                workspace_root=workspace,
                mode="resume",
                no_plugins=self.no_plugins,
                plugin_configs=self._mcp_plugin_config(
                    workspace, session_id, mcp_servers
                ),
                model_override=self.llm_override,
            ))
        except SessionNotFound as exc:
            raise RequestError.resource_not_found(session_id) from exc
        await self._prepare_session(session_id, opened.event_cursor)

    async def _prepare_session(self, session_id: str, event_cursor: int) -> None:
        existing = self._event_tasks.get(session_id)
        if existing is not None:
            if not existing.done():
                existing.cancel()
            await asyncio.gather(existing, return_exceptions=True)
        events = await self.sessions.stream_events(
            session_id,
            "agent",
            after=event_cursor,
        )
        task = asyncio.create_task(
            self._forward_session_events(session_id, events),
            name=f"xbot-acp-events-{session_id}",
        )
        self._event_tasks[session_id] = task

    async def _thread(self, session_id: str) -> ThreadSummary:
        try:
            return await self.sessions.thread_summary(session_id, "agent")
        except (SessionNotFound, ThreadNotActive) as exc:
            raise RequestError.resource_not_found(session_id) from exc

    async def _forward_session_events(
        self,
        session_id: str,
        events: SessionEventSubscription,
    ) -> None:
        try:
            summary = await self._thread(session_id)
            mapper = ACPEventMapper(context_size=summary.context_window)
            fallback_window = summary.context_window
            async for frame in events:
                await self._resolve_interaction(session_id, frame.event)
                active = self._active_prompts.get(session_id)
                if (
                    active is not None
                    and active.turn_id is None
                    and isinstance(frame.scope, TurnScope)
                    and isinstance(frame.event, MessagePublishedEvent)
                    and frame.event.record.root.id == active.request_id
                ):
                    active.turn_id = frame.scope.turn_id
                active_prompt = (
                    active
                    if (
                        active is not None
                        and isinstance(frame.scope, TurnScope)
                        and frame.scope.turn_id == active.turn_id
                    )
                    else None
                )
                event_mapper = active_prompt.mapper if active_prompt else mapper
                if (
                    isinstance(frame.event, AgentConfiguredEvent)
                ):
                    # Track the live window locally: the mapper instance is
                    # never mutated from outside.
                    fallback_window = (
                        frame.event.runtime_selection.model.context_window
                    )
                for update in event_mapper.updates(
                    frame.event,
                    fallback_context_size=fallback_window,
                ):
                    await self._update(session_id, update)
                if active_prompt is not None:
                    if isinstance(frame.event, (LoopTurnEnded, LoopError)):
                        active_prompt.completed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            active = self._active_prompts.get(session_id)
            if active is not None:
                active.failure = exc
                active.completed.set()
            self._log.error(
                "acp.events.failed",
                session_id=session_id,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            await events.aclose()
            # The session stream can end without a turn terminal frame (the
            # session closed, or the subscription stopped) and the task can be
            # cancelled. An active prompt must be released either way, or its
            # waiter — and the session's prompt slot — hang forever.
            active = self._active_prompts.get(session_id)
            if active is not None and not active.completed.is_set():
                if active.failure is None:
                    active.failure = RuntimeError(
                        "session event stream ended before the turn completed"
                    )
                active.completed.set()

    async def _update(self, session_id: str, update: Any) -> None:
        if self.connection is None:
            raise RequestError.internal_error({"reason": "ACP client disconnected"})
        await self.connection.session_update(session_id=session_id, update=update)

    async def _command_catalog(self, session_id: str) -> CommandCatalog:
        return await self.sessions.dispatch(
            session_id,
            "agent",
            LIST_COMMANDS,
            EmptyRequest(),
        )

    async def _slash_command(
        self,
        session_id: str,
        content: str,
    ) -> tuple[str, str] | None:
        if not content.startswith("/") or "\n" in content:
            return None
        raw = content[1:]
        name, _, args = raw.partition(" ")
        catalog = await self._command_catalog(session_id)
        command = next(
            (item for item in catalog.commands if item.name == name),
            None,
        )
        if command is None or command.kind != "server":
            return None
        return name, args

    async def _announce_commands(self, session_id: str) -> None:
        commands = (await self._command_catalog(session_id)).commands
        if not commands:
            return
        await self._update(
            session_id,
            AvailableCommandsUpdate(
                session_update="available_commands_update",
                available_commands=[
                    AvailableCommand(
                        name=item.name,
                        description=item.description,
                    )
                    for item in commands
                ],
            ),
        )

    async def _config_options(
        self,
        session_id: str,
    ) -> list[SessionConfigOptionSelect]:
        options: list[SessionConfigOptionSelect] = []
        catalog = await self.sessions.dispatch(
            session_id,
            "agent",
            LIST_AGENTS,
            EmptyRequest(),
        )
        definitions = catalog.agents
        agents = [
            definition
            for definition in definitions
            if definition.mode != "subagent"
        ]
        if agents:
            options.append(SessionConfigOptionSelect(
                id="agent",
                name="Agent",
                category="_agent",
                type="select",
                current_value=catalog.active or agents[0].name,
                options=[
                    SessionConfigSelectOption(
                        value=definition.name,
                        name=definition.name,
                        description=definition.description or None,
                    )
                    for definition in agents
                ],
            ))

        providers = await self.sessions.dispatch(
            session_id,
            "agent",
            LIST_PROVIDERS,
            EmptyRequest(),
        )
        if providers.providers:
            summary = await self._thread(session_id)
            options.append(SessionConfigOptionSelect(
                id="provider",
                name="Provider / model",
                category="model",
                type="select",
                current_value=summary.provider,
                options=[
                    SessionConfigSelectOption(value=item.name, name=item.name)
                    for item in providers.providers
                ],
            ))
        return options

    async def _run_command(
        self, session_id: str, name: str, raw_args: str
    ) -> None:
        result = await self.sessions.dispatch(
            session_id,
            "agent",
            EXECUTE_COMMAND,
            ExecuteCommand(name, "server", raw_args),
        )
        await self._update(
            session_id,
            update_agent_message_text(result.message),
        )

    async def _replay_history(self, session_id: str) -> None:
        pages = []
        cursor = None
        while True:
            page = await self.sessions.message_page(
                session_id,
                "agent",
                cursor=cursor,
                limit=200,
            )
            pages.append(page)
            cursor = page.older_cursor
            if cursor is None:
                break
        for page in reversed(pages):
            for update in replay_history(conversation_replay(page.items)):
                await self._update(session_id, update)

    async def _handle_interaction(
        self,
        session_id: str,
        event: InteractionRequest,
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> InteractionResolution:
        del timeout_seconds
        request_id = event.interaction_id
        correlation_id = tool_call_id
        if self.connection is None:
            if isinstance(event, PermissionRequest):
                return Denied(reason="ACP client disconnected")
            return InputCancelled(reason="ACP client disconnected")
        if isinstance(event, PermissionRequest):
            if isinstance(event.subject, ToolPermission):
                call = event.subject.tool_call
                call_id = str(call.id)
                call_name = call.name
                call_args = call.args
            elif isinstance(event.subject, NamedPermission):
                call_id = correlation_id or request_id
                call_name = event.subject.tool
                call_args = event.subject.params
            else:  # pragma: no cover - PermissionSubject is closed
                raise TypeError(
                    f"Unsupported permission subject: {event.subject!r}"
                )
            response: RequestPermissionResponse = (
                await self.connection.request_permission(
                    session_id=session_id,
                    tool_call=ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=call_id,
                        title=call_name,
                        raw_input=call_args,
                    ),
                    options=[
                        PermissionOption(
                            option_id="allow_once",
                            name="Allow once",
                            kind="allow_once",
                        ),
                        PermissionOption(
                            option_id="allow_session",
                            name="Allow for session",
                            kind="allow_always",
                        ),
                        PermissionOption(
                            option_id="deny",
                            name="Deny",
                            kind="reject_once",
                        ),
                    ],
                )
            )
            outcome = response.outcome
            if isinstance(outcome, AllowedOutcome):
                allowed = outcome.option_id in {
                    "allow_once",
                    "allow_session",
                }
                if allowed:
                    return Allowed(scope=(
                        "session"
                        if outcome.option_id == "allow_session"
                        else "once"
                    ))
                return Denied(reason="permission denied")
            return Denied(
                reason=(
                    "permission request cancelled"
                    if isinstance(outcome, DeniedOutcome)
                    else "permission denied"
                )
            )

        if not isinstance(event, UserInputRequest):
            raise TypeError(f"Unsupported interaction request: {event!r}")
        correlation_id = event.tool_call_id or correlation_id
        options = event.options
        elicitation = getattr(self.client_capabilities, "elicitation", None)
        if getattr(elicitation, "form", None) is None:
            return InputCancelled(
                reason="ACP client does not support form elicitation"
            )
        labels = [
            option.label
            for option in options
        ]
        mode = ElicitationFormSessionMode(
            session_id=session_id,
            tool_call_id=correlation_id or None,
            requested_schema=ElicitationSchema(
                properties={
                    "answer": ElicitationStringPropertySchema(
                        type="string",
                        title="Answer",
                        enum=labels or None,
                    )
                },
                required=["answer"],
            ),
        )
        response = await self.connection.create_elicitation(
            message=event.question,
            mode=mode,
        )
        content = getattr(response, "content", None)
        if not isinstance(content, dict) or "answer" not in content:
            return InputCancelled(reason="user declined")
        return Answered(answer=content["answer"])

    async def _resolve_interaction(
        self,
        session_id: str,
        event: InteractionRequest,
    ) -> None:
        result = await self._handle_interaction(session_id, event)
        request_id = event.interaction_id
        if isinstance(result, (Denied, InputCancelled, InputTimedOut)):
            await self.sessions.cancel_interaction(
                session_id,
                "agent",
                event.kind,
                request_id,
                result.reason,
            )
            return
        if isinstance(event, PermissionRequest):
            if not isinstance(result, Allowed):
                raise TypeError(
                    f"Permission request received {type(result).__name__} resolution"
                )
            await self.sessions.respond_permission(
                session_id,
                "agent",
                request_id,
                "allow",
                result.scope,
            )
            return
        if not isinstance(result, Answered):
            raise TypeError(
                f"User input received {type(result).__name__} resolution"
            )
        await self.sessions.respond_user_input(
            session_id,
            "agent",
            request_id,
            result.answer,
        )

    @staticmethod
    def _reject_additional_directories(
        additional_directories: list[str] | None,
    ) -> None:
        if additional_directories:
            raise RequestError.invalid_params({
                "additionalDirectories": "not supported"
            })

    def _mcp_plugin_config(
        self,
        workspace: str,
        session_id: str | None,
        mcp_servers: list[Any] | None,
    ) -> dict[str, dict[str, JsonValue]] | None:
        if not mcp_servers:
            return None
        if self.no_plugins:
            raise RequestError.invalid_params({
                "mcpServers": "plugins are disabled"
            })
        # Plugin enablement is decided by the plugin tree (xcore.yaml /
        # plugins.yaml); requested servers are injected directly.
        servers: dict[str, JsonValue] = {}
        for server in mcp_servers:
            name = str(getattr(server, "name", ""))
            if not _MCP_NAME.fullmatch(name):
                raise RequestError.invalid_params({
                    "mcpServers": f"invalid server name: {name!r}"
                })
            if isinstance(server, McpServerStdio):
                servers[name] = {
                    "type": "local",
                    "command": [server.command, *server.args],
                    "cwd": workspace,
                    "env": {
                        item.name: item.value
                        for item in server.env
                    },
                    "required": True,
                }
            elif isinstance(server, HttpMcpServer):
                servers[name] = {
                    "type": "remote",
                    "url": server.url,
                    "headers": {
                        item.name: item.value
                        for item in server.headers
                    },
                    "required": True,
                }
            elif isinstance(server, SseMcpServer):
                raise RequestError.invalid_params({
                    "mcpServers": "SSE transport is not supported"
                })
            else:
                raise RequestError.invalid_params({
                    "mcpServers": f"unsupported server type: {type(server).__name__}"
                })
        return {MCP_PLUGIN_ID: {"servers": servers}}


def _workspace(cwd: str) -> str:
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise RequestError.invalid_params({"cwd": cwd})
    return str(path.resolve())


def _prompt_content(blocks: list[Any]) -> tuple[str, list[ImageInput]]:
    parts: list[str] = []
    images: list[ImageInput] = []
    for block in blocks:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            parts.append(str(block.text))
        elif block_type == "image":
            images.append(ImageInput(
                data=str(block.data),
                media_type=str(block.mime_type),
            ))
        elif block_type == "resource":
            resource = block.resource
            text = getattr(resource, "text", None)
            if text is None:
                raise RequestError.invalid_params({
                    "prompt": "binary embedded resources are not supported"
                })
            parts.append(
                f"<embedded_context uri={quoteattr(str(resource.uri))}>\n"
                f"{escape(str(text))}\n"
                "</embedded_context>"
            )
        elif block_type == "resource_link":
            parts.append(
                f"<resource_link uri={quoteattr(str(block.uri))} "
                f"name={quoteattr(str(block.name))} />"
            )
        else:
            raise RequestError.invalid_params({
                "prompt": f"unsupported content type: {block_type}"
            })
    content = "\n\n".join(parts).strip()
    if not content and not images:
        raise RequestError.invalid_params({"prompt": "prompt is empty"})
    return content, images


def _usage(data: TokenCounters | None) -> Usage | None:
    if data is None:
        return None
    return Usage(
        input_tokens=data.input,
        output_tokens=data.output,
        total_tokens=data.input + data.output,
        cached_read_tokens=data.cache_read,
        cached_write_tokens=(
            data.cache_create
            + data.prompt_cache_write
        ),
    )


__all__ = ["XBotACPAgent"]
