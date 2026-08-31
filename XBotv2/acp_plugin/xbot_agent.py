"""ACP Agent implementation backed by the XBot session runtime."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
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
from XBotv2.acp_plugin.events import ACPEventMapper, replay_history
from XBotv2.session.history import conversation_replay
from XBotv2.session.event_stream import SessionEventFrame
from XBotv2.agents import LIST_AGENTS, SELECT_AGENT, SelectAgent
from XBotv2.commands import (
    EXECUTE_COMMAND,
    LIST_COMMANDS,
    CommandCatalog,
    ExecuteCommand,
)
from XBotv2.core import EmptyRequest, JsonObject
from XBotv2.core.errors import OperationError
from XBotv2.core.runtime_logging import DEFAULT_RUNTIME_LOG, RuntimeLog
from XBotv2.llm import LIST_PROVIDERS, SELECT_PROVIDER, SelectProvider
from XBotv2.mcp_plugin import MCP_PLUGIN_ID
from XBotv2.session import (
    ImageUpload,
    OpenSession,
    SendMessage,
    SessionsPort,
    SessionNotFound,
    SessionStreamEvent,
    ThreadSnapshot,
    ThreadNotActive,
)

_MCP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class XBotACPAgent:
    """Expose XBot as a stable ACP v1 Agent."""

    def __init__(
        self,
        *,
        sessions: SessionsPort,
        provider_name: str,
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
        await self._prepare_session(opened.session_id, opened.event_cursor)
        self._log.info(
            "acp.session.created",
            session_id=opened.session_id,
            workspace_root=workspace,
        )
        return NewSessionResponse(
            session_id=opened.session_id,
            config_options=await self._config_options(opened.session_id),
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
                title=snapshot.title or snapshot.session_id,
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

        mapper = ACPEventMapper(context_size=summary.context_window)
        stream = await self.sessions.stream_message(SendMessage(
            session_id=session_id,
            thread_id="agent",
            content=content,
            request_id=f"acp:{session_id}",
            images=tuple(images),
        ))
        async for event in stream:
            await self._resolve_interaction(session_id, event)
            for update in mapper.updates(event.to_dict()):
                await self._update(session_id, update)
        if mapper.error is not None:
            self._log.error(
                "acp.prompt.failed",
                session_id=session_id,
                error_type="mapped_runtime_error",
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            raise RequestError.internal_error(mapper.error)
        self._log.info(
            "acp.prompt.finished",
            session_id=session_id,
            content_chars=len(content),
            images=len(images),
            stop_reason=mapper.stop_reason,
            usage=mapper.usage,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        return PromptResponse(
            stop_reason=mapper.stop_reason,
            usage=_usage(mapper.usage),
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

    async def _thread(self, session_id: str) -> ThreadSnapshot:
        try:
            return await self.sessions.thread_summary(session_id, "agent")
        except (SessionNotFound, ThreadNotActive) as exc:
            raise RequestError.resource_not_found(session_id) from exc

    async def _forward_session_events(
        self,
        session_id: str,
        events: AsyncIterator[SessionEventFrame],
    ) -> None:
        try:
            summary = await self._thread(session_id)
            mapper = ACPEventMapper(context_size=summary.context_window)
            async for frame in events:
                for update in mapper.updates(frame.event.to_dict()):
                    await self._update(session_id, update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.error(
                "acp.events.failed",
                session_id=session_id,
                error_type=type(exc).__name__,
            )
            raise

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
        cursors: list[str | None] = [None]
        latest = await self.sessions.message_page(
            session_id,
            "agent",
            cursor=None,
            limit=200,
        )
        cursor = latest.next_cursor
        while cursor is not None:
            cursors.append(cursor)
            page = await self.sessions.message_page(
                session_id,
                "agent",
                cursor=cursor,
                limit=200,
            )
            cursor = page.next_cursor
        for cursor in reversed(cursors):
            page = (
                latest
                if cursor is None
                else await self.sessions.message_page(
                    session_id,
                    "agent",
                    cursor=cursor,
                    limit=200,
                )
            )
            for update in replay_history(conversation_replay(page.messages)):
                await self._update(session_id, update)

    async def _handle_interaction(
        self,
        session_id: str,
        event: SessionStreamEvent,
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str = "",
    ) -> JsonObject:
        del timeout_seconds
        data = event.data
        request_id = str(data.get("request_id") or "")
        correlation_id = str(data.get("tool_call_id") or tool_call_id or "")
        if self.connection is None:
            return {
                "request_id": request_id,
                "status": "disconnected",
                "reason": "ACP client disconnected",
            }
        if event.type == "permission_request":
            call = data.get("tool_call") or {}
            call_id = str(call.get("id") or correlation_id or request_id)
            response: RequestPermissionResponse = (
                await self.connection.request_permission(
                    session_id=session_id,
                    tool_call=ToolCallProgress(
                        session_update="tool_call_update",
                        tool_call_id=call_id,
                        title=str(call.get("name") or "Permission required"),
                        raw_input=call.get("args"),
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
                return {
                    "request_id": request_id,
                    "status": "answered",
                    "decision": "allow" if allowed else "deny",
                    "scope": (
                        "session"
                        if allowed and outcome.option_id == "allow_session"
                        else "once"
                    ),
                }
            return {
                "request_id": request_id,
                "status": "cancelled"
                if isinstance(outcome, DeniedOutcome)
                else "answered",
                "decision": "deny",
                "scope": "once",
            }

        options = data.get("options") or []
        elicitation = getattr(self.client_capabilities, "elicitation", None)
        if getattr(elicitation, "form", None) is None:
            return {
                "request_id": request_id,
                "status": "cancelled",
                "reason": "ACP client does not support form elicitation",
            }
        labels = [
            str(option.get("label") or "")
            for option in options
            if isinstance(option, dict) and option.get("label")
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
            message=str(data.get("question") or "Input required"),
            mode=mode,
        )
        content = getattr(response, "content", None)
        if not isinstance(content, dict) or "answer" not in content:
            return {
                "request_id": request_id,
                "status": "cancelled",
                "reason": "user declined",
            }
        return {
            "request_id": request_id,
            "status": "answered",
            "answer": content["answer"],
        }

    async def _resolve_interaction(
        self,
        session_id: str,
        event: SessionStreamEvent,
    ) -> None:
        if event.type not in {"permission_request", "user_input_required"}:
            return
        result = await self._handle_interaction(session_id, event)
        request_id = str(result.get("request_id") or "")
        if result.get("status") != "answered":
            await self.sessions.cancel_interaction(
                session_id,
                "agent",
                event.type,
                request_id,
                str(result.get("reason") or "cancelled"),
            )
            return
        if event.type == "permission_request":
            await self.sessions.respond_permission(
                session_id,
                "agent",
                request_id,
                str(result.get("decision") or "deny"),
                str(result.get("scope") or "once"),
            )
            return
        await self.sessions.respond_user_input(
            session_id,
            "agent",
            request_id,
            result.get("answer"),
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
    ) -> dict[str, JsonObject] | None:
        if not mcp_servers:
            return None
        if self.no_plugins:
            raise RequestError.invalid_params({
                "mcpServers": "plugins are disabled"
            })
        # Plugin enablement is decided by the plugin tree (xcore.yaml /
        # plugins.yaml); requested servers are injected directly.
        servers: JsonObject = {}
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


def _prompt_content(blocks: list[Any]) -> tuple[str, list[ImageUpload]]:
    parts: list[str] = []
    images: list[ImageUpload] = []
    for block in blocks:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            parts.append(str(block.text))
        elif block_type == "image":
            images.append(ImageUpload(
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


def _usage(data: dict[str, int] | None) -> Usage | None:
    if data is None:
        return None
    return Usage(
        input_tokens=data["input_tokens"],
        output_tokens=data["output_tokens"],
        total_tokens=data["total_tokens"],
        cached_read_tokens=data["cache_read_input_tokens"],
        cached_write_tokens=(
            data["cache_creation_input_tokens"]
            + data["prompt_cache_write_tokens"]
        ),
    )


__all__ = ["XBotACPAgent"]
